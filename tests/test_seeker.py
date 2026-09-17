"""Seeker Genesis Token verification: mainnet parsing and the account-bound record."""

from __future__ import annotations

import unittest

from forecast_application.errors import AppError
from forecast_application.seeker import (
    SGT_GROUP,
    SKR_MINT,
    SeekerVerification,
    candidate_mints,
    genesis_member,
    skr_atomic,
    skr_display,
)

from tests import test_web_application as fixtures

SGT_MINT = "9isJna18xnaXdwykFmWM6V3yLZR5yYgLHhaZrFqqS3nE"


def token_account(mint: str, amount: str, decimals: int) -> dict:
    return {"pubkey": "x", "account": {"data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": {"amount": amount, "decimals": decimals}}}}}}


def mint_account(mint: str, group: str | None, number: int | None = 75501) -> dict:
    extensions = [{"extension": "metadataPointer", "state": {}}]
    if group:
        extensions.append({"extension": "tokenGroupMember", "state": {"group": group, "mint": mint, "memberNumber": number}})
    return {"data": {"parsed": {"info": {"decimals": 0, "supply": "1", "extensions": extensions}}}}


class ParsingTests(unittest.TestCase):
    def test_only_single_indivisible_units_are_candidates(self) -> None:
        accounts = [token_account(SGT_MINT, "1", 0), token_account("pump", "885901241", 6), token_account("nft2", "2", 0)]
        self.assertEqual(candidate_mints(accounts), [SGT_MINT])
        self.assertEqual(candidate_mints([]), [])
        self.assertEqual(candidate_mints([{"account": {"data": "garbage"}}]), [])

    def test_candidate_mints_are_unique_in_first_seen_order(self) -> None:
        accounts = [token_account(SGT_MINT, "1", 0), token_account("other", "1", 0), token_account(SGT_MINT, "1", 0)]
        self.assertEqual(candidate_mints(accounts), [SGT_MINT, "other"])

    def test_genesis_member_requires_the_sgt_group(self) -> None:
        mints = ["other", SGT_MINT]
        accounts = [mint_account("other", "SomeOtherGroup111111111111111111111111111111"), mint_account(SGT_MINT, SGT_GROUP)]
        self.assertEqual(genesis_member(accounts, mints), (SGT_MINT, 75501))
        self.assertIsNone(genesis_member([mint_account("other", None)], ["other"]))
        # A member record that names a different mint is not accepted for this mint.
        forged = mint_account("forged", SGT_GROUP)
        forged["data"]["parsed"]["info"]["extensions"][1]["state"]["mint"] = SGT_MINT
        self.assertIsNone(genesis_member([forged], ["forged"]))

    def test_skr_balance_sums_only_skr_and_formats_six_decimals(self) -> None:
        accounts = [token_account(SKR_MINT, "325515", 6), token_account(SKR_MINT, "1000000", 6), token_account("pump", "5", 6)]
        self.assertEqual(skr_atomic(accounts), 1325515)
        self.assertEqual(skr_display(1325515), "1.325515")
        self.assertEqual(skr_display(0), "0")
        self.assertEqual(skr_display(8_701_000_000), "8701")


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApplicationTests.asyncSetUp
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)

    def rpc_with(self, holdings: dict[str, tuple[str, int]], group: str | None = SGT_GROUP):
        calls = []

        async def rpc(method: str, params: list) -> dict:
            calls.append((method, params))
            if method == "getTokenAccountsByOwner" and "programId" in params[1]:
                return {"context": {"slot": 100}, "value": [token_account(m, a, d) for m, (a, d) in holdings.items() if d == 0]}
            if method == "getTokenAccountsByOwner":
                return {"context": {"slot": 101}, "value": [token_account(m, a, d) for m, (a, d) in holdings.items() if m == SKR_MINT]}
            if method == "getMultipleAccounts":
                return {"context": {"slot": 100}, "value": [mint_account(m, group if m == SGT_MINT else None) for m in params[0]]}
            raise AssertionError(method)
        return rpc, calls

    async def seeker(self, rpc):
        service = SeekerVerification(self.db, rpc=rpc, now_ms=lambda: self.now, rate_limit=self.app.rate_limit)
        await self.db.execute("INSERT INTO wallet_identities(address,user_id,status,created_at,converted_at) VALUES(?,?,'active',?,?)",
                              ("zit7RTKXGZryp7LAUCVxaN1x4JZMF6rBpBhXYCGcVfU", self.uid, self.now, self.now))
        return service

    async def legacy_seeker(self, rpc):
        address = "zit7RTKXGZryp7LAUCVxaN1x4JZMF6rBpBhXYCGcVfU"
        await self.db.execute("INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at) "
                              "VALUES('legacy-proof',?,?,?,'link_forecast_profile','solana:devnet','test',?,?)",
                              (self.uid, address, "https://forecast.example", self.now, self.now+1000))
        await self.db.execute("INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) "
                              "VALUES(?,?,'solana:devnet',?,'legacy-proof',1)", (self.uid, address, self.now))
        return SeekerVerification(self.db, rpc=rpc, now_ms=lambda: self.now, rate_limit=self.app.rate_limit)

    async def test_a_seeker_owner_is_recorded_with_member_number_and_skr(self) -> None:
        rpc, calls = self.rpc_with({SGT_MINT: ("1", 0), SKR_MINT: ("325515", 6)})
        service = await self.seeker(rpc)
        result = await service.verify(self.uid)
        self.assertEqual(result["memberNumber"], 75501)
        self.assertEqual(result["skr"], "0.325515")
        self.assertTrue(result["verified"])
        self.assertEqual(calls[0][1][0], "zit7RTKXGZryp7LAUCVxaN1x4JZMF6rBpBhXYCGcVfU")
        self.assertEqual(await service.public_badge(self.uid), {"memberNumber": 75501})
        self.assertIsNone(await service.public_badge(self.other))

    async def test_a_wallet_without_a_genesis_token_is_refused(self) -> None:
        rpc, _ = self.rpc_with({SKR_MINT: ("1000000", 6)})
        service = await self.seeker(rpc)
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_not_found")
        self.assertIsNone(await service.status(self.uid))

    async def test_a_genesis_token_after_the_first_hundred_candidates_is_found(self) -> None:
        holdings = {f"other-{i}": ("1", 0) for i in range(200)}
        holdings[SGT_MINT] = ("1", 0)
        rpc, calls = self.rpc_with(holdings)
        service = await self.seeker(rpc)
        self.assertTrue((await service.verify(self.uid))["verified"])
        batches = [params[0] for method, params in calls if method == "getMultipleAccounts"]
        self.assertEqual(list(map(len, batches)), [100, 100, 1])
        self.assertEqual([mint for batch in batches for mint in batch], list(holdings))

    async def test_negative_refresh_hides_badge_but_preserves_evidence_and_mint_claim(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0), SKR_MINT: ("123456", 6)})
        service = await self.seeker(rpc)
        await service.verify(self.uid)
        before = await self.db.first("SELECT * FROM seeker_verifications WHERE user_id=?", (self.uid,))
        self.now += 1
        service.rpc, _ = self.rpc_with({})
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_not_found")
        self.assertIsNone(await service.status(self.uid))
        self.assertIsNone(await service.public_badge(self.uid))
        after = await self.db.first("SELECT * FROM seeker_verifications WHERE user_id=?", (self.uid,))
        for field in ("address", "genesis_mint", "member_number", "verified_at", "refreshed_at", "skr_atomic", "slot"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["invalidated_at"], self.now)
        await self.db.execute("INSERT INTO wallet_identities(address,user_id,status,created_at,converted_at) VALUES(?,?,'active',?,?)",
                              ("4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T", self.other, self.now, self.now))
        service.rpc = rpc
        with self.assertRaises(AppError) as caught:
            await service.verify(self.other)
        self.assertEqual(caught.exception.code, "seeker_already_claimed")
        self.now += 1
        self.assertTrue((await service.verify(self.uid))["verified"])
        restored = await self.db.first("SELECT * FROM seeker_verifications WHERE user_id=?", (self.uid,))
        self.assertIsNone(restored["invalidated_at"])
        self.assertEqual(restored["verified_at"], before["verified_at"])

    async def test_failed_or_incomplete_rpc_refresh_preserves_badge(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        expected = await service.verify(self.uid)
        for malformed in (None, {}, {"value": None}, {"value": "bad"}, {"value": [None]}):
            async def incomplete(method, params):
                return malformed
            service.rpc = incomplete
            with self.assertRaises(AppError) as caught:
                await service.verify(self.uid)
            self.assertEqual(caught.exception.code, "seeker_rpc_unavailable")
            self.assertEqual(await service.status(self.uid), expected)
            self.assertIsNotNone(await service.public_badge(self.uid))
            self.now += 3_600_000

    async def test_transport_error_in_later_mint_batch_preserves_badge(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        expected = await service.verify(self.uid)
        holdings = {f"other-{i}": ("1", 0) for i in range(101)}
        many, _ = self.rpc_with(holdings)
        async def broken(method, params):
            if method == "getMultipleAccounts" and len(params[0]) == 1:
                raise RuntimeError("transport")
            return await many(method, params)
        service.rpc = broken
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_rpc_unavailable")
        self.assertEqual(await service.status(self.uid), expected)

    async def test_incomplete_mint_responses_cannot_invalidate_existing_evidence(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        expected = await service.verify(self.uid)
        for accounts in ([], [None], [{"data": {"parsed": {"info": {"unexpected": True}}}}],
                         [mint_account(SGT_MINT, None), mint_account("extra", None)]):
            async def incomplete(method, params):
                return {"value": accounts} if method == "getMultipleAccounts" else await rpc(method, params)
            service.rpc = incomplete
            with self.assertRaises(AppError) as caught:
                await service.verify(self.uid)
            self.assertEqual(caught.exception.code, "seeker_rpc_unavailable")
            self.assertEqual(await service.status(self.uid), expected)

    async def test_all_candidate_batches_must_be_negative_before_invalidation(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        await service.verify(self.uid)
        service.rpc, calls = self.rpc_with({f"other-{i}": ("1", 0) for i in range(201)})
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_not_found")
        self.assertEqual([len(params[0]) for method, params in calls if method == "getMultipleAccounts"], [100, 100, 1])
        self.assertIsNone(await service.status(self.uid))

    async def test_badge_requires_the_same_current_wallet(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.legacy_seeker(rpc)
        await service.verify(self.uid)
        await self.db.execute("DELETE FROM wallet_links WHERE user_id=?", (self.uid,))
        self.assertIsNone(await service.status(self.uid))
        self.assertIsNone(await service.public_badge(self.uid))

    async def test_wallet_change_during_rpc_does_not_publish_a_badge(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.legacy_seeker(rpc)
        async def changed(method, params):
            result = await rpc(method, params)
            if method == "getMultipleAccounts":
                await self.db.execute("DELETE FROM wallet_links WHERE user_id=?", (self.uid,))
            return result
        service.rpc = changed
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_verification_changed")
        self.assertIsNone(await self.db.first("SELECT * FROM seeker_verifications WHERE user_id=?", (self.uid,)))

    async def test_old_negative_refresh_cannot_invalidate_a_newer_positive_result(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        await service.verify(self.uid)
        newer = type(service)(self.db, rpc=rpc, now_ms=lambda: self.now, rate_limit=self.app.rate_limit)
        async def superseded(method, params):
            if "programId" in params[1]:
                await newer.verify(self.uid)
            return {"context": {"slot": 101}, "value": []}
        service.rpc = superseded
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_verification_changed")
        self.assertTrue((await service.status(self.uid))["verified"])

    async def test_old_positive_refresh_cannot_restore_newer_invalidated_evidence(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        await service.verify(self.uid)
        empty, _ = self.rpc_with({})
        newer = type(service)(self.db, rpc=empty, now_ms=lambda: self.now, rate_limit=self.app.rate_limit)
        async def superseded(method, params):
            if "programId" in params[1]:
                with self.assertRaises(AppError) as caught:
                    await newer.verify(self.uid)
                self.assertEqual(caught.exception.code, "seeker_not_found")
            return await rpc(method, params)
        service.rpc = superseded
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "seeker_verification_changed")
        self.assertIsNone(await service.status(self.uid))

    async def test_one_genesis_token_cannot_badge_two_accounts(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = await self.seeker(rpc)
        await service.verify(self.uid)
        await self.db.execute("INSERT INTO wallet_identities(address,user_id,status,created_at,converted_at) VALUES(?,?,'active',?,?)",
                              ("4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T", self.other, self.now, self.now))
        with self.assertRaises(AppError) as caught:
            await service.verify(self.other)
        self.assertEqual(caught.exception.code, "seeker_already_claimed")

    async def test_rpc_failure_is_a_retryable_service_error_and_no_rpc_means_unavailable(self) -> None:
        async def broken(method, params):
            raise RuntimeError("socket")
        service = await self.seeker(broken)
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "seeker_rpc_unavailable")
        unavailable = SeekerVerification(self.db, rpc=None, now_ms=lambda: self.now, rate_limit=self.app.rate_limit)
        self.assertFalse(unavailable.available)
        with self.assertRaises(AppError):
            await unavailable.verify(self.uid)

    async def test_without_a_wallet_verification_asks_for_sign_in(self) -> None:
        rpc, _ = self.rpc_with({SGT_MINT: ("1", 0)})
        service = SeekerVerification(self.db, rpc=rpc, now_ms=lambda: self.now, rate_limit=self.app.rate_limit)
        with self.assertRaises(AppError) as caught:
            await service.verify(self.uid)
        self.assertEqual(caught.exception.code, "wallet_required")
