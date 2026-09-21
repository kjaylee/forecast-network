"""The v2 risk feed fixtures the contract test, the producer tests, the series tests and the
golden generators share — deliberately without `cryptography`.

The contract test module raises `SkipTest` at import when `cryptography` is absent, which is
right for a test and wrong for a fixture: every module that imported a fixture from it skipped
with it, and the dependency-free CI job silently ran 947 tests where the tree holds 1,043, and
refused three golden checks with ModuleNotFoundError. Nothing here signs anything.
"""

from __future__ import annotations

from forecast_domain.models import Category
from forecast_domain.risk_feed import (
    CanonicalRiskDefinitionV2,
    ChannelCoverageV2,
    RiskFeedBindingV2,
    RiskFeedPayloadV2,
    RiskFeedSeriesV2,
    RiskFeedSignalV2,
    RiskMappingProfileV2,
)
from forecast_domain.serialization import content_hash

HOUR = 3_600_000
S = 1_800_000_000_000            # target start
E = S + 48 * HOUR                # 48-hour containing window
H = 24 * HOUR                    # depegRisk1d policy horizon
A = S + 30 * 60_000              # forecast as-of, after start (containing mode permits this)
ISSUED = S + 2 * HOUR            # issue time
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1"


def golden_definition() -> CanonicalRiskDefinitionV2:
    return CanonicalRiskDefinitionV2(
        definition_id="usdc-depeg-1d",
        definition_version="canonical-risk-definition-v2.1",
        asset="USDC",
        channel="depegRisk1d",
        policy_horizon_ms=H,
        predicate="any completed 5-minute candle closes strictly below threshold on both primary sources",
        units="USD",
        threshold="0.9900",
        sampling_grid_ms=300_000,
        witness_semantics="candle start timestamp inside [start,end)",
        interval_rule="[start,end)",
        baseline_policy="absolute threshold; no fixed-start baseline",
        source_policy="Kraken and Bitstamp OHLC; both required; missing data is INVALID",
        invalid_data_policy="INVALID, never NO",
        mapping_profile_id="monotone-any-event-containment",
        mapping_profile_version="containment-profile-v1",
        mapping_kind="containing_upper_estimate",
        source_freshness_max_ms=120_000,
        inference_freshness_max_ms=2 * HOUR,
        clock_skew_max_ms=5_000,
        calibration_cohort_id="usdc-depeg-1d-w48-containing",
    )


def golden_profile() -> RiskMappingProfileV2:
    return RiskMappingProfileV2(
        profile_id="monotone-any-event-containment",
        profile_version="containment-profile-v1",
        mapping_kind="containing_upper_estimate",
        predicate_class="monotone_any_event",
        max_forecast_age_ms=2 * HOUR,
        max_policy_lag_ms=0,
        source_freshness_max_ms=120_000,
        clock_skew_max_ms=5_000,
        review_reference="risk-feed-window-v2 plan, section B",
    )


def golden_exact_profile() -> RiskMappingProfileV2:
    return RiskMappingProfileV2(
        profile_id="exact-dated-target",
        profile_version="exact-profile-v1",
        mapping_kind="exact_dated",
        predicate_class="exact_target",
        max_forecast_age_ms=6 * HOUR,
        max_policy_lag_ms=HOUR,
        source_freshness_max_ms=120_000,
        clock_skew_max_ms=5_000,
        review_reference="risk-feed-window-v2 plan, alternative A",
    )


TEMPLATE = ("During [{start}, {end}), will USDC/USD close strictly below USD 0.9900 on both Kraken and Bitstamp "
            "in any same completed 5-minute candle? Submissions close exactly {end}.")


def golden_series() -> RiskFeedSeriesV2:
    return RiskFeedSeriesV2(
        series_id="usdc-depeg-1d-w48", feed_id="devnet-stable-risk-v2", channel="depegRisk1d", asset="USDC",
        definition_hash=content_hash(golden_definition()), mapping_profile_hash=content_hash(golden_profile()),
        mapping_kind="containing_upper_estimate", policy_horizon_ms=H, window_ms=48 * HOUR, cadence_ms=12 * HOUR,
        lead_ms=3 * HOUR, question_template=TEMPLATE)


def golden_binding() -> RiskFeedBindingV2:
    definition = golden_definition()
    return RiskFeedBindingV2(
        binding_id="usdc-depeg-1d-2026-09-15T09-v2",
        forecast_id="forecast-usdc-048",
        specification_hash="a" * 64,
        channel="depegRisk1d",
        asset="USDC",
        category=Category.CRYPTO,
        series_id="usdc-depeg-1d-w48",
        episode_id="2026-09-15T09:00:00Z",
        target_start_ms=S,
        target_end_ms=E,
        policy_horizon_ms=H,
        definition_hash=content_hash(definition),
        mapping_profile_id=definition.mapping_profile_id,
        mapping_profile_version=definition.mapping_profile_version,
        mapping_profile_hash=content_hash(golden_profile()),
        mapping_kind="containing_upper_estimate",
        question_event_definition_hash="f" * 64,
        approval_artifact_hash="1" * 64,
        authorization_valid_from_ms=S - 6 * HOUR,
        authorization_valid_until_ms=E + HOUR,
        operational_valid_from_ms=S,
        operational_valid_until_ms=E - H,
    )


def golden_signal(binding: RiskFeedBindingV2) -> RiskFeedSignalV2:
    return RiskFeedSignalV2(
        binding_id=binding.binding_id,
        source="ai",
        question_probability_bp=150,
        confidence_bp=6000,
        sample_count=1,
        forecast_as_of_ms=A,
        information_cutoff_ms=A,
        evaluation_started_at_ms=A + 1_000,
        evaluation_completed_at_ms=A + 40_000,
        source_capture_started_at_ms=A - 20_000,
        source_capture_completed_at_ms=A,
        source_watermark_ms=A - 300_000,
        source_bundle_hash="2" * 64,
        estimate_hash="3" * 64,
        coverage_evidence_hash="4" * 64,
        evidence_hash="b" * 64,
        dependence_group="c" * 64,
        estimator_version="gemini-2.5-flash:refresh-v1",
    )


def golden_payload() -> RiskFeedPayloadV2:
    binding = golden_binding()
    return RiskFeedPayloadV2(
        genesis_hash=GENESIS,
        feed_id="devnet-stable-risk-v2",
        sequence=1,
        key_id="feed-key-1",
        issued_at_ms=ISSUED,
        expires_at_ms=ISSUED + 120_000,
        bindings=(binding,),
        signals=(golden_signal(binding),),
        channel_coverage=(
            ChannelCoverageV2(channel="depegRisk1d", status="covered", binding_id=binding.binding_id),
            ChannelCoverageV2(channel="depegRisk7d", status="unsupported",
                              reason="no reviewed containment profile"),
        ),
        profile_set_hash="5" * 64,
        weight_set_hash="d" * 64,
        weight_set_version="source-calibration-v2",
        calibration_cohort_id="usdc-depeg-1d-w48-containing",
    )
