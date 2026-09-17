"""Additive v2 risk feed contract: typed measurement targets, separated clocks, v2 signature domain."""

from __future__ import annotations

import unittest
from dataclasses import replace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from forecast_domain.errors import ValidationError
from forecast_domain.models import Category
from forecast_domain.risk_feed import (
    SIGNATURE_PREFIX,
    SIGNATURE_PREFIX_V2,
    CanonicalRiskDefinitionV2,
    ChannelCoverageV2,
    RiskFeedBindingV2,
    RiskFeedPayloadV2,
    RiskFeedSeriesV2,
    RiskFeedSignalV2,
    RiskMappingProfileV2,
    SignedRiskFeedV2,
    signing_bytes_v2,
)
from forecast_domain.serialization import canonical_bytes, content_hash, from_dict, to_dict

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


def golden_envelope() -> SignedRiskFeedV2:
    # Public deterministic TEST key; never used for live signing.
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    payload = golden_payload()
    return SignedRiskFeedV2(payload=payload, public_key_hex=public.hex(),
                            signature_hex=key.sign(signing_bytes_v2(payload)).hex())


class RiskFeedV2ContractTests(unittest.TestCase):
    def test_v1_signature_domain_is_unchanged(self):
        # The v1 golden vector keeps its own test module; v2 only adds a distinct domain.
        self.assertEqual(SIGNATURE_PREFIX, b"forecast-risk-feed-v1:signature:")
        self.assertNotEqual(SIGNATURE_PREFIX, SIGNATURE_PREFIX_V2)
        self.assertEqual(len(SIGNATURE_PREFIX), len(SIGNATURE_PREFIX_V2))

    def test_ed25519_canonical_roundtrip_and_golden_vector(self):
        envelope = golden_envelope()
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        key.public_key().verify(bytes.fromhex(envelope.signature_hex), signing_bytes_v2(envelope.payload))
        self.assertEqual(envelope, from_dict(SignedRiskFeedV2, to_dict(envelope)))
        self.assertEqual(SIGNATURE_PREFIX_V2, b"forecast-risk-feed-v2:signature:")
        self.assertEqual(signing_bytes_v2(envelope.payload),
                         b"forecast-risk-feed-v2:signature:" + canonical_bytes(envelope.payload))
        self.assertEqual(signing_bytes_v2(envelope.payload)[:32], SIGNATURE_PREFIX_V2)
        self.assertEqual(envelope.public_key_hex,
                         "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
        self.assertEqual(
            envelope.signature_hex,
            "d906de8900e3621c8496f6035647a764bc80b64a50c11b207a45f26f1b417deb654c96776c1204dfe9b0adc4e4adf628ff6b90830347b0f2cd346fba55f3e608",
        )
        self.assertEqual(content_hash(envelope.payload), "89be80bbccaccf6175e7ce257caf52d54cff7a6672eff9f27d5e686de3ee9e37")

    def test_mapping_profile_rules(self):
        with self.assertRaises(ValidationError):
            replace(golden_profile(), predicate_class="exact_target")
        with self.assertRaises(ValidationError):
            replace(golden_profile(), max_policy_lag_ms=1)
        with self.assertRaises(ValidationError):
            replace(golden_exact_profile(), predicate_class="monotone_any_event")
        self.assertEqual(golden_binding().mapping_profile_hash, content_hash(golden_profile()))

    def test_series_template_and_conformance(self):
        series = golden_series()
        aligned = (S // series.cadence_ms + 1) * series.cadence_ms
        episode = replace(golden_binding(), target_start_ms=aligned, target_end_ms=aligned + 48 * HOUR,
                          operational_valid_from_ms=aligned, operational_valid_until_ms=aligned + 24 * HOUR,
                          authorization_valid_until_ms=aligned + 49 * HOUR)
        self.assertTrue(series.conforms(episode))
        self.assertFalse(series.conforms(golden_binding()))          # golden start is not cadence-aligned
        for kwargs in [
            {"operational_valid_until_ms": aligned + 24 * HOUR - 1},  # wrong overlap reservation
            {"series_id": "other"},
            {"mapping_profile_hash": "0" * 64},
        ]:
            with self.subTest(kwargs):
                self.assertFalse(series.conforms(replace(episode, **kwargs)))
        for kwargs in [
            {"window_ms": 30 * HOUR},                                  # less than H + cadence
            {"lead_ms": 13 * HOUR},
            {"question_template": "no placeholders"},
            {"mapping_kind": "exact_dated"},                          # window must equal H
        ]:
            with self.subTest(kwargs):
                with self.assertRaises(ValidationError):
                    replace(series, **kwargs)
        self.assertEqual(from_dict(RiskFeedSeriesV2, to_dict(series)), series)

    def test_binding_target_and_validity_rules(self):
        binding = golden_binding()
        for kwargs, message in [
            ({"target_end_ms": S}, "empty target"),
            ({"interval": "[start,end]"}, "closed interval"),
            ({"policy_horizon_ms": 23 * HOUR}, "channel horizon mismatch"),
            ({"operational_valid_from_ms": S - 1}, "operational use before target start"),
            ({"operational_valid_until_ms": E - H + 1}, "policy horizon escapes target end"),
            ({"authorization_valid_until_ms": E - H - 1}, "operational outside authorization"),
            ({"authorization_valid_from_ms": E + HOUR}, "empty authorization"),
            ({"version": "canonical-risk-binding-v1"}, "v1 version on v2 record"),
        ]:
            with self.subTest(message):
                with self.assertRaises(ValidationError):
                    replace(binding, **kwargs)
        # Exact dated mode needs the target to equal one policy horizon.
        with self.assertRaises(ValidationError):
            replace(binding, mapping_kind="exact_dated")
        exact = replace(binding, mapping_kind="exact_dated", target_end_ms=S + H,
                        operational_valid_until_ms=S + H, authorization_valid_until_ms=S + H + HOUR)
        self.assertEqual(exact.target_end_ms - exact.target_start_ms, exact.policy_horizon_ms)

    def test_signal_clock_roles_and_pool_provenance(self):
        signal = golden_signal(golden_binding())
        for kwargs in [
            {"information_cutoff_ms": A + 1},                # cutoff after as-of
            {"evaluation_completed_at_ms": A - 1},           # completed before as-of
            {"evaluation_started_at_ms": A + 40_001},        # started after completion
            {"source_capture_completed_at_ms": A + 1},       # capture after as-of
            {"source_capture_started_at_ms": A + 1},         # capture start after completion
            {"source_watermark_ms": A + 1},                  # source event after as-of
            {"value_kind": "rolling_probability"},
            {"units": "percent"},
            {"question_probability_bp": 10_001},
            {"oldest_member_as_of_ms": A - 10, "newest_member_as_of_ms": A - 20, "constituent_dataset_hash": "6" * 64},
            {"oldest_member_as_of_ms": A - 10},              # partial pool provenance
        ]:
            with self.subTest(kwargs):
                with self.assertRaises(ValidationError):
                    replace(signal, **kwargs)
        with self.assertRaises(ValidationError):
            replace(signal, source="crowd", sample_count=5)  # pools must declare constituent provenance
        crowd = replace(signal, source="crowd", sample_count=5, oldest_member_as_of_ms=A - 3 * HOUR,
                        newest_member_as_of_ms=A - 10, constituent_dataset_hash="6" * 64)
        self.assertEqual(crowd.oldest_member_as_of_ms, A - 3 * HOUR)
        with self.assertRaises(ValidationError):
            replace(crowd, newest_member_as_of_ms=A + 1)
        self.assertIsNone(replace(signal, source_watermark_ms=None).source_watermark_ms)

    def test_payload_validity_coverage_and_ordering(self):
        payload = golden_payload()
        binding = payload.bindings[0]
        with self.assertRaises(ValidationError):
            replace(payload, expires_at_ms=ISSUED + 120_001)
        with self.assertRaises(ValidationError):
            replace(payload, issued_at_ms=S - 1, expires_at_ms=S + 100)        # before operational validity
        with self.assertRaises(ValidationError):
            replace(payload, issued_at_ms=E - H - 1, expires_at_ms=E - H + 1)  # expiry escapes E-H
        with self.assertRaises(ValidationError):
            replace(payload, issued_at_ms=A + 39_999, expires_at_ms=A + 60_000)  # issued before evaluation done
        with self.assertRaises(ValidationError):
            replace(payload, signals=payload.signals * 2)
        with self.assertRaises(ValidationError):
            replace(payload, bindings=(binding, replace(binding, binding_id="zzz", channel="depegRisk7d",
                                                        policy_horizon_ms=168 * HOUR,
                                                        target_end_ms=S + 200 * HOUR,
                                                        authorization_valid_until_ms=S + 201 * HOUR)))
        with self.assertRaises(ValidationError):
            replace(payload, channel_coverage=payload.channel_coverage[::-1])  # unsorted channels
        with self.assertRaises(ValidationError):
            replace(payload, channel_coverage=(replace(payload.channel_coverage[0], binding_id="missing"),
                                               payload.channel_coverage[1]))
        with self.assertRaises(ValidationError):
            replace(payload, channel_coverage=(payload.channel_coverage[1],))  # bound channel not reported
        with self.assertRaises(ValidationError):
            replace(payload, channel_coverage=(replace(payload.channel_coverage[0], status="unavailable"),
                                               payload.channel_coverage[1]))  # bound channel reported as missing
        with self.assertRaises(ValidationError):
            ChannelCoverageV2(channel="depegRisk7d", status="unsupported")   # missing status needs a reason
        with self.assertRaises(ValidationError):
            ChannelCoverageV2(channel="depegRisk7d", status="covered", reason="x")  # covered needs a binding
        exact_binding = replace(binding, mapping_kind="exact_dated", target_end_ms=S + H,
                                operational_valid_until_ms=S + H, authorization_valid_until_ms=S + H + HOUR)
        with self.assertRaises(ValidationError):
            replace(payload, bindings=(exact_binding,))  # exact dated refuses an as-of after the target start
        advance = replace(payload.signals[0], forecast_as_of_ms=S - HOUR, information_cutoff_ms=S - HOUR,
                          evaluation_started_at_ms=S - HOUR, evaluation_completed_at_ms=S - HOUR + 40_000,
                          source_capture_started_at_ms=S - HOUR - 20_000,
                          source_capture_completed_at_ms=S - HOUR, source_watermark_ms=S - HOUR - 300_000)
        dated = replace(payload, bindings=(exact_binding,), signals=(advance,))
        self.assertLessEqual(dated.signals[0].forecast_as_of_ms, exact_binding.target_start_ms)
        with self.assertRaises(ValidationError):
            replace(dated, signals=(replace(advance, forecast_as_of_ms=S + 1, evaluation_completed_at_ms=S + 2),))

    def test_decode_refuses_commands_purpose_and_bool_units(self):
        for path, value in [("purpose", "forecast-risk-feed-v1"), ("purpose", "mint"),
                            ("sequence", True), ("issued_at_ms", 1.5), ("command", "mint")]:
            raw = to_dict(golden_payload())
            raw[path] = value
            with self.subTest(path=path, value=value):
                with self.assertRaises(ValidationError):
                    from_dict(RiskFeedPayloadV2, raw)


if __name__ == "__main__":
    unittest.main()
