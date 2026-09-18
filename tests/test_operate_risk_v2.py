"""The supervised per-minute operator reports the Worker's tick outcome without leaking the token."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

try:
    import operate_risk_v2  # noqa: E402
except RuntimeError as error:  # pragma: no cover
    # The operator reads the keychain map on import, and that map lives on the
    # operator host. A CI runner has no operator configuration and should not
    # pretend to.
    raise unittest.SkipTest(f"operator configuration is required: {error}") from error


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class OperateRiskV2Tests(unittest.TestCase):
    def test_tick_posts_authenticated_empty_body_and_returns_feed_outcomes(self):
        seen = {}

        def urlopen(request, timeout):
            seen["url"], seen["method"], seen["body"] = request.full_url, request.get_method(), request.data
            seen["auth"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            return _Response(json.dumps({"data": {"status": "ticked", "feeds": [{"feedId": "f", "published": 3}]}}).encode())

        with patch.object(operate_risk_v2, "secret", return_value="operator-token"), \
                patch.object(operate_risk_v2.urllib.request, "urlopen", urlopen):
            result = operate_risk_v2.tick("https://example.test", 5.0)
        self.assertEqual((seen["url"], seen["method"], seen["body"], seen["timeout"]),
                         ("https://example.test/api/admin/risk/v2/operate", "POST", b"{}", 5.0))
        self.assertEqual(seen["auth"], "Bearer operator-token")
        self.assertEqual(result["httpStatus"], 200)
        self.assertEqual(result["feeds"], [{"feedId": "f", "published": 3}])

    def test_http_and_network_failures_are_reported_not_raised(self):
        def http_error(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, io.BytesIO(b'{"error":"x"}'))

        def network_error(request, timeout):
            raise urllib.error.URLError("down")

        with patch.object(operate_risk_v2, "secret", return_value="t"):
            with patch.object(operate_risk_v2.urllib.request, "urlopen", http_error):
                failed = operate_risk_v2.tick("https://example.test", 1.0)
            with patch.object(operate_risk_v2.urllib.request, "urlopen", network_error):
                down = operate_risk_v2.tick("https://example.test", 1.0)
        self.assertEqual((failed["httpStatus"], failed["error"]), (503, '{"error":"x"}'))
        self.assertEqual((down["httpStatus"], down["error"]), (None, "URLError"))
        for result in (failed, down):
            self.assertNotIn("t", json.dumps(result).replace("httpStatus", "").replace("URLError", ""))

    def test_main_exit_code_follows_http_status(self):
        with patch.object(operate_risk_v2, "tick", return_value={"httpStatus": 200}), \
                patch.object(operate_risk_v2, "heartbeat_ping") as beat, \
                patch.object(sys, "argv", ["operate_risk_v2.py"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(operate_risk_v2.main(), 0)
            beat.assert_called_once_with("operator", failed=False, note="httpStatus=200")
        self.assertEqual(json.loads(out.getvalue())["event"], "risk_v2_operate")
        with patch.object(operate_risk_v2, "tick", return_value={"httpStatus": 500}), \
                patch.object(operate_risk_v2, "heartbeat_ping") as beat, \
                patch.object(sys, "argv", ["operate_risk_v2.py"]), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(operate_risk_v2.main(), 1)
            beat.assert_called_once_with("operator", failed=True, note="httpStatus=500")


class LoopTests(unittest.TestCase):
    """The loop paces itself, and gives up rather than hanging."""

    def run_loop(self, durations, *, cadence=60.0, sleeps=3):
        class Done(Exception):
            pass

        clock = {"t": 0.0, "i": 0}
        recorded = []

        def monotonic():
            return clock["t"]

        def sleep(seconds):
            recorded.append(seconds)
            clock["t"] += seconds
            if len(recorded) >= sleeps:
                raise Done

        def tick(origin, timeout):
            clock["t"] += durations[min(clock["i"], len(durations) - 1)]
            clock["i"] += 1
            return {"httpStatus": 200, "seconds": 0.0}

        fake = SimpleNamespace(monotonic=monotonic, sleep=sleep, time=lambda: clock["t"])
        with patch.object(operate_risk_v2, "time", fake), \
                patch.object(operate_risk_v2, "tick", tick), \
                patch.object(operate_risk_v2, "heartbeat_ping") as beat, \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(Done):
                operate_risk_v2.loop("https://example.test", 50.0, cadence)
        return recorded, beat

    def test_each_sleep_is_what_is_left_of_the_cadence(self):
        sleeps, _ = self.run_loop([3.0], cadence=60.0)
        self.assertEqual(sleeps, [57.0, 57.0, 57.0])

    def test_a_cycle_that_overruns_the_cadence_still_waits_a_second(self):
        sleeps, _ = self.run_loop([80.0], cadence=60.0)
        self.assertEqual(sleeps, [1.0, 1.0, 1.0])

    def test_every_tick_is_reported_to_the_dead_mans_switch(self):
        # Three sleeps means three completed ticks before the test stops the loop.
        _, beat = self.run_loop([1.0])
        self.assertEqual(beat.call_count, 3, "one ping per tick, including the ones that end normally")

    def test_a_stuck_cycle_exits_instead_of_hanging_forever(self):
        # Three cadences of no progress means something is stuck, and the supervisor
        # restarting a clean process is better than a loop that never finishes a tick.
        with patch.object(operate_risk_v2, "tick", lambda origin, timeout: {"httpStatus": 200}), \
                patch.object(operate_risk_v2, "heartbeat_ping"), \
                patch("sys.stdout", new_callable=io.StringIO):
            clock = {"t": 0.0}

            def monotonic():
                clock["t"] += 200.0
                return clock["t"]

            with patch.object(operate_risk_v2, "time", SimpleNamespace(
                    monotonic=monotonic, sleep=lambda s: None, time=lambda: 0)):
                self.assertEqual(operate_risk_v2.loop("https://example.test", 50.0, 60.0), 1)

    def test_a_failed_tick_is_reported_as_failure_and_does_not_stop_the_loop(self):
        class Done(Exception):
            pass

        clock = {"t": 0.0}

        def sleep(seconds):
            raise Done

        def tick(origin, timeout):
            return {"httpStatus": 500, "seconds": 0.0}

        fake = SimpleNamespace(monotonic=lambda: clock["t"], sleep=sleep, time=lambda: 0)
        with patch.object(operate_risk_v2, "time", fake), \
                patch.object(operate_risk_v2, "tick", tick), \
                patch.object(operate_risk_v2, "heartbeat_ping") as beat, \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(Done):
                operate_risk_v2.loop("https://example.test", 50.0, 60.0)
        beat.assert_called_once_with("operator", failed=True, note="httpStatus=500")


if __name__ == "__main__":
    unittest.main()
