"""Explicit v2 upgrade for reviewed, irreversible positive announcement events.

The specification remains v1 and byte-identical. A trigger is a review artifact,
not proof that an LLM is correct: retained official bytes and independent review
are adapter responsibilities, and the ordinary challenge process still applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from .errors import ConcurrencyError, TransitionError
from .lifecycle import (
    Command,
    CommandPayload,
    CommandReceipt,
    DomainEvent,
    Forecast,
    LifecycleState,
    Lock,
    ProposeResolution,
    TransitionResult,
    _commit_transition,
    _require,
    apply_command,
)
from .models import (
    HASH,
    ID,
    AIProvenance,
    AITask,
    EvidenceSnapshot,
    ForecastSpecification,
    Outcome,
    Resolution,
    SourceVerification,
    counter_judge_input_hash,
    counter_judge_output_hash,
    resolution_input_hash,
)
from .records import MAX_SAFE_INTEGER, Record
from .serialization import content_hash, loads


def early_trigger_input_hash(forecast_id: str, specification_hash: str, clause_id: str,
                             evidence: tuple[EvidenceSnapshot, ...],
                             source_verifications: tuple[SourceVerification, ...],
                             event_at_ms: int, observed_at_ms: int,
                             event_time_basis: str = "published_instant") -> str:
    return content_hash({"schema_version": 2, "kind": "early_positive_trigger_input",
                         "forecast_id": forecast_id, "specification_hash": specification_hash,
                         "clause_id": clause_id, "evidence": evidence,
                         "source_verifications": source_verifications,
                         "event_at_ms": event_at_ms, "observed_at_ms": observed_at_ms,
                         "event_time_basis": event_time_basis})


def early_qualification_output_hash(input_hash: str, qualification: str) -> str:
    return content_hash({"schema_version": 2, "kind": "early_positive_qualification",
                         "input_hash": input_hash, "qualification": qualification,
                         "monotonic_kind": "official_announcement_by_deadline",
                         "proposed_outcome": Outcome.YES, "irreversible": True,
                         "conditions_fully_satisfied": True, "invalidation_clear": True})


@dataclass(frozen=True, slots=True, kw_only=True)
class EarlyResolutionTrigger(Record):
    schema_version: int = field(default=2, metadata={"const": 2})
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    clause_id: str = field(metadata=ID)
    evidence: tuple[EvidenceSnapshot, ...] = field(metadata={"minItems": 1, "maxItems": 16})
    source_verifications: tuple[SourceVerification, ...] = field(metadata={"minItems": 1, "maxItems": 16})
    event_at_ms: int
    observed_at_ms: int
    qualification: str
    qualifier: AIProvenance
    counter_qualifier: AIProvenance
    event_time_basis: Literal["published_instant", "observed_upper_bound"] = "published_instant"
    monotonic_kind: Literal["official_announcement_by_deadline"] = "official_announcement_by_deadline"
    proposed_outcome: Literal["YES"] = "YES"
    irreversible: Literal[True] = True
    conditions_fully_satisfied: Literal[True] = True
    invalidation_clear: Literal[True] = True

    def validate(self) -> None:
        _require(self.event_at_ms <= self.observed_at_ms, "event cannot follow observation")
        if self.event_time_basis == "observed_upper_bound":
            _require(self.event_at_ms == self.observed_at_ms,
                     "observed upper bound must equal observation time, not a fabricated event timestamp")
        hashes = {item.evidence_hash for item in self.evidence}
        _require(len(hashes) == len(self.evidence), "trigger evidence must be unique")
        _require(len({item.evidence_id for item in self.evidence}) == len(self.evidence),
                 "trigger evidence IDs must be unique")
        _require(len(self.source_verifications) == len(self.evidence)
                 and {item.evidence_hash for item in self.source_verifications} == hashes,
                 "trigger verification must cover exact evidence")
        by_hash = {item.evidence_hash: item for item in self.evidence}
        for item in self.evidence:
            _require(item.collector is not None, "trigger evidence requires collection provenance")
            _require(item.collected_at_ms <= self.observed_at_ms,
                     "trigger collection cannot follow observation")
            if self.event_time_basis == "published_instant":
                _require(self.event_at_ms <= item.collected_at_ms,
                         "precise event cannot follow collection")
        for verification in self.source_verifications:
            snapshot = by_hash[verification.evidence_hash]
            _require(verification.verified and verification.source_id == snapshot.source_id,
                     "trigger requires verified exact sources")
            _require(snapshot.collected_at_ms <= verification.verifier.created_at_ms
                     <= self.qualifier.created_at_ms, "trigger source verification order invalid")
        _require(self.qualifier.task == AITask.AMBIGUITY_JUDGE,
                 "early qualification requires semantic ambiguity review")
        _require(self.qualifier.input_hash == self.input_hash,
                 "qualification input does not bind exact trigger")
        _require(self.qualifier.output_hash == early_qualification_output_hash(
            self.input_hash, self.qualification), "qualification output commitment mismatch")
        _require(self.counter_qualifier.task == AITask.COUNTER_JUDGE,
                 "early qualification requires counter-review")
        _require(self.counter_qualifier.provider.casefold() != self.qualifier.provider.casefold(),
                 "early counter-review must use an independent provider")
        _require(self.counter_qualifier.input_hash == counter_judge_input_hash(
            self.input_hash, self.qualifier), "early counter-review input binding mismatch")
        _require(self.counter_qualifier.output_hash == counter_judge_output_hash(
            self.qualifier.output_hash, True), "early counter-review must affirm exact qualification")
        _require(self.observed_at_ms <= self.qualifier.created_at_ms
                 <= self.counter_qualifier.created_at_ms, "early review time order invalid")

    @property
    def input_hash(self) -> str:
        return early_trigger_input_hash(self.forecast_id, self.specification_hash, self.clause_id,
                                        self.evidence, self.source_verifications,
                                        self.event_at_ms, self.observed_at_ms, self.event_time_basis)

    @property
    def trigger_hash(self) -> str:
        return content_hash(self)

    @property
    def qualified_at_ms(self) -> int:
        return self.counter_qualifier.created_at_ms

    def validate_for(self, specification: ForecastSpecification) -> None:
        _require(self.specification_hash == specification.specification_hash,
                 "early trigger targets another specification")
        _require(any(rule.clause_id == self.clause_id and rule.outcome == Outcome.YES
                     for rule in specification.rules), "early trigger must identify the YES clause")
        _require(specification.open_at_ms <= self.event_at_ms <= self.observed_at_ms
                 <= self.qualified_at_ms < specification.close_at_ms,
                 "early event and completed review must occur within the original open window")
        sources = {item.source_id: item for item in specification.source_policy.primary_sources}
        _require(all(item.source_id in sources and sources[item.source_id].is_official
                     for item in self.evidence), "early trigger requires published official primary sources")
        _require(all(urlsplit(item.url).hostname == urlsplit(sources[item.source_id].url).hostname
                     for item in self.evidence), "early evidence host differs from official source")


@dataclass(frozen=True, slots=True, kw_only=True)
class EarlyResolution(Resolution):
    schema_version: int = field(default=2, metadata={"const": 2})
    trigger: EarlyResolutionTrigger

    def validate(self) -> None:
        Resolution.validate(self)
        _require(self.forecast_id == self.trigger.forecast_id
                 and self.specification_hash == self.trigger.specification_hash,
                 "early resolution trigger binding mismatch")
        _require(self.proposed_outcome == Outcome.YES
                 and self.rule_matches == (self.trigger.clause_id,),
                 "early resolution permits only the qualified YES clause")
        _require(self.evidence == self.trigger.evidence
                 and self.source_verifications == self.trigger.source_verifications,
                 "early resolution requires exact retained trigger evidence and verification")
        _require(self.judge.created_at_ms >= self.trigger.qualified_at_ms,
                 "early resolution judge cannot predate semantic review")

    @property
    def decision_input_hash(self) -> str:
        return content_hash({"schema_version": 2, "kind": "early_resolution_input",
                             "trigger_hash": self.trigger.trigger_hash,
                             "resolution_input_hash": resolution_input_hash(
                                 self.forecast_id, self.specification_hash,
                                 self.evidence, self.source_verifications)})

    def _validate_evidence_time(self, specification: ForecastSpecification) -> None:
        self.trigger.validate_for(specification)
        _require(self.proposed_at_ms >= self.trigger.qualified_at_ms,
                 "early proposal cannot precede qualification")


@dataclass(frozen=True, slots=True, kw_only=True)
class ForecastV2(Forecast):
    schema_version: int = field(default=2, metadata={"const": 2})
    early_trigger: EarlyResolutionTrigger
    upgrade_source: Forecast
    upgraded_from_state_hash: str = field(metadata=HASH)
    upgraded_from_event_hash: str = field(metadata=HASH)
    upgraded_at_revision: int = field(metadata={"minimum": 1})
    upgraded_at_ms: int
    resolution: Resolution | EarlyResolution | None = None

    def validate(self) -> None:
        self.early_trigger.validate_for(self.specification)
        source = self.upgrade_source
        _require(source.state == LifecycleState.OPEN
                 and source.forecast_id == self.forecast_id and source.creator_id == self.creator_id
                 and source.specification == self.specification
                 and source.created_at_ms == self.created_at_ms
                 and source.published_at_ms == self.published_at_ms
                 and source.validation_assessment == self.validation_assessment,
                 "upgrade must retain the exact published source identity and specification")
        _require(content_hash(source) == self.upgraded_from_state_hash
                 and source.audit_head_hash == self.upgraded_from_event_hash
                 and source.revision + 1 == self.upgraded_at_revision
                 and source.updated_at_ms <= self.upgraded_at_ms,
                 "upgrade source history commitments or time are invalid")
        _require(self.early_trigger.forecast_id == self.forecast_id,
                 "early trigger belongs to another forecast")
        _require(self.state not in {LifecycleState.DRAFT, LifecycleState.VALIDATING, LifecycleState.OPEN},
                 "early upgrade permanently closes new participation")
        _require(self.early_trigger.qualified_at_ms <= self.upgraded_at_ms
                 < self.specification.close_at_ms, "upgrade must follow review and precede expiry")
        _require(self.upgraded_at_revision <= self.revision and self.upgraded_at_ms <= self.updated_at_ms,
                 "upgrade is outside aggregate history")
        if self.revision == self.upgraded_at_revision:
            _require(self.latest_event is not None and self.latest_event.command_name == "lock"
                     and self.latest_event.old_state == LifecycleState.OPEN
                     and self.latest_event.previous_event_hash == self.upgraded_from_event_hash
                     and self.latest_event.artifact_hash == self.early_trigger.trigger_hash,
                     "upgrade requires explicit OPEN-to-LOCKED trigger audit event")
        if isinstance(self.resolution, EarlyResolution):
            _require(self.resolution.trigger == self.early_trigger,
                     "resolution cannot substitute the accepted early trigger")
        elif self.resolution is not None:
            _require(self.resolution.proposed_at_ms >= self.specification.close_at_ms,
                     "ordinary replacement resolution must wait for original expiry")
        Forecast.validate(self)

    def _resolution_not_before_ms(self) -> int:
        return self.upgraded_at_ms

    def _transition_result(self, receipt: CommandReceipt,
                           events: tuple[DomainEvent, ...]) -> TransitionResultV2:
        return TransitionResultV2(forecast=self, receipt=receipt, events=events)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionResultV2(TransitionResult):
    schema_version: int = field(default=2, metadata={"const": 2})
    forecast: ForecastV2


@dataclass(frozen=True, slots=True, kw_only=True)
class LockEarly(Lock):
    schema_version: int = field(default=2, metadata={"const": 2})
    trigger: EarlyResolutionTrigger


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeEarlyResolution(ProposeResolution):
    schema_version: int = field(default=2, metadata={"const": 2})
    resolution: EarlyResolution


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandV2(Command):
    schema_version: int = field(default=2, metadata={"const": 2})
    payload: CommandPayload | LockEarly | ProposeEarlyResolution


def apply_early_command(forecast: Forecast, command: Command, *, now_ms: int,
                        prior_receipt: CommandReceipt | None = None) -> TransitionResult:
    """Explicitly upgrade once, then preserve all ordinary challenge/CAS/retry gates."""
    if prior_receipt is not None or not isinstance(command.payload, LockEarly):
        return apply_command(forecast, command, now_ms=now_ms, prior_receipt=prior_receipt)
    _require(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER,
             "now_ms must be a portable nonnegative integer")
    if command.expected_revision != forecast.revision:
        raise ConcurrencyError("expected revision does not match current forecast revision")
    if (type(forecast) is not Forecast or forecast.state != LifecycleState.OPEN
            or not forecast.updated_at_ms <= now_ms < forecast.specification.close_at_ms
            or forecast.revision >= MAX_SAFE_INTEGER):
        raise TransitionError("early upgrade requires an unexpired v1 OPEN forecast and current time")
    trigger = command.payload.trigger
    trigger.validate_for(forecast.specification)
    _require(trigger.forecast_id == forecast.forecast_id and trigger.qualified_at_ms <= now_ms,
             "early trigger must belong to forecast and have completed review")
    _require(forecast.audit_head_hash is not None, "upgrade requires existing audit chain")
    return _commit_transition(forecast, command, now_ms=now_ms, forecast_type=ForecastV2,
                              artifact_hash=trigger.trigger_hash,
                              changes={"schema_version": 2, "state": LifecycleState.LOCKED,
                                       "early_trigger": trigger, "upgrade_source": forecast,
                                       "upgraded_from_state_hash": content_hash(forecast),
                                       "upgraded_from_event_hash": forecast.audit_head_hash,
                                       "upgraded_at_revision": forecast.revision + 1,
                                       "upgraded_at_ms": now_ms})


def loads_forecast(raw: str | bytes) -> Forecast:
    """Strictly decode a persisted v1 or v2 snapshot; never rewrite older bytes."""
    from .errors import ValidationError
    try:
        return loads(Forecast, raw)
    except ValidationError as original:
        try:
            return loads(ForecastV2, raw)
        except ValidationError as newer:
            raise ValidationError(f"Invalid v1/v2 forecast: {original}; {newer}") from newer
