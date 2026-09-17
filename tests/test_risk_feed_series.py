"""Recurring episodes publish through the real seed path ahead of their start and bind deterministically."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from forecast_application.risk_feed_series import (
    configure_series,
    create_due_episodes,
    episode_question,
    next_episode_start,
)
from forecast_application.risk_feed_v2 import (
    admit_definition,
    admit_profile,
    operational_bindings_v2,
)
from forecast_domain.errors import ValidationError
from forecast_domain.models import Category
from forecast_domain.risk_feed import RiskFeedSeriesV2
from forecast_domain.serialization import content_hash, loads

from tests import test_web_application as fixtures
from tests.test_risk_feed_v2_contract import golden_definition, golden_profile, golden_series
from tests.test_web_ai import specification

HOUR = 3_600_000


def spell(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RiskFeedSeriesTests(unittest.IsolatedAsyncioTestCase):
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.ApplicationTests.asyncSetUp(self)
        self.definition, self.profile = golden_definition(), golden_profile()
        await admit_definition(self.db, feed_id="risk-v2", definition=self.definition, approved_by="a", now_ms=self.now)
        await admit_profile(self.db, feed_id="risk-v2", profile=self.profile, approved_by="a", now_ms=self.now)
        self.series = replace(golden_series(), feed_id="risk-v2")
        self.seeded = []

    async def seed(self, question):
        """The real operator seed path with the deterministic compile adapter honoring the window."""
        self.seeded.append(question)
        end = int(datetime.strptime(question.split(", ")[1].split(")")[0], "%Y-%m-%dT%H:%M:%SZ")
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)
        original_spec = fixtures.fixtures.specification
        policy = specification().source_policy
        with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw: original_spec(
                **{**kw, "source_policy": policy, "close_at_ms": end, "category": Category.CRYPTO})):
            return await self.app.seed(question, uncertainty_band=None, canonical_risk=True)

    async def test_series_needs_admitted_records_and_first_start_is_aligned_with_lead(self):
        with self.assertRaises(ValidationError):
            await configure_series(self.db, series=replace(self.series, definition_hash="0" * 64), enabled=True,
                                   configured_by="a", now_ms=self.now)
        digest = await configure_series(self.db, series=self.series, enabled=True, configured_by="a", now_ms=self.now)
        self.assertEqual(digest, content_hash(self.series))
        row = await self.db.first("SELECT * FROM risk_feed_series_v2 WHERE series_id=?", (self.series.series_id,))
        self.assertEqual(loads(RiskFeedSeriesV2, row["series_json"]), self.series)
        first = next_episode_start(self.series, latest_start_ms=None, now_ms=self.now)
        self.assertEqual(first % self.series.cadence_ms, 0)
        self.assertGreater(first, self.now)
        self.assertLessEqual(first - self.series.cadence_ms, self.now)
        self.assertEqual(next_episode_start(self.series, latest_start_ms=None, now_ms=first), first + self.series.cadence_ms)
        self.assertEqual(next_episode_start(self.series, latest_start_ms=first, now_ms=self.now),
                         first + self.series.cadence_ms)
        question = episode_question(self.series, first)
        self.assertIn(f"[{spell(first)}, {spell(first + 48 * HOUR)})", question)

    async def test_due_episode_is_published_bound_once_and_next_one_waits_for_its_lead(self):
        await configure_series(self.db, series=self.series, enabled=True, configured_by="a", now_ms=self.now)
        first = next_episode_start(self.series, latest_start_ms=None, now_ms=self.now)
        # Before the lead window nothing is created.
        self.now = first - self.series.lead_ms - 1000
        outcomes = await create_due_episodes(self.db, now_ms=self.now, seed=self.seed)
        self.assertEqual((outcomes[0]["nextStartMs"], outcomes[0]["created"], self.seeded), (first, None, []))
        # Inside the lead window the episode is published through the canonical seed path and bound.
        self.now = first - self.series.lead_ms + 1000
        outcomes = await create_due_episodes(self.db, now_ms=self.now, seed=self.seed)
        binding_id = f"{self.series.series_id}-{spell(first)}"
        self.assertEqual(outcomes[0]["created"], binding_id)
        self.assertEqual(len(self.seeded), 1)
        row = await self.db.first("SELECT binding_json,approved_by FROM risk_feed_bindings_v2 WHERE binding_id=?",
                                  (binding_id,))
        binding = json.loads(row["binding_json"])
        self.assertEqual((binding["target_start_ms"], binding["target_end_ms"], binding["operational_valid_until_ms"]),
                         (first, first + 48 * HOUR, first + 24 * HOUR))
        self.assertTrue(self.series.conforms(loads(__import__("forecast_domain.risk_feed", fromlist=["x"]).RiskFeedBindingV2,
                                                   row["binding_json"])))
        self.assertEqual(row["approved_by"], "series:" + self.series.series_id)
        # A repeated tick in the same lead window is idempotent (same question, same binding).
        outcomes = await create_due_episodes(self.db, now_ms=self.now + 60_000, seed=self.seed)
        self.assertEqual(outcomes[0]["nextStartMs"], first + self.series.cadence_ms)
        self.assertIsNone(outcomes[0]["created"])
        self.assertEqual((await self.db.first("SELECT COUNT(*) n FROM risk_feed_bindings_v2"))["n"], 1)
        # The following episode only appears once its own lead window opens, then both overlap operationally.
        self.now = first + self.series.cadence_ms - self.series.lead_ms + 1000
        outcomes = await create_due_episodes(self.db, now_ms=self.now, seed=self.seed)
        self.assertEqual(outcomes[0]["created"], f"{self.series.series_id}-{spell(first + self.series.cadence_ms)}")
        self.now = first + self.series.cadence_ms + 1000
        self.assertEqual(len(await operational_bindings_v2(self.db, feed_id="risk-v2", now_ms=self.now)), 2)
        log = await self.db.all("SELECT outcome FROM risk_feed_series_log_v2 ORDER BY attempted_at")
        self.assertEqual([r["outcome"] for r in log], ["published", "published"])

    async def test_failed_seed_is_logged_and_retried_without_partial_binding(self):
        await configure_series(self.db, series=self.series, enabled=True, configured_by="a", now_ms=self.now)
        first = next_episode_start(self.series, latest_start_ms=None, now_ms=self.now)
        self.now = first - self.series.lead_ms + 1000

        async def failing(question):
            raise RuntimeError("provider down")

        outcomes = await create_due_episodes(self.db, now_ms=self.now, seed=failing)
        self.assertEqual(outcomes[0]["failure"], "RuntimeError")
        self.assertEqual((await self.db.first("SELECT COUNT(*) n FROM risk_feed_bindings_v2"))["n"], 0)
        log = await self.db.first("SELECT outcome FROM risk_feed_series_log_v2")
        self.assertEqual(log["outcome"], "failed:RuntimeError")
        outcomes = await create_due_episodes(self.db, now_ms=self.now + 60_000, seed=self.seed)
        self.assertIsNone(outcomes[0]["created"])                     # inside the retry backoff
        self.assertEqual(outcomes[0]["backoffUntilMs"], self.now + 600_000)
        self.assertEqual(self.seeded, [])
        outcomes = await create_due_episodes(self.db, now_ms=self.now + 600_000, seed=self.seed)
        self.assertIsNotNone(outcomes[0]["created"])


if __name__ == "__main__":
    unittest.main()
