"""Canonical operator seeding has its own bounded quota inside the global cap."""

import unittest

from forecast_application.errors import AppError
from forecast_application.service import (
    DAY_MS,
    MAX_DAILY_AI_CALLS,
    MAX_DAILY_RISK_SEEDS,
    MAX_DAILY_USER_AI_CALLS,
)

from tests import test_web_application as fixtures


class RiskSeedBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApplicationTests.asyncSetUp
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)

    async def test_operator_seed_keeps_user_quota_and_idempotent_published_result(self):
        for _ in range(MAX_DAILY_USER_AI_CALLS):
            await self.app.rate_limit("ai:user:system_editorial", MAX_DAILY_USER_AI_CALLS, DAY_MS)
        question = "Will this precisely defined future canonical risk event occur?"
        first = await self.app.seed(question, uncertainty_band=None, canonical_risk=True)
        again = await self.app.seed(question, uncertainty_band=None, canonical_risk=True)
        self.assertEqual(first["forecast"]["id"], again["forecast"]["id"])
        quota = await self.db.first("SELECT count FROM rate_limits WHERE scope='ai:risk-seed'")
        self.assertEqual(quota["count"], 1)
        self.assertEqual((await self.db.first("SELECT count FROM rate_limits WHERE scope='ai:user:system_editorial'"))["count"], MAX_DAILY_USER_AI_CALLS)
        with self.assertRaises(AppError) as error:
            await self.app.compile_forecast("system_editorial", "Will a normal user question still obey its own limit?")
        self.assertEqual(error.exception.status, 429)

    async def test_operator_quota_and_global_quota_each_reject_before_provider(self):
        for scope, limit in (("ai:risk-seed", MAX_DAILY_RISK_SEEDS), ("ai:global", MAX_DAILY_AI_CALLS)):
            with self.subTest(scope=scope):
                await self.db.execute("DELETE FROM rate_limits")
                for _ in range(limit):
                    await self.app.rate_limit(scope, limit, DAY_MS)
                calls = self.ai.calls
                with self.assertRaises(AppError) as error:
                    await self.app.seed("Will another defined canonical risk event occur?", uncertainty_band=None, canonical_risk=True)
                self.assertEqual(error.exception.status, 429)
                self.assertEqual(self.ai.calls, calls)
                self.assertEqual((await self.db.first("SELECT count(*) n FROM ai_leases"))["n"], 0)


class CanonicalSeriesCompileTests(unittest.IsolatedAsyncioTestCase):
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.ApplicationTests.asyncSetUp(self)

    async def test_only_operator_canonical_seeds_declare_distinct_measurement_windows(self):
        await self.app.compile_forecast(self.uid, "Will Acme officially announce Product X before the deadline?")
        self.assertFalse(self.ai.last_distinct_windows)
        await self.app.seed("Will a defined canonical risk event occur within the published window?",
                            uncertainty_band=None, canonical_risk=True)
        self.assertTrue(self.ai.last_distinct_windows)
        await self.app.seed("Will an ordinary editorial event occur before the deadline?")
        self.assertFalse(self.ai.last_distinct_windows)
