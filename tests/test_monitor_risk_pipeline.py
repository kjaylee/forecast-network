"""The pipeline monitor flags stale feeds, starved ticks, missed episodes and an idle keeper."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

try:
    import monitor_risk_pipeline as monitor  # noqa: E402
except RuntimeError as error:  # pragma: no cover
    # The monitor reads the operator's keychain map on import, and that map lives on
    # the operator host. A CI runner has no operator configuration and should not
    # pretend to.
    raise unittest.SkipTest(f"operator configuration is required: {error}") from error

NOW = 1_800_000_000_000


def health(**changes):
    feed = {"feedId": "f", "enabled": True, "latestSequence": 10, "latestAgeMs": 30_000, "coveredChannels": ["depegRisk1d"],
            "ticksLast30m": 29, "failedTicksLast30m": 0}
    feed.update({k: v for k, v in changes.items() if k in feed})
    series = {"seriesId": "s", "enabled": True, "latestEpisodeStartMs": NOW - 3_600_000,
              "nextEpisodeStartMs": NOW + 3_600_000, "failedAttemptsLast24h": 0}
    series.update({k: v for k, v in changes.items() if k in series})
    return {"serverTime": NOW, "feeds": [feed], "series": [series],
            "sourceWatch": changes.get("sourceWatch", {"total": 9, "failing": 0, "stale": 0})}


KEEPER_OK = {"available": True, "lastStatus": "finalized", "lastAgeMs": 60_000, "feedStatus": "verified", "feedFailure": "none"}


class MonitorTests(unittest.TestCase):
    def test_healthy_pipeline_has_no_problems(self):
        self.assertEqual(monitor.evaluate(health(), KEEPER_OK, NOW), [])

    def test_upstream_failures_are_warnings_not_degradation(self):
        h = health(sourceWatch={"total": 9, "failing": 1, "stale": 0})
        self.assertEqual(monitor.evaluate(h, KEEPER_OK, NOW), [])
        self.assertEqual(monitor.warnings(h), ["1 enabled watch sources failing upstream (external)"])
        self.assertEqual(monitor.warnings(None), [])

    def test_each_degradation_is_named(self):
        cases = [
            (health(latestAgeMs=200_000), KEEPER_OK, "stale"),
            (health(ticksLast30m=5), KEEPER_OK, "ticks in 30 min"),
            (health(failedTicksLast30m=3), KEEPER_OK, "failed ticks"),
            (health(failedAttemptsLast24h=3), KEEPER_OK, "episode attempts"),
            (health(nextEpisodeStartMs=NOW - 1), KEEPER_OK, "missed episode"),
            (health(sourceWatch={"total": 9, "failing": 1, "stale": 2}), KEEPER_OK, "watch sources stale"),
            (None, KEEPER_OK, "unreachable"),
            (health(), {"available": False}, "journal unavailable"),
            (health(), dict(KEEPER_OK, lastAgeMs=700_000), "keeper idle"),
            (health(), dict(KEEPER_OK, feedStatus="unavailable", feedFailure="invalid"), "feed unavailable"),
        ]
        for h, k, expected in cases:
            with self.subTest(expected):
                problems = monitor.evaluate(h, k, NOW)
                self.assertTrue(any(expected in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
