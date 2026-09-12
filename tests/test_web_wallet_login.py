"""Wallet authentication races, permanent identity, and atomic point grants."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from forecast_application.auth import Authentication
from forecast_application.database import SQLiteDatabase
from forecast_application.errors import AppError
from forecast_application.wallet_login import PURPOSE, WalletLogin
from forecast_application.wallets import (
    CHALLENGE_LIFETIME_MS,
    WalletService,
    decode_address,
    encode_address,
)

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://forecast.example"
KEY = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
OTHER_KEY = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")


class WalletLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        for path in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(path.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now = 1_800_000_000_000
        self.counter = 0
        self.on_verify = None
        self.verifications = 0
        self.address, self.other_address = encode_address(KEY), encode_address(OTHER_KEY)
        self.auth = Authentication(self.db, lambda: self.now, self.hash, self.token)
        self.login = WalletLogin(self.db, now_ms=lambda: self.now, token_hash=self.hash, random_token=self.token,
                                 verify_signature=self.verify_signature, origin=ORIGIN)
        self.wallets = WalletService(self.db, lambda: self.now, self.token, self.verify_signature, ORIGIN)

    async def asyncTearDown(self):
        self.connection.close()

    @staticmethod
    def hash(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def token(self):
        self.counter += 1
        return self.hash("secure fixture token "+str(self.counter))

    async def verify_signature(self, public_key, message, signature):
        self.verifications += 1
        await asyncio.sleep(0)
        if self.on_verify:
            await self.on_verify()
        return signature == hashlib.sha512(b"TEST ONLY: "+public_key+message).digest()

    @staticmethod
    def signed(challenge, message=None, key=None):
        raw = (message if message is not None else challenge["message"]).encode()
        signature = hashlib.sha512(b"TEST ONLY: "+(key or decode_address(challenge["address"]))+raw).digest()
        return {"challengeId": challenge["challengeId"], "address": challenge["address"],
                "signature": base64.b64encode(signature).decode()}

    async def prepare(self, address=None, *, mode="login", session=None, expected=None, context=None):
        context = context or (await self.login.context())["contextToken"]
        challenge = await self.login.challenge(context, {"address": address or self.address,
            "mode": mode, "expectedUserId": expected}, session)
        return context, challenge

    async def signed_in(self, address=None):
        context, challenge = await self.prepare(address)
        return context, await self.login.verify(context, self.signed(challenge))

    async def assert_unchanged(self, call):
        before = list(self.connection.iterdump())
        with self.assertRaises(AppError):
            await call()
        self.assertEqual(before, list(self.connection.iterdump()))

    async def test_signup_grants_are_atomic_and_no_recovery_credential_exists(self):
        context, result = await self.signed_in()
        self.assertNotIn("recoveryCode", result)
        self.assertEqual(result["points"]["available"], 1500)
        self.assertEqual(result["user"]["displayName"], "Forecaster "+self.address[:8])
        uid = result["user"]["id"]
        self.assertEqual((await self.db.first("SELECT recovery_hash FROM users"))["recovery_hash"], "disabled-wallet:"+uid)
        self.assertEqual((await self.auth.authenticate(result["sessionToken"], context))["id"], uid)
        self.assertIsNone(await self.auth.authenticate(result["sessionToken"]))
        self.assertIsNone(await self.auth.authenticate(result["sessionToken"], self.token()))
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM point_awards"))["n"], 2)

    async def test_creation_hook_runs_only_after_valid_new_wallet_ownership(self):
        calls = []
        async def creation():
            calls.append("created")
        self.login.on_create = creation
        context, challenge = await self.prepare()
        self.assertEqual(calls, [])
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge, key=OTHER_KEY)))
        self.assertEqual(calls, [])
        await self.login.verify(context, self.signed(challenge))
        self.assertEqual(calls, ["created"])
        context, challenge = await self.prepare()
        await self.login.verify(context, self.signed(challenge))
        self.assertEqual(calls, ["created"])
        guest = await self.auth.register("Migrating guest")
        context, challenge = await self.prepare(self.other_address, mode="migrate",
                                               session=guest["sessionToken"], expected=guest["user"]["id"])
        await self.login.verify(context, self.signed(challenge), guest["sessionToken"])
        self.assertEqual(calls, ["created"])

    async def test_creation_quota_failure_leaves_no_identity_grants_session_or_consumed_nonce(self):
        async def quota_exceeded():
            raise AppError(429, "rate_limited", "Account creation is temporarily limited.")
        self.login.on_create = quota_exceeded
        context, challenge = await self.prepare()
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))
        for table in ("users", "wallet_identities", "point_awards", "point_ledger", "sessions"):
            self.assertEqual((await self.db.first("SELECT count(*) AS n FROM "+table))["n"], 0)
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_login_challenges"))["used_at"])

    async def test_cancellation_during_creation_limiter_prevents_account_creation(self):
        context, challenge = await self.prepare()
        async def cancel():
            await self.login.cancel(context)
        self.login.on_create = cancel
        with self.assertRaises(AppError):
            await self.login.verify(context, self.signed(challenge))
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 0)

    async def test_login_restores_same_profile_without_new_grants_or_renaming(self):
        _, first = await self.signed_in()
        context, challenge = await self.prepare()
        self.assertNotIn(first["user"]["id"], challenge["message"])
        self.assertNotIn(first["user"]["displayName"], challenge["message"])
        result = await self.login.verify(context, self.signed(challenge))
        self.assertEqual(result["user"], first["user"])
        self.assertEqual(result["points"]["available"], 1500)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 1)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM point_ledger"))["n"], 2)

    async def test_wallet_label_hint_names_new_profile_without_claiming_a_verified_handle(self):
        context = (await self.login.context())["contextToken"]
        challenge = await self.login.challenge(context, {"address": self.address, "mode": "login",
            "expectedUserId": None, "displayName": "spritz.skr"})
        result = await self.login.verify(context, self.signed(challenge))
        self.assertEqual(result["user"]["displayName"], "spritz.skr")
        self.assertNotEqual(result["user"]["handle"], "spritz.skr")
        self.assertNotIn("nameVerified", result["user"])

    async def test_wallet_label_hint_never_overwrites_a_returning_profile_custom_name(self):
        _, first = await self.signed_in()
        await self.db.execute("UPDATE users SET display_name=? WHERE id=?", ("My custom profile", first["user"]["id"]))
        context = (await self.login.context())["contextToken"]
        challenge = await self.login.challenge(context, {"address": self.address, "mode": "login",
            "expectedUserId": None, "displayName": "Wallet label.skr"})
        result = await self.login.verify(context, self.signed(challenge))
        self.assertEqual(result["user"]["id"], first["user"]["id"])
        self.assertEqual(result["user"]["displayName"], "My custom profile")

    async def test_wallet_label_hint_never_renames_an_explicitly_migrated_guest(self):
        guest = await self.auth.register("My old profile")
        context = (await self.login.context())["contextToken"]
        challenge = await self.login.challenge(context, {"address": self.address, "mode": "migrate",
            "expectedUserId": guest["user"]["id"], "displayName": "Wallet label.skr"}, guest["sessionToken"])
        result = await self.login.verify(context, self.signed(challenge), guest["sessionToken"])
        self.assertEqual(result["user"], guest["user"])

    async def test_messages_bind_domain_uri_chain_purpose_nonce_and_time(self):
        _, challenge = await self.prepare()
        for value in ("Domain: forecast.example", "URI: "+ORIGIN+"/auth/wallet", "Origin: "+ORIGIN,
                      "Address: "+self.address, "Chain: solana:devnet", "Purpose ID: "+PURPOSE,
                      "Proof roles: sign_in_forecast_profile, link_forecast_profile", challenge["challengeId"],
                      "Issued at: "+str(self.now), "Expires at: "+str(self.now+CHALLENGE_LIFETIME_MS)):
            self.assertIn(value, challenge["message"])

    async def test_no_signable_challenge_is_issued_without_bound_cookie(self):
        result = await self.login.context()
        self.assertEqual(set(result), {"contextToken", "expiresAt"})
        with self.assertRaises(AppError):
            await self.login.challenge(None, {"address": self.address, "mode": "login", "expectedUserId": None})
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM wallet_login_challenges"))["n"], 0)

    async def test_proof_cannot_be_transferred_between_browser_contexts(self):
        _, challenge = await self.prepare()
        other = (await self.login.context())["contextToken"]
        await self.assert_unchanged(lambda: self.login.verify(other, self.signed(challenge)))
        self.assertEqual(self.verifications, 0)

    async def test_wrong_message_key_and_signature_fail_without_writes(self):
        context, challenge = await self.prepare()
        for message in (challenge["message"].replace("solana:devnet", "solana:mainnet"),
                        challenge["message"].replace(ORIGIN, "https://attacker.example"),
                        challenge["message"].replace(PURPOSE, "transfer_assets")):
            await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge, message)))
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge, key=OTHER_KEY)))

    async def test_small_order_key_is_rejected_before_verifier(self):
        context = (await self.login.context())["contextToken"]
        for raw in (bytes(32), b"\1"+bytes(31)):
            with self.assertRaises(AppError):
                await self.login.challenge(context, {"address": encode_address(raw), "mode": "login", "expectedUserId": None})
        self.assertEqual(self.verifications, 0)

    async def test_consumed_signature_cannot_be_replayed(self):
        context, challenge = await self.prepare()
        await self.login.verify(context, self.signed(challenge))
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))

    async def test_expiry_is_checked_again_after_verification(self):
        context, challenge = await self.prepare()
        async def expire():
            self.now += CHALLENGE_LIFETIME_MS
        self.on_verify = expire
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))

    async def test_logout_during_verification_prevents_session_creation(self):
        context, challenge = await self.prepare()
        async def logout():
            await self.auth.logout(None, context)
        self.on_verify = logout
        with self.assertRaises(AppError):
            await self.login.verify(context, self.signed(challenge))
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM sessions"))["n"], 0)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 0)

    async def test_newer_challenge_cancels_earlier_proof(self):
        context, old = await self.prepare()
        _, latest = await self.prepare(context=context)
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(old)))
        await self.login.verify(context, self.signed(latest))

    async def test_stale_anonymous_challenge_cannot_overwrite_new_recovery_login(self):
        guest = await self.auth.register("Restored Guest")
        context = (await self.login.context())["contextToken"]
        original = self.db.batch
        recovered = []
        async def raced(statements):
            if any("INSERT INTO wallet_login_challenges" in sql for sql, _ in statements):
                recovered.append(await self.auth.login(guest["recoveryCode"], context))
            return await original(statements)
        self.db.batch = raced
        with self.assertRaises(AppError):
            await self.prepare(context=context)
        self.assertEqual((await self.auth.authenticate(recovered[0]["sessionToken"], context))["id"], guest["user"]["id"])
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM wallet_login_challenges"))["n"], 0)
        self.assertIsNone((await self.db.first("SELECT latest_challenge_id FROM wallet_login_contexts"))["latest_challenge_id"])

    async def test_stale_concurrent_challenge_cannot_replace_newer_committed_challenge(self):
        context = (await self.login.context())["contextToken"]
        original = self.db.batch
        newer = []
        async def raced(statements):
            if any("INSERT INTO wallet_login_challenges" in sql for sql, _ in statements) and not newer:
                self.db.batch = original
                newer.append((await self.prepare(context=context))[1])
            return await original(statements)
        self.db.batch = raced
        with self.assertRaises(AppError):
            await self.prepare(context=context)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM wallet_login_challenges"))["n"], 1)
        self.assertEqual((await self.db.first("SELECT latest_challenge_id FROM wallet_login_contexts"))["latest_challenge_id"], newer[0]["challengeId"])
        await self.login.verify(context, self.signed(newer[0]))

    async def test_newer_challenge_during_verification_wins(self):
        context, old = await self.prepare()
        async def newer():
            await self.prepare(context=context)
        self.on_verify = newer
        with self.assertRaises(AppError):
            await self.login.verify(context, self.signed(old))
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 0)

    async def test_same_wallet_concurrent_signup_creates_one_profile_and_two_grants(self):
        first_context, first = await self.prepare()
        second_context, second = await self.prepare()
        results = await asyncio.gather(self.login.verify(first_context, self.signed(first)),
                                       self.login.verify(second_context, self.signed(second)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, AppError) for result in results), 1)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 1)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM point_awards"))["n"], 2)
        self.assertEqual((await self.db.first("SELECT sum(available) AS n FROM point_accounts"))["n"], 1500)

    async def test_transaction_failure_rolls_back_profile_grants_identity_and_nonce(self):
        context, challenge = await self.prepare()
        self.connection.execute("CREATE TRIGGER force_login_failure BEFORE INSERT ON wallet_login_audit BEGIN SELECT RAISE(ABORT,'fixture'); END")
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))

    async def test_storage_guard_rechecks_context_after_all_application_reads(self):
        context, challenge = await self.prepare()
        original = self.db.batch
        async def raced(statements):
            if any("INSERT INTO wallet_login_audit" in sql for sql, _ in statements):
                await self.auth.logout(None, context)
            return await original(statements)
        self.db.batch = raced
        with self.assertRaises(AppError):
            await self.login.verify(context, self.signed(challenge))
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 0)
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_login_challenges"))["used_at"])

    async def test_verification_provider_outage_does_not_consume_nonce(self):
        context, challenge = await self.prepare()
        async def unavailable():
            raise RuntimeError("fixture provider unavailable")
        self.on_verify = unavailable
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))
        self.on_verify = None
        await self.login.verify(context, self.signed(challenge))

    async def test_guest_migration_preserves_uid_points_and_disables_old_code_and_sessions(self):
        guest = await self.auth.register("Existing Forecaster")
        older = await self.auth.login(guest["recoveryCode"])
        uid = guest["user"]["id"]
        context, challenge = await self.prepare(mode="migrate", session=guest["sessionToken"], expected=uid)
        self.assertIn("Existing profile: "+uid, challenge["message"])
        result = await self.login.verify(context, self.signed(challenge), guest["sessionToken"])
        self.assertEqual(result["user"], guest["user"])
        self.assertEqual(result["points"]["available"], 1500)
        self.assertIsNone(await self.auth.authenticate(guest["sessionToken"]))
        self.assertIsNone(await self.auth.authenticate(older["sessionToken"]))
        with self.assertRaises(AppError):
            await self.auth.login(guest["recoveryCode"])

    async def test_migration_requires_live_original_session_after_crypto(self):
        guest = await self.auth.register("Guest")
        context, challenge = await self.prepare(mode="migrate", session=guest["sessionToken"], expected=guest["user"]["id"])
        async def logout():
            await self.auth.logout(guest["sessionToken"])
        self.on_verify = logout
        with self.assertRaises(AppError):
            await self.login.verify(context, self.signed(challenge), guest["sessionToken"])
        self.assertIsNone(await self.db.first("SELECT * FROM wallet_identities"))
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_login_challenges"))["used_at"])

    async def test_migration_storage_guard_rechecks_original_session_after_application_reads(self):
        guest = await self.auth.register("Guest")
        context, challenge = await self.prepare(mode="migrate", session=guest["sessionToken"], expected=guest["user"]["id"])
        original = self.db.batch
        async def raced(statements):
            if any("INSERT INTO wallet_login_audit" in sql for sql, _ in statements):
                await self.auth.logout(guest["sessionToken"])
            return await original(statements)
        self.db.batch = raced
        with self.assertRaises(AppError):
            await self.login.verify(context, self.signed(challenge), guest["sessionToken"])
        self.assertIsNone(await self.db.first("SELECT * FROM wallet_identities"))
        self.assertIsNone((await self.db.first("SELECT used_at FROM wallet_login_challenges"))["used_at"])

    async def test_migration_rejects_missing_wrong_user_and_other_session(self):
        guest = await self.auth.register("Guest")
        other = await self.auth.register("Other Guest")
        for session, expected in ((None, guest["user"]["id"]), (guest["sessionToken"], other["user"]["id"])):
            with self.assertRaises(AppError):
                await self.prepare(mode="migrate", session=session, expected=expected)
        context, challenge = await self.prepare(mode="migrate", session=guest["sessionToken"], expected=guest["user"]["id"])
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge), other["sessionToken"]))

    async def test_wallet_owned_by_other_profile_is_never_merged(self):
        _, existing = await self.signed_in()
        guest = await self.auth.register("Guest")
        with self.assertRaises(AppError):
            await self.prepare(mode="migrate", session=guest["sessionToken"], expected=guest["user"]["id"])
        self.assertNotEqual(existing["user"]["id"], guest["user"]["id"])
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 2)

    async def test_existing_guest_link_is_adopted_only_after_valid_wallet_signature(self):
        guest = await self.auth.register("Old linked guest")
        proof = await self.wallets.challenge(guest["user"]["id"], self.address)
        await self.wallets.link(guest["user"]["id"], self.signed(proof))
        self.assertIsNotNone(await self.auth.login(guest["recoveryCode"]))
        context, challenge = await self.prepare()
        result = await self.login.verify(context, self.signed(challenge))
        self.assertEqual(result["user"], guest["user"])
        self.assertEqual(result["points"]["available"], 1500)
        with self.assertRaises(AppError):
            await self.auth.login(guest["recoveryCode"])

    async def test_unlinked_historical_wallet_cannot_create_another_profile(self):
        guest = await self.auth.register("Former owner")
        proof = await self.wallets.challenge(guest["user"]["id"], self.address)
        await self.wallets.link(guest["user"]["id"], self.signed(proof))
        await self.wallets.unlink(guest["user"]["id"])
        with self.assertRaises(AppError) as error:
            await self.prepare()
        self.assertEqual(error.exception.code, "wallet_identity_retired")
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 1)

    async def test_login_wallet_cannot_be_unlinked_or_replaced_using_only_session(self):
        _, result = await self.signed_in()
        uid = result["user"]["id"]
        await self.assert_unchanged(lambda: self.wallets.unlink(uid))
        await self.assert_unchanged(lambda: self.wallets.challenge(uid, self.other_address))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM wallet_links WHERE user_id=?", (uid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE wallet_links SET address=? WHERE user_id=?", (self.other_address, uid))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM wallet_identities WHERE user_id=?", (uid,))

    async def test_stale_verify_cookie_and_logout_cannot_restore_old_session(self):
        context, first = await self.signed_in()
        _, challenge = await self.prepare(context=context)
        self.assertIsNotNone(await self.auth.authenticate(first["sessionToken"], context))
        second = await self.login.verify(context, self.signed(challenge))
        self.assertIsNone(await self.auth.authenticate(first["sessionToken"], context))
        self.assertIsNotNone(await self.auth.authenticate(second["sessionToken"], context))
        await self.auth.logout(second["sessionToken"], context)
        for result in (first, second):
            self.assertIsNone(await self.auth.authenticate(result["sessionToken"], context))

    async def test_context_cancel_revokes_active_session_and_all_pending_signatures(self):
        context, result = await self.signed_in()
        _, challenge = await self.prepare(context=context)
        await self.login.cancel(context)
        self.assertIsNone(await self.auth.authenticate(result["sessionToken"], context))
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))
        new_context = await self.login.context(context)
        self.assertNotEqual(new_context["contextToken"], context)

    async def test_logout_uses_session_context_even_if_context_cookie_was_lost(self):
        context, result = await self.signed_in()
        _, challenge = await self.prepare(context=context)
        await self.auth.logout(result["sessionToken"])
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))
        self.assertIsNone(await self.auth.authenticate(result["sessionToken"], context))

    async def test_mnemonic_or_whitespace_recovery_input_never_reaches_database(self):
        original = self.db.first
        lookups = []
        async def tracked(sql, params=()):
            lookups.append(sql)
            return await original(sql, params)
        self.db.first = tracked
        for value in ("word "*12, " "+"a"*32, "a"*32+"\n", ["a"*32]):
            with self.assertRaises(AppError):
                await self.auth.login(value)
        self.assertEqual(lookups, [])

    async def test_new_recovery_session_is_browser_bound_and_revokes_pending_wallet_proofs(self):
        guest = await self.auth.register("Restored Guest")
        context, challenge = await self.prepare()
        result = await self.auth.login(guest["recoveryCode"], context)
        self.assertEqual((await self.auth.authenticate(result["sessionToken"], context))["id"], guest["user"]["id"])
        self.assertIsNone(await self.auth.authenticate(result["sessionToken"]))
        self.assertIsNone(await self.auth.authenticate(result["sessionToken"], self.token()))
        await self.assert_unchanged(lambda: self.login.verify(context, self.signed(challenge)))

    async def test_delayed_recovery_cookie_cannot_override_new_wallet_session(self):
        guest = await self.auth.register("Restored Guest")
        context = (await self.login.context())["contextToken"]
        recovery = await self.auth.login(guest["recoveryCode"], context)
        # This result's cookie is deliberately delivered only after the newer
        # wallet request has committed; server authentication must reject it.
        _, challenge = await self.prepare(context=context)
        wallet = await self.login.verify(context, self.signed(challenge))
        self.assertIsNone(await self.auth.authenticate(recovery["sessionToken"], context))
        self.assertEqual((await self.auth.authenticate(wallet["sessionToken"], context))["id"], wallet["user"]["id"])
        self.assertNotEqual(wallet["user"]["id"], guest["user"]["id"])

    async def test_delayed_wallet_cookie_cannot_override_new_recovery_session(self):
        guest = await self.auth.register("Restored Guest")
        context, wallet = await self.signed_in()
        recovery = await self.auth.login(guest["recoveryCode"], context)
        self.assertIsNone(await self.auth.authenticate(wallet["sessionToken"], context))
        self.assertEqual((await self.auth.authenticate(recovery["sessionToken"], context))["id"], guest["user"]["id"])

    async def test_new_wallet_challenge_during_recovery_lookup_invalidates_recovery_transaction(self):
        guest = await self.auth.register("Restored Guest")
        context = (await self.login.context())["contextToken"]
        original = self.db.first
        async def raced(sql, params=()):
            row = await original(sql, params)
            if sql.startswith("SELECT * FROM users WHERE recovery_hash="):
                await self.prepare(context=context)
            return row
        self.db.first = raced
        with self.assertRaises(AppError) as error:
            await self.auth.login(guest["recoveryCode"], context)
        self.assertEqual(error.exception.code, "wallet_login_changed")
        self.assertEqual((await original("SELECT count(*) AS n FROM sessions WHERE context_hash IS NOT NULL"))["n"], 0)

    async def test_wallet_commit_during_recovery_lookup_is_guarded_even_when_nonce_id_is_unchanged(self):
        guest = await self.auth.register("Restored Guest")
        context, challenge = await self.prepare()
        original = self.db.first
        wallet_results = []
        async def raced(sql, params=()):
            row = await original(sql, params)
            if sql.startswith("SELECT * FROM users WHERE recovery_hash="):
                wallet_results.append(await self.login.verify(context, self.signed(challenge)))
            return row
        self.db.first = raced
        with self.assertRaises(AppError) as error:
            await self.auth.login(guest["recoveryCode"], context)
        self.assertEqual(error.exception.code, "wallet_login_changed")
        self.assertIsNotNone(await self.auth.authenticate(wallet_results[0]["sessionToken"], context))

    async def test_recovery_conversion_during_lookup_never_issues_a_legacy_session(self):
        guest = await self.auth.register("Converting Guest")
        context, challenge = await self.prepare(mode="migrate", session=guest["sessionToken"], expected=guest["user"]["id"])
        original = self.db.first
        async def raced(sql, params=()):
            row = await original(sql, params)
            if sql.startswith("SELECT * FROM users WHERE recovery_hash="):
                await self.login.verify(context, self.signed(challenge), guest["sessionToken"])
            return row
        self.db.first = raced
        with self.assertRaises(AppError) as error:
            await self.auth.login(guest["recoveryCode"], context)
        self.assertEqual(error.exception.code, "invalid_recovery_code")
        self.assertEqual((await original("SELECT count(*) AS n FROM sessions"))["n"], 1)

    async def test_canceled_recovery_context_cannot_issue_session(self):
        guest = await self.auth.register("Guest")
        context = (await self.login.context())["contextToken"]
        await self.login.cancel(context)
        await self.assert_unchanged(lambda: self.auth.login(guest["recoveryCode"], context))

    async def test_cancel_preserves_ancient_unbound_guest_session(self):
        guest = await self.auth.register("Guest")
        context, _ = await self.prepare(mode="migrate", session=guest["sessionToken"], expected=guest["user"]["id"])
        await self.login.cancel(context)
        self.assertIsNotNone(await self.auth.authenticate(guest["sessionToken"]))

    async def test_malformed_challenge_and_verify_contracts_are_rejected(self):
        context = (await self.login.context())["contextToken"]
        for body in ({"address": self.address, "mode": [], "expectedUserId": None},
                     {"address": self.address, "mode": "login"},
                     {"address": self.address, "mode": "login", "expectedUserId": None, "message": "attacker"}):
            await self.assert_unchanged(lambda: self.login.challenge(context, body))
        _, challenge = await self.prepare(context=context)
        body = {**self.signed(challenge), "message": "attacker"}
        await self.assert_unchanged(lambda: self.login.verify(context, body))

    async def test_actual_signed_roles_are_retained_without_legacy_proof_confusion(self):
        context, challenge = await self.prepare()
        result = await self.login.verify(context, self.signed(challenge))
        retained = await self.db.first("SELECT * FROM wallet_challenges WHERE id=?", (challenge["challengeId"],))
        login_proof = await self.db.first("SELECT * FROM wallet_login_challenges WHERE id=?", (challenge["challengeId"],))
        audit = json.loads((await self.db.first("SELECT body FROM wallet_login_audit"))["body"])
        self.assertEqual(retained["message"], challenge["message"])
        self.assertEqual(retained["purpose"], "link_forecast_profile")
        self.assertIn(retained["purpose"], challenge["message"])
        self.assertEqual(login_proof["purpose"], PURPOSE)
        self.assertEqual(audit["proofRoles"], ["sign_in_forecast_profile", "link_forecast_profile"])
        commitment = self.hash("wallet-login-target:"+challenge["challengeId"]+":"+result["user"]["id"])
        self.assertIn("Profile commitment: "+commitment, retained["message"])
        self.assertEqual(audit["messageSha256"], self.hash(challenge["message"]))
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM wallet_audit"))["n"], 0)
        with self.assertRaises(AppError):
            await self.wallets.link(result["user"]["id"], self.signed(challenge))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE wallet_login_challenges SET message='wrong purpose' WHERE id=?", (challenge["challengeId"],))


class WalletLoginMigrationTests(unittest.TestCase):
    def test_backfill_keeps_current_owner_and_tombstones_unlinked_keys_without_disabling_recovery(self):
        connection = sqlite3.connect(":memory:")
        try:
            for path in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
                if path.name >= "0016":
                    break
                connection.executescript(path.read_text())
            for uid in ("old", "current"):
                connection.execute("INSERT INTO users VALUES(?,?,?,?,?)", (uid, uid, uid, uid+"-recovery", 1))
            for key, uid in (("retired", "old"), ("reused", "old")):
                connection.execute("INSERT INTO wallet_audit(id,user_id,address,kind,body,created_at) VALUES(?,?,?,'wallet_unlinked','{}',1)",
                                   (key, uid, key))
            connection.execute("INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at,used_at) "
                               "VALUES('legacy','current','reused','https://forecast.example','link_forecast_profile','solana:devnet','retained',1,2,1)")
            connection.execute("INSERT INTO wallet_links VALUES('current','reused','solana:devnet',1,'legacy',1)")
            connection.executescript((ROOT / "apps/web/migrations/0016_wallet_login.sql").read_text())
            self.assertEqual(connection.execute("SELECT user_id,status,converted_at FROM wallet_identities WHERE address='retired'").fetchone(),
                             ("old", "tombstone", None))
            self.assertEqual(connection.execute("SELECT user_id,status,converted_at FROM wallet_identities WHERE address='reused'").fetchone(),
                             ("current", "active", None))
            self.assertEqual(connection.execute("SELECT recovery_hash FROM users WHERE id='current'").fetchone()[0], "current-recovery")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()
