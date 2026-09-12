"""Pure, immutable forecasting lifecycle with durable-receipt/CAS adapter contracts.

This module never reads a clock or performs an external effect. A persistence
adapter must atomically compare-and-swap the aggregate revision and persist its
new event and receipt. Supplying a trusted persisted receipt makes retries safe;
a pure function alone cannot serialize concurrent processes.
"""

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Literal, TypeVar

from .errors import ConcurrencyError, IdempotencyConflict, TransitionError, ValidationError
from .models import (
    HASH,
    ID,
    AIProvenance,
    AITask,
    Dispute,
    DisputeReview,
    ForecastSpecification,
    Outcome,
    Resolution,
    UserForecast,
    ValidationAssessment,
)
from .records import MAX_SAFE_INTEGER, Record
from .serialization import content_hash

MAX_ACTIVE_DISPUTES = 256
_T = TypeVar("_T")


class LifecycleState(str, Enum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    RESOLVING = "RESOLVING"
    PROPOSED = "PROPOSED"
    CHALLENGE = "CHALLENGE"
    DISPUTED = "DISPUTED"
    ESCALATED = "ESCALATED"
    PAUSED = "PAUSED"
    FINALIZED = "FINALIZED"
    ARCHIVED = "ARCHIVED"


class DomainEffect(str, Enum):
    REPUTATION_UPDATE_REQUIRED = "REPUTATION_UPDATE_REQUIRED"
    RESULT_NOTIFICATION_REQUIRED = "RESULT_NOTIFICATION_REQUIRED"
    RESOLUTION_COMMITMENT_REQUIRED = "RESOLUTION_COMMITMENT_REQUIRED"


_PAUSABLE = frozenset({LifecycleState.RESOLVING, LifecycleState.PROPOSED,
                      LifecycleState.CHALLENGE, LifecycleState.DISPUTED,
                      LifecycleState.ESCALATED})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _present(value: _T | None, message: str) -> _T:
    if value is None:
        raise ValidationError(message)
    return value


def _guard(condition: bool, message: str) -> None:
    if not condition:
        raise TransitionError(message)


def _digest(value: str, name: str) -> None:
    _require(len(value) == 64 and all(c in "0123456789abcdef" for c in value),
             f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class Pause(Record):
    previous_state: LifecycleState
    paused_at_ms: int
    configured_providers: tuple[str, ...] = field(metadata={"minItems": 1, "maxItems": 32, "uniqueItems": True})
    unavailable_providers: tuple[str, ...] = field(metadata={"minItems": 1, "maxItems": 32, "uniqueItems": True})
    reason: str

    def validate(self) -> None:
        _require(self.previous_state in _PAUSABLE, "state cannot pause for provider failure")
        _require(bool(self.configured_providers), "configured providers must not be empty")
        _require(len(set(self.configured_providers)) == len(self.configured_providers),
                 "configured providers must be unique")
        _require(len(set(self.unavailable_providers)) == len(self.unavailable_providers),
                 "unavailable providers must be unique")
        _require(set(self.configured_providers) == set(self.unavailable_providers),
                 "provider outage requires every configured provider to be unavailable")


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent(Record):
    forecast_id: str = field(metadata=ID)
    command_id: str = field(metadata=ID)
    command_name: str
    old_state: LifecycleState
    new_state: LifecycleState
    revision: int
    occurred_at_ms: int
    specification_hash: str = field(metadata=HASH)
    state_hash: str = field(metadata=HASH)
    artifact_hash: str | None
    previous_event_hash: str | None
    effects: tuple[DomainEffect, ...] = ()

    def validate(self) -> None:
        _require(self.revision > 0, "event revision must be positive")
        for name in ("specification_hash", "state_hash"):
            _digest(getattr(self, name), name)
        for name in ("artifact_hash", "previous_event_hash"):
            value = getattr(self, name)
            if value is not None:
                _digest(value, name)
        _require(self.command_name in _ALLOWED_STATES, "unknown event command")
        _require(self.old_state in _ALLOWED_STATES[self.command_name],
                 "event command is illegal from old state")
        _require(self.new_state in _TARGET_STATES[self.command_name],
                 "event command cannot reach new state")
        _require(len(set(self.effects)) == len(self.effects), "event effects must be unique")
        expected = tuple(DomainEffect) if self.command_name == "finalize" else ()
        _require(self.effects == expected, "event effects must agree with command")


@dataclass(frozen=True, slots=True, kw_only=True)
class Forecast(Record):
    forecast_id: str = field(metadata=ID)
    creator_id: str = field(metadata=ID)
    specification: ForecastSpecification
    specification_hash: str = field(metadata=HASH)
    created_at_ms: int
    updated_at_ms: int
    state: LifecycleState = LifecycleState.DRAFT
    revision: int = 0
    published_at_ms: int | None = None
    validation_assessment: ValidationAssessment | None = None
    resolution: Resolution | None = None
    challenge_started_at_ms: int | None = None
    challenge_until_ms: int | None = None
    disputes: tuple[Dispute, ...] = field(default=(), metadata={"maxItems": MAX_ACTIVE_DISPUTES})
    dispute_reviews: tuple[DisputeReview, ...] = field(default=(), metadata={"maxItems": MAX_ACTIVE_DISPUTES})
    pause: Pause | None = None
    finalized_outcome: Outcome | None = None
    finalized_resolution_hash: str | None = None
    latest_event: DomainEvent | None = None
    audit_head_hash: str | None = None

    def validate(self) -> None:
        _require(self.specification_hash == self.specification.specification_hash,
                 "specification hash does not identify specification")
        _require(self.created_at_ms <= self.updated_at_ms, "aggregate time runs backwards")
        _require((self.state == LifecycleState.PAUSED) == (self.pause is not None),
                 "PAUSED state and pause context must agree")
        effective_state = self.pause.previous_state if self.pause else self.state
        if self.pause:
            _require(self.created_at_ms <= self.pause.paused_at_ms <= self.updated_at_ms,
                     "pause time is outside aggregate history")
        unpublished = effective_state in {LifecycleState.DRAFT, LifecycleState.VALIDATING}
        _require(unpublished == (self.published_at_ms is None),
                 "published timestamp must agree with lifecycle state")
        if self.published_at_ms is not None:
            _require(self.created_at_ms <= self.published_at_ms <= self.updated_at_ms,
                     "publication time is outside aggregate history")
            _require(self.published_at_ms < self.specification.close_at_ms,
                     "forecast cannot be published after expiry")
            _require(self.validation_assessment is not None, "published forecast requires validation")
            assessment = _present(self.validation_assessment, "publication requires validation")
            assessment.require_publishable(self.specification)
            _require(assessment.validated_at_ms <= self.published_at_ms,
                     "publication cannot predate completed validation")
        elif self.validation_assessment is not None:
            _require(self.validation_assessment.specification_hash == self.specification_hash,
                     "validation is bound to a different specification")
        if self.validation_assessment is not None:
            _require(self.validation_assessment.validated_at_ms <= self.updated_at_ms,
                     "validation assessment is in the future")
        if effective_state == LifecycleState.VALIDATING:
            _require(self.validation_assessment is None, "pending validation cannot include a completed assessment")
        pre_resolution = effective_state in {
            LifecycleState.DRAFT, LifecycleState.VALIDATING, LifecycleState.OPEN,
            LifecycleState.LOCKED, LifecycleState.RESOLVING,
        }
        _require(pre_resolution == (self.resolution is None),
                 "resolution presence must agree with lifecycle state")
        if effective_state not in {LifecycleState.DRAFT, LifecycleState.VALIDATING,
                                   LifecycleState.OPEN}:
            _require(self.updated_at_ms >= self._resolution_not_before_ms(),
                     "post-open state requires expiry")
        if self.resolution:
            _require(self.resolution.forecast_id == self.forecast_id,
                     "resolution belongs to another forecast")
            self.resolution.require_proposable(self.specification)
            _require(self._resolution_not_before_ms() <= self.resolution.proposed_at_ms <= self.updated_at_ms,
                     "resolution proposal must be after expiry and within history")
        challenged = effective_state in {LifecycleState.CHALLENGE, LifecycleState.DISPUTED,
                                         LifecycleState.ESCALATED, LifecycleState.FINALIZED,
                                         LifecycleState.ARCHIVED}
        _require(challenged == (self.challenge_started_at_ms is not None),
                 "challenge start must agree with lifecycle state")
        _require(challenged == (self.challenge_until_ms is not None),
                 "challenge deadline must agree with lifecycle state")
        if challenged:
            proposal = _present(self.resolution, "challenge requires resolution")
            start = _present(self.challenge_started_at_ms, "challenge requires start")
            deadline = _present(self.challenge_until_ms, "challenge requires deadline")
            _require(proposal.proposed_at_ms <= start <= self.updated_at_ms,
                     "challenge cannot precede resolution")
            _require(start < deadline,
                     "challenge duration must be positive")
        _require(len(self.disputes) <= MAX_ACTIVE_DISPUTES, "too many active disputes")
        _require(len(self.dispute_reviews) <= MAX_ACTIVE_DISPUTES, "too many active reviews")
        _require(len({d.dispute_id for d in self.disputes}) == len(self.disputes),
                 "duplicate dispute IDs")
        _require(len({d.dispute_hash for d in self.disputes}) == len(self.disputes),
                 "duplicate dispute commitments")
        _require(len({r.dispute_hash for r in self.dispute_reviews}) == len(self.dispute_reviews),
                 "duplicate reviews")
        if self.disputes or self.dispute_reviews:
            _require(challenged, "disputes require challenge history")
        disputes_by_hash = {d.dispute_hash: d for d in self.disputes}
        for dispute in self.disputes:
            _require(dispute.forecast_id == self.forecast_id, "dispute belongs to another forecast")
            dispute.validate_for(self.specification, _present(self.resolution, "dispute requires resolution"))
            _require(_present(self.challenge_started_at_ms, "dispute requires challenge start")
                     <= dispute.submitted_at_ms
                     < _present(self.challenge_until_ms, "dispute requires challenge deadline"),
                     "dispute was submitted outside challenge window")
            _require(dispute.submitted_at_ms <= self.updated_at_ms, "dispute is in the future")
        for review in self.dispute_reviews:
            _require(review.dispute_hash in disputes_by_hash, "review has no corresponding dispute")
            dispute = disputes_by_hash[review.dispute_hash]
            review.require_valid_for(dispute, _present(self.resolution, "review requires resolution"), self.specification)
            _require(dispute.submitted_at_ms <= review.reviewed_at_ms <= self.updated_at_ms,
                     "review time is outside dispute history")
        if effective_state == LifecycleState.DISPUTED:
            _require(bool(self.disputes), "DISPUTED requires a dispute")
        if effective_state == LifecycleState.ESCALATED:
            _require(len(self.disputes) == len(self.dispute_reviews),
                     "ESCALATED requires completed dispute reviews")
            _require(any(r.material_conflict for r in self.dispute_reviews),
                     "ESCALATED requires material conflict")
        if effective_state in {LifecycleState.CHALLENGE, LifecycleState.FINALIZED,
                                LifecycleState.ARCHIVED}:
            _require(len(self.disputes) == len(self.dispute_reviews),
                     "pending disputes block challenge restoration and finalization")
            _require(not any(r.material_conflict for r in self.dispute_reviews),
                     "material conflict blocks retained proposal")
        terminal = effective_state in {LifecycleState.FINALIZED, LifecycleState.ARCHIVED}
        _require(terminal == (self.finalized_outcome is not None), "final outcome must agree with state")
        _require(terminal == (self.finalized_resolution_hash is not None),
                 "final resolution commitment must agree with state")
        if terminal:
            proposal = _present(self.resolution, "finalization requires resolution")
            _require(self.finalized_outcome == proposal.proposed_outcome,
                     "final outcome differs from resolution")
            _require(self.finalized_resolution_hash == proposal.resolution_hash,
                     "final resolution commitment differs from proposal")
            _require(self.updated_at_ms >= _present(self.challenge_until_ms, "finalization requires deadline"),
                     "finalization requires completed challenge window")
        if self.revision == 0:
            _require(self.state == LifecycleState.DRAFT and self.updated_at_ms == self.created_at_ms,
                     "initial snapshot must be a newly created draft")
            _require(self.latest_event is None and self.audit_head_hash is None,
                     "initial snapshot cannot have an audit history")
            _require(self.validation_assessment is None, "initial draft cannot contain validation")
        else:
            _require(self.latest_event is not None and self.audit_head_hash is not None,
                     "mutated snapshot requires audit head")
            event = _present(self.latest_event, "mutated snapshot requires event")
            _require(self.audit_head_hash == content_hash(event), "audit head hash is invalid")
            _require(event.forecast_id == self.forecast_id and event.new_state == self.state,
                     "audit event does not identify aggregate state")
            _require(event.revision == self.revision and event.occurred_at_ms == self.updated_at_ms,
                     "audit event revision/time differs from aggregate")
            _require(event.specification_hash == self.specification_hash,
                     "audit event identifies another specification")
            _require(event.state_hash == content_hash(_snapshot_values(self)),
                     "audit state commitment does not identify snapshot")
            _require((self.revision == 1) == (event.previous_event_hash is None),
                     "audit predecessor must agree with revision")


    def _resolution_not_before_ms(self) -> int:
        return self.specification.close_at_ms

    def _transition_result(self, receipt: "CommandReceipt",
                           events: tuple[DomainEvent, ...]) -> "TransitionResult":
        return TransitionResult(forecast=self, receipt=receipt, events=events)


@dataclass(frozen=True, slots=True, kw_only=True)
class EditSpecification(Record):
    specification: ForecastSpecification
    kind: Literal["edit_specification"] = "edit_specification"


@dataclass(frozen=True, slots=True, kw_only=True)
class BeginValidation(Record):
    kind: Literal["begin_validation"] = "begin_validation"


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectValidation(Record):
    assessment: ValidationAssessment
    kind: Literal["reject_validation"] = "reject_validation"


@dataclass(frozen=True, slots=True, kw_only=True)
class Publish(Record):
    assessment: ValidationAssessment
    kind: Literal["publish"] = "publish"


@dataclass(frozen=True, slots=True, kw_only=True)
class Lock(Record):
    kind: Literal["lock"] = "lock"


@dataclass(frozen=True, slots=True, kw_only=True)
class BeginResolution(Record):
    kind: Literal["begin_resolution"] = "begin_resolution"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeResolution(Record):
    resolution: Resolution
    kind: Literal["propose_resolution"] = "propose_resolution"


@dataclass(frozen=True, slots=True, kw_only=True)
class BeginChallenge(Record):
    duration_ms: int = field(metadata={"minimum": 1})
    kind: Literal["begin_challenge"] = "begin_challenge"

    def validate(self) -> None:
        _require(self.duration_ms > 0, "challenge duration must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitForecast(Record):
    user_forecast: UserForecast
    kind: Literal["submit_forecast"] = "submit_forecast"


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitDispute(Record):
    dispute: Dispute
    kind: Literal["submit_dispute"] = "submit_dispute"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewDispute(Record):
    review: DisputeReview
    kind: Literal["review_dispute"] = "review_dispute"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetainProposal(Record):
    kind: Literal["retain_proposal"] = "retain_proposal"


@dataclass(frozen=True, slots=True, kw_only=True)
class Escalate(Record):
    kind: Literal["escalate"] = "escalate"


@dataclass(frozen=True, slots=True, kw_only=True)
class AdjudicateResolution(Record):
    resolution: Resolution
    adjudicator: AIProvenance
    kind: Literal["adjudicate_resolution"] = "adjudicate_resolution"


@dataclass(frozen=True, slots=True, kw_only=True)
class Finalize(Record):
    kind: Literal["finalize"] = "finalize"


@dataclass(frozen=True, slots=True, kw_only=True)
class Archive(Record):
    kind: Literal["archive"] = "archive"


@dataclass(frozen=True, slots=True, kw_only=True)
class PauseForProviderOutage(Record):
    configured_providers: tuple[str, ...] = field(metadata={"minItems": 1, "maxItems": 32, "uniqueItems": True})
    unavailable_providers: tuple[str, ...] = field(metadata={"minItems": 1, "maxItems": 32, "uniqueItems": True})
    reason: str
    kind: Literal["pause_for_provider_outage"] = "pause_for_provider_outage"

    def validate(self) -> None:
        Pause(previous_state=LifecycleState.RESOLVING, paused_at_ms=0,
              configured_providers=self.configured_providers,
              unavailable_providers=self.unavailable_providers, reason=self.reason)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResumeAfterProviderRecovery(Record):
    recovered_provider: str = field(metadata=ID)
    kind: Literal["resume_after_provider_recovery"] = "resume_after_provider_recovery"


CommandPayload = (EditSpecification | BeginValidation | RejectValidation | Publish | Lock
                  | BeginResolution | ProposeResolution | BeginChallenge | SubmitForecast
                  | SubmitDispute | ReviewDispute | RetainProposal | Escalate
                  | AdjudicateResolution | Finalize | Archive | PauseForProviderOutage
                  | ResumeAfterProviderRecovery)


@dataclass(frozen=True, slots=True, kw_only=True)
class Command(Record):
    idempotency_key: str = field(metadata=ID)
    expected_revision: int
    payload: CommandPayload


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandReceipt(Record):
    forecast_id: str = field(metadata=ID)
    idempotency_key: str = field(metadata=ID)
    command_hash: str = field(metadata=HASH)
    revision: int
    event_hash: str = field(metadata=HASH)
    accepted_at_ms: int
    accepted_user_forecast: UserForecast | None = None

    def validate(self) -> None:
        _require(self.revision > 0, "receipt revision must be positive")
        _digest(self.command_hash, "command_hash")
        _digest(self.event_hash, "event_hash")
        if self.accepted_user_forecast is not None:
            _require(self.accepted_user_forecast.forecast_id == self.forecast_id,
                     "receipt forecast submission belongs to another forecast")
            _require(self.accepted_user_forecast.submitted_at_ms == self.accepted_at_ms,
                     "accepted submission timestamp must match receipt")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionResult(Record):
    forecast: Forecast
    receipt: CommandReceipt
    events: tuple[DomainEvent, ...] = field(metadata={"maxItems": 1})

    def validate(self) -> None:
        _require(self.forecast.forecast_id == self.receipt.forecast_id,
                 "result receipt belongs to another forecast")
        _require(self.receipt.revision <= self.forecast.revision, "receipt is ahead of aggregate")
        _require(self.receipt.accepted_at_ms <= self.forecast.updated_at_ms,
                 "receipt time is ahead of aggregate")
        _require(len(self.events) <= 1, "one accepted command emits one audit event")
        if self.receipt.accepted_user_forecast is not None:
            _require(self.receipt.accepted_user_forecast.specification_hash == self.forecast.specification_hash,
                     "accepted submission must identify the immutable published specification")
        if self.events:
            _require(self.events[0] == self.forecast.latest_event, "result event differs from aggregate")
            _require(self.receipt.event_hash == self.forecast.audit_head_hash,
                     "result receipt differs from event commitment")
            _require(self.receipt.revision == self.forecast.revision,
                     "new event receipt must match current revision")
            _require(self.receipt.accepted_at_ms == self.events[0].occurred_at_ms,
                     "receipt acceptance time must match event")
            _require(self.receipt.idempotency_key == self.events[0].command_id,
                     "receipt command key must match event")


_ALLOWED_STATES = {
    "edit_specification": frozenset({LifecycleState.DRAFT}),
    "begin_validation": frozenset({LifecycleState.DRAFT}),
    "reject_validation": frozenset({LifecycleState.VALIDATING}),
    "publish": frozenset({LifecycleState.VALIDATING}),
    "lock": frozenset({LifecycleState.OPEN}),
    "begin_resolution": frozenset({LifecycleState.LOCKED}),
    "propose_resolution": frozenset({LifecycleState.RESOLVING}),
    "begin_challenge": frozenset({LifecycleState.PROPOSED}),
    "submit_forecast": frozenset({LifecycleState.OPEN}),
    "submit_dispute": frozenset({LifecycleState.CHALLENGE, LifecycleState.DISPUTED}),
    "review_dispute": frozenset({LifecycleState.DISPUTED}),
    "retain_proposal": frozenset({LifecycleState.DISPUTED}),
    "escalate": frozenset({LifecycleState.DISPUTED}),
    "adjudicate_resolution": frozenset({LifecycleState.ESCALATED}),
    "finalize": frozenset({LifecycleState.CHALLENGE}),
    "archive": frozenset({LifecycleState.FINALIZED}),
    "pause_for_provider_outage": _PAUSABLE,
    "resume_after_provider_recovery": frozenset({LifecycleState.PAUSED}),
}
_TARGET_STATES = {
    "edit_specification": frozenset({LifecycleState.DRAFT}),
    "begin_validation": frozenset({LifecycleState.VALIDATING}),
    "reject_validation": frozenset({LifecycleState.DRAFT}),
    "publish": frozenset({LifecycleState.OPEN}),
    "lock": frozenset({LifecycleState.LOCKED}),
    "begin_resolution": frozenset({LifecycleState.RESOLVING}),
    "propose_resolution": frozenset({LifecycleState.PROPOSED}),
    "begin_challenge": frozenset({LifecycleState.CHALLENGE}),
    "submit_forecast": frozenset({LifecycleState.OPEN}),
    "submit_dispute": frozenset({LifecycleState.DISPUTED}),
    "review_dispute": frozenset({LifecycleState.DISPUTED}),
    "retain_proposal": frozenset({LifecycleState.CHALLENGE}),
    "escalate": frozenset({LifecycleState.ESCALATED}),
    "adjudicate_resolution": frozenset({LifecycleState.PROPOSED}),
    "finalize": frozenset({LifecycleState.FINALIZED}),
    "archive": frozenset({LifecycleState.ARCHIVED}),
    "pause_for_provider_outage": frozenset({LifecycleState.PAUSED}),
    "resume_after_provider_recovery": _PAUSABLE,
}


def _snapshot_values(forecast: Forecast) -> dict[str, Any]:
    return {field.name: getattr(forecast, field.name) for field in fields(forecast)
            if field.name not in {"latest_event", "audit_head_hash"}}


def create_forecast(*, forecast_id: str, creator_id: str,
                    specification: ForecastSpecification, now_ms: int) -> Forecast:
    """Construct a valid initial snapshot without performing an external effect."""
    return Forecast(forecast_id=forecast_id, creator_id=creator_id,
                    specification=specification, specification_hash=specification.specification_hash,
                    created_at_ms=now_ms, updated_at_ms=now_ms)


def adjudication_input_hash(forecast: Forecast, replacement: Resolution) -> str:
    """Canonical adjudication input binding for a trusted independent AI decision."""
    proposal = _present(forecast.resolution, "adjudication requires an existing resolution")
    return content_hash({"schema_version": 1,
                         "previous_resolution_hash": proposal.resolution_hash,
                         "dispute_review_hashes": tuple(content_hash(r) for r in forecast.dispute_reviews),
                         "replacement_resolution_hash": replacement.resolution_hash})


def apply_command(forecast: Forecast, command: Command, *, now_ms: int,
                  prior_receipt: CommandReceipt | None = None) -> TransitionResult:
    """Decide one command. Persist aggregate, events and receipt atomically using CAS.

    ``prior_receipt`` must come from trusted persistence scoped by forecast and
    idempotency key. An exact retry returns this *current* aggregate unchanged,
    the original receipt, and no new events, even after subsequent mutations.
    """
    _require(type(now_ms) is int and 0 <= now_ms <= MAX_SAFE_INTEGER,
             "now_ms must be a portable nonnegative integer")
    command_hash = content_hash(command)
    if prior_receipt is not None:
        if (prior_receipt.forecast_id != forecast.forecast_id
                or prior_receipt.idempotency_key != command.idempotency_key
                or prior_receipt.command_hash != command_hash):
            raise IdempotencyConflict("idempotency key was previously used for a different command")
        _require(prior_receipt.revision <= forecast.revision, "receipt is ahead of current aggregate")
        _require(prior_receipt.revision == command.expected_revision + 1,
                 "receipt revision does not follow the accepted command revision")
        expected_submission = (command.payload.user_forecast
                               if isinstance(command.payload, SubmitForecast) else None)
        _require(prior_receipt.accepted_user_forecast == expected_submission,
                 "receipt accepted submission does not match the original command payload")
        return forecast._transition_result(prior_receipt, ())
    if command.expected_revision != forecast.revision:
        raise ConcurrencyError("expected revision does not match current forecast revision")
    _guard(now_ms >= forecast.updated_at_ms, "command cannot backdate aggregate history")
    _guard(forecast.revision < MAX_SAFE_INTEGER, "revision overflow")
    payload = command.payload
    _guard(forecast.state in _ALLOWED_STATES[payload.kind],
           f"{payload.kind} is not permitted from {forecast.state.value}")
    changes: dict[str, Any] = {}
    artifact_hash = None
    accepted_submission = None
    effects: tuple[DomainEffect, ...] = ()

    if isinstance(payload, EditSpecification):
        changes.update(specification=payload.specification,
                       specification_hash=payload.specification.specification_hash,
                       validation_assessment=None)
        artifact_hash = payload.specification.specification_hash
    elif isinstance(payload, BeginValidation):
        changes.update(state=LifecycleState.VALIDATING, validation_assessment=None)
    elif isinstance(payload, RejectValidation):
        _guard(payload.assessment.validated_at_ms <= now_ms, "validation result is in the future")
        _guard(payload.assessment.specification_hash == forecast.specification_hash,
               "validation assessment targets a different specification")
        try:
            payload.assessment.require_publishable(forecast.specification)
        except ValidationError:
            pass
        else:
            raise TransitionError("publishable validation cannot be recorded as rejected")
        changes.update(state=LifecycleState.DRAFT, validation_assessment=payload.assessment)
        artifact_hash = content_hash(payload.assessment)
    elif isinstance(payload, Publish):
        _guard(now_ms < forecast.specification.close_at_ms, "cannot publish an expired forecast")
        payload.assessment.require_publishable(forecast.specification)
        changes.update(state=LifecycleState.OPEN, published_at_ms=now_ms,
                       validation_assessment=payload.assessment)
        artifact_hash = content_hash(payload.assessment)
    elif isinstance(payload, Lock):
        _guard(now_ms >= forecast.specification.close_at_ms, "cannot lock before expiry")
        changes["state"] = LifecycleState.LOCKED
    elif isinstance(payload, BeginResolution):
        _guard(now_ms >= forecast._resolution_not_before_ms(), "cannot resolve before expiry")
        changes["state"] = LifecycleState.RESOLVING
    elif isinstance(payload, ProposeResolution):
        _guard(payload.resolution.proposed_at_ms == now_ms, "proposal time must match command time")
        changes.update(state=LifecycleState.PROPOSED, resolution=payload.resolution)
        artifact_hash = payload.resolution.resolution_hash
    elif isinstance(payload, BeginChallenge):
        deadline = now_ms + payload.duration_ms
        _guard(deadline <= MAX_SAFE_INTEGER, "challenge deadline overflow")
        changes.update(state=LifecycleState.CHALLENGE, challenge_started_at_ms=now_ms,
                       challenge_until_ms=deadline)
    elif isinstance(payload, SubmitForecast):
        submission = payload.user_forecast
        _guard(forecast.specification.open_at_ms <= now_ms < forecast.specification.close_at_ms,
               "forecast submissions require the open time window")
        _guard(submission.forecast_id == forecast.forecast_id
               and submission.specification_hash == forecast.specification_hash,
               "submission targets a different forecast or specification")
        _guard(submission.submitted_at_ms == now_ms, "submission time must match command time")
        accepted_submission = submission
        artifact_hash = content_hash(submission)
    elif isinstance(payload, SubmitDispute):
        dispute = payload.dispute
        _guard(now_ms < _present(forecast.challenge_until_ms, "dispute requires deadline"), "dispute window has closed")
        _guard(dispute.submitted_at_ms == now_ms, "dispute time must match command time")
        _guard(len(forecast.disputes) < MAX_ACTIVE_DISPUTES, "active dispute capacity reached")
        _guard(all(d.dispute_id != dispute.dispute_id for d in forecast.disputes),
               "dispute ID already exists")
        changes.update(state=LifecycleState.DISPUTED, disputes=forecast.disputes + (dispute,))
        artifact_hash = dispute.dispute_hash
    elif isinstance(payload, ReviewDispute):
        review = payload.review
        _guard(review.reviewed_at_ms == now_ms, "review time must match command time")
        _guard(all(r.dispute_hash != review.dispute_hash for r in forecast.dispute_reviews),
               "dispute has already been reviewed")
        changes["dispute_reviews"] = forecast.dispute_reviews + (review,)
        artifact_hash = content_hash(review)
    elif isinstance(payload, RetainProposal):
        _guard(len(forecast.disputes) == len(forecast.dispute_reviews),
               "every dispute requires a completed review")
        _guard(not any(r.material_conflict for r in forecast.dispute_reviews),
               "material conflict requires escalation")
        changes["state"] = LifecycleState.CHALLENGE
        artifact_hash = _present(forecast.resolution, "command requires resolution").resolution_hash
    elif isinstance(payload, Escalate):
        _guard(len(forecast.disputes) == len(forecast.dispute_reviews),
               "complete all pending dispute reviews before escalation")
        _guard(any(r.material_conflict for r in forecast.dispute_reviews),
               "escalation requires a material reviewed conflict")
        changes["state"] = LifecycleState.ESCALATED
        artifact_hash = _present(forecast.resolution, "command requires resolution").resolution_hash
    elif isinstance(payload, AdjudicateResolution):
        original_resolution = _present(forecast.resolution, "adjudication requires resolution")
        _guard(len(forecast.disputes) == len(forecast.dispute_reviews),
               "adjudication requires all pending dispute reviews")
        _guard(payload.resolution.proposed_at_ms == now_ms, "adjudicated proposal time must match command")
        _guard(payload.adjudicator.task == AITask.ADJUDICATION, "independent adjudication task is required")
        _guard(payload.adjudicator.provider.casefold() not in {
            original_resolution.judge.provider.casefold(), original_resolution.counter_judge.provider.casefold()},
            "adjudication must use a provider independent from original judges")
        _guard(payload.adjudicator.input_hash == adjudication_input_hash(forecast, payload.resolution),
               "adjudication input does not bind proposal and dispute reviews")
        _guard(payload.adjudicator.output_hash == payload.resolution.resolution_hash,
               "adjudication output does not identify replacement resolution")
        _guard(payload.adjudicator.created_at_ms == now_ms, "adjudication time must match command")
        changes.update(state=LifecycleState.PROPOSED, resolution=payload.resolution,
                       challenge_started_at_ms=None, challenge_until_ms=None,
                       disputes=(), dispute_reviews=())
        artifact_hash = content_hash(payload)
    elif isinstance(payload, Finalize):
        original_resolution = _present(forecast.resolution, "finalization requires resolution")
        _guard(now_ms >= _present(forecast.challenge_until_ms, "finalization requires deadline"), "challenge window has not ended")
        _guard(len(forecast.disputes) == len(forecast.dispute_reviews), "pending disputes block finalization")
        _guard(not any(r.material_conflict for r in forecast.dispute_reviews),
               "material disputes block finalization")
        changes.update(state=LifecycleState.FINALIZED,
                       finalized_outcome=original_resolution.proposed_outcome,
                       finalized_resolution_hash=original_resolution.resolution_hash)
        artifact_hash = _present(forecast.resolution, "command requires resolution").resolution_hash
        effects = tuple(DomainEffect)
    elif isinstance(payload, Archive):
        changes["state"] = LifecycleState.ARCHIVED
        artifact_hash = forecast.finalized_resolution_hash
    elif isinstance(payload, PauseForProviderOutage):
        pause = Pause(previous_state=forecast.state, paused_at_ms=now_ms,
                      configured_providers=payload.configured_providers,
                      unavailable_providers=payload.unavailable_providers, reason=payload.reason)
        changes.update(state=LifecycleState.PAUSED, pause=pause)
        artifact_hash = content_hash(pause)
    elif isinstance(payload, ResumeAfterProviderRecovery):
        pause = _present(forecast.pause, "recovery requires pause context")
        _guard(payload.recovered_provider in pause.configured_providers,
               "recovered provider is not part of configured routing policy")
        changes.update(state=pause.previous_state, pause=None)
        if forecast.challenge_until_ms is not None:
            deadline = forecast.challenge_until_ms + now_ms - pause.paused_at_ms
            _guard(deadline <= MAX_SAFE_INTEGER, "resumed challenge deadline overflow")
            changes["challenge_until_ms"] = deadline
        artifact_hash = content_hash(payload)
    else:
        raise TransitionError("unsupported command payload")

    return _commit_transition(forecast, command, now_ms=now_ms, changes=changes,
                              artifact_hash=artifact_hash, effects=effects,
                              accepted_submission=accepted_submission)


def _commit_transition(forecast: Forecast, command: Command, *, now_ms: int,
                       changes: dict[str, Any], artifact_hash: str | None,
                       effects: tuple[DomainEffect, ...] = (),
                       accepted_submission: UserForecast | None = None,
                       forecast_type: type[Forecast] | None = None) -> TransitionResult:
    command_hash = content_hash(command)
    values = _snapshot_values(forecast)
    values.update(changes, updated_at_ms=now_ms, revision=forecast.revision + 1)
    event = DomainEvent(forecast_id=forecast.forecast_id, command_id=command.idempotency_key,
                        command_name=command.payload.kind, old_state=forecast.state,
                        new_state=values["state"], revision=values["revision"], occurred_at_ms=now_ms,
                        specification_hash=values["specification_hash"], state_hash=content_hash(values),
                        artifact_hash=artifact_hash, previous_event_hash=forecast.audit_head_hash,
                        effects=effects)
    event_hash = content_hash(event)
    updated = (forecast_type or type(forecast))(**values, latest_event=event, audit_head_hash=event_hash)
    receipt = CommandReceipt(forecast_id=forecast.forecast_id, idempotency_key=command.idempotency_key,
                             command_hash=command_hash, revision=updated.revision, event_hash=event_hash,
                             accepted_at_ms=now_ms, accepted_user_forecast=accepted_submission)
    return updated._transition_result(receipt, (event,))
