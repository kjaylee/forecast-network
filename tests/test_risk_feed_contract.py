"""Shared signed feed contract and deterministic Ed25519 interoperability vector."""

from __future__ import annotations

import unittest
from dataclasses import replace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from forecast_domain.errors import ValidationError
from forecast_domain.models import Category
from forecast_domain.risk_feed import (
    RiskFeedBinding,
    RiskFeedPayload,
    RiskFeedSignal,
    SignedRiskFeed,
    signing_bytes,
)
from forecast_domain.serialization import canonical_bytes, from_dict, to_dict

T = 1_800_000_000_000
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1"


def golden_payload() -> RiskFeedPayload:
    binding = RiskFeedBinding(
        binding_id="usdc-depeg-1d-v1",
        forecast_id="forecast-usdc-001",
        specification_hash="a" * 64,
        channel="depegRisk1d",
        horizon_hours=24,
        asset="USDC",
        category=Category.CRYPTO,
        valid_from_ms=T - 1000,
        valid_until_ms=T + 86400000,
    )
    signal = RiskFeedSignal(
        binding_id=binding.binding_id,
        source="crowd",
        probability_bp=1200,
        confidence_bp=3500,
        sample_count=5,
        observed_at_ms=T - 100,
        evidence_hash="b" * 64,
        dependence_group="c" * 64,
    )
    return RiskFeedPayload(
        genesis_hash=GENESIS,
        feed_id="devnet-stable-risk",
        sequence=1,
        key_id="feed-key-1",
        issued_at_ms=T,
        expires_at_ms=T + 120000,
        bindings=(binding,),
        signals=(signal,),
        weight_set_hash="d" * 64,
        weight_set_version="source-calibration-v1",
    )


def golden_envelope() -> SignedRiskFeed:
    # Public deterministic TEST key; never used for live signing.
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    payload = golden_payload()
    return SignedRiskFeed(
        payload=payload,
        public_key_hex=public.hex(),
        signature_hex=key.sign(signing_bytes(payload)).hex(),
    )


class RiskFeedContractTests(unittest.TestCase):
    def test_ed25519_canonical_roundtrip(self):
        envelope = golden_envelope()
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        key.public_key().verify(
            bytes.fromhex(envelope.signature_hex), signing_bytes(envelope.payload)
        )
        self.assertEqual(envelope, from_dict(SignedRiskFeed, to_dict(envelope)))
        self.assertEqual(
            envelope.signature_hex,
            "2e59bd259830754f162288e98921e42c6f5f05741678fc9bb11f28104ba8259d4ef9078603f90f1b32dab20b33bb6a9198aefe24b4fc2e1f39f0e0ea807c5504",
        )
        self.assertEqual(
            envelope.public_key_hex,
            "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",
        )
        self.assertEqual(
            signing_bytes(envelope.payload),
            b"forecast-risk-feed-v1:signature:" + canonical_bytes(envelope.payload),
        )

    def test_decode_refuses_commands_bools_units_and_future_source(self):
        for path, value in [
            ("purpose", "mint"),
            ("sequence", True),
            ("issued_at_ms", 1.5),
            ("command", "mint"),
        ]:
            raw = to_dict(golden_payload())
            raw[path] = value
            with self.assertRaises(ValidationError):
                from_dict(RiskFeedPayload, raw)
        payload = golden_payload()
        for kwargs in [{"units": "percent"}, {"observed_at_ms": T + 1}, {"probability_bp": 10001}]:
            with self.assertRaises(ValidationError):
                replace(payload, signals=(replace(payload.signals[0], **kwargs),))
        with self.assertRaises(ValidationError):
            replace(payload, expires_at_ms=T + 120001)
        with self.assertRaises(ValidationError):
            replace(payload, bindings=(replace(payload.bindings[0], horizon_hours=1),))
        with self.assertRaises(ValidationError):
            replace(payload, signals=payload.signals * 2)
