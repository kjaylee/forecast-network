"""Immutable, versioned forecasting records and their business invariants.

Hashes commit to complete records, never to mutable web resources. AI provenance
is an auditable attestation; future adapters authenticate who may supply it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from .errors import ValidationError
from .records import MAX_SAFE_INTEGER, Record

ID = {"pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?![\s\S])"}
HASH = {"pattern": r"^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64}
COUNT = {"minimum": 0, "maximum": MAX_SAFE_INTEGER}
BP = {"minimum": 0, "maximum": 10000}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _https_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValidationError("source URL is malformed") from error
    _require(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and not any(character.isspace() for character in value)
        and (port is None or 1 <= port <= 65535),
        "source URL must be absolute HTTPS without credentials, whitespace or fragment",
    )


def _hash(value: object) -> str:
    from .serialization import content_hash

    return content_hash(value)


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"
    INVALID = "INVALID"


class ForecastChoice(str, Enum):
    YES = "YES"
    NO = "NO"


class Category(str, Enum):
    TECHNOLOGY = "TECHNOLOGY"
    CRYPTO = "CRYPTO"
    SCIENCE = "SCIENCE"
    ENTERTAINMENT = "ENTERTAINMENT"
    WORLD = "WORLD"
    SPORTS = "SPORTS"
    OTHER = "OTHER"


class AITask(str, Enum):
    MARKET_COMPILER = "MARKET_COMPILER"
    AMBIGUITY_JUDGE = "AMBIGUITY_JUDGE"
    DUPLICATE_DETECTOR = "DUPLICATE_DETECTOR"
    SOURCE_VERIFIER = "SOURCE_VERIFIER"
    EVIDENCE_COLLECTOR = "EVIDENCE_COLLECTOR"
    RESOLUTION_JUDGE = "RESOLUTION_JUDGE"
    COUNTER_JUDGE = "COUNTER_JUDGE"
    DISPUTE_ANALYST = "DISPUTE_ANALYST"
    INDEPENDENT_REJUDGE = "INDEPENDENT_REJUDGE"
    ADJUDICATION = "ADJUDICATION"


class ConflictStatus(str, Enum):
    CLEAR = "CLEAR"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class ReviewDisposition(str, Enum):
    RETAIN_PROPOSAL = "RETAIN_PROPOSAL"
    MATERIAL_CONFLICT = "MATERIAL_CONFLICT"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleClause(Record):
    clause_id: str = field(metadata=ID)
    outcome: Outcome
    condition: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Source(Record):
    source_id: str = field(metadata=ID)
    name: str
    url: str
    is_official: bool

    def validate(self) -> None:
        _https_url(self.url)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePolicy(Record):
    primary_sources: tuple[Source, ...] = field(metadata={"minItems": 1})
    fallback_sources: tuple[Source, ...] = ()

    def validate(self) -> None:
        sources = self.primary_sources + self.fallback_sources
        _require(all(source.is_official for source in self.primary_sources),
                 "primary resolution sources must be official")
        _require(len({source.source_id for source in sources}) == len(sources),
                 "source IDs must be unique across the policy")
        _require(len({source.url for source in sources}) == len(sources),
                 "source URLs must be unique across the policy")

    @property
    def sources(self) -> tuple[Source, ...]:
        return self.primary_sources + self.fallback_sources


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplicateCandidate(Record):
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    similarity_bp: int = field(metadata=BP)
    materially_different_rules: bool
    explanation: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ForecastSpecification(Record):
    canonical_question: str
    rules: tuple[RuleClause, ...] = field(metadata={"minItems": 3, "maxItems": 3})
    open_at_ms: int = field(metadata=COUNT)
    close_at_ms: int = field(metadata=COUNT)
    source_policy: SourcePolicy
    invalidation_rules: tuple[str, ...]
    category: Category
    share_title: str
    ambiguity_score_bp: int = field(metadata=BP)
    duplicate_candidates: tuple[DuplicateCandidate, ...] = ()

    def validate(self) -> None:
        _require(self.open_at_ms < self.close_at_ms, "opening must precede closing")
        _require({rule.outcome for rule in self.rules} == set(Outcome),
                 "specification requires one YES, NO and INVALID clause")
        _require(len(self.clause_ids) == len(set(self.clause_ids)), "clause IDs must be unique")
        _require(len(set(self.invalidation_rules)) == len(self.invalidation_rules),
                 "invalidation rules must be unique")
        _require(len({item.forecast_id for item in self.duplicate_candidates})
                 == len(self.duplicate_candidates), "duplicate candidate forecast IDs must be unique")

    @property
    def specification_hash(self) -> str:
        return _hash(self)

    @property
    def clause_ids(self) -> tuple[str, ...]:
        return tuple(rule.clause_id for rule in self.rules)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(source.source_id for source in self.source_policy.sources)

    def validate_rule_references(self, clause_ids: tuple[str, ...]) -> None:
        _require(set(clause_ids).issubset(self.clause_ids), "unknown specification rule clause")


@dataclass(frozen=True, slots=True, kw_only=True)
class AIProvenance(Record):
    task: AITask
    provider: str = field(metadata=ID)
    model: str
    model_version: str
    policy_version: str
    input_hash: str = field(metadata=HASH)
    output_hash: str = field(metadata=HASH)
    created_at_ms: int = field(metadata=COUNT)


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationAssessment(Record):
    specification_hash: str = field(metadata=HASH)
    deterministic_check_version: str
    deterministic_passed: bool
    objectively_resolvable: bool
    ambiguity_passed: bool
    duplicate_check_completed: bool
    ambiguity_limit_bp: int = field(metadata=BP)
    duplicate_similarity_threshold_bp: int = field(metadata={"minimum": 1, "maximum": 10000})
    compiler: AIProvenance
    ambiguity_judge: AIProvenance
    duplicate_detector: AIProvenance
    validated_at_ms: int = field(metadata=COUNT)
    explanation: str

    def validate(self) -> None:
        _require(self.compiler.task == AITask.MARKET_COMPILER, "compiler provenance task mismatch")
        _require(self.compiler.output_hash == self.specification_hash,
                 "compiler output must bind the exact specification")
        for provenance, task in ((self.ambiguity_judge, AITask.AMBIGUITY_JUDGE),
                                 (self.duplicate_detector, AITask.DUPLICATE_DETECTOR)):
            _require(provenance.task == task, "validation provenance task mismatch")
            _require(provenance.input_hash == self.specification_hash,
                     "validation input must bind the exact specification")
        _require(all(item.created_at_ms <= self.validated_at_ms for item in
                     (self.compiler, self.ambiguity_judge, self.duplicate_detector)),
                 "validation cannot predate its AI decisions")
        _require(self.ambiguity_judge.output_hash == ambiguity_output_hash(
            self.specification_hash, self.objectively_resolvable, self.ambiguity_passed,
            self.ambiguity_limit_bp, self.explanation), "ambiguity decision output commitment mismatch")
        _require(self.duplicate_detector.output_hash == duplicate_output_hash(
            self.specification_hash, self.duplicate_check_completed,
            self.duplicate_similarity_threshold_bp, self.explanation),
            "duplicate decision output commitment mismatch")

    def require_publishable(self, specification: ForecastSpecification) -> None:
        _require(self.specification_hash == specification.specification_hash,
                 "validation proof refers to a different specification")
        _require(self.deterministic_passed and self.objectively_resolvable
                 and self.ambiguity_passed and self.duplicate_check_completed,
                 "all publication validation checks must pass")
        _require(specification.ambiguity_score_bp <= self.ambiguity_limit_bp,
                 "specification ambiguity exceeds its validated limit")
        _require(all(candidate.similarity_bp < self.duplicate_similarity_threshold_bp
                     or candidate.materially_different_rules
                     for candidate in specification.duplicate_candidates),
                 "materially equivalent duplicate cannot publish")


def ambiguity_output_hash(specification_hash: str, objectively_resolvable: bool,
                           ambiguity_passed: bool, ambiguity_limit_bp: int, explanation: str) -> str:
    return _hash({"schema_version": 1, "kind": "ambiguity_output",
                  "specification_hash": specification_hash,
                  "objectively_resolvable": objectively_resolvable,
                  "ambiguity_passed": ambiguity_passed, "ambiguity_limit_bp": ambiguity_limit_bp,
                  "explanation": explanation})


def duplicate_output_hash(specification_hash: str, duplicate_check_completed: bool,
                           duplicate_similarity_threshold_bp: int, explanation: str) -> str:
    return _hash({"schema_version": 1, "kind": "duplicate_output",
                  "specification_hash": specification_hash,
                  "duplicate_check_completed": duplicate_check_completed,
                  "duplicate_similarity_threshold_bp": duplicate_similarity_threshold_bp,
                  "explanation": explanation})


@dataclass(frozen=True, slots=True, kw_only=True)
class UserForecast(Record):
    forecaster_id: str = field(metadata=ID)
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    outcome: ForecastChoice
    confidence: int = field(metadata={"minimum": 0, "maximum": 100})
    submitted_at_ms: int = field(metadata=COUNT)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceSnapshot(Record):
    evidence_id: str = field(metadata=ID)
    source_id: str = field(metadata=ID)
    url: str
    content_sha256: str = field(metadata=HASH)
    snapshot_uri: str
    collected_at_ms: int = field(metadata=COUNT)
    collector: AIProvenance | None = None

    def validate(self) -> None:
        _https_url(self.url)
        _require(self.snapshot_uri == "urn:sha256:" + self.content_sha256,
                 "evidence snapshot URI must bind its immutable content digest")
        if self.collector is not None:
            _require(self.collector.task == AITask.EVIDENCE_COLLECTOR,
                     "evidence collector provenance task mismatch")
            _require(self.collector.output_hash == self.content_sha256,
                     "collector output must bind retained evidence content")
            _require(self.collector.created_at_ms <= self.collected_at_ms,
                     "snapshot cannot predate evidence collection")

    @property
    def evidence_hash(self) -> str:
        return _hash(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceVerification(Record):
    evidence_hash: str = field(metadata=HASH)
    source_id: str = field(metadata=ID)
    verified: bool
    explanation: str
    verifier: AIProvenance

    def validate(self) -> None:
        _require(self.verifier.task == AITask.SOURCE_VERIFIER, "source verification task mismatch")
        _require(self.verifier.input_hash == self.evidence_hash,
                 "source verification must bind the exact evidence snapshot")
        _require(self.verifier.output_hash == source_verification_output_hash(
            self.evidence_hash, self.source_id, self.verified, self.explanation),
            "source verification decision output commitment mismatch")


def source_verification_output_hash(evidence_hash: str, source_id: str, verified: bool,
                                    explanation: str) -> str:
    return _hash({"schema_version": 1, "kind": "source_verification_output",
                  "evidence_hash": evidence_hash, "source_id": source_id,
                  "verified": verified, "explanation": explanation})


def resolution_input_hash(
    forecast_id: str, specification_hash: str, evidence: tuple[EvidenceSnapshot, ...],
    source_verifications: tuple[SourceVerification, ...],
) -> str:
    """Input shared by the judge and counter-judge, without self-referential hashes."""
    return _hash({"schema_version": 1, "kind": "resolution_input", "forecast_id": forecast_id,
                  "specification_hash": specification_hash, "evidence": evidence,
                  "source_verifications": source_verifications})


def resolution_output_hash(
    decision_input_hash: str, proposed_outcome: Outcome, confidence_bp: int,
    rule_matches: tuple[str, ...], rule_conflicts: tuple[str, ...], reason_summary: str,
    conflict_status: ConflictStatus, conflict_explanation: str | None,
) -> str:
    return _hash({"schema_version": 1, "kind": "resolution_output",
                  "decision_input_hash": decision_input_hash, "proposed_outcome": proposed_outcome,
                  "confidence_bp": confidence_bp, "rule_matches": rule_matches,
                  "rule_conflicts": rule_conflicts, "reason_summary": reason_summary,
                  "conflict_status": conflict_status, "conflict_explanation": conflict_explanation})


def counter_judge_input_hash(decision_input_hash: str, judge: AIProvenance) -> str:
    return _hash({"schema_version": 1, "kind": "counter_judge_input",
                  "decision_input_hash": decision_input_hash, "judge": judge})


def counter_judge_output_hash(judge_output_hash: str, agrees: bool) -> str:
    return _hash({"schema_version": 1, "kind": "counter_judge_output",
                  "judge_output_hash": judge_output_hash, "agrees": agrees})


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolution(Record):
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    proposed_outcome: Outcome
    confidence_bp: int = field(metadata=BP)
    evidence: tuple[EvidenceSnapshot, ...] = field(metadata={"minItems": 1})
    source_verifications: tuple[SourceVerification, ...] = field(metadata={"minItems": 1})
    rule_matches: tuple[str, ...] = field(metadata={"minItems": 1, "uniqueItems": True})
    rule_conflicts: tuple[str, ...] = field(metadata={"uniqueItems": True})
    reason_summary: str
    judge: AIProvenance
    counter_judge: AIProvenance
    counter_judge_agrees: bool
    conflict_status: ConflictStatus
    proposed_at_ms: int = field(metadata=COUNT)
    conflict_explanation: str | None = None

    def validate(self) -> None:
        hashes = {item.evidence_hash for item in self.evidence}
        _require(len(hashes) == len(self.evidence), "resolution evidence snapshots must be unique")
        _require(len({item.evidence_id for item in self.evidence}) == len(self.evidence),
                 "resolution evidence IDs must be unique")
        _require(len(self.source_verifications) == len(self.evidence),
                 "every resolution evidence snapshot requires one verification")
        _require({item.evidence_hash for item in self.source_verifications} == hashes,
                 "source verifications must cover the exact evidence snapshots")
        by_hash = {item.evidence_hash: item for item in self.evidence}
        for verification in self.source_verifications:
            _require(verification.source_id == by_hash[verification.evidence_hash].source_id,
                     "source verification refers to a different source")
            _require(verification.verifier.created_at_ms
                     >= by_hash[verification.evidence_hash].collected_at_ms,
                     "source verification cannot predate evidence collection")
        _require(self.judge.task == AITask.RESOLUTION_JUDGE, "resolution judge task mismatch")
        _require(self.counter_judge.task == AITask.COUNTER_JUDGE, "counter-judge task mismatch")
        _require(self.judge.input_hash == self.decision_input_hash,
                 "resolution judge input must bind evidence, verification and specification")
        _require(self.counter_judge.input_hash == counter_judge_input_hash(self.decision_input_hash, self.judge),
                 "counter-judge input must bind evidence and the exact judge decision")
        _require(self.judge.output_hash == resolution_output_hash(
            self.decision_input_hash, self.proposed_outcome, self.confidence_bp, self.rule_matches,
            self.rule_conflicts, self.reason_summary, self.conflict_status, self.conflict_explanation),
            "resolution decision output commitment mismatch")
        _require(self.counter_judge.output_hash == counter_judge_output_hash(
            self.judge.output_hash, self.counter_judge_agrees),
            "counter-judge decision output commitment mismatch")
        _require(self.counter_judge.created_at_ms >= self.judge.created_at_ms,
                 "counter-judge cannot predate the resolution judge")
        evidence_ready_at = max(item.verifier.created_at_ms for item in self.source_verifications)
        _require(self.judge.created_at_ms >= evidence_ready_at,
                 "resolution judge cannot predate source verification")
        _require(self.proposed_at_ms >= self.counter_judge.created_at_ms,
                 "proposal cannot predate its counter-judge")
        _require(not (set(self.rule_matches) & set(self.rule_conflicts)),
                 "a rule cannot simultaneously match and conflict")
        if self.conflict_status == ConflictStatus.CLEAR:
            _require(not self.rule_conflicts and self.counter_judge_agrees,
                     "clear resolution cannot contain conflicts or judge disagreement")
            _require(self.conflict_explanation is None, "clear resolution has no conflict explanation")
        else:
            _require(self.conflict_explanation is not None,
                     "conflicting resolution requires an explicit explanation")
        if self.conflict_status == ConflictStatus.RESOLVED:
            _require(self.counter_judge_agrees, "resolved conflict requires counter-judge agreement")

    @property
    def decision_input_hash(self) -> str:
        return resolution_input_hash(self.forecast_id, self.specification_hash,
                                     self.evidence, self.source_verifications)

    @property
    def resolution_hash(self) -> str:
        return _hash(self)

    def validate_for(self, specification: ForecastSpecification) -> None:
        _require(self.specification_hash == specification.specification_hash,
                 "resolution refers to a different specification")
        specification.validate_rule_references(self.rule_matches + self.rule_conflicts)
        matched_outcomes = {rule.outcome for rule in specification.rules
                            if rule.clause_id in self.rule_matches}
        _require(matched_outcomes == {self.proposed_outcome},
                 "resolution may match only its selected outcome clause")
        _require(all(item.source_id in specification.source_ids for item in self.evidence),
                 "resolution evidence source is outside the published source policy")
        source_by_id = {source.source_id: source for source in specification.source_policy.sources}
        _require(all(urlsplit(item.url).hostname == urlsplit(source_by_id[item.source_id].url).hostname
                     for item in self.evidence), "evidence URL is outside its declared source host")
        self._validate_evidence_time(specification)

    def _validate_evidence_time(self, specification: ForecastSpecification) -> None:
        _require(all(item.collected_at_ms >= specification.close_at_ms for item in self.evidence),
                 "resolution evidence must be collected after forecast expiry")

    def require_proposable(self, specification: ForecastSpecification) -> None:
        self.validate_for(specification)
        _require(all(item.verified for item in self.source_verifications),
                 "resolution requires verified evidence sources")
        _require(self.conflict_status != ConflictStatus.UNRESOLVED and self.counter_judge_agrees,
                 "unresolved evidence or judge conflict blocks proposal")


@dataclass(frozen=True, slots=True, kw_only=True)
class Dispute(Record):
    dispute_id: str = field(metadata=ID)
    disputant_id: str = field(metadata=ID)
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    resolution_hash: str = field(metadata=HASH)
    claim: str
    evidence: tuple[EvidenceSnapshot, ...] = field(metadata={"minItems": 1})
    rule_clause_id: str = field(metadata=ID)
    explanation: str
    submitted_at_ms: int = field(metadata=COUNT)

    def validate(self) -> None:
        _require(len({item.evidence_id for item in self.evidence}) == len(self.evidence),
                 "dispute evidence IDs must be unique")
        _require(all(item.collected_at_ms <= self.submitted_at_ms for item in self.evidence),
                 "dispute cannot predate its evidence")

    @property
    def dispute_hash(self) -> str:
        return _hash(self)

    @property
    def evidence_hash(self) -> str:
        return _hash({"schema_version": 1, "kind": "dispute_evidence", "evidence": self.evidence})

    def validate_for(self, specification: ForecastSpecification, resolution: Resolution) -> None:
        _require(self.forecast_id == resolution.forecast_id, "dispute forecast binding mismatch")
        _require(self.specification_hash == specification.specification_hash
                 == resolution.specification_hash, "dispute specification binding mismatch")
        _require(self.resolution_hash == resolution.resolution_hash, "dispute proposal binding mismatch")
        specification.validate_rule_references((self.rule_clause_id,))
        _require(self.submitted_at_ms >= resolution.proposed_at_ms,
                 "dispute cannot predate the proposal")


@dataclass(frozen=True, slots=True, kw_only=True)
class DisputeReview(Record):
    dispute_hash: str = field(metadata=HASH)
    specification_hash: str = field(metadata=HASH)
    resolution_hash: str = field(metadata=HASH)
    evidence_hash: str = field(metadata=HASH)
    evidence_validated: bool
    material_conflict: bool
    disposition: ReviewDisposition
    evidence_validation: AIProvenance
    counter_analysis: AIProvenance
    independent_judge: AIProvenance
    reason_summary: str
    reviewed_at_ms: int = field(metadata=COUNT)

    def validate(self) -> None:
        expected_flags = {
            ReviewDisposition.RETAIN_PROPOSAL: (True, False),
            ReviewDisposition.MATERIAL_CONFLICT: (True, True),
            ReviewDisposition.INVALID_EVIDENCE: (False, False),
        }
        _require((self.evidence_validated, self.material_conflict) == expected_flags[self.disposition],
                 "review disposition must agree with evidence validation and material conflict")
        _require(self.evidence_validation.task == AITask.SOURCE_VERIFIER,
                 "dispute evidence validation provenance task mismatch")
        _require(self.evidence_validation.input_hash == self.evidence_hash,
                 "dispute evidence validation must bind its evidence bundle")
        _require(self.counter_analysis.task == AITask.DISPUTE_ANALYST,
                 "dispute counter-analysis provenance task mismatch")
        _require(self.counter_analysis.input_hash == self.dispute_hash,
                 "counter-analysis must bind the exact dispute")
        _require(self.independent_judge.task == AITask.INDEPENDENT_REJUDGE,
                 "dispute independent review provenance task mismatch")
        _require(self.independent_judge.input_hash == self.review_input_hash,
                 "independent review must bind the dispute and preceding analyses")
        _require(self.evidence_validation.output_hash == dispute_evidence_output_hash(
            self.evidence_hash, self.evidence_validated), "dispute evidence decision output mismatch")
        _require(self.counter_analysis.output_hash == dispute_analysis_output_hash(
            self.dispute_hash, self.evidence_hash, self.material_conflict, self.reason_summary),
            "dispute analysis decision output mismatch")
        _require(self.independent_judge.output_hash == dispute_review_output_hash(
            self.review_input_hash, self.evidence_validated, self.material_conflict,
            self.disposition, self.reason_summary),
            "independent re-judge decision output mismatch")
        _require(self.independent_judge.provider.casefold() != self.counter_analysis.provider.casefold(),
                 "independent re-judge must use a different provider from counter-analysis")
        _require(self.evidence_validation.created_at_ms <= self.counter_analysis.created_at_ms
                 <= self.independent_judge.created_at_ms <= self.reviewed_at_ms,
                 "dispute review steps must follow their causal order")

    @property
    def review_input_hash(self) -> str:
        return dispute_review_input_hash(self.dispute_hash, self.specification_hash,
                                         self.resolution_hash, self.evidence_hash,
                                         self.evidence_validation, self.counter_analysis)

    @property
    def review_hash(self) -> str:
        return _hash(self)

    def require_valid_for(self, dispute: Dispute, resolution: Resolution,
                          specification: ForecastSpecification) -> None:
        dispute.validate_for(specification, resolution)
        _require(self.dispute_hash == dispute.dispute_hash, "review refers to a different dispute")
        _require(self.specification_hash == specification.specification_hash,
                 "review specification binding mismatch")
        _require(self.resolution_hash == resolution.resolution_hash, "review proposal binding mismatch")
        _require(self.evidence_hash == dispute.evidence_hash, "review evidence binding mismatch")
        _require(self.evidence_validation.created_at_ms >= dispute.submitted_at_ms,
                 "dispute review cannot predate dispute submission")
        original_providers = {resolution.judge.provider.casefold(), resolution.counter_judge.provider.casefold()}
        _require(self.independent_judge.provider.casefold() not in original_providers,
                 "dispute re-judge must be independent of both original decision providers")


def dispute_review_input_hash(
    dispute_hash: str, specification_hash: str, resolution_hash: str, evidence_hash: str,
    evidence_validation: AIProvenance, counter_analysis: AIProvenance,
) -> str:
    return _hash({"schema_version": 1, "kind": "dispute_review_input", "dispute_hash": dispute_hash,
                  "specification_hash": specification_hash, "resolution_hash": resolution_hash,
                  "evidence_hash": evidence_hash, "evidence_validation": evidence_validation,
                  "counter_analysis": counter_analysis})


def dispute_evidence_output_hash(evidence_hash: str, evidence_validated: bool) -> str:
    return _hash({"schema_version": 1, "kind": "dispute_evidence_output",
                  "evidence_hash": evidence_hash, "evidence_validated": evidence_validated})


def dispute_analysis_output_hash(dispute_hash: str, evidence_hash: str, material_conflict: bool,
                                 reason_summary: str) -> str:
    return _hash({"schema_version": 1, "kind": "dispute_analysis_output",
                  "dispute_hash": dispute_hash, "evidence_hash": evidence_hash,
                  "material_conflict": material_conflict, "reason_summary": reason_summary})


def dispute_review_output_hash(review_input_hash: str, evidence_validated: bool,
                               material_conflict: bool, disposition: ReviewDisposition,
                               reason_summary: str) -> str:
    return _hash({"schema_version": 1, "kind": "dispute_review_output",
                  "review_input_hash": review_input_hash, "evidence_validated": evidence_validated,
                  "material_conflict": material_conflict, "disposition": disposition,
                  "reason_summary": reason_summary})


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainScore(Record):
    category: Category
    total_forecasts: int = field(metadata=COUNT)
    resolved_forecasts: int = field(metadata=COUNT)
    correct_forecasts: int = field(metadata=COUNT)
    accuracy_bp: int | None = field(metadata=BP)
    brier_score_bp: int | None = field(metadata=BP)
    calibration_score_bp: int | None = field(metadata=BP)

    def validate(self) -> None:
        _require(self.correct_forecasts <= self.resolved_forecasts <= self.total_forecasts,
                 "domain forecast counts must satisfy correct <= resolved <= total")
        _validate_metrics(self.resolved_forecasts, self.correct_forecasts, self.accuracy_bp,
                          self.brier_score_bp, self.calibration_score_bp)


def _validate_metrics(resolved: int, correct: int, accuracy: int | None,
                      brier: int | None, calibration: int | None) -> None:
    if resolved == 0:
        _require(accuracy is None and brier is None and calibration is None,
                 "unresolved forecasts have no measured accuracy, Brier or calibration scores")
    else:
        _require(accuracy == correct * 10000 // resolved,
                 "accuracy basis points must equal floor(correct * 10000 / resolved)")
        _require(brier is not None and calibration is not None,
                 "resolved forecasts require Brier and calibration scores")


@dataclass(frozen=True, slots=True, kw_only=True)
class UserReputation(Record):
    user_id: str = field(metadata=ID)
    total_forecasts: int = field(metadata=COUNT)
    resolved_forecasts: int = field(metadata=COUNT)
    correct_forecasts: int = field(metadata=COUNT)
    invalid_forecasts: int = field(metadata=COUNT)
    accuracy_bp: int | None = field(metadata=BP)
    brier_score_bp: int | None = field(metadata=BP)
    calibration_score_bp: int | None = field(metadata=BP)
    consistency_score_bp: int | None = field(metadata=BP)
    domain_scores: tuple[DomainScore, ...]
    total_disputes: int = field(metadata=COUNT)
    resolved_disputes: int = field(metadata=COUNT)
    successful_disputes: int = field(metadata=COUNT)
    dispute_accuracy_bp: int | None = field(metadata=BP)
    creator_quality_bp: int | None = field(metadata=BP)
    created_at_ms: int = field(metadata=COUNT)
    updated_at_ms: int = field(metadata=COUNT)

    def validate(self) -> None:
        _require(self.correct_forecasts <= self.resolved_forecasts
                 and self.resolved_forecasts + self.invalid_forecasts <= self.total_forecasts,
                 "resolved binary and invalid forecast counts must be disjoint within total")
        _validate_metrics(self.resolved_forecasts, self.correct_forecasts, self.accuracy_bp,
                          self.brier_score_bp, self.calibration_score_bp)
        _require(self.resolved_forecasts > 0 or self.consistency_score_bp is None,
                 "consistency score requires resolved forecasts")
        _require(len({score.category for score in self.domain_scores}) == len(self.domain_scores),
                 "domain score categories must be unique")
        for name in ("total_forecasts", "resolved_forecasts", "correct_forecasts"):
            _require(sum(getattr(score, name) for score in self.domain_scores) <= getattr(self, name),
                     "domain score counts cannot exceed overall counts")
        _require(sum(score.resolved_forecasts - score.correct_forecasts for score in self.domain_scores)
                 <= self.resolved_forecasts - self.correct_forecasts,
                 "domain incorrect counts cannot exceed overall incorrect forecasts")
        _require(sum(score.total_forecasts - score.resolved_forecasts for score in self.domain_scores)
                 <= self.total_forecasts - self.resolved_forecasts,
                 "domain unresolved counts cannot exceed overall non-binary-resolved forecasts")
        _require(self.successful_disputes <= self.resolved_disputes <= self.total_disputes,
                 "dispute counts must satisfy successful <= resolved <= total")
        if self.resolved_disputes == 0:
            _require(self.dispute_accuracy_bp is None, "unresolved disputes have no accuracy score")
        else:
            _require(self.dispute_accuracy_bp == self.successful_disputes * 10000 // self.resolved_disputes,
                     "dispute accuracy must agree with successful and resolved counts")
        _require(self.created_at_ms <= self.updated_at_ms, "reputation update cannot predate creation")


@dataclass(frozen=True, slots=True, kw_only=True)
class CreatorProfile(Record):
    creator_id: str = field(metadata=ID)
    markets_created: int = field(metadata=COUNT)
    resolved_markets: int = field(metadata=COUNT)
    invalid_markets: int = field(metadata=COUNT)
    disputed_markets: int = field(metadata=COUNT)
    total_participation: int = field(metadata=COUNT)
    follower_count: int = field(metadata=COUNT)
    creator_quality_bp: int | None = field(metadata=BP)
    created_at_ms: int = field(metadata=COUNT)
    updated_at_ms: int = field(metadata=COUNT)

    def validate(self) -> None:
        _require(self.invalid_markets <= self.resolved_markets <= self.markets_created,
                 "creator counts must satisfy invalid <= resolved <= created")
        _require(self.disputed_markets <= self.markets_created,
                 "disputed markets cannot exceed created markets")
        _require(self.markets_created > 0 or self.total_participation == 0,
                 "participation requires at least one created market")
        _require(self.resolved_markets > 0 or self.creator_quality_bp is None,
                 "creator quality requires resolved markets")
        _require(self.created_at_ms <= self.updated_at_ms, "creator update cannot predate creation")

    @property
    def invalid_rate_bp(self) -> int | None:
        return self.invalid_markets * 10000 // self.resolved_markets if self.resolved_markets else None

    @property
    def dispute_rate_bp(self) -> int | None:
        return self.disputed_markets * 10000 // self.markets_created if self.markets_created else None

    @property
    def average_participation(self) -> int | None:
        """Floor of distinct-forecast participation count divided by markets created."""
        return self.total_participation // self.markets_created if self.markets_created else None
