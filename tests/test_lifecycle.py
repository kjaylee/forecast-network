"""Lifecycle acceptance, failure, audit, and distributed-retry contract tests."""

import dataclasses
import unittest

from forecast_domain.errors import (
    ConcurrencyError,
    IdempotencyConflict,
    TransitionError,
    ValidationError,
)
from forecast_domain.lifecycle import (
    MAX_SAFE_INTEGER,
    AdjudicateResolution,
    Archive,
    BeginChallenge,
    BeginResolution,
    BeginValidation,
    Command,
    DomainEffect,
    EditSpecification,
    Escalate,
    Finalize,
    Forecast,
    LifecycleState,
    Lock,
    Pause,
    PauseForProviderOutage,
    ProposeResolution,
    Publish,
    RejectValidation,
    ResumeAfterProviderRecovery,
    RetainProposal,
    ReviewDispute,
    SubmitDispute,
    SubmitForecast,
    apply_command,
)
from forecast_domain.models import ConflictStatus, DuplicateCandidate, Outcome, ReviewDisposition
from forecast_domain.serialization import content_hash, from_dict, to_dict

from tests import model_fixtures as model
from tests.lifecycle_fixtures import adjudication, snapshots, step


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.states = snapshots()

    def state(self, name):
        return self.states[LifecycleState(name)]

    def assert_rejected(self, forecast, payload, now_ms, exception=(TransitionError, ValidationError)):
        before = to_dict(forecast)
        with self.assertRaises(exception):
            step(forecast, payload, now_ms)
        self.assertEqual(before, to_dict(forecast), "rejection must preserve its input snapshot")

    def test_nominal_path_and_immutable_archive(self):
        self.assertEqual(set(self.states), set(LifecycleState))
        initial = self.state("DRAFT")
        for forecast in self.states.values():
            self.assertIs(forecast.specification, initial.specification)
            self.assertEqual(forecast.specification_hash, initial.specification_hash)
            self.assertEqual(from_dict(Forecast, to_dict(forecast)), forecast)
        final = self.state("FINALIZED")
        archived = self.state("ARCHIVED")
        self.assertEqual(final.finalized_outcome, Outcome.YES)
        self.assertEqual(archived.finalized_resolution_hash, final.finalized_resolution_hash)
        self.assertIs(archived.resolution, final.resolution)
        self.assertEqual(final.latest_event.effects, tuple(DomainEffect))
        self.assertEqual(archived.latest_event.effects, ())

    def test_all_state_command_pairs_reject_forbidden_edges(self):
        # Independent acceptance table derived from the agreed lifecycle, not
        # imported from the implementation's transition table.
        allowed = {
            "edit_specification": {"DRAFT"}, "begin_validation": {"DRAFT"},
            "reject_validation": {"VALIDATING"}, "publish": {"VALIDATING"},
            "lock": {"OPEN"}, "begin_resolution": {"LOCKED"},
            "propose_resolution": {"RESOLVING"}, "begin_challenge": {"PROPOSED"},
            "submit_forecast": {"OPEN"}, "submit_dispute": {"CHALLENGE", "DISPUTED"},
            "review_dispute": {"DISPUTED"}, "retain_proposal": {"DISPUTED"},
            "escalate": {"DISPUTED"}, "adjudicate_resolution": {"ESCALATED"},
            "finalize": {"CHALLENGE"}, "archive": {"FINALIZED"},
            "pause_for_provider_outage": {"RESOLVING", "PROPOSED", "CHALLENGE", "DISPUTED", "ESCALATED"},
            "resume_after_provider_recovery": {"PAUSED"},
        }
        spec = self.state("DRAFT").specification
        proposal = model.resolution(spec)
        dispute = model.dispute(spec, proposal)
        payloads = (
            EditSpecification(specification=spec), BeginValidation(),
            RejectValidation(assessment=model.validation(spec, deterministic_passed=False)),
            Publish(assessment=model.validation(spec)), Lock(), BeginResolution(),
            ProposeResolution(resolution=proposal), BeginChallenge(duration_ms=1000),
            SubmitForecast(user_forecast=model.user_forecast(spec)), SubmitDispute(dispute=dispute),
            ReviewDispute(review=model.review(dispute, proposal, spec)), RetainProposal(),
            Escalate(), adjudication(self.state("ESCALATED")), Finalize(), Archive(),
            PauseForProviderOutage(configured_providers=("a",), unavailable_providers=("a",), reason="Outage"),
            ResumeAfterProviderRecovery(recovered_provider="a"),
        )
        self.assertEqual({payload.kind for payload in payloads}, set(allowed))
        for forecast in self.states.values():
            for payload in payloads:
                if forecast.state.value not in allowed[payload.kind]:
                    with self.subTest(state=forecast.state.value, command=payload.kind):
                        self.assert_rejected(forecast, payload, 5000, TransitionError)

    def test_draft_edit_rejection_and_validation_retry(self):
        draft = self.state("DRAFT")
        changed = model.specification(share_title="A revised share title")
        edited = step(draft, EditSpecification(specification=changed), 1).forecast
        self.assertNotEqual(edited.specification_hash, draft.specification_hash)
        validating = step(edited, BeginValidation(), 10).forecast
        rejected = step(validating, RejectValidation(assessment=model.validation(
            changed, objectively_resolvable=False)), 60).forecast
        self.assertEqual(rejected.state, LifecycleState.DRAFT)
        retried = step(rejected, BeginValidation(), 61).forecast
        published = step(retried, Publish(assessment=model.validation(changed)), 62).forecast
        self.assertEqual(published.state, LifecycleState.OPEN)
        self.assert_rejected(self.state("VALIDATING"), RejectValidation(
            assessment=model.validation()), 60)
        self.assert_rejected(published, EditSpecification(specification=draft.specification), 63)

    def test_publish_proof_and_duplicate_spam_guards(self):
        validating = self.state("VALIDATING")
        for flag in ("deterministic_passed", "objectively_resolvable", "ambiguity_passed", "duplicate_check_completed"):
            with self.subTest(flag=flag):
                self.assert_rejected(validating, Publish(assessment=model.validation(**{flag: False})), 60)
        self.assert_rejected(validating, Publish(assessment=model.validation(
            model.specification(share_title="Different spec"))), 60)
        self.assert_rejected(validating, Publish(assessment=model.validation()), 59)
        self.assert_rejected(validating, Publish(assessment=model.validation()), 1000)
        for material_difference in (False, True):
            duplicate = DuplicateCandidate(forecast_id="existing", specification_hash="d" * 64,
                similarity_bp=9900, materially_different_rules=material_difference,
                explanation="Explicit comparison of closing date and rules.")
            spec = model.specification(duplicate_candidates=(duplicate,))
            edited = step(self.state("DRAFT"), EditSpecification(specification=spec), 1).forecast
            pending = step(edited, BeginValidation(), 10).forecast
            payload = Publish(assessment=model.validation(spec))
            if material_difference:
                self.assertEqual(step(pending, payload, 60).forecast.state, LifecycleState.OPEN)
            else:
                self.assert_rejected(pending, payload, 60)

    def test_forecast_acceptance_exact_window_and_bindings(self):
        opened = self.state("OPEN")
        for at in (100, 999):
            result = step(opened, SubmitForecast(user_forecast=model.user_forecast(submitted_at_ms=at)), at)
            self.assertEqual(result.receipt.accepted_user_forecast.submitted_at_ms, at)
            self.assertEqual(result.forecast.state, LifecycleState.OPEN)
            self.assertEqual(result.forecast.revision, opened.revision + 1)
        for at in (99, 1000):
            self.assert_rejected(opened, SubmitForecast(user_forecast=model.user_forecast(submitted_at_ms=at)), at)
        for overrides in ({"forecast_id": "other"}, {"specification_hash": "a" * 64}, {"submitted_at_ms": 201}):
            self.assert_rejected(opened, SubmitForecast(user_forecast=model.user_forecast(**overrides)), 200)
        self.assert_rejected(opened, Lock(), 999)
        self.assertEqual(step(opened, Lock(), 1000).forecast.state, LifecycleState.LOCKED)

    def test_resolution_requires_verified_bound_evidence_and_no_conflict(self):
        resolving = self.state("RESOLVING")
        self.assert_rejected(resolving, ProposeResolution(resolution=model.resolution(forecast_id="other")), 2000)
        foreign = model.specification(share_title="Other spec")
        self.assert_rejected(resolving, ProposeResolution(resolution=model.resolution(foreign)), 2000)
        unverified = model.source_verification(model.evidence(), verified=False)
        self.assert_rejected(resolving, ProposeResolution(resolution=model.resolution(
            source_verifications=(unverified,))), 2000)
        unresolved = model.resolution(conflict_status=ConflictStatus.UNRESOLVED,
            counter_judge_agrees=False, conflict_explanation="Independent evidence conflicts.")
        self.assert_rejected(resolving, ProposeResolution(resolution=unresolved), 2000)
        self.assert_rejected(resolving, ProposeResolution(resolution=model.resolution()), 1999)
        self.assert_rejected(self.state("PROPOSED"), Finalize(), 4000)

    def test_challenge_and_finalization_exact_boundaries(self):
        for duration in (0, -1, True):
            with self.assertRaises(ValidationError):
                BeginChallenge(duration_ms=duration)
        challenge = self.state("CHALLENGE")
        self.assert_rejected(challenge, Finalize(), 3099)
        self.assertEqual(step(challenge, Finalize(), 3100).forecast.state, LifecycleState.FINALIZED)
        timely = model.dispute(proposal=challenge.resolution, submitted_at_ms=3099)
        self.assertEqual(step(challenge, SubmitDispute(dispute=timely), 3099).forecast.state, LifecycleState.DISPUTED)
        late = model.dispute(proposal=challenge.resolution, submitted_at_ms=3100)
        self.assert_rejected(challenge, SubmitDispute(dispute=late), 3100)

    def test_invalid_resolution_preserves_explicit_outcome(self):
        resolving = self.state("RESOLVING")
        proposed = step(resolving, ProposeResolution(resolution=model.resolution(outcome=Outcome.INVALID)), 2000).forecast
        challenge = step(proposed, BeginChallenge(duration_ms=10), 2100).forecast
        final = step(challenge, Finalize(), 2110).forecast
        self.assertEqual(final.finalized_outcome, Outcome.INVALID)

    def test_pending_dispute_blocks_finalization_and_retention(self):
        disputed = self.state("DISPUTED")
        self.assert_rejected(disputed, Finalize(), 4000)
        self.assert_rejected(disputed, RetainProposal(), 4000)
        self.assert_rejected(disputed, Escalate(), 4000)
        duplicate = model.dispute(proposal=disputed.resolution, submitted_at_ms=2250)
        self.assert_rejected(disputed, SubmitDispute(dispute=duplicate), 2250)
        unknown_rule = model.dispute(proposal=disputed.resolution, dispute_id="bad-rule", rule_clause_id="not-a-clause")
        self.assert_rejected(self.state("CHALLENGE"), SubmitDispute(dispute=unknown_rule), 2200)

    def test_review_requires_exact_binding_validated_evidence_independence(self):
        disputed = self.state("DISPUTED")
        item = disputed.disputes[0]
        for changes in ({"provider": "provider-a"}, {"provider": "PROVIDER-A"}):
            with self.subTest(changes=changes):
                try:
                    review = model.review(item, disputed.resolution, disputed.specification, **changes)
                except ValidationError:
                    continue
                self.assert_rejected(disputed, ReviewDispute(review=review), 2300)
        foreign_item = model.dispute(proposal=disputed.resolution, dispute_id="different")
        self.assert_rejected(disputed, ReviewDispute(review=model.review(foreign_item, disputed.resolution)), 2300)

    def test_invalid_evidence_is_explicitly_dismissed_without_locking_finalization(self):
        disputed = self.state("DISPUTED")
        reviewed = step(disputed, ReviewDispute(review=model.review(
            disputed.disputes[0], disputed.resolution, evidence_validated=False)), 2300).forecast
        self.assertEqual(reviewed.dispute_reviews[0].disposition, ReviewDisposition.INVALID_EVIDENCE)
        self.assertFalse(reviewed.dispute_reviews[0].material_conflict)
        retained = step(reviewed, RetainProposal(), 2400).forecast
        self.assertEqual(retained.challenge_until_ms, 3100)
        self.assertEqual(step(retained, Finalize(), 3100).forecast.finalized_outcome, Outcome.YES)

    def test_nonmaterial_reviews_restore_original_challenge_deadline(self):
        disputed = self.state("DISPUTED")
        reviewed = step(disputed, ReviewDispute(review=model.review(
            disputed.disputes[0], disputed.resolution)), 2300).forecast
        self.assert_rejected(reviewed, Escalate(), 2400)
        self.assert_rejected(reviewed, ReviewDispute(review=reviewed.dispute_reviews[0]), 2300)
        retained = step(reviewed, RetainProposal(), 4000).forecast
        self.assertEqual(retained.state, LifecycleState.CHALLENGE)
        self.assertEqual(retained.challenge_until_ms, 3100)
        self.assertIs(retained.resolution, disputed.resolution)
        self.assertEqual(step(retained, Finalize(), 4000).forecast.finalized_outcome, Outcome.YES)

    def test_material_review_requires_independent_adjudication_and_new_challenge(self):
        disputed = self.state("DISPUTED")
        reviewed = step(disputed, ReviewDispute(review=model.review(
            disputed.disputes[0], disputed.resolution, material_conflict=True)), 2300).forecast
        self.assert_rejected(reviewed, RetainProposal(), 2400)
        escalated = step(reviewed, Escalate(), 2400).forecast
        self.assert_rejected(escalated, Finalize(), 4000)
        self.assert_rejected(escalated, adjudication(escalated, provider="PROVIDER-A"), 4000)
        valid = adjudication(escalated)
        wrong_binding = AdjudicateResolution(resolution=valid.resolution,
            adjudicator=dataclasses.replace(valid.adjudicator, input_hash="a" * 64))
        self.assert_rejected(escalated, wrong_binding, 4000)
        proposed = step(escalated, valid, 4000).forecast
        self.assertEqual(proposed.resolution.proposed_outcome, Outcome.NO)
        self.assertIsNone(proposed.challenge_until_ms)
        self.assertEqual(proposed.disputes, ())
        self.assertEqual(proposed.dispute_reviews, ())
        self.assert_rejected(proposed, Finalize(), 4000)
        renewed = step(proposed, BeginChallenge(duration_ms=1000), 4001).forecast
        self.assertEqual(renewed.challenge_until_ms, 5001)
        self.assert_rejected(renewed, Finalize(), 5000)
        self.assertEqual(step(renewed, Finalize(), 5001).forecast.finalized_outcome, Outcome.NO)

    def test_multiple_disputes_complete_before_escalation(self):
        first = self.state("DISPUTED")
        second = model.dispute(proposal=first.resolution, dispute_id="dispute-2", submitted_at_ms=2250)
        both = step(first, SubmitDispute(dispute=second), 2250).forecast
        partly_reviewed = step(both, ReviewDispute(review=model.review(
            both.disputes[0], both.resolution, material_conflict=True)), 2300).forecast
        self.assert_rejected(partly_reviewed, Escalate(), 2400)
        # Even a caller that recomputes public hashes cannot decode an illegal
        # escalated snapshot with a pending review. Hashes are not authentication.
        values = {field.name: getattr(partly_reviewed, field.name) for field in dataclasses.fields(partly_reviewed)
                  if field.name not in {"latest_event", "audit_head_hash"}}
        values["state"] = LifecycleState.ESCALATED
        event = dataclasses.replace(partly_reviewed.latest_event, command_name="escalate",
            new_state=LifecycleState.ESCALATED, state_hash=content_hash(values))
        with self.assertRaises(ValidationError):
            Forecast(**values, latest_event=event, audit_head_hash=content_hash(event))
        complete = step(partly_reviewed, ReviewDispute(review=model.review(
            second, both.resolution, reviewed_at_ms=2400)), 2400).forecast
        self.assertEqual(step(complete, Escalate(), 2400).forecast.state, LifecycleState.ESCALATED)

    def test_provider_outage_pause_recovery_all_applicable_states(self):
        for name in ("RESOLVING", "PROPOSED", "CHALLENGE", "DISPUTED", "ESCALATED"):
            with self.subTest(state=name):
                original = self.state(name)
                at = max(original.updated_at_ms, 2500)
                paused = step(original, PauseForProviderOutage(configured_providers=("a", "b"),
                    unavailable_providers=("a", "b"), reason="All providers unavailable"), at).forecast
                self.assertEqual(paused.pause.previous_state, original.state)
                self.assert_rejected(paused, Finalize(), at + 10000)
                recovered = step(paused, ResumeAfterProviderRecovery(recovered_provider="b"), at + 1000).forecast
                self.assertEqual(recovered.state, original.state)
                self.assertIsNone(recovered.pause)
                if original.challenge_until_ms is not None:
                    self.assertEqual(recovered.challenge_until_ms, original.challenge_until_ms + 1000)
                else:
                    self.assertIsNone(recovered.challenge_until_ms)
                self.assertIs(recovered.resolution, original.resolution)
        paused = self.state("PAUSED")
        self.assert_rejected(paused, ResumeAfterProviderRecovery(recovered_provider="unconfigured"), 2500)
        with self.assertRaises(ValidationError):
            PauseForProviderOutage(configured_providers=("a", "b"), unavailable_providers=("a",), reason="One failed")
        recovered = step(paused, ResumeAfterProviderRecovery(recovered_provider="provider-b"), 4000).forecast
        self.assertEqual(recovered.challenge_until_ms, 4900)
        self.assert_rejected(recovered, Finalize(), 4899)
        self.assertEqual(step(recovered, Finalize(), 4900).forecast.state, LifecycleState.FINALIZED)

    def test_exact_retry_returns_current_snapshot_original_receipt_no_duplicate_intents(self):
        challenge = self.state("CHALLENGE")
        command = Command(idempotency_key="finalize-once", expected_revision=challenge.revision, payload=Finalize())
        accepted = apply_command(challenge, command, now_ms=3100)
        # Simulated response loss after transactional persistence of the accepted result.
        retried = apply_command(accepted.forecast, command, now_ms=3200, prior_receipt=accepted.receipt)
        self.assertIs(retried.forecast, accepted.forecast)
        self.assertIs(retried.receipt, accepted.receipt)
        self.assertEqual(retried.events, ())
        archived = step(accepted.forecast, Archive(), 3300).forecast
        late_retry = apply_command(archived, command, now_ms=4000, prior_receipt=accepted.receipt)
        self.assertIs(late_retry.forecast, archived, "retry must never return a historical snapshot")
        self.assertIs(late_retry.receipt, accepted.receipt)
        self.assertEqual(late_retry.events, ())
        reused = dataclasses.replace(command, expected_revision=archived.revision)
        with self.assertRaises(IdempotencyConflict):
            apply_command(archived, reused, now_ms=4000, prior_receipt=accepted.receipt)
        reused_payload = dataclasses.replace(command, payload=Archive())
        with self.assertRaises(IdempotencyConflict):
            apply_command(archived, reused_payload, now_ms=4000, prior_receipt=accepted.receipt)

    def test_retry_receipt_submission_must_match_original_command_exactly(self):
        opened = self.state("OPEN")
        submission = model.user_forecast()
        command = Command(idempotency_key="forecast-once", expected_revision=opened.revision,
                          payload=SubmitForecast(user_forecast=submission))
        accepted = apply_command(opened, command, now_ms=200)
        for reported in (None, dataclasses.replace(submission, confidence=99)):
            with self.subTest(reported=reported), self.assertRaises(ValidationError):
                corrupt = dataclasses.replace(accepted.receipt, accepted_user_forecast=reported)
                apply_command(accepted.forecast, command, now_ms=201, prior_receipt=corrupt)
        retried = apply_command(accepted.forecast, command, now_ms=201,
                                prior_receipt=accepted.receipt)
        self.assertEqual(retried.receipt.accepted_user_forecast, submission)
        self.assertEqual(retried.events, ())

        challenge = self.state("CHALLENGE")
        final_command = Command(idempotency_key="finalize-no-submission",
                                expected_revision=challenge.revision, payload=Finalize())
        finalized = apply_command(challenge, final_command, now_ms=3100)
        extra = dataclasses.replace(finalized.receipt,
            accepted_user_forecast=model.user_forecast(submitted_at_ms=3100))
        with self.assertRaises(ValidationError):
            apply_command(finalized.forecast, final_command, now_ms=3101, prior_receipt=extra)

    def test_simultaneous_finalization_and_stale_cache_require_persistence_cas(self):
        challenge = self.state("CHALLENGE")
        first = Command(idempotency_key="finalize-a", expected_revision=challenge.revision, payload=Finalize())
        second = Command(idempotency_key="finalize-b", expected_revision=challenge.revision, payload=Finalize())
        accepted = apply_command(challenge, first, now_ms=3100)
        with self.assertRaises(ConcurrencyError):
            apply_command(accepted.forecast, second, now_ms=3100)
        # Both decisions can be computed from one old immutable snapshot. Future
        # persistence MUST accept only one CAS, including its event and receipt.
        speculative = apply_command(challenge, second, now_ms=3100)
        self.assertEqual(speculative.forecast.revision, accepted.forecast.revision)
        self.assertNotEqual(speculative.forecast.audit_head_hash, accepted.forecast.audit_head_hash)
        with self.assertRaises(ConcurrencyError):
            apply_command(self.state("ARCHIVED"), first, now_ms=4000)

    def test_events_deterministic_linked_and_clock_not_backdated(self):
        challenge = self.state("CHALLENGE")
        command = Command(idempotency_key="deterministic", expected_revision=challenge.revision, payload=Finalize())
        one = apply_command(challenge, command, now_ms=3100)
        two = apply_command(challenge, command, now_ms=3100)
        self.assertEqual(one, two)
        event = one.events[0]
        self.assertEqual(event.previous_event_hash, challenge.audit_head_hash)
        self.assertEqual(event.specification_hash, challenge.specification_hash)
        self.assertEqual(event.artifact_hash, challenge.resolution.resolution_hash)
        self.assertEqual(one.forecast.audit_head_hash, content_hash(event))
        self.assertEqual(event.revision, challenge.revision + 1)
        self.assert_rejected(challenge, BeginChallenge(duration_ms=1), 2099)
        self.assert_rejected(self.state("DRAFT"), BeginValidation(), -1)
        with self.assertRaises(ValidationError):
            apply_command(challenge, command, now_ms=True)

    def test_deadline_overflow_fails_without_mutation(self):
        self.assert_rejected(self.state("PROPOSED"), BeginChallenge(duration_ms=MAX_SAFE_INTEGER), 2100)
        paused = self.state("PAUSED")
        self.assert_rejected(paused, ResumeAfterProviderRecovery(recovered_provider="provider-a"), MAX_SAFE_INTEGER)

    def test_revision_overflow_fails_before_creating_event(self):
        opened = self.state("OPEN")
        values = {field.name: getattr(opened, field.name) for field in dataclasses.fields(opened)
                  if field.name not in {"latest_event", "audit_head_hash"}}
        values["revision"] = MAX_SAFE_INTEGER
        event = dataclasses.replace(opened.latest_event, revision=MAX_SAFE_INTEGER,
                                    state_hash=content_hash(values))
        saturated = Forecast(**values, latest_event=event, audit_head_hash=content_hash(event))
        self.assert_rejected(saturated, Lock(), 1000, TransitionError)

    def test_forged_snapshots_cannot_bypass_terminal_pause_or_hash_invariants(self):
        challenge = self.state("CHALLENGE")
        for changes in ({"state": LifecycleState.FINALIZED}, {"state": LifecycleState.OPEN},
                        {"specification_hash": "a" * 64}, {"audit_head_hash": "b" * 64},
                        {"revision": challenge.revision + 1}, {"challenge_until_ms": 2100},
                        {"finalized_outcome": Outcome.YES}, {"latest_event": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                dataclasses.replace(challenge, **changes)
        encoded = to_dict(challenge)
        encoded["resolution"]["proposed_outcome"] = "NO"
        with self.assertRaises(ValidationError):
            from_dict(Forecast, encoded)
        with self.assertRaises(ValidationError):
            dataclasses.replace(self.state("PAUSED"), pause=None)
        with self.assertRaises(ValidationError):
            Pause(previous_state=LifecycleState.OPEN, paused_at_ms=200,
                  configured_providers=("a",), unavailable_providers=("a",), reason="Outage")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            challenge.state = LifecycleState.FINALIZED


if __name__ == "__main__":
    unittest.main()
