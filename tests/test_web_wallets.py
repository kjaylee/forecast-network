"""Wallet-use-case tests; the Worker separately verifies real Ed25519 vectors.

The deterministic fixture verifier below is injected only by tests. It exercises
exact byte binding and failure behavior without introducing a crypto dependency.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

from forecast_application.database import SQLiteDatabase
from forecast_application.errors import AppError
from forecast_application.wallets import (
    CHAIN,
    CHALLENGE_LIFETIME_MS,
    WalletService,
    decode_address,
    decode_signature,
    encode_address,
)

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://forecast.example"
RFC_PUBLIC_KEY_1 = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC_PUBLIC_KEY_2 = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")


class WalletTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now = 1_800_000_000_000
        self.nonce = 0
        self.verifications = []
        self.verify_error = False
        self.expire_during_verify = False
        self.verify_result = True
        self.wallets = self.make_service()
        self.public_key = RFC_PUBLIC_KEY_1
        self.address = encode_address(self.public_key)
        self.other_address = encode_address(RFC_PUBLIC_KEY_2)
        for identifier in ("user-a", "user-b"):
            await self.db.execute("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                                  (identifier, identifier, identifier, identifier+"-hash", self.now))

    def random_token(self):
        self.nonce += 1
        return hashlib.sha256(str(self.nonce).encode()).hexdigest()

    def make_service(self, origin=ORIGIN):
        return WalletService(self.db, now_ms=lambda: self.now, random_token=self.random_token,
                             verify_signature=self.verify_signature, origin=origin)

    async def verify_signature(self, public_key, message, signature):
        self.verifications.append((public_key, message, signature))
        await asyncio.sleep(0)
        if self.expire_during_verify:
            self.now += CHALLENGE_LIFETIME_MS
        if self.verify_error:
            raise RuntimeError("private crypto backend failure")
        expected = hashlib.sha512(b"TEST ONLY: "+public_key+message).digest()
        return self.verify_result if signature == expected else False

    @staticmethod
    def signed(challenge, *, message=None, public_key=None):
        key = decode_address(challenge["address"]) if public_key is None else public_key
        raw_message = (challenge["message"] if message is None else message).encode("utf-8")
        signature = hashlib.sha512(b"TEST ONLY: "+key+raw_message).digest()
        return {"challengeId": challenge["challengeId"], "address": challenge["address"],
                "signature": base64.b64encode(signature).decode("ascii")}

    async def asyncTearDown(self):
        self.connection.close()

    async def test_address_and_signature_decoding_are_canonical_and_bounded(self):
        self.assertEqual(decode_address(self.address), self.public_key)
        self.assertEqual(encode_address(b"\0"*32), "1"*32)
        with self.assertRaises(AppError):
            decode_address("1"*32)
        for value in ("", "0"*44, "z"*44, " " + self.address, "1"*31, "1"*33, [], "A"*200):
            with self.subTest(value=value), self.assertRaises(AppError):
                decode_address(value)
        encoded = base64.b64encode(b"s"*64).decode("ascii")
        self.assertEqual(decode_signature(encoded), b"s"*64)
        for value in (encoded.rstrip("="), encoded+"\n", "!"*88, base64.b64encode(b"s"*63).decode(), None):
            with self.subTest(value=value), self.assertRaises(AppError):
                decode_signature(value)

    async def test_all_eight_canonical_small_order_ed25519_points_are_rejected(self):
        # Independently reviewed torsion encodings: orders 8, 4, 2, and 1.
        vectors = (
            "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
            "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
            "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
            "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
            "00"*32, "00"*31+"80", "ec"+"ff"*30+"7f", "01"+"00"*31,
        )
        for encoded in vectors:
            with self.subTest(encoded=encoded), self.assertRaises(AppError) as caught:
                decode_address(encode_address(bytes.fromhex(encoded)))
            self.assertEqual(caught.exception.code, "wallet_address_invalid")

    async def test_noncanonical_curve_encodings_and_off_curve_points_are_rejected(self):
        vectors = ["01"+"00"*30+"80", "ec"+"ff"*31, "02"+"00"*31]
        for y in (2**255-19, 2**255-18, 2**255-1):
            vectors.extend(((y | sign << 255).to_bytes(32, "little").hex() for sign in (0, 1)))
        for encoded in vectors:
            with self.subTest(encoded=encoded), self.assertRaises(AppError):
                decode_address(encode_address(bytes.fromhex(encoded)))

    async def test_identity_signature_bypass_is_blocked_before_backend_verification(self):
        identity_address = encode_address(b"\1"+b"\0"*31)
        with self.assertRaises(AppError) as caught:
            await self.wallets.challenge("user-a", identity_address)
        self.assertEqual(caught.exception.code, "wallet_address_invalid")
        body = {"challengeId": "wc_"+"a"*32, "address": identity_address,
                "signature": base64.b64encode(b"\1"+b"\0"*63).decode("ascii")}
        with self.assertRaises(AppError):
            await self.wallets.link("user-a", body)
        self.assertEqual(self.verifications, [])

    async def test_rfc_and_real_crypto_generated_wallet_keys_are_accepted(self):
        for raw in (RFC_PUBLIC_KEY_1, RFC_PUBLIC_KEY_2):
            self.assertEqual(decode_address(encode_address(raw)), raw)
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable; RFC 8032 fixed public keys were verified")
        # Public keys only. Private keys remain ephemeral inside the crypto API
        # and are neither exported, logged nor written to disk.
        script = "const c=require('node:crypto');console.log(JSON.stringify(Array.from({length:16},()=>" \
                 "c.generateKeyPairSync('ed25519').publicKey.export({type:'spki',format:'der'}).subarray(-32).toString('hex'))))"
        output = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True,
                                env={**os.environ, "TMPDIR": str(ROOT / "tmp")})
        for encoded in json.loads(output.stdout):
            raw = bytes.fromhex(encoded)
            self.assertEqual(decode_address(encode_address(raw)), raw)

    async def test_challenge_binds_exact_origin_account_address_purpose_chain_and_expiry(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        self.assertEqual(challenge["expiresAt"], self.now+CHALLENGE_LIFETIME_MS)
        for expected in ("User: user-a", "Address: "+self.address, "Origin: "+ORIGIN,
                         "Purpose ID: link_forecast_profile", "Chain: solana:devnet (Solana Devnet)",
                         "Challenge: "+challenge["challengeId"], "Expires at: "+str(challenge["expiresAt"]),
                         "does not authorize a transaction or transfer"):
            self.assertIn(expected, challenge["message"])
        self.assertEqual(challenge["chain"], CHAIN)
        self.assertIsNone((await self.wallets.get_wallet("user-a"))["wallet"])

    async def test_valid_signature_links_only_after_verification_and_preserves_public_audit(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        body = self.signed(challenge)
        result = await self.wallets.link("user-a", body)
        self.assertEqual(result["wallet"], {"address": self.address, "chain": CHAIN, "linkedAt": self.now})
        self.assertEqual(self.verifications[0], (self.public_key, challenge["message"].encode(), decode_signature(body["signature"])))
        self.assertIsNotNone((await self.db.first("SELECT used_at FROM wallet_challenges"))["used_at"])
        audit = json.loads((await self.db.first("SELECT body FROM wallet_audit"))["body"])
        self.assertEqual(audit["messageSha256"], hashlib.sha256(challenge["message"].encode()).hexdigest())
        self.assertEqual(audit["signatureSha256"], hashlib.sha256(decode_signature(body["signature"])).hexdigest())
        self.assertNotIn("signature", audit)
        self.assertNotIn("message", audit)
        self.assertEqual(audit["verification"], "ed25519_sign_message")
        self.assertNotIn("transaction", result["wallet"])
        self.assertNotIn("program", result["wallet"])
        self.assertEqual(result["points"]["available"], 1500)
        self.assertTrue(result["points"]["onboarding"]["wallet"]["completed"])
        self.assertTrue(result["points"]["onboarding"]["wallet"]["linked"])

    async def test_wallet_bonus_is_once_per_account_and_rewarded_address_lifetime(self):
        initial = await self.wallets.get_wallet("user-a")
        self.assertEqual(initial["points"]["available"], 1000)
        challenge = await self.wallets.challenge("user-a", self.address)
        linked = await self.wallets.link("user-a", self.signed(challenge))
        self.assertEqual(linked["points"]["available"], 1500)
        disconnected = await self.wallets.unlink("user-a")
        self.assertEqual(disconnected["points"]["available"], 1500)
        self.assertTrue(disconnected["points"]["onboarding"]["wallet"]["completed"])
        self.assertFalse(disconnected["points"]["onboarding"]["wallet"]["linked"])
        relink = await self.wallets.challenge("user-a", self.address)
        self.assertEqual((await self.wallets.link("user-a", self.signed(relink)))["points"]["available"], 1500)
        replace_challenge = await self.wallets.challenge("user-a", self.other_address)
        self.assertEqual((await self.wallets.link("user-a", self.signed(replace_challenge)))["points"]["available"], 1500)
        reuse = await self.wallets.challenge("user-b", self.address)
        reused = await self.wallets.link("user-b", self.signed(reuse))
        self.assertEqual(reused["points"]["available"], 1000)
        self.assertFalse(reused["points"]["onboarding"]["wallet"]["completed"])
        self.assertEqual(reused["points"]["onboarding"]["wallet"]["reason"], "wallet_already_rewarded")

    async def test_failed_wallet_signature_grants_no_bonus(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        self.verify_result = False
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", self.signed(challenge))
        self.assertEqual(caught.exception.code, "wallet_signature_invalid")
        points = (await self.wallets.get_wallet("user-a"))["points"]
        self.assertEqual(points["available"], 1000)
        self.assertFalse(points["onboarding"]["wallet"]["completed"])

    async def test_wallet_grant_rolls_back_with_link_audit_transaction_failure(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        original_batch = self.db.batch
        before = (await self.wallets.get_wallet("user-a"))["points"]
        async def fail_link(statements):
            if any("INSERT INTO wallet_links(" in sql for sql, _ in statements):
                return await original_batch([*statements,
                    ("INSERT INTO mutation_guards(token,valid) VALUES('wallet-grant-rollback',0)", ())])
            return await original_batch(statements)
        self.db.batch = fail_link
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", self.signed(challenge))
        self.db.batch = original_batch
        self.assertEqual(caught.exception.code, "wallet_storage_unavailable")
        self.assertEqual((await self.wallets.get_wallet("user-a"))["points"], before)
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_challenges WHERE id=?",
                                             (challenge["challengeId"],)))["used_at"])
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_links"))["n"], 0)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit"))["n"], 0)

    async def test_wrong_signature_or_mutated_message_does_not_consume_nonce(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        for body in (self.signed(challenge, message=challenge["message"]+"changed"),
                     self.signed(challenge, public_key=b"x"*32)):
            with self.assertRaises(AppError) as caught:
                await self.wallets.link("user-a", body)
            self.assertEqual(caught.exception.code, "wallet_signature_invalid")
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_challenges"))["used_at"])
        self.assertIsNone((await self.wallets.get_wallet("user-a"))["wallet"])

    async def test_foreign_user_address_and_origin_fail_before_crypto(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        body = self.signed(challenge)
        calls = ((self.wallets, "user-b", body),
                 (self.wallets, "user-a", {**body, "address": self.other_address}),
                 (self.make_service("https://different.example"), "user-a", body))
        for service, user, value in calls:
            with self.assertRaises(AppError) as caught:
                await service.link(user, value)
            self.assertEqual(caught.exception.code, "wallet_challenge_invalid")
        self.assertEqual(len(self.verifications), 0)

    async def test_expiry_at_exact_boundary_and_during_verification(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        self.now = challenge["expiresAt"]
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", self.signed(challenge))
        self.assertEqual(caught.exception.code, "wallet_challenge_expired")
        self.assertEqual(len(self.verifications), 0)
        fresh = await self.wallets.challenge("user-a", self.address)
        self.expire_during_verify = True
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", self.signed(fresh))
        self.assertEqual(caught.exception.code, "wallet_challenge_expired")
        self.assertIsNone((await self.wallets.get_wallet("user-a"))["wallet"])

    async def test_replay_and_simultaneous_same_nonce_accept_exactly_once(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        body = self.signed(challenge)
        result = await asyncio.gather(self.wallets.link("user-a", body), self.wallets.link("user-a", body),
                                      return_exceptions=True)
        errors = [value for value in result if isinstance(value, AppError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "wallet_challenge_used")
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit"))["n"], 1)
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", body)
        self.assertEqual(caught.exception.code, "wallet_challenge_used")

    async def test_unique_wallet_address_cannot_belong_to_two_profiles(self):
        first = await self.wallets.challenge("user-a", self.address)
        second = await self.wallets.challenge("user-b", self.address)
        values = await asyncio.gather(self.wallets.link("user-a", self.signed(first)),
                                      self.wallets.link("user-b", self.signed(second)), return_exceptions=True)
        errors = [value for value in values if isinstance(value, AppError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "wallet_already_linked")
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_links"))["n"], 1)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit"))["n"], 1)

    async def test_link_replacement_keeps_one_wallet_and_records_each_proof(self):
        first = await self.wallets.challenge("user-a", self.address)
        await self.wallets.link("user-a", self.signed(first))
        self.now += 1
        second = await self.wallets.challenge("user-a", self.other_address)
        result = await self.wallets.link("user-a", self.signed(second))
        self.assertEqual(result["wallet"]["address"], self.other_address)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_links"))["n"], 1)
        self.assertEqual((await self.db.first("SELECT revision FROM wallet_links"))["revision"], 2)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit"))["n"], 2)

    async def test_failure_after_nonce_consumption_rolls_back_every_effect(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        await self.db.execute("CREATE TRIGGER reject_wallet_audit BEFORE INSERT ON wallet_audit "
                              "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(AppError):
            await self.wallets.link("user-a", self.signed(challenge))
        self.assertIsNone((await self.wallets.get_wallet("user-a"))["wallet"])
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_challenges"))["used_at"])
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM mutation_guards"))["n"], 0)
        await self.db.execute("DROP TRIGGER reject_wallet_audit")
        self.assertIsNotNone((await self.wallets.link("user-a", self.signed(challenge)))["wallet"])

    async def test_verifier_failure_never_claims_signature_accepted_or_leaks_details(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        self.verify_error = True
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", self.signed(challenge))
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("private", caught.exception.message)
        self.assertIsNone((await self.wallets.get_wallet("user-a"))["wallet"])
        self.verify_error = False
        self.verify_result = 1
        with self.assertRaises(AppError):
            await self.wallets.link("user-a", self.signed(challenge))

    async def test_unlink_is_idempotent_and_does_not_touch_other_users(self):
        first = await self.wallets.challenge("user-a", self.address)
        second = await self.wallets.challenge("user-b", self.other_address)
        await self.wallets.link("user-a", self.signed(first))
        await self.wallets.link("user-b", self.signed(second))
        self.assertIsNone((await self.wallets.unlink("user-a"))["wallet"])
        self.assertIsNone((await self.wallets.unlink("user-a"))["wallet"])
        self.assertEqual((await self.wallets.get_wallet("user-b"))["wallet"]["address"], self.other_address)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit WHERE kind='wallet_unlinked'"))["n"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE wallet_audit SET address=?", (self.other_address,))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("DELETE FROM wallet_audit")

    async def test_unlink_failure_rolls_back_link_and_audit(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        await self.wallets.link("user-a", self.signed(challenge))
        await self.db.execute("CREATE TRIGGER reject_wallet_unlink BEFORE INSERT ON wallet_audit "
                              "WHEN NEW.kind='wallet_unlinked' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(AppError):
            await self.wallets.unlink("user-a")
        self.assertEqual((await self.wallets.get_wallet("user-a"))["wallet"]["address"], self.address)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit"))["n"], 1)

    async def test_unlink_revokes_signed_pending_requests_even_without_an_existing_link(self):
        pending = await self.wallets.challenge("user-a", self.address)
        signature = self.signed(pending)
        self.assertIsNone((await self.wallets.unlink("user-a"))["wallet"])
        with self.assertRaises(AppError) as caught:
            await self.wallets.link("user-a", signature)
        self.assertEqual(caught.exception.code, "wallet_challenge_revoked")
        self.assertEqual(len(self.verifications), 0)
        self.assertIsNone((await self.wallets.get_wallet("user-a"))["wallet"])

    async def test_unlink_revokes_all_unused_requests_but_preserves_verified_history(self):
        verified = await self.wallets.challenge("user-a", self.address)
        await self.wallets.link("user-a", self.signed(verified))
        pending = await self.wallets.challenge("user-a", self.other_address)
        another = await self.wallets.challenge("user-a", self.address)
        foreign = await self.wallets.challenge("user-b", self.other_address)
        await self.wallets.unlink("user-a")
        for request in (pending, another):
            with self.assertRaises(AppError) as caught:
                await self.wallets.link("user-a", self.signed(request))
            self.assertEqual(caught.exception.code, "wallet_challenge_revoked")
        used = await self.db.first("SELECT used_at,revoked_at FROM wallet_challenges WHERE id=?", (verified["challengeId"],))
        self.assertIsNotNone(used["used_at"])
        self.assertIsNone(used["revoked_at"])
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit WHERE kind='wallet_linked'"))["n"], 1)
        self.assertEqual((await self.wallets.link("user-b", self.signed(foreign)))["wallet"]["address"], self.other_address)

    async def test_unlink_stale_snapshot_cannot_remove_a_new_link(self):
        challenge = await self.wallets.challenge("user-a", self.address)
        await self.wallets.link("user-a", self.signed(challenge))
        original_batch = self.db.batch

        async def race(statements):
            self.db.batch = original_batch
            await self.db.execute("UPDATE wallet_links SET address=?,revision=revision+1 WHERE user_id='user-a'",
                                  (self.other_address,))
            return await original_batch(statements)

        self.db.batch = race
        with self.assertRaises(AppError) as caught:
            await self.wallets.unlink("user-a")
        self.assertEqual(caught.exception.code, "wallet_conflict")
        self.assertEqual((await self.wallets.get_wallet("user-a"))["wallet"]["address"], self.other_address)

    async def test_unlink_aba_same_address_relinked_at_revision_one_is_preserved(self):
        initial = await self.wallets.challenge("user-a", self.address)
        await self.wallets.link("user-a", self.signed(initial))
        original_batch = self.db.batch
        fresh_generation = None

        async def race(statements):
            nonlocal fresh_generation
            self.db.batch = original_batch
            await self.wallets.unlink("user-a")
            fresh = await self.wallets.challenge("user-a", self.address)
            fresh_generation = fresh["challengeId"]
            await self.wallets.link("user-a", self.signed(fresh))
            return await original_batch(statements)

        self.db.batch = race
        with self.assertRaises(AppError) as caught:
            await self.wallets.unlink("user-a")
        self.assertEqual(caught.exception.code, "wallet_conflict")
        current = await self.db.first("SELECT address,revision,generation FROM wallet_links WHERE user_id='user-a'")
        self.assertEqual(current["address"], self.address)
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["generation"], fresh_generation)
        self.assertNotEqual(current["generation"], initial["challengeId"])
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM wallet_audit"))["n"], 3)

    async def test_authentication_and_invalid_origin_are_rejected(self):
        with self.assertRaises(AppError) as caught:
            await self.wallets.challenge("unknown-user", self.address)
        self.assertEqual(caught.exception.status, 401)
        for origin in ("http://public.example", "https://example.com/path", "https://user@example.com", "https://example.com/"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.make_service(origin)
        self.assertIsNotNone(self.make_service("http://localhost:8787"))

    async def test_malformed_link_body_and_nonce_are_rejected_without_verification(self):
        for body in ({}, {"challengeId": "short", "address": self.address, "signature": "x"*88},
                     {"challengeId": "wc_"+"a"*32, "address": self.address, "signature": "x"*88, "userId": "user-b"}):
            with self.subTest(body=body), self.assertRaises(AppError):
                await self.wallets.link("user-a", body)
        self.assertEqual(len(self.verifications), 0)
