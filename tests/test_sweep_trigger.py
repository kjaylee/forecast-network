"""The operator host drives the sweep, and reports honestly when it cannot."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

try:
    import sweep_trigger  # noqa: E402
except RuntimeError as error:  # pragma: no cover
    # The token comes from the operator's keychain map, which lives on the operator host.
    raise unittest.SkipTest(f"operator configuration is required: {error}") from error


class _Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SweepTests(unittest.TestCase):
    def test_a_successful_sweep_reports_what_it_collected(self):
        with patch.object(sweep_trigger.urllib.request, "urlopen",
                          return_value=_Response({"data": {"sources": {"polled": 4},
                                                           "phaseMs": {"automation": 1900, "registry": 5300,
                                                                       "total": 7300}}})):
            result = sweep_trigger.sweep("https://example.test", 240.0)
        self.assertEqual((result["httpStatus"], result["polled"]), (200, 4))
        self.assertEqual(result["phases"]["registry"], 5300,
                         "the phases are the whole reason this exists; carry them through")

    def test_an_http_failure_is_reported_rather_than_raised(self):
        def fail(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "no", {}, io.BytesIO(b"error code: 1101"))

        with patch.object(sweep_trigger.urllib.request, "urlopen", fail):
            result = sweep_trigger.sweep("https://example.test", 240.0)
        self.assertEqual(result["httpStatus"], 500)
        self.assertIn("1101", result["error"])

    def test_a_transport_failure_is_named_without_the_endpoint(self):
        def fail(request, timeout=None):
            raise urllib.error.URLError("connection refused to https://internal.example")

        with patch.object(sweep_trigger.urllib.request, "urlopen", fail):
            result = sweep_trigger.sweep("https://example.test", 240.0)
        self.assertIsNone(result["httpStatus"])
        self.assertEqual(result["error"], "URLError")
        self.assertNotIn("internal.example", json.dumps(result))

    def test_the_exit_code_and_heartbeat_follow_the_http_status(self):
        for status, expected in ((200, 0), (500, 1)):
            with self.subTest(status=status):
                with patch.object(sweep_trigger, "sweep",
                                  return_value={"httpStatus": status, "seconds": 1.0}), \
                        patch.object(sweep_trigger, "heartbeat_ping") as beat, \
                        patch.object(sys, "argv", ["sweep_trigger.py"]), \
                        patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(sweep_trigger.main(), expected)
                record = json.loads(out.getvalue())
                self.assertEqual(record["event"], "sweep_trigger")
                beat.assert_called_once_with("sweep", failed=status != 200, note=f"httpStatus={status}")

    def test_a_sweep_may_run_longer_than_the_feed_tick_it_is_not_sharing_a_loop_with(self):
        # The default timeout has to exceed the worst measured sweep, because this runs in
        # its own process precisely so it cannot push the feed past its expiry.
        with patch.object(sys, "argv", ["sweep_trigger.py"]), \
                patch.object(sweep_trigger, "sweep", return_value={"httpStatus": 200}) as called, \
                patch.object(sweep_trigger, "heartbeat_ping"), \
                patch("sys.stdout", new_callable=io.StringIO):
            sweep_trigger.main()
        self.assertGreaterEqual(called.call_args.args[1], 120.0)
