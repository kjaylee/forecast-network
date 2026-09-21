"""Additive v2 risk feed contract: typed measurement targets, separated clocks, v2 signature domain."""

from __future__ import annotations

import unittest
from dataclasses import replace

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as error:  # pragma: no cover
    # The dependency-free CI job exercises the domain without installing anything.
    raise unittest.SkipTest(f"cryptography is required: {error}") from error
from forecast_domain.errors import ValidationError
from forecast_domain.risk_feed import (
    SIGNATURE_PREFIX,
    SIGNATURE_PREFIX_V2,
    ChannelCoverageV2,
    RiskFeedPayloadV2,
    RiskFeedSeriesV2,
    SignedRiskFeedV2,
    signing_bytes_v2,
)
from forecast_domain.serialization import canonical_bytes, content_hash, from_dict, to_dict

from tests.risk_feed_fixtures import (  # noqa: E402,F401 — re-exported for the importers that name this module
    GENESIS,
    HOUR,
    ISSUED,
    TEMPLATE,
    A,
    E,
    H,
    S,
    golden_binding,
    golden_definition,
    golden_exact_profile,
    golden_payload,
    golden_profile,
    golden_series,
    golden_signal,
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
