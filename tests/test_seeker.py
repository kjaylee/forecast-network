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
