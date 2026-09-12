"""v2 early-positive lifecycle, compatibility, and decoded-boundary regression tests."""

import hashlib
import json
import unittest
from dataclasses import fields, replace
from pathlib import Path

from forecast_domain.early_resolution import (
    CommandV2,
    EarlyResolution,
    EarlyResolutionTrigger,
    ForecastV2,
    LockEarly,
    ProposeEarlyResolution,
    apply_early_command,
    early_qualification_output_hash,
    early_trigger_input_hash,
    loads_forecast,
)
from forecast_domain.errors import (
    ConcurrencyError,
    IdempotencyConflict,
    TransitionError,
    ValidationError,
)
from forecast_domain.lifecycle import (
    Archive,
    BeginChallenge,
    BeginResolution,
    Command,
    Escalate,
    Finalize,
    Forecast,
    LifecycleState,
    Lock,
    PauseForProviderOutage,
    ResumeAfterProviderRecovery,
    RetainProposal,
    ReviewDispute,
    SubmitDispute,
    SubmitForecast,
    apply_command,
)
from forecast_domain.models import (
    AITask,
    Outcome,
    counter_judge_input_hash,
    counter_judge_output_hash,
    resolution_input_hash,
    resolution_output_hash,
)
from forecast_domain.serialization import content_hash, dumps, from_dict, loads, to_dict

from tests import model_fixtures as m
from tests.lifecycle_fixtures import adjudication, snapshots


def trigger(spec=None, **changes):
    spec = spec or m.specification()
    evidence = changes.pop("evidence", (m.evidence(spec, collected_at_ms=600),))
    verifications = changes.pop("source_verifications", tuple(m.source_verification(e) for e in evidence))
    values = dict(forecast_id="forecast-1", specification_hash=spec.specification_hash,
                  clause_id="yes-rule", evidence=evidence, source_verifications=verifications,
                  event_at_ms=590, observed_at_ms=610,
                  qualification="Official announcement before the deadline is irreversible; all identity conditions match and invalidation is clear.")
    values.update(changes)
    digest = early_trigger_input_hash(values["forecast_id"], values["specification_hash"],
                                     values["clause_id"], evidence, verifications,
                                     values["event_at_ms"], values["observed_at_ms"],
                                     values.get("event_time_basis", "published_instant"))
    values.setdefault("qualifier", m.provenance(AITask.AMBIGUITY_JUDGE, input_hash=digest,
                      output_hash=early_qualification_output_hash(digest, values["qualification"]),
                      created_at_ms=620))
    values.setdefault("counter_qualifier", m.provenance(AITask.COUNTER_JUDGE,
                      provider="independent-provider", created_at_ms=630,
                      input_hash=counter_judge_input_hash(digest, values["qualifier"]),
                      output_hash=counter_judge_output_hash(values["qualifier"].output_hash, True)))
    return EarlyResolutionTrigger(**values)


def proposal(t=None, *, at=650, **changes):
    t = t or trigger()
    source = m.resolution(evidence=t.evidence, source_verifications=t.source_verifications,
                          proposed_at_ms=at)
    values = {f.name: getattr(source, f.name) for f in fields(source)}
    values.update(schema_version=2, trigger=t)
    values.update(changes)
    digest = content_hash({"schema_version": 2, "kind": "early_resolution_input",
                           "trigger_hash": t.trigger_hash,
                           "resolution_input_hash": resolution_input_hash(
                               values["forecast_id"], values["specification_hash"],
                               values["evidence"], values["source_verifications"])})
    output = resolution_output_hash(digest, values["proposed_outcome"], values["confidence_bp"],
                                    values["rule_matches"], values["rule_conflicts"],
                                    values["reason_summary"], values["conflict_status"],
                                    values["conflict_explanation"])
    values["judge"] = m.provenance(AITask.RESOLUTION_JUDGE, input_hash=digest,
                                  output_hash=output, created_at_ms=640)
    values["counter_judge"] = m.provenance(AITask.COUNTER_JUDGE,
                                          input_hash=counter_judge_input_hash(digest, values["judge"]),
                                          output_hash=counter_judge_output_hash(output, True),
                                          created_at_ms=641)
    return EarlyResolution(**values)


def step(forecast, payload, now, *, key=None):
    cls = CommandV2 if isinstance(payload, (LockEarly, ProposeEarlyResolution)) else Command
    command = cls(idempotency_key=key or f"early-{forecast.revision}-{payload.kind}",
                  expected_revision=forecast.revision, payload=payload)
    return apply_early_command(forecast, command, now_ms=now)


def early_states():
    opened = snapshots()[LifecycleState.OPEN]
    locked = step(opened, LockEarly(trigger=trigger()), 635).forecast
    resolving = step(locked, BeginResolution(), 640).forecast
    proposed = step(resolving, ProposeEarlyResolution(resolution=proposal()), 650).forecast
    challenge = step(proposed, BeginChallenge(duration_ms=100), 660).forecast
    return opened, locked, resolving, proposed, challenge


class EarlyDomainTests(unittest.TestCase):
    def test_real_early_flow_roundtrips_without_specification_or_clock_rewrite(self):
        states = early_states()
        final = step(states[-1], Finalize(), 760).forecast
        archived = step(final, Archive(), 770).forecast
        for state in (*states, final, archived):
            self.assertEqual(loads_forecast(dumps(state)), state)
            self.assertEqual(state.specification, states[0].specification)
            self.assertEqual(state.specification_hash, states[0].specification_hash)
            self.assertLess(state.updated_at_ms, state.specification.close_at_ms)
        self.assertEqual(final.finalized_outcome, Outcome.YES)
        self.assertEqual(states[1].upgrade_source, states[0])
        self.assertEqual(states[1].latest_event.previous_event_hash, states[0].audit_head_hash)
        self.assertEqual(states[1].revision, states[0].revision + 1)

    def test_all_existing_v1_snapshots_still_roundtrip_exactly(self):
        for state in snapshots().values():
            self.assertIs(type(loads_forecast(dumps(state))), Forecast)
            self.assertEqual(dumps(loads_forecast(dumps(state))), dumps(state))

    def test_existing_forty_schema_bytes_are_preserved(self):
        baseline = json.loads((Path(__file__).parent / "fixtures/legacy-schema-hashes-v1.json").read_text())
        self.assertEqual(len(baseline), 40)
        for name, digest in baseline.items():
            path = Path(__file__).parents[1] / "schemas/v1" / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, name)

    def test_original_early_lock_and_evidence_guards_remain(self):
        opened = snapshots()[LifecycleState.OPEN]
        with self.assertRaises(TransitionError):
            step(opened, Lock(), 635)
        early = m.resolution(evidence=trigger().evidence, proposed_at_ms=650)
        with self.assertRaises(ValidationError):
            early.require_proposable(opened.specification)
        with self.assertRaises(ValidationError):
            Command(idempotency_key="no-upgrade", expected_revision=2, payload=LockEarly(trigger=trigger()))
        with self.assertRaises(ValidationError):
            loads(Forecast, dumps(early_states()[1]))

    def test_lock_upgrade_retry_and_earlier_submission_receipt(self):
        opened = snapshots()[LifecycleState.OPEN]
        command = Command(idempotency_key="earlier-submission", expected_revision=opened.revision,
                          payload=SubmitForecast(user_forecast=m.user_forecast()))
        accepted = apply_command(opened, command, now_ms=200)
        upgrade_command = CommandV2(idempotency_key="upgrade", expected_revision=accepted.forecast.revision,
                                    payload=LockEarly(trigger=trigger()))
        upgraded = apply_early_command(accepted.forecast, upgrade_command, now_ms=635)
        resolved = step(upgraded.forecast, BeginResolution(), 640).forecast
        replay = apply_early_command(resolved, command, now_ms=650, prior_receipt=accepted.receipt)
        self.assertEqual(replay.forecast, resolved)
        self.assertEqual(replay.receipt, accepted.receipt)
        self.assertEqual(replay.events, ())
        replay_upgrade = apply_early_command(resolved, upgrade_command, now_ms=650,
                                            prior_receipt=upgraded.receipt)
        self.assertEqual(replay_upgrade.events, ())
        with self.assertRaises(IdempotencyConflict):
            apply_early_command(resolved, replace(upgrade_command, idempotency_key="different"),
                                now_ms=650, prior_receipt=upgraded.receipt)

    def test_rejected_commands_leave_original_aggregate_unchanged(self):
        opened = snapshots()[LifecycleState.OPEN]
        original = dumps(opened)
        for at in (60, 629, 1000, True, -1):
            with self.subTest(at=at), self.assertRaises((ValidationError, TransitionError)):
                step(opened, LockEarly(trigger=trigger()), at)
        with self.assertRaises(ConcurrencyError):
            apply_early_command(opened, CommandV2(idempotency_key="stale", expected_revision=0,
                                                 payload=LockEarly(trigger=trigger())), now_ms=635)
        self.assertEqual(dumps(opened), original)

    def test_new_submissions_cannot_enter_after_upgrade(self):
        locked = early_states()[1]
        with self.assertRaises(TransitionError):
            step(locked, SubmitForecast(user_forecast=m.user_forecast(submitted_at_ms=636)), 636)
        with self.assertRaises(TransitionError):
            step(locked, LockEarly(trigger=trigger()), 636)

    def test_disputes_and_finalization_use_normal_gates_before_original_deadline(self):
        challenge = early_states()[-1]
        with self.assertRaises(TransitionError):
            step(challenge, Finalize(), 759)
        dispute = m.dispute(proposal=challenge.resolution, submitted_at_ms=700)
        disputed = step(challenge, SubmitDispute(dispute=dispute), 700).forecast
        with self.assertRaises(TransitionError):
            step(disputed, Finalize(), 760)
        review = m.review(dispute, challenge.resolution, reviewed_at_ms=720)
        reviewed = step(disputed, ReviewDispute(review=review), 720).forecast
        retained = step(reviewed, RetainProposal(), 730).forecast
        final = step(retained, Finalize(), 760).forecast
        self.assertEqual(loads_forecast(dumps(final)), final)
        self.assertEqual(final.dispute_reviews, (review,))

    def test_material_dispute_escalation_blocks_finalization(self):
        challenge = early_states()[-1]
        dispute = m.dispute(proposal=challenge.resolution, submitted_at_ms=700)
        disputed = step(challenge, SubmitDispute(dispute=dispute), 700).forecast
        review = m.review(dispute, challenge.resolution, reviewed_at_ms=720, material_conflict=True)
        reviewed = step(disputed, ReviewDispute(review=review), 720).forecast
        escalated = step(reviewed, Escalate(), 730).forecast
        self.assertEqual(loads_forecast(dumps(escalated)), escalated)
        with self.assertRaises(TransitionError):
            step(escalated, Finalize(), 760)

    def test_no_or_nonmonotonic_or_partial_qualification_is_rejected_on_decode(self):
        valid = to_dict(trigger())
        for field, value in (("proposed_outcome", "NO"), ("monotonic_kind", "future_price"),
                             ("irreversible", False), ("conditions_fully_satisfied", False),
                             ("invalidation_clear", False)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                from_dict(EarlyResolutionTrigger, {**valid, field: value})
        with self.assertRaises(ValidationError):
            proposal(proposed_outcome=Outcome.NO, rule_matches=("no-rule",))

    def test_trigger_bindings_and_review_tamper_are_rejected(self):
        t = trigger()
        for field, value in (("forecast_id", "another"), ("specification_hash", "b" * 64),
                             ("clause_id", "no-rule"), ("qualification", "Changed meaning"),
                             ("event_at_ms", 599), ("observed_at_ms", 611)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                replace(t, **{field: value})
        with self.assertRaises(ValidationError):
            trigger(counter_qualifier=replace(t.counter_qualifier, provider=t.qualifier.provider))
        with self.assertRaises(ValidationError):
            trigger(qualifier=replace(t.qualifier, task=AITask.MARKET_COMPILER))

    def test_rebound_trigger_still_checks_specification_and_time(self):
        for changes in ({"forecast_id": "another"}, {"specification_hash": "b" * 64},
                        {"clause_id": "no-rule"}, {"event_at_ms": 99}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                step(snapshots()[LifecycleState.OPEN], LockEarly(trigger=trigger(**changes)), 635)
        with self.assertRaises(ValidationError):
            trigger(event_at_ms=611)
        with self.assertRaises(ValidationError):
            trigger(observed_at_ms=599)

    def test_only_exact_official_primary_evidence_is_eligible(self):
        for e in (m.evidence(collected_at_ms=600, source_id="news-wire", url="https://wire.example/story"),
                  m.evidence(collected_at_ms=600, url="https://imposter.example/news"),
                  m.evidence(collected_at_ms=600, collector=None)):
            with self.subTest(source=e.source_id, url=e.url), self.assertRaises(ValidationError):
                trigger(evidence=(e,)).validate_for(m.specification())
        e = m.evidence(collected_at_ms=600)
        with self.assertRaises(ValidationError):
            trigger(evidence=(e,), source_verifications=(m.source_verification(e, verified=False),))

    def test_proposal_cannot_reuse_unbound_judge_or_change_retained_evidence(self):
        p = proposal()
        ordinary = m.resolution(evidence=p.evidence, proposed_at_ms=650)
        with self.assertRaises(ValidationError):
            replace(p, judge=ordinary.judge, counter_judge=ordinary.counter_judge)
        with self.assertRaises(ValidationError):
            replace(p, trigger=trigger(event_at_ms=589))
        with self.assertRaises(ValidationError):
            replace(p, proposed_at_ms=639)

    def test_decoded_snapshot_checks_upgrade_source_and_temporal_context(self):
        locked = early_states()[1]
        value = to_dict(locked)
        for field, replacement in (("upgraded_from_state_hash", "a" * 64),
                                   ("upgraded_from_event_hash", "a" * 64),
                                   ("upgraded_at_revision", 1), ("upgraded_at_ms", 629),
                                   ("state", "OPEN")):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                from_dict(ForecastV2, {**value, field: replacement})
        value["upgrade_source"]["specification"]["close_at_ms"] = 999
        with self.assertRaises(ValidationError):
            from_dict(ForecastV2, value)

    def test_exact_versions_and_typed_early_command_roundtrip(self):
        command = CommandV2(idempotency_key="early", expected_revision=2, payload=LockEarly(trigger=trigger()))
        self.assertEqual(loads(CommandV2, dumps(command)), command)
        with self.assertRaises(ValidationError):
            loads(Command, dumps(command))
        with self.assertRaises(ValidationError):
            loads_forecast(dumps(replace(trigger(), schema_version=2)))
        with self.assertRaises(ValidationError):
            loads_forecast('{"schema_version":2,"schema_version":1}')

    def test_observed_upper_bound_is_explicit_and_hash_bound(self):
        t = trigger(event_at_ms=610, event_time_basis="observed_upper_bound")
        t.validate_for(m.specification())
        self.assertEqual(loads(EarlyResolutionTrigger, dumps(t)), t)
        self.assertNotEqual(t.input_hash, trigger().input_hash)
        with self.assertRaises(ValidationError):
            trigger(event_at_ms=590, event_time_basis="observed_upper_bound")
        with self.assertRaises(ValidationError):
            replace(t, event_time_basis="published_instant")
        with self.assertRaises(ValidationError):
            from_dict(EarlyResolutionTrigger, {**to_dict(t), "event_time_basis": "inferred_midnight"})
        final = step(early_states()[0], LockEarly(trigger=t), 635).forecast
        self.assertEqual(final.early_trigger.event_time_basis, "observed_upper_bound")

    def test_real_provider_outage_extends_early_challenge_and_preserves_context(self):
        challenge = early_states()[-1]
        paused = step(challenge, PauseForProviderOutage(
            configured_providers=("provider-a",), unavailable_providers=("provider-a",),
            reason="All configured providers timed out."), 700).forecast
        recovered = step(paused, ResumeAfterProviderRecovery(recovered_provider="provider-a"), 750).forecast
        self.assertEqual(recovered.challenge_until_ms, 810)
        self.assertEqual(loads_forecast(dumps(recovered)), recovered)
        with self.assertRaises(TransitionError):
            step(recovered, Finalize(), 809)
        self.assertEqual(step(recovered, Finalize(), 810).forecast.finalized_outcome, Outcome.YES)

    def test_escalated_early_yes_can_be_replaced_only_after_original_expiry(self):
        challenge = early_states()[-1]
        dispute = m.dispute(proposal=challenge.resolution, submitted_at_ms=700)
        disputed = step(challenge, SubmitDispute(dispute=dispute), 700).forecast
        reviewed = step(disputed, ReviewDispute(review=m.review(
            dispute, challenge.resolution, reviewed_at_ms=720, material_conflict=True)), 720).forecast
        escalated = step(reviewed, Escalate(), 730).forecast
        replacement = step(escalated, adjudication(escalated, now_ms=2000), 2000).forecast
        self.assertEqual(replacement.resolution.proposed_outcome, Outcome.NO)
        second_challenge = step(replacement, BeginChallenge(duration_ms=100), 2010).forecast
        finalized = step(second_challenge, Finalize(), 2110).forecast
        self.assertEqual(finalized.finalized_outcome, Outcome.NO)
        self.assertEqual(loads_forecast(dumps(finalized)), finalized)
        self.assertEqual(finalized.specification_hash, challenge.specification_hash)
