"""Wallet-signed Devnet memo attestations: exact wire format, relayer signing, device-reported confirmation."""

from __future__ import annotations

import base64
import hashlib
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from forecast_application.attestation import MEMO_PROGRAM, memo_text, partially_signed_transaction
from forecast_application.errors import AppError
from forecast_application.solana_wire import base58_encode, shortvec

from tests import test_web_application as fixtures


class AttestationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApplicationTests.asyncSetUp
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    publish = fixtures.ApplicationTests.publish

    async def wire(self):
        from forecast_application.attestation import Attestations
        self.relayer_key = Ed25519PrivateKey.generate()
        self.relayer = self.relayer_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.user_key = Ed25519PrivateKey.generate()
        user_pub = self.user_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.address = base58_encode(user_pub)
        self.signed: list[bytes] = []

        async def sign(message: bytes) -> bytes:
            self.signed.append(message)
            return self.relayer_key.sign(message)
        self.app.attestations = Attestations(self.db, relayer=self.relayer, sign=sign, now_ms=lambda: self.now,
                                             random_token=self.random_token, rate_limit=self.app.rate_limit)
        await self.db.execute("INSERT INTO wallet_identities(address,user_id,status,created_at,converted_at) VALUES(?,?,'active',?,?)",
                              (self.address, self.other, self.now, self.now))
        card = await self.publish()
        await self.app.submit_forecast(self.other, card["id"], "YES", 70, card["revision"], "attest-vote", 0)
        return card["id"], user_pub

    async def test_prepare_builds_a_relayer_paid_memo_that_only_the_wallet_still_needs_to_sign(self) -> None:
        fid, user_pub = await self.wire()
        blockhash = bytes(range(32))
        prepared = await self.app.attestations.prepare(self.other, fid, {"blockhash": base58_encode(blockhash)})
        tx = base64.b64decode(prepared["transaction"])
        self.assertEqual(tx[0], 2)                                  # two signatures
        relayer_signature, wallet_slot, message = tx[1:65], tx[65:129], tx[129:]
        self.assertEqual(wallet_slot, bytes(64))                    # wallet fills this
        self.relayer_key.public_key().verify(relayer_signature, message)
        self.assertEqual(self.signed, [message])
        # header: 2 signers, 1 read-only signer (the wallet), 1 read-only unsigned (memo program)
        self.assertEqual(message[:3], bytes([2, 1, 1]))
        self.assertEqual(message[3], 3)
        keys = [message[4 + 32*i: 36 + 32*i] for i in range(3)]
        self.assertEqual(keys, [self.relayer, user_pub, MEMO_PROGRAM])
        self.assertEqual(message[100:132], blockhash)
        memo = prepared["memo"].encode()
        self.assertTrue(message.endswith(shortvec(len(memo)) + memo))
        receipt = await self.db.first("SELECT body FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?", (fid, self.other))
        self.assertEqual(prepared["memo"], memo_text(fid, hashlib.sha256(receipt["body"].encode()).hexdigest()))
        self.assertEqual(prepared["signerAddress"], self.address)
        self.assertEqual(prepared["feePayer"], base58_encode(self.relayer))
        self.assertEqual(await self.db.first("SELECT status FROM forecast_attestations"), {"status": "prepared"})

    async def test_confirm_records_the_device_reported_signature_once(self) -> None:
        fid, _ = await self.wire()
        prepared = await self.app.attestations.prepare(self.other, fid, {"blockhash": base58_encode(bytes(range(1, 33)))})
        signature = base58_encode(bytes(range(64)))
        status = await self.app.attestations.confirm(self.other, fid, {"attestationId": prepared["attestationId"], "signature": signature, "slot": 12345})
        self.assertEqual((status["status"], status["signature"], status["slot"]), ("submitted", signature, 12345))
        self.assertEqual(status["explorer"], f"https://explorer.solana.com/tx/{signature}?cluster=devnet")
        again = await self.app.attestations.confirm(self.other, fid, {"attestationId": prepared["attestationId"], "signature": signature})
        self.assertEqual(again["status"], "submitted")
        with self.assertRaises(AppError) as raised:
            await self.app.attestations.confirm(self.other, fid, {"attestationId": prepared["attestationId"], "signature": base58_encode(bytes(64, ))})
        self.assertIn(raised.exception.code, {"attestation_confirm_invalid", "attestation_conflict"})
        detail = await self.app.forecast_detail(fid, self.other)
        self.assertEqual(detail["attestation"]["status"], "submitted")
        self.assertIsNone((await self.app.forecast_detail(fid, None))["attestation"])

    async def test_prepare_requires_a_wallet_and_a_recorded_forecast(self) -> None:
        fid, _ = await self.wire()
        with self.assertRaises(AppError) as raised:
            await self.app.attestations.prepare(self.uid, fid, {"blockhash": base58_encode(bytes(32))})
        self.assertEqual(raised.exception.code, "attestation_requires_forecast")
        with self.assertRaises(AppError) as raised:
            await self.app.attestations.prepare(self.other, fid, {"blockhash": "not-a-hash"})
        self.assertEqual(raised.exception.code, "attestation_blockhash")
        with self.assertRaises(AppError) as raised:
            await self.app.attestations.confirm(self.other, fid, {"attestationId": "at_missing", "signature": base58_encode(bytes(range(64)))})
        self.assertEqual(raised.exception.code, "attestation_not_found")

    def test_partial_serialization_rejects_bad_signatures(self) -> None:
        with self.assertRaises(ValueError):
            partially_signed_transaction(b"msg", (b"a" * 32,), {b"a" * 32: b"short"})


if __name__ == "__main__":
    unittest.main()
