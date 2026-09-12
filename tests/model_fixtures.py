"""Deterministic immutable records shared by model, contract and lifecycle tests."""

from __future__ import annotations

import hashlib
from typing import Any

from forecast_domain.models import (
    AIProvenance,
    AITask,
    Category,
    ConflictStatus,
    CreatorProfile,
    Dispute,
    DisputeReview,
    DomainScore,
    DuplicateCandidate,
    EvidenceSnapshot,
    ForecastChoice,
    ForecastSpecification,
    Outcome,
    Resolution,
    ReviewDisposition,
    RuleClause,
    Source,
    SourcePolicy,
    SourceVerification,
    UserForecast,
    UserReputation,
    ValidationAssessment,
    ambiguity_output_hash,
    counter_judge_input_hash,
    counter_judge_output_hash,
    dispute_analysis_output_hash,
    dispute_evidence_output_hash,
    dispute_review_input_hash,
    dispute_review_output_hash,
    duplicate_output_hash,
    resolution_input_hash,
    resolution_output_hash,
    source_verification_output_hash,
)
from forecast_domain.records import Record
from forecast_domain.serialization import content_hash

HASH_A = "a" * 64
HASH_B = "b" * 64


def provenance(task: AITask, *, input_hash: str = HASH_A, output_hash: str = HASH_B,
               provider: str = "provider-a", created_at_ms: int = 50, **changes: Any) -> AIProvenance:
    values: dict[str, Any] = dict(task=task, provider=provider, model="grounded-model",
                                  model_version="2026-09-01", policy_version="policy-v1",
                                  input_hash=input_hash, output_hash=output_hash,
                                  created_at_ms=created_at_ms)
    values.update(changes)
    return AIProvenance(**values)


def specification(**changes: Any) -> ForecastSpecification:
    values: dict[str, Any] = dict(
        canonical_question="Will Acme officially announce Product X before the deadline?",
        rules=(RuleClause(clause_id="yes-rule", outcome=Outcome.YES,
                          condition="An official announcement dated before closing names Product X."),
               RuleClause(clause_id="no-rule", outcome=Outcome.NO,
                          condition="No qualifying announcement exists at closing."),
               RuleClause(clause_id="invalid-rule", outcome=Outcome.INVALID,
                          condition="The named company or product cannot be uniquely identified.")),
        open_at_ms=100, close_at_ms=1000,
        source_policy=SourcePolicy(primary_sources=(Source(
            source_id="acme-news", name="Acme Newsroom", url="https://acme.example/news",
            is_official=True),), fallback_sources=(Source(
                source_id="news-wire", name="Independent News Wire",
                url="https://wire.example/acme", is_official=False),)),
        invalidation_rules=("Apply INVALID if the named product has multiple incompatible identities.",),
        category=Category.TECHNOLOGY, share_title="Will Acme announce Product X?",
        ambiguity_score_bp=500, duplicate_candidates=(),
    )
    values.update(changes)
    return ForecastSpecification(**values)


def validation(spec: ForecastSpecification | None = None, **changes: Any) -> ValidationAssessment:
    spec = specification() if spec is None else spec
    digest = spec.specification_hash
    values: dict[str, Any] = dict(
        specification_hash=digest, deterministic_check_version="deterministic-v1",
        deterministic_passed=True, objectively_resolvable=True, ambiguity_passed=True,
        duplicate_check_completed=True, ambiguity_limit_bp=1000, duplicate_similarity_threshold_bp=8500,
        validated_at_ms=60, explanation="Objective rules, official sources, no equivalent duplicates.",
    )
    values.update(changes)
    values.setdefault("compiler", provenance(AITask.MARKET_COMPILER, output_hash=digest))
    values.setdefault("ambiguity_judge", provenance(
        AITask.AMBIGUITY_JUDGE, input_hash=digest, created_at_ms=51,
        output_hash=ambiguity_output_hash(digest, values["objectively_resolvable"],
                                           values["ambiguity_passed"], values["ambiguity_limit_bp"],
                                           values["explanation"])))
    values.setdefault("duplicate_detector", provenance(
        AITask.DUPLICATE_DETECTOR, input_hash=digest, created_at_ms=52,
        output_hash=duplicate_output_hash(digest, values["duplicate_check_completed"],
                                           values["duplicate_similarity_threshold_bp"], values["explanation"])))
    return ValidationAssessment(**values)


def user_forecast(spec: ForecastSpecification | None = None, **changes: Any) -> UserForecast:
    spec = specification() if spec is None else spec
    values: dict[str, Any] = dict(forecaster_id="user-1", forecast_id="forecast-1",
                                  specification_hash=spec.specification_hash,
                                  outcome=ForecastChoice.YES, confidence=70, submitted_at_ms=200)
    values.update(changes)
    return UserForecast(**values)


def evidence(spec: ForecastSpecification | None = None, *, evidence_id: str = "evidence-1",
             collected_at_ms: int | None = None, content: str = "Immutable official announcement.",
             **changes: Any) -> EvidenceSnapshot:
    spec = specification() if spec is None else spec
    at = spec.close_at_ms + 10 if collected_at_ms is None else collected_at_ms
    digest = hashlib.sha256(content.encode()).hexdigest()
    values: dict[str, Any] = dict(
        evidence_id=evidence_id, source_id=spec.source_policy.primary_sources[0].source_id,
        url="https://acme.example/news/product-x", content_sha256=digest,
        snapshot_uri="urn:sha256:" + digest, collected_at_ms=at,
        collector=provenance(AITask.EVIDENCE_COLLECTOR, output_hash=digest, created_at_ms=at),
    )
    values.update(changes)
    return EvidenceSnapshot(**values)


def source_verification(snapshot: EvidenceSnapshot, **changes: Any) -> SourceVerification:
    values: dict[str, Any] = dict(
        evidence_hash=snapshot.evidence_hash, source_id=snapshot.source_id, verified=True,
        explanation="Snapshot origin and retained bytes verified.",
    )
    values.update(changes)
    values.setdefault("verifier", provenance(
        AITask.SOURCE_VERIFIER, input_hash=values["evidence_hash"],
        created_at_ms=snapshot.collected_at_ms + 1,
        output_hash=source_verification_output_hash(values["evidence_hash"], values["source_id"],
                                                    values["verified"], values["explanation"])))
    return SourceVerification(**values)


def resolution(spec: ForecastSpecification | None = None, *, forecast_id: str = "forecast-1",
               proposed_at_ms: int = 2000, outcome: Outcome = Outcome.YES,
               **changes: Any) -> Resolution:
    spec = specification() if spec is None else spec
    snapshots = changes.pop("evidence", (evidence(spec),))
    verifications = changes.pop("source_verifications", tuple(source_verification(item) for item in snapshots))
    digest = resolution_input_hash(forecast_id, spec.specification_hash, snapshots, verifications)
    judge_at = max(item.verifier.created_at_ms for item in verifications) + 1
    values: dict[str, Any] = dict(
        forecast_id=forecast_id, specification_hash=spec.specification_hash, proposed_outcome=outcome,
        confidence_bp=9400, evidence=snapshots, source_verifications=verifications,
        rule_matches=(next(rule.clause_id for rule in spec.rules if rule.outcome == outcome),),
        rule_conflicts=(), reason_summary="The retained official evidence satisfies the selected clause.",
        counter_judge_agrees=True, conflict_status=ConflictStatus.CLEAR, proposed_at_ms=proposed_at_ms,
        conflict_explanation=None,
    )
    values.update(changes)
    output_digest = resolution_output_hash(digest, values["proposed_outcome"], values["confidence_bp"],
                                           values["rule_matches"], values["rule_conflicts"],
                                           values["reason_summary"], values["conflict_status"],
                                           values["conflict_explanation"])
    values.setdefault("judge", provenance(AITask.RESOLUTION_JUDGE, input_hash=digest,
                                          output_hash=output_digest, created_at_ms=judge_at))
    values.setdefault("counter_judge", provenance(
        AITask.COUNTER_JUDGE, input_hash=counter_judge_input_hash(digest, values["judge"]),
        output_hash=counter_judge_output_hash(values["judge"].output_hash, values["counter_judge_agrees"]),
        created_at_ms=judge_at + 1))
    return Resolution(**values)


def dispute(spec: ForecastSpecification | None = None, proposal: Resolution | None = None,
            *, dispute_id: str = "dispute-1", submitted_at_ms: int = 2200,
            **changes: Any) -> Dispute:
    spec = specification() if spec is None else spec
    proposal = resolution(spec) if proposal is None else proposal
    values: dict[str, Any] = dict(
        dispute_id=dispute_id, disputant_id="disputant-1", forecast_id=proposal.forecast_id,
        specification_hash=spec.specification_hash, resolution_hash=proposal.resolution_hash,
        claim="The announcement may name a different product.",
        evidence=(evidence(spec, evidence_id="dispute-evidence", collected_at_ms=submitted_at_ms - 1,
                           content="Retained counter-evidence with product identity details.", collector=None),),
        rule_clause_id="yes-rule", explanation="The cited rule requires an exact product identity.",
        submitted_at_ms=submitted_at_ms,
    )
    values.update(changes)
    return Dispute(**values)


def review(dispute_record: Dispute | None = None, proposal: Resolution | None = None,
           spec: ForecastSpecification | None = None, *, material_conflict: bool = False,
           reviewed_at_ms: int = 2300, provider: str = "independent-provider",
           **changes: Any) -> DisputeReview:
    spec = specification() if spec is None else spec
    proposal = resolution(spec) if proposal is None else proposal
    dispute_record = dispute(spec, proposal) if dispute_record is None else dispute_record
    values: dict[str, Any] = dict(
        dispute_hash=dispute_record.dispute_hash, specification_hash=spec.specification_hash,
        resolution_hash=proposal.resolution_hash, evidence_hash=dispute_record.evidence_hash,
        evidence_validated=True, material_conflict=material_conflict,
        reason_summary="Independent source-grounded review of the exact dispute and counter-analysis.",
        reviewed_at_ms=reviewed_at_ms,
    )
    values.update(changes)
    values.setdefault("disposition", ReviewDisposition.MATERIAL_CONFLICT if values["material_conflict"]
                      else ReviewDisposition.RETAIN_PROPOSAL if values["evidence_validated"]
                      else ReviewDisposition.INVALID_EVIDENCE)
    values.setdefault("evidence_validation", provenance(
        AITask.SOURCE_VERIFIER, input_hash=values["evidence_hash"], created_at_ms=reviewed_at_ms - 3,
        output_hash=dispute_evidence_output_hash(values["evidence_hash"], values["evidence_validated"])))
    values.setdefault("counter_analysis", provenance(
        AITask.DISPUTE_ANALYST, input_hash=values["dispute_hash"], created_at_ms=reviewed_at_ms - 2,
        output_hash=dispute_analysis_output_hash(values["dispute_hash"], values["evidence_hash"],
                                                values["material_conflict"], values["reason_summary"])))
    digest = dispute_review_input_hash(values["dispute_hash"], values["specification_hash"],
                                       values["resolution_hash"], values["evidence_hash"],
                                       values["evidence_validation"], values["counter_analysis"])
    values.setdefault("independent_judge", provenance(
        AITask.INDEPENDENT_REJUDGE, provider=provider, input_hash=digest, created_at_ms=reviewed_at_ms - 1,
        output_hash=dispute_review_output_hash(digest, values["evidence_validated"],
                                              values["material_conflict"], values["disposition"],
                                              values["reason_summary"])))
    return DisputeReview(**values)


def reputation(**changes: Any) -> UserReputation:
    values: dict[str, Any] = dict(
        user_id="user-1", total_forecasts=12, resolved_forecasts=10, correct_forecasts=8,
        invalid_forecasts=1, accuracy_bp=8000, brier_score_bp=1400, calibration_score_bp=9100,
        consistency_score_bp=9000,
        domain_scores=(DomainScore(category=Category.TECHNOLOGY, total_forecasts=12,
                                   resolved_forecasts=10, correct_forecasts=8, accuracy_bp=8000,
                                   brier_score_bp=1400, calibration_score_bp=9100),),
        total_disputes=3, resolved_disputes=2, successful_disputes=1, dispute_accuracy_bp=5000,
        creator_quality_bp=None, created_at_ms=0, updated_at_ms=3000,
    )
    values.update(changes)
    return UserReputation(**values)


def creator(**changes: Any) -> CreatorProfile:
    values: dict[str, Any] = dict(creator_id="creator-1", markets_created=20, resolved_markets=10,
                                  invalid_markets=1, disputed_markets=2, total_participation=240,
                                  follower_count=30, creator_quality_bp=9000,
                                  created_at_ms=0, updated_at_ms=3000)
    values.update(changes)
    return CreatorProfile(**values)


def all_model_records() -> tuple[Record, ...]:
    spec = specification()
    proof = validation(spec)
    proposal = resolution(spec)
    challenge = dispute(spec, proposal)
    rep = reputation()
    return (spec, spec.rules[0], spec.source_policy, spec.source_policy.sources[0],
            DuplicateCandidate(forecast_id="earlier-forecast", specification_hash=content_hash("earlier"),
                               similarity_bp=9000, materially_different_rules=True,
                               explanation="A different closing date materially changes the rules."),
            proof, proof.compiler, user_forecast(spec), proposal, proposal.evidence[0],
            proposal.source_verifications[0], challenge, review(challenge, proposal, spec),
            rep, rep.domain_scores[0], creator())
