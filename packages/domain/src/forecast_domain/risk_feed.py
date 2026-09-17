"""Purpose-separated, immutable probability feed shared with the risk consumer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .errors import ValidationError
from .models import Category
from .records import Record
from .serialization import canonical_bytes

HASH = {"pattern": "^[0-9a-f]{64}$"}
ID = {"pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}
BP = {"minimum": 0, "maximum": 10000}
Channel = Literal[
    "depegRisk1d",
    "depegRisk7d",
    "depegRisk30d",
    "reserveLossRisk",
    "liquidityStressRisk",
    "stableCollateralRisk",
    "btcCrashRisk",
    "ethCrashRisk",
    "solCrashRisk",
    "oracleFailureRisk",
    "bridgeFailureRisk",
    "counterpartyRisk",
]
Source = Literal["ai", "crowd", "top", "market"]
Status = Literal["new", "provisional", "established"]
SIGNATURE_PREFIX = b"forecast-risk-feed-v1:signature:"
SIGNATURE_PREFIX_V2 = b"forecast-risk-feed-v2:signature:"
FEED_TTL_MS = 120000
CHANNEL_HORIZON_MS = {"depegRisk1d": 86_400_000, "depegRisk7d": 604_800_000, "depegRisk30d": 2_592_000_000}
MappingKind = Literal["exact_dated", "containing_upper_estimate"]
CoverageStatus = Literal["covered", "unavailable", "saturated", "unsupported"]


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValidationError(message)


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedBinding(Record):
    binding_id: str = field(metadata=ID)
    version: Literal["canonical-risk-binding-v1"] = "canonical-risk-binding-v1"
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    channel: Channel
    horizon_hours: int = field(metadata={"minimum": 1, "maximum": 8760})
    asset: str = field(metadata={"pattern": "^[A-Z][A-Z0-9]{0,15}$"})
    category: Category
    valid_from_ms: int
    valid_until_ms: int

    def validate(self) -> None:
        require(self.valid_from_ms < self.valid_until_ms, "binding validity is empty")
        expected = {"depegRisk1d": 24, "depegRisk7d": 168, "depegRisk30d": 720}.get(self.channel)
        require(expected is None or self.horizon_hours == expected, "channel horizon mismatch")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedSignal(Record):
    binding_id: str = field(metadata=ID)
    source: Source
    probability_bp: int = field(metadata=BP)
    confidence_bp: int = field(metadata=BP)
    sample_count: int = field(metadata={"minimum": 1, "maximum": 1000000})
    units: Literal["basis-points"] = "basis-points"
    observed_at_ms: int
    evidence_hash: str = field(metadata=HASH)
    dependence_group: str = field(metadata=HASH)
    calibration_status: Status = "provisional"


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedPayload(Record):
    purpose: Literal["forecast-risk-feed-v1"] = "forecast-risk-feed-v1"
    genesis_hash: str = field(metadata={"pattern": "^[1-9A-HJ-NP-Za-km-z]{32,44}$"})
    feed_id: str = field(metadata=ID)
    sequence: int = field(metadata={"minimum": 1})
    key_id: str = field(metadata=ID)
    issued_at_ms: int
    expires_at_ms: int
    bindings: tuple[RiskFeedBinding, ...] = field(metadata={"minItems": 1, "maxItems": 12})
    signals: tuple[RiskFeedSignal, ...] = field(metadata={"minItems": 1, "maxItems": 48})
    weight_set_hash: str = field(metadata=HASH)
    weight_set_version: str = field(metadata=ID)

    def validate(self) -> None:
        require(
            0 < self.expires_at_ms - self.issued_at_ms <= 120000,
            "feed lifetime exceeds 120 seconds",
        )
        bound = {b.binding_id: b for b in self.bindings}
        require(len(bound) == len(self.bindings), "duplicate binding")
        require(
            len({b.channel for b in self.bindings}) == len(self.bindings),
            "duplicate canonical channel",
        )
        require(
            tuple(sorted(bound)) == tuple(b.binding_id for b in self.bindings),
            "bindings must be sorted",
        )
        identities = [(s.binding_id, s.source) for s in self.signals]
        require(
            len(set(identities)) == len(identities) and identities == sorted(identities),
            "signals must be unique and sorted",
        )
        require(
            {s.binding_id for s in self.signals} == set(bound), "every signal needs a used binding"
        )
        for binding in self.bindings:
            require(
                binding.valid_from_ms
                <= self.issued_at_ms
                < self.expires_at_ms
                <= binding.valid_until_ms,
                "feed outside canonical binding validity",
            )
        for signal in self.signals:
            binding = bound[signal.binding_id]
            require(
                binding.valid_from_ms <= signal.observed_at_ms <= self.issued_at_ms,
                "signal outside binding or after issue time",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SignedRiskFeed(Record):
    payload: RiskFeedPayload
    public_key_hex: str = field(metadata=HASH)
    signature_hex: str = field(metadata={"pattern": "^[0-9a-f]{128}$"})


def signing_bytes(payload: RiskFeedPayload) -> bytes:
    payload.__post_init__()
    return SIGNATURE_PREFIX + canonical_bytes(payload)




# --- v2: typed measurement targets and separated clocks (additive; v1 above is frozen) ---


@dataclass(frozen=True, slots=True, kw_only=True)
class CanonicalRiskDefinitionV2(Record):
    """Operator-approved registry artifact: what a channel measures and how a question may map to it."""

    definition_id: str = field(metadata=ID)
    definition_version: str = field(metadata=ID)
    asset: str = field(metadata={"pattern": "^[A-Z][A-Z0-9]{0,15}$"})
    channel: Channel
    policy_horizon_ms: int = field(metadata={"minimum": 1, "maximum": 31_536_000_000})
    predicate: str = field(metadata={"maxLength": 2000})
    units: str = field(metadata={"maxLength": 32})
    threshold: str = field(metadata={"maxLength": 64})
    sampling_grid_ms: int = field(metadata={"minimum": 1, "maximum": 86_400_000})
    witness_semantics: str = field(metadata={"maxLength": 500})
    interval_rule: Literal["[start,end)"] = "[start,end)"
    baseline_policy: str = field(metadata={"maxLength": 500})
    source_policy: str = field(metadata={"maxLength": 2000})
    invalid_data_policy: str = field(metadata={"maxLength": 500})
    mapping_profile_id: str = field(metadata=ID)
    mapping_profile_version: str = field(metadata=ID)
    mapping_kind: MappingKind
    source_freshness_max_ms: int = field(metadata={"minimum": 1, "maximum": 86_400_000})
    inference_freshness_max_ms: int = field(metadata={"minimum": 1, "maximum": 21_600_000})
    clock_skew_max_ms: int = field(metadata={"minimum": 0, "maximum": 600_000})
    calibration_cohort_id: str = field(metadata=ID)

    def validate(self) -> None:
        expected = CHANNEL_HORIZON_MS.get(self.channel)
        require(expected is None or self.policy_horizon_ms == expected, "channel horizon mismatch")
        require(self.sampling_grid_ms <= self.policy_horizon_ms, "sampling grid exceeds policy horizon")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskMappingProfileV2(Record):
    """Reviewed relationship between a question's event and the channel's policy event."""

    profile_id: str = field(metadata=ID)
    profile_version: str = field(metadata=ID)
    mapping_kind: MappingKind
    predicate_class: Literal["monotone_any_event", "exact_target"]
    max_forecast_age_ms: int = field(metadata={"minimum": 1, "maximum": 21_600_000})
    max_policy_lag_ms: int = field(metadata={"minimum": 0, "maximum": 31_536_000_000})
    source_freshness_max_ms: int = field(metadata={"minimum": 1, "maximum": 86_400_000})
    clock_skew_max_ms: int = field(metadata={"minimum": 0, "maximum": 600_000})
    review_reference: str = field(metadata={"maxLength": 500})

    def validate(self) -> None:
        if self.mapping_kind == "containing_upper_estimate":
            require(self.predicate_class == "monotone_any_event", "containment needs a monotone any-event predicate")
            require(self.max_policy_lag_ms == 0, "containment has no policy lag; coverage is by target interval")
        else:
            require(self.predicate_class == "exact_target", "exact dated mapping targets the declared window")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedSeriesV2(Record):
    """Operator-approved recurring episode template; both producer and consumer hold the same record.

    A binding conforms to the series when it names the same channel/asset/definition/profile,
    spans exactly window_ms from a start aligned to cadence_ms, and reserves the last policy
    horizon of the window for the next episode's overlap (operational validity [S, E-H)).
    """

    series_id: str = field(metadata=ID)
    version: Literal["canonical-risk-series-v2"] = "canonical-risk-series-v2"
    feed_id: str = field(metadata=ID)
    channel: Channel
    asset: str = field(metadata={"pattern": "^[A-Z][A-Z0-9]{0,15}$"})
    definition_hash: str = field(metadata=HASH)
    mapping_profile_hash: str = field(metadata=HASH)
    mapping_kind: MappingKind
    policy_horizon_ms: int = field(metadata={"minimum": 1, "maximum": 31_536_000_000})
    window_ms: int = field(metadata={"minimum": 1, "maximum": 31_536_000_000})
    cadence_ms: int = field(metadata={"minimum": 60_000, "maximum": 31_536_000_000})
    lead_ms: int = field(metadata={"minimum": 60_000, "maximum": 86_400_000})
    question_template: str = field(metadata={"maxLength": 1000})

    def validate(self) -> None:
        expected = CHANNEL_HORIZON_MS.get(self.channel)
        require(expected is None or self.policy_horizon_ms == expected, "channel horizon mismatch")
        if self.mapping_kind == "containing_upper_estimate":
            require(self.window_ms - self.policy_horizon_ms >= self.cadence_ms,
                    "containing episodes must overlap by at least one cadence")
        else:
            require(self.window_ms == self.policy_horizon_ms, "exact dated episodes span one policy horizon")
        require(self.cadence_ms <= self.window_ms, "cadence exceeds the episode window")
        require(self.lead_ms < self.cadence_ms, "lead time must be shorter than the cadence")
        require("{start}" in self.question_template and "{end}" in self.question_template,
                "template must place the episode start and end")

    def conforms(self, binding: RiskFeedBindingV2) -> bool:
        """Deterministic template check; the binding's signature still comes from the producer."""
        return (
            binding.series_id == self.series_id and binding.channel == self.channel and binding.asset == self.asset
            and binding.definition_hash == self.definition_hash
            and binding.mapping_profile_hash == self.mapping_profile_hash
            and binding.mapping_kind == self.mapping_kind and binding.policy_horizon_ms == self.policy_horizon_ms
            and binding.target_end_ms - binding.target_start_ms == self.window_ms
            and binding.target_start_ms % self.cadence_ms == 0
            and binding.operational_valid_from_ms == binding.target_start_ms
            and binding.operational_valid_until_ms == binding.target_end_ms - (
                self.policy_horizon_ms if self.mapping_kind == "containing_upper_estimate" else 0)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedBindingV2(Record):
    """Immutable Forecast question bound to a typed target [S,E) and a reviewed mapping profile."""

    binding_id: str = field(metadata=ID)
    version: Literal["canonical-risk-binding-v2"] = "canonical-risk-binding-v2"
    forecast_id: str = field(metadata=ID)
    specification_hash: str = field(metadata=HASH)
    channel: Channel
    asset: str = field(metadata={"pattern": "^[A-Z][A-Z0-9]{0,15}$"})
    category: Category
    series_id: str = field(metadata=ID)
    episode_id: str = field(metadata=ID)
    target_start_ms: int
    target_end_ms: int
    interval: Literal["[start,end)"] = "[start,end)"
    policy_horizon_ms: int = field(metadata={"minimum": 1, "maximum": 31_536_000_000})
    definition_hash: str = field(metadata=HASH)
    mapping_profile_id: str = field(metadata=ID)
    mapping_profile_version: str = field(metadata=ID)
    mapping_profile_hash: str = field(metadata=HASH)
    mapping_kind: MappingKind
    question_event_definition_hash: str = field(metadata=HASH)
    approval_artifact_hash: str = field(metadata=HASH)
    # Advance approval/forecasting may start before S; policy use may not.
    authorization_valid_from_ms: int
    authorization_valid_until_ms: int
    operational_valid_from_ms: int
    operational_valid_until_ms: int

    def validate(self) -> None:
        require(self.target_start_ms < self.target_end_ms, "target interval is empty")
        expected = CHANNEL_HORIZON_MS.get(self.channel)
        require(expected is None or self.policy_horizon_ms == expected, "channel horizon mismatch")
        require(self.authorization_valid_from_ms < self.authorization_valid_until_ms, "authorization validity is empty")
        require(self.operational_valid_from_ms < self.operational_valid_until_ms, "operational validity is empty")
        require(
            self.authorization_valid_from_ms <= self.operational_valid_from_ms
            and self.operational_valid_until_ms <= self.authorization_valid_until_ms,
            "operational validity outside authorization",
        )
        require(self.target_start_ms <= self.operational_valid_from_ms, "policy use before target start")
        if self.mapping_kind == "containing_upper_estimate":
            # Every policy time T inside operational validity keeps [T,T+H) inside [S,E).
            require(
                self.operational_valid_until_ms + self.policy_horizon_ms <= self.target_end_ms,
                "policy horizon escapes target end",
            )
        else:
            require(
                self.target_end_ms - self.target_start_ms == self.policy_horizon_ms,
                "exact dated target must span one policy horizon",
            )
            require(self.operational_valid_until_ms <= self.target_end_ms, "dated forecast used after its target")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedSignalV2(Record):
    """One source's estimate of the bound question's YES event, with every clock role recorded separately."""

    binding_id: str = field(metadata=ID)
    source: Source
    value_kind: Literal["question_probability"] = "question_probability"
    question_probability_bp: int = field(metadata=BP)
    confidence_bp: int = field(metadata=BP)
    sample_count: int = field(metadata={"minimum": 1, "maximum": 1000000})
    units: Literal["basis-points"] = "basis-points"
    forecast_as_of_ms: int
    information_cutoff_ms: int
    evaluation_started_at_ms: int
    evaluation_completed_at_ms: int
    source_capture_started_at_ms: int
    source_capture_completed_at_ms: int
    source_watermark_ms: int | None
    source_bundle_hash: str = field(metadata=HASH)
    estimate_hash: str = field(metadata=HASH)
    coverage_evidence_hash: str = field(metadata=HASH)
    evidence_hash: str = field(metadata=HASH)
    dependence_group: str = field(metadata=HASH)
    estimator_version: str = field(metadata=ID)
    oldest_member_as_of_ms: int | None = None
    newest_member_as_of_ms: int | None = None
    constituent_dataset_hash: str | None = field(default=None, metadata=HASH)
    calibration_status: Status = "provisional"

    def validate(self) -> None:
        require(self.information_cutoff_ms <= self.forecast_as_of_ms, "information cutoff after forecast as-of")
        require(self.forecast_as_of_ms <= self.evaluation_completed_at_ms, "evaluation completed before its as-of")
        require(self.evaluation_started_at_ms <= self.evaluation_completed_at_ms, "evaluation ends before it starts")
        require(
            self.source_capture_started_at_ms <= self.source_capture_completed_at_ms <= self.forecast_as_of_ms,
            "source capture outside the forecast information set",
        )
        require(
            self.source_watermark_ms is None or self.source_watermark_ms <= self.forecast_as_of_ms,
            "source event after forecast as-of",
        )
        pool = (self.oldest_member_as_of_ms, self.newest_member_as_of_ms, self.constituent_dataset_hash)
        if self.source in ("crowd", "top"):
            require(all(value is not None for value in pool), "pooled sources declare constituent provenance")
        else:
            require(all(value is None for value in pool), "single-model sources have no constituent pool")
        if self.oldest_member_as_of_ms is not None and self.newest_member_as_of_ms is not None:
            require(
                self.oldest_member_as_of_ms <= self.newest_member_as_of_ms <= self.forecast_as_of_ms,
                "pool members outside the forecast information set",
            )


def freshness_as_of_ms(signal: RiskFeedSignalV2) -> int:
    """A pool is only as fresh as its oldest member; the newest submission never retimes peers."""
    return signal.forecast_as_of_ms if signal.oldest_member_as_of_ms is None else signal.oldest_member_as_of_ms


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelCoverageV2(Record):
    """Visible status for every required channel; missing mappings stay in the denominator."""

    channel: Channel
    status: CoverageStatus
    binding_id: str | None = field(default=None, metadata=ID)
    reason: str | None = field(default=None, metadata={"maxLength": 500})

    def validate(self) -> None:
        if self.status == "covered":
            require(self.binding_id is not None and self.reason is None, "covered channels reference one binding")
        else:
            require(self.binding_id is None and self.reason is not None, "uncovered channels state a reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskFeedPayloadV2(Record):
    purpose: Literal["forecast-risk-feed-v2"] = "forecast-risk-feed-v2"
    genesis_hash: str = field(metadata={"pattern": "^[1-9A-HJ-NP-Za-km-z]{32,44}$"})
    feed_id: str = field(metadata=ID)
    sequence: int = field(metadata={"minimum": 1})
    key_id: str = field(metadata=ID)
    issued_at_ms: int
    expires_at_ms: int
    bindings: tuple[RiskFeedBindingV2, ...] = field(metadata={"maxItems": 12})
    signals: tuple[RiskFeedSignalV2, ...] = field(metadata={"maxItems": 48})
    channel_coverage: tuple[ChannelCoverageV2, ...] = field(metadata={"minItems": 1, "maxItems": 12})
    profile_set_hash: str = field(metadata=HASH)
    weight_set_hash: str = field(metadata=HASH)
    weight_set_version: str = field(metadata=ID)
    calibration_cohort_id: str = field(metadata=ID)

    def validate(self) -> None:
        require(0 < self.expires_at_ms - self.issued_at_ms <= FEED_TTL_MS, "feed lifetime exceeds 120 seconds")
        bound = {b.binding_id: b for b in self.bindings}
        require(len(bound) == len(self.bindings), "duplicate binding")
        require(len({b.channel for b in self.bindings}) == len(self.bindings), "duplicate canonical channel")
        require(tuple(sorted(bound)) == tuple(b.binding_id for b in self.bindings), "bindings must be sorted")
        identities = [(s.binding_id, s.source) for s in self.signals]
        require(len(set(identities)) == len(identities) and identities == sorted(identities),
                "signals must be unique and sorted")
        require({s.binding_id for s in self.signals} == set(bound), "every binding needs a signal and vice versa")
        for binding in self.bindings:
            require(
                binding.operational_valid_from_ms <= self.issued_at_ms
                < self.expires_at_ms <= binding.operational_valid_until_ms,
                "feed outside operational binding validity",
            )
        for signal in self.signals:
            binding = bound[signal.binding_id]
            require(
                binding.authorization_valid_from_ms <= signal.forecast_as_of_ms
                and signal.evaluation_completed_at_ms <= self.issued_at_ms,
                "signal outside binding authorization or after issue time",
            )
            if binding.mapping_kind == "exact_dated":
                # A dated forecast estimates the whole [S,E) only from information at or before S.
                require(signal.forecast_as_of_ms <= binding.target_start_ms,
                        "exact dated estimates need an as-of at or before the target start")
        channels = [c.channel for c in self.channel_coverage]
        require(len(set(channels)) == len(channels) and channels == sorted(channels),
                "channel coverage must be unique and sorted")
        covered = {c.channel: c.binding_id for c in self.channel_coverage if c.status == "covered"}
        require(covered == {b.channel: b.binding_id for b in self.bindings},
                "channel coverage must name exactly the bound channels")


@dataclass(frozen=True, slots=True, kw_only=True)
class SignedRiskFeedV2(Record):
    payload: RiskFeedPayloadV2
    public_key_hex: str = field(metadata=HASH)
    signature_hex: str = field(metadata={"pattern": "^[0-9a-f]{128}$"})


def signing_bytes_v2(payload: RiskFeedPayloadV2) -> bytes:
    payload.__post_init__()
    return SIGNATURE_PREFIX_V2 + canonical_bytes(payload)


FEED_RECORD_TYPES = (RiskFeedBinding, RiskFeedSignal, RiskFeedPayload, SignedRiskFeed)
FEED_RECORD_TYPES_V2 = (CanonicalRiskDefinitionV2, RiskMappingProfileV2, RiskFeedSeriesV2, RiskFeedBindingV2,
                        RiskFeedSignalV2, ChannelCoverageV2, RiskFeedPayloadV2, SignedRiskFeedV2)
