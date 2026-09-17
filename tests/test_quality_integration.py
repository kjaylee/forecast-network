"""Actual migrated SQLite/API proof for quality views, ranking and cohorts."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from forecast_application import discovery, reputation
from forecast_domain.models import Category, Outcome

from tests import test_web_application as application_tests


class QualityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = application_tests.ApplicationTests.asyncSetUp
    asyncTearDown = application_tests.ApplicationTests.asyncTearDown
    random_token = application_tests.ApplicationTests.random_token
    token_hash = staticmethod(application_tests.ApplicationTests.token_hash)
    publish = application_tests.ApplicationTests.publish
    profile_ledger_fixture = application_tests.ApplicationTests.profile_ledger_fixture
    _points_forecast = application_tests.ApplicationTests._points_forecast

    async def test_actual_profile_consistency_and_category_qualification(self):
        for index in range(20):
            age = (15, 45, 75)[index % 3]
            await self.profile_ledger_fixture(index, confidence=90, finalized_at=self.now-age*discovery.DAY_MS-index*1000)
        profile = (await self.app.me(self.other))["reputation"]
        public = (await self.app.creator(self.other))["creator"]["reputation"]
        self.assertEqual(profile["consistencyScore"], 1.0)
        self.assertEqual(profile["expertise"][0]["status"], "qualified")
        self.assertEqual(profile["expertise"][0]["category"], "technology")
        self.assertEqual(public["expertise"], profile["expertise"])
        self.assertEqual(profile["eligibleHistoryCount"], 20)
        # Existing signed profile remains exactly v1 with no new mutable-display fields.
        card = await self.app.profile_card(self.other)
        self.assertEqual(card["methodology"]["version"], "profile-card-v1")
        self.assertNotIn("expertise", json.loads(card["canonicalJson"]))

    async def test_detail_sql_cohorts_match_pure_exact_member_policy(self):
        for index in range(20):
            await self.profile_ledger_fixture(index, confidence=90, finalized_at=self.now-(index+1)*discovery.DAY_MS)
        original = application_tests.fixtures.specification
        with patch.object(application_tests.fixtures, "specification", side_effect=lambda **kw: original(**{**kw, "category": Category.SCIENCE})):
            for index in range(20, 40):
                await self.profile_ledger_fixture(index, user_id=self.uid, confidence=100,
                                                  finalized_at=self.now-(index+1)*discovery.DAY_MS)
        card = await self.publish()
        await self.app.submit_forecast(self.other, card["id"], "YES", 80, card["revision"], "quality-vote-alice")
        await self.app.submit_forecast(self.uid, card["id"], "NO", 80, card["revision"]+1, "quality-vote-bob")
        history = await self.db.all("SELECT * FROM forecast_quality_history")
        submissions = await self.db.all("SELECT *,1 AS eligible,submitted_at AS eligibility_at FROM eligible_user_forecasts WHERE forecast_id=?", (card["id"],))
        expected = reputation.cohort_statistics(submissions, history, forecast_id=card["id"], category="technology", as_of_ms=self.now)
        detail = (await self.app.forecast_detail(card["id"]))["forecast"]
        for cohort in ("crowd", "top", "expert"):
            self.assertEqual(detail[cohort], expected[cohort])
        self.assertEqual(detail["expert"], {"probability": 80.0, "count": 1})
        self.assertEqual(detail["top"], {"probability": 20.0, "count": 1})
        self.assertEqual(detail["quality"]["version"], discovery.DISCOVERY_VERSION)
        listed = (await self.app.list_forecasts())["items"][0]
        self.assertEqual(listed["expert"], detail["expert"])

    async def test_finalized_target_cannot_qualify_itself(self):
        target = None
        for index in range(20):
            target = await self.profile_ledger_fixture(index, confidence=90,
                finalized_at=self.now-(index+1)*discovery.DAY_MS)
        profile = await self.app.reputation(self.other)
        self.assertTrue(profile["expertise"][0]["qualified"])
        detail = (await self.app.forecast_detail(target.forecast_id))["forecast"]
        self.assertEqual(detail["expert"], {"probability": None, "count": 0})

    async def test_future_invalid_and_held_results_do_not_qualify(self):
        for index in range(19):
            await self.profile_ledger_fixture(index, confidence=90,
                finalized_at=self.now-(index+1)*discovery.DAY_MS)
        await self.profile_ledger_fixture(20, confidence=100, finalized_at=self.now+discovery.DAY_MS)
        await self.profile_ledger_fixture(21, outcome=Outcome.INVALID, finalized_at=self.now-discovery.DAY_MS)
        profile = await self.app.reputation(self.other)
        self.assertEqual(profile["eligibleHistoryCount"], 19)
        self.assertFalse(profile["expertise"][0]["qualified"])
        row = await self.publish()
        await self.app.participation_holds.change(row["id"], {
            "action": "hold", "expectedRevision": 0, "expectedHoldId": None,
            "specificationHash": row["specificationHash"], "reason": "known_outcome_review",
            "evidenceUrl": "https://example.org/announcement", "idempotencyKey": "quality-hold-history"})
        listing = await self.app.list_forecasts()
        self.assertNotIn(row["id"], listing["dailyIds"])
        self.assertFalse(next(item for item in listing["items"] if item["id"] == row["id"])["quality"]["active"])

    async def test_full_inventory_sql_ranking_before_pagination_and_golden_parity(self):
        for index in range(35):
            await self.profile_ledger_fixture(index, finalized_at=self.now-(index+1)*discovery.DAY_MS,
                                               outcome=Outcome.INVALID if index % 4 == 0 else Outcome.YES)
        for index in range(3):
            card = await self._points_forecast(f"quality-{index}")
            await self.db.execute("UPDATE forecasts SET share_count=? WHERE id=?", (1000000*index, card["id"]))
        rows = await self.db.all(discovery.candidate_sql(self.now, self.other))
        for row in rows:
            score = discovery.score_forecast(row, as_of_ms=self.now)
            self.assertEqual(row["quality_score"], score["scoreBp"])
            self.assertEqual(row["discovery_tie"], discovery.tie_break(row["id"], self.now, self.other))
        expected = sorted(rows, key=lambda row: (-int(discovery.score_forecast(row, as_of_ms=self.now)["active"]),
                            -discovery.score_forecast(row, as_of_ms=self.now)["scoreBp"],
                            discovery.tie_break(row["id"], self.now, self.other), row["id"]))
        page1 = await self.app.list_forecasts(self.other)
        page2 = await self.app.list_forecasts(self.other, cursor=page1["nextCursor"])
        actual = [row["id"] for row in page1["items"]+page2["items"]]
        self.assertEqual(actual, [row["id"] for row in expected])
        self.assertEqual(len(actual), 38)
        self.assertIsNone(page2["nextCursor"])
        all_daily = discovery.recommendations(rows, as_of_ms=self.now, user_id=self.other)
        self.assertEqual(page1["dailyIds"], [row["id"] for row in all_daily])
        self.assertEqual(page1["dailyRecommendations"][0]["quality"]["version"], discovery.DISCOVERY_VERSION)

    async def test_current_ai_count_cannot_include_future_prediction(self):
        self.ai.ai_forecast = {"probability": 55, "provider": "test", "model": "test", "asOf": self.now+1000}
        card = await self.publish()
        detail = (await self.app.forecast_detail(card["id"]))["forecast"]
        self.assertEqual(detail["ai"]["count"], 0)
        self.assertIsNone(detail["ai"]["probability"])
