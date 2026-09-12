"""Business invariants, immutable evidence and decision-attestation regressions."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields, replace

from forecast_domain.errors import ValidationError
from forecast_domain.models import (
    AITask,
    Category,
    ConflictStatus,
    DuplicateCandidate,
    ForecastChoice,
    Outcome,
    ReviewDisposition,
    SourcePolicy,
)
from forecast_domain.records import MAX_SAFE_INTEGER
from forecast_domain.serialization import content_hash, dumps, loads

from tests.model_fixtures import (
    HASH_A,
    HASH_B,
    all_model_records,
    creator,
    dispute,
    evidence,
    provenance,
    reputation,
    resolution,
    review,
    source_verification,
    specification,
    user_forecast,
    validation,
)


class ModelContractTests(unittest.TestCase):
    def test_every_model_round_trips_and_is_frozen(self) -> None:
        for record in all_model_records():
            with self.subTest(model=type(record).__name__):
                self.assertEqual(loads(type(record), dumps(record)), record)
                self.assertEqual(content_hash(loads(type(record), dumps(record))), content_hash(record))
                with self.assertRaises(FrozenInstanceError):
                    record.schema_version = 2

    def test_every_model_rejects_unsupported_schema_version(self) -> None:
        for record in all_model_records():
            with self.subTest(model=type(record).__name__), self.assertRaises(ValidationError):
                replace(record, schema_version=2)

    def test_mutable_containers_cannot_enter_frozen_records(self) -> None:
        spec = specification()
        for changes in ({"rules": list(spec.rules)}, {"invalidation_rules": ["one rule"]},
                        {"duplicate_candidates": []}, {"source_policy": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(spec, **changes)

    def test_nonblank_text_and_unicode_scalars(self) -> None:
        for text in ("", " \t\n", "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(ValidationError):
                specification(canonical_question=text)

    def test_identifiers_reject_whitespace_and_trailing_newline(self) -> None:
        for identifier in ("", " ", "bad id", "id\n", "id\r\n", "../id", "x" * 129):
            with self.subTest(identifier=repr(identifier)), self.assertRaises(ValidationError):
                user_forecast(forecaster_id=identifier)
            with self.subTest(provider=repr(identifier)), self.assertRaises(ValidationError):
                provenance(AITask.MARKET_COMPILER, provider=identifier)

    def test_hashes_must_be_lowercase_sha256(self) -> None:
        for digest in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n"):
            with self.subTest(digest=digest), self.assertRaises(ValidationError):
                user_forecast(specification_hash=digest)

    def test_integer_types_and_portable_timestamp_bounds(self) -> None:
        for value in (-1, True, 1.0, "1", MAX_SAFE_INTEGER + 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                user_forecast(submitted_at_ms=value)
        self.assertEqual(user_forecast(submitted_at_ms=MAX_SAFE_INTEGER).submitted_at_ms, MAX_SAFE_INTEGER)

    def test_binary_forecast_confidence_boundaries_and_enum_types(self) -> None:
        self.assertEqual(user_forecast(confidence=0).confidence, 0)
        self.assertEqual(user_forecast(confidence=100).confidence, 100)
        for confidence in (-1, 101, True, 70.0):
            with self.subTest(confidence=confidence), self.assertRaises(ValidationError):
                user_forecast(confidence=confidence)
        for outcome in (Outcome.INVALID, Outcome.YES, "YES", "INVALID"):
            with self.subTest(outcome=outcome), self.assertRaises(ValidationError):
                user_forecast(outcome=outcome)
        self.assertEqual(user_forecast(outcome=ForecastChoice.NO).outcome, ForecastChoice.NO)

    def test_basis_point_boundaries(self) -> None:
        for score in (0, 10000):
            self.assertEqual(specification(ambiguity_score_bp=score).ambiguity_score_bp, score)
        for score in (-1, 10001, True, 0.5):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                specification(ambiguity_score_bp=score)

    def test_no_monetary_or_transfer_fields_exist(self) -> None:
        forbidden = {"balance", "payout", "transfer", "redeem", "token", "mint", "price", "stake"}
        for record in all_model_records():
            with self.subTest(model=type(record).__name__):
                self.assertFalse(forbidden & {item.name for item in fields(record)})


class SpecificationTests(unittest.TestCase):
    def test_time_order_and_complete_unique_outcome_clauses(self) -> None:
        spec = specification()
        bad_cases = ({"close_at_ms": 100}, {"open_at_ms": 1001}, {"rules": spec.rules[:2]},
                     {"rules": (spec.rules[0], spec.rules[0], spec.rules[2])},
                     {"rules": (spec.rules[0], replace(spec.rules[1], clause_id="yes-rule"), spec.rules[2])})
        for changes in bad_cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(spec, **changes)

    def test_source_policy_requires_official_primary_and_unique_sources(self) -> None:
        policy = specification().source_policy
        cases = ({"primary_sources": ()},
                 {"primary_sources": (replace(policy.primary_sources[0], is_official=False),)},
                 {"fallback_sources": policy.primary_sources},
                 {"fallback_sources": (replace(policy.primary_sources[0], source_id="another"),)})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(policy, **changes)
        self.assertTrue(SourcePolicy(primary_sources=policy.primary_sources).primary_sources)

    def test_source_url_validation(self) -> None:
        source = specification().source_policy.primary_sources[0]
        for url in ("http://example.org", "file:///etc/passwd", "relative", "https://u:p@example.org",
                    "https://example.org/#fragment", "https://example.org/a b", "https://example.org:0"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                replace(source, url=url)

    def test_publishable_assessment_commits_exact_specification(self) -> None:
        spec = specification()
        proof = validation(spec)
        proof.require_publishable(spec)
        changed = replace(spec, close_at_ms=1100)
        with self.assertRaisesRegex(ValidationError, "different specification"):
            proof.require_publishable(changed)
        self.assertNotEqual(changed.specification_hash, spec.specification_hash)

    def test_failed_publication_checks_and_ambiguity_threshold(self) -> None:
        spec = specification()
        for flag in ("deterministic_passed", "objectively_resolvable", "ambiguity_passed", "duplicate_check_completed"):
            with self.subTest(flag=flag), self.assertRaises(ValidationError):
                validation(spec, **{flag: False}).require_publishable(spec)
        with self.assertRaisesRegex(ValidationError, "ambiguity exceeds"):
            validation(spec, ambiguity_limit_bp=499).require_publishable(spec)

    def test_equivalent_duplicate_rejected_materially_different_rules_allowed(self) -> None:
        candidate = DuplicateCandidate(forecast_id="other", specification_hash=HASH_A,
                                       similarity_bp=8500, materially_different_rules=False,
                                       explanation="The same question and closing rules.")
        spec = specification(duplicate_candidates=(candidate,))
        with self.assertRaisesRegex(ValidationError, "equivalent duplicate"):
            validation(spec).require_publishable(spec)
        distinct = replace(spec, duplicate_candidates=(replace(candidate, materially_different_rules=True),))
        validation(distinct).require_publishable(distinct)
        below_threshold = replace(spec, duplicate_candidates=(replace(candidate, similarity_bp=8499),))
        validation(below_threshold).require_publishable(below_threshold)

    def test_validation_provenance_roles_time_and_output_cannot_be_forged(self) -> None:
        proof = validation()
        changes = ({"ambiguity_passed": False}, {"objectively_resolvable": False},
                   {"ambiguity_limit_bp": 9000}, {"duplicate_check_completed": False},
                   {"duplicate_similarity_threshold_bp": 9900}, {"explanation": "Changed decision."},
                   {"validated_at_ms": 49},
                   {"compiler": replace(proof.compiler, output_hash=HASH_A)},
                   {"ambiguity_judge": replace(proof.ambiguity_judge, task=AITask.COUNTER_JUDGE)})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                replace(proof, **change)


class ResolutionEvidenceTests(unittest.TestCase):
    def test_all_outcomes_require_their_own_clause(self) -> None:
        spec = specification()
        for outcome in Outcome:
            with self.subTest(outcome=outcome):
                resolution(spec, outcome=outcome).require_proposable(spec)

    def test_rule_references_must_be_known_and_only_match_selected_outcome(self) -> None:
        spec = specification()
        for clauses in (("unknown",), ("no-rule",), ("yes-rule", "no-rule")):
            with self.subTest(clauses=clauses), self.assertRaises(ValidationError):
                resolution(spec, rule_matches=clauses).require_proposable(spec)

    def test_snapshot_digest_and_uri_bind_retained_content(self) -> None:
        snapshot = evidence()
        with self.assertRaisesRegex(ValidationError, "immutable content digest"):
            replace(snapshot, content_sha256=HASH_A)
        with self.assertRaisesRegex(ValidationError, "collector output"):
            replace(snapshot, content_sha256=HASH_A, snapshot_uri="urn:sha256:" + HASH_A)
        changed_url = replace(snapshot, url="https://acme.example/news/moved")
        self.assertEqual(changed_url.content_sha256, snapshot.content_sha256)
        self.assertNotEqual(changed_url.evidence_hash, snapshot.evidence_hash)
        self.assertEqual(snapshot.url, "https://acme.example/news/product-x")

    def test_deterministic_user_capture_needs_no_ai_but_verification_is_required(self) -> None:
        spec = specification()
        snapshot = evidence(spec, collector=None)
        self.assertIsNone(snapshot.collector)
        proposal = resolution(spec, evidence=(snapshot,),
                              source_verifications=(source_verification(snapshot, verified=False),))
        with self.assertRaisesRegex(ValidationError, "verified evidence sources"):
            proposal.require_proposable(spec)

    def test_empty_duplicate_and_unbound_evidence_are_rejected(self) -> None:
        proposal = resolution()
        cases = ({"evidence": ()}, {"evidence": proposal.evidence * 2},
                 {"source_verifications": ()},
                 {"source_verifications": (source_verification(evidence(evidence_id="foreign")),)})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(proposal, **changes)

    def test_source_verification_flags_and_origin_cannot_be_forged(self) -> None:
        snapshot = evidence()
        verified = source_verification(snapshot)
        for changes in ({"verified": False}, {"source_id": "another-source"},
                        {"explanation": "New source decision."}, {"evidence_hash": HASH_B}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(verified, **changes)
        with self.assertRaisesRegex(ValidationError, "different source"):
            resolution(source_verifications=(source_verification(snapshot, source_id="other"),))

    def test_resolution_source_policy_host_expiry_and_specification_bindings(self) -> None:
        spec = specification()
        cases = (evidence(spec, source_id="outside-policy"),
                 evidence(spec, url="https://attacker.example/acme"),
                 evidence(spec, collected_at_ms=999))
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(ValidationError):
                resolution(spec, evidence=(snapshot,)).require_proposable(spec)
        with self.assertRaisesRegex(ValidationError, "different specification"):
            resolution(spec).require_proposable(replace(spec, close_at_ms=1001))

    def test_resolution_decision_fields_cannot_change_under_prior_provenance(self) -> None:
        proposal = resolution()
        cases = ({"proposed_outcome": Outcome.NO}, {"confidence_bp": 9300},
                 {"rule_matches": ("no-rule",)}, {"reason_summary": "A different judgment."},
                 {"rule_conflicts": ("no-rule",)}, {"counter_judge_agrees": False},
                 {"conflict_status": ConflictStatus.RESOLVED, "conflict_explanation": "Resolved."})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(proposal, **changes)

    def test_judge_input_counter_input_roles_and_causal_times(self) -> None:
        proposal = resolution()
        cases = ({"judge": replace(proposal.judge, input_hash=HASH_A)},
                 {"counter_judge": replace(proposal.counter_judge, input_hash=HASH_A)},
                 {"counter_judge": replace(proposal.counter_judge, task=AITask.RESOLUTION_JUDGE)},
                 {"counter_judge": replace(proposal.counter_judge, created_at_ms=1000)},
                 {"proposed_at_ms": 1000})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(proposal, **changes)

    def test_unresolved_conflict_and_unverified_sources_block_proposal(self) -> None:
        spec = specification()
        unresolved = resolution(spec, counter_judge_agrees=False, conflict_status=ConflictStatus.UNRESOLVED,
                                conflict_explanation="Evidence conflicts remain unresolved.", rule_conflicts=("no-rule",))
        with self.assertRaisesRegex(ValidationError, "unresolved"):
            unresolved.require_proposable(spec)
        resolved = resolution(spec, conflict_status=ConflictStatus.RESOLVED,
                              conflict_explanation="Primary evidence supersedes outdated fallback report.",
                              rule_conflicts=("no-rule",))
        resolved.require_proposable(spec)


class DisputeReviewTests(unittest.TestCase):
    def test_dispute_preserves_author_and_user_evidence(self) -> None:
        record = dispute()
        self.assertEqual(record.disputant_id, "disputant-1")
        self.assertIsNone(record.evidence[0].collector)
        record.validate_for(specification(), resolution())
        with self.assertRaises(ValidationError):
            replace(record, disputant_id="user\n")

    def test_dispute_forecast_spec_proposal_and_clause_bindings(self) -> None:
        record, spec, proposal = dispute(), specification(), resolution()
        for changes in ({"forecast_id": "foreign"}, {"specification_hash": HASH_A},
                        {"resolution_hash": HASH_B}, {"rule_clause_id": "foreign-rule"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(record, **changes).validate_for(spec, proposal)

    def test_dispute_evidence_and_submission_order(self) -> None:
        record = dispute()
        for changes in ({"evidence": ()}, {"evidence": record.evidence * 2}, {"submitted_at_ms": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(record, **changes)
        early = dispute(submitted_at_ms=1900)
        with self.assertRaisesRegex(ValidationError, "predate the proposal"):
            early.validate_for(specification(), resolution())

    def test_all_explicit_review_dispositions_are_bound_and_admissible(self) -> None:
        spec, proposal, challenge = specification(), resolution(), dispute()
        for changes, expected in (({}, ReviewDisposition.RETAIN_PROPOSAL),
                                  ({"material_conflict": True}, ReviewDisposition.MATERIAL_CONFLICT),
                                  ({"evidence_validated": False}, ReviewDisposition.INVALID_EVIDENCE)):
            with self.subTest(disposition=expected):
                record = review(challenge, proposal, spec, **changes)
                self.assertEqual(record.disposition, expected)
                record.require_valid_for(challenge, proposal, spec)

    def test_disposition_and_decision_flags_cannot_be_silently_rewritten(self) -> None:
        record = review()
        for changes in ({"material_conflict": True}, {"evidence_validated": False},
                        {"disposition": ReviewDisposition.INVALID_EVIDENCE},
                        {"reason_summary": "New outcome."},
                        {"material_conflict": True, "disposition": ReviewDisposition.MATERIAL_CONFLICT},
                        {"evidence_validated": False, "disposition": ReviewDisposition.INVALID_EVIDENCE}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(record, **changes)

    def test_review_must_bind_exact_dispute_spec_proposal_and_evidence(self) -> None:
        for changes in ({"dispute_hash": HASH_A}, {"resolution_hash": HASH_A},
                        {"specification_hash": HASH_A}, {"evidence_hash": HASH_A}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                review(**changes).require_valid_for(dispute(), resolution(), specification())

    def test_review_cannot_be_self_attested_by_same_provider(self) -> None:
        for provider in ("provider-a", "PROVIDER-A"):
            with self.subTest(provider=provider), self.assertRaises(ValidationError):
                review(provider=provider)
        analysis = replace(review().counter_analysis, provider="provider-b")
        original_judge_review = review(provider="provider-a", counter_analysis=analysis)
        with self.assertRaisesRegex(ValidationError, "original decision providers"):
            original_judge_review.require_valid_for(dispute(), resolution(), specification())

    def test_missing_wrong_role_and_future_review_steps_rejected(self) -> None:
        record = review()
        cases = ({"evidence_validation": None},
                 {"counter_analysis": replace(record.counter_analysis, task=AITask.COUNTER_JUDGE)},
                 {"independent_judge": replace(record.independent_judge, task=AITask.DISPUTE_ANALYST)},
                 {"reviewed_at_ms": 2200})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(record, **changes)


class ReputationAndCreatorTests(unittest.TestCase):
    def test_accuracy_and_dispute_ratios_agree_with_counts(self) -> None:
        for changes in ({"accuracy_bp": 7999}, {"dispute_accuracy_bp": 9999},
                        {"correct_forecasts": 11}, {"resolved_forecasts": 13},
                        {"successful_disputes": 3}, {"resolved_disputes": 4}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                reputation(**changes)

    def test_invalid_forecasts_are_disjoint_from_binary_resolution_metrics(self) -> None:
        rep = reputation()
        self.assertEqual(rep.resolved_forecasts, 10)
        self.assertEqual(rep.invalid_forecasts, 1)
        with self.assertRaisesRegex(ValidationError, "disjoint"):
            replace(rep, invalid_forecasts=3)

    def test_new_profile_uses_absent_metrics_instead_of_fabricated_zero_scores(self) -> None:
        rep = reputation(total_forecasts=0, resolved_forecasts=0, correct_forecasts=0,
                         invalid_forecasts=0, accuracy_bp=None, brier_score_bp=None,
                         calibration_score_bp=None, consistency_score_bp=None, domain_scores=(),
                         total_disputes=0, resolved_disputes=0, successful_disputes=0,
                         dispute_accuracy_bp=None)
        self.assertIsNone(rep.accuracy_bp)
        for changes in ({"accuracy_bp": 0}, {"brier_score_bp": 0}, {"calibration_score_bp": 0},
                        {"consistency_score_bp": 0}, {"dispute_accuracy_bp": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(rep, **changes)

    def test_domain_scores_are_unique_consistent_and_within_overall_counts(self) -> None:
        rep = reputation()
        score = rep.domain_scores[0]
        for scores in ((score, score),
                       (replace(score, total_forecasts=13),),
                       (score, replace(score, category=Category.SCIENCE))):
            with self.subTest(scores=scores), self.assertRaises(ValidationError):
                replace(rep, domain_scores=scores)
        with self.assertRaises(ValidationError):
            replace(score, accuracy_bp=9999)

    def test_reputation_metric_ranges_and_timestamp_order(self) -> None:
        for changes in ({"total_forecasts": -1}, {"brier_score_bp": 10001},
                        {"calibration_score_bp": True}, {"consistency_score_bp": -1},
                        {"creator_quality_bp": 10001}, {"created_at_ms": 3001}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                reputation(**changes)

    def test_domain_residual_counts_cannot_contradict_overall_results(self) -> None:
        rep = reputation()
        score = rep.domain_scores[0]
        incorrect = replace(score, total_forecasts=1, resolved_forecasts=1,
                            correct_forecasts=0, accuracy_bp=0)
        with self.assertRaisesRegex(ValidationError, "incorrect counts"):
            reputation(total_forecasts=1, resolved_forecasts=1, correct_forecasts=1,
                       invalid_forecasts=0, accuracy_bp=10000, domain_scores=(incorrect,))
        unresolved = replace(score, total_forecasts=2, resolved_forecasts=1,
                             correct_forecasts=1, accuracy_bp=10000)
        with self.assertRaisesRegex(ValidationError, "unresolved counts"):
            reputation(total_forecasts=2, resolved_forecasts=2, correct_forecasts=2,
                       invalid_forecasts=0, accuracy_bp=10000, domain_scores=(unresolved,))

    def test_creator_counts_and_derived_metrics(self) -> None:
        profile = creator()
        self.assertEqual(profile.invalid_rate_bp, 1000)
        self.assertEqual(profile.dispute_rate_bp, 1000)
        self.assertEqual(profile.average_participation, 12)
        for changes in ({"invalid_markets": 11}, {"resolved_markets": 21},
                        {"disputed_markets": 21}, {"follower_count": -1},
                        {"total_participation": MAX_SAFE_INTEGER + 1}, {"created_at_ms": 3001}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(profile, **changes)

    def test_new_creator_has_no_quality_or_participation_history(self) -> None:
        profile = creator(markets_created=0, resolved_markets=0, invalid_markets=0,
                          disputed_markets=0, total_participation=0, creator_quality_bp=None)
        self.assertIsNone(profile.invalid_rate_bp)
        self.assertIsNone(profile.dispute_rate_bp)
        self.assertIsNone(profile.average_participation)
        for changes in ({"total_participation": 1}, {"creator_quality_bp": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                replace(profile, **changes)


if __name__ == "__main__":
    unittest.main()
