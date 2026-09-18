"""The pipeline monitor flags stale feeds, starved ticks, missed episodes and an idle keeper."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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



class DriftReportingTests(unittest.TestCase):
    """A deployed copy that stopped matching the repository is a problem, not a detail."""

    def run_monitor(self, deployed, repo):
        with patch.object(monitor, "DEPLOYED", deployed), \
                patch.object(monitor, "REPO_SCRIPTS", repo), \
                patch.object(monitor, "fetch_health", return_value=None), \
                patch.object(monitor, "keeper_status", return_value={"available": False}), \
                patch.object(monitor, "heartbeat_ping"), \
                patch.object(sys, "argv", ["monitor_risk_pipeline.py", "--no-notify",
                                           "--state", str(deployed.parent / "state.json")]), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            monitor.main()
        return json.loads(out.getvalue())

    def test_a_module_the_deployed_copy_imports_but_never_got_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployed, repo = root / "deployed", root / "repo" / "scripts"
            deployed.mkdir(parents=True)
            repo.mkdir(parents=True)
            for directory in (deployed, repo):
                (directory / "operator.py").write_text("from heartbeat import ping\n")
            (repo / "heartbeat.py").write_text("def ping(): ...\n")
            record = self.run_monitor(deployed, repo)
            self.assertTrue(any("deployment drift" in problem and "heartbeat" in problem
                                for problem in record["problems"]), record["problems"])

    def test_a_matching_deployment_reports_no_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployed, repo = root / "deployed", root / "repo" / "scripts"
            deployed.mkdir(parents=True)
            repo.mkdir(parents=True)
            for directory in (deployed, repo):
                (directory / "operator.py").write_text("from heartbeat import ping\n")
                (directory / "heartbeat.py").write_text("def ping(): ...\n")
            record = self.run_monitor(deployed, repo)
            self.assertFalse(any("deployment drift" in problem for problem in record["problems"]))

    def test_the_comparison_is_skipped_rather_than_guessed_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployed = Path(tmp) / "deployed"
            deployed.mkdir()
            (deployed / "operator.py").write_text("x = 1\n")
            record = self.run_monitor(deployed, None)
            self.assertFalse(any("deployment drift" in problem for problem in record["problems"]))


if __name__ == "__main__":
    unittest.main()


class StuckForecastReportingTests(unittest.TestCase):
    """Forecasts that cannot clear themselves are reported, and are not alarms."""

    def test_they_appear_as_a_warning(self):
        warned = monitor.warnings(dict(health(), stuckForecasts=3))
        self.assertTrue(any("3 forecasts carry a job_error" in w for w in warned), warned)

    def test_they_do_not_degrade_the_pipeline(self):
        # Three forecasts have been retrying for days and nothing will clear them, so an
        # alert would repeat forever and stop meaning anything. It needs a decision, not a
        # wake-up call.
        self.assertEqual(monitor.evaluate(dict(health(), stuckForecasts=3), KEEPER_OK, NOW), [])

    def test_an_absent_count_is_not_reported(self):
        self.assertEqual([w for w in monitor.warnings(health()) if "job_error" in w], [])
