"""The supervised per-minute operator reports the Worker's tick outcome without leaking the token."""

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
                patch.object(sys, "argv", ["operate_risk_v2.py"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(operate_risk_v2.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["event"], "risk_v2_operate")
        with patch.object(operate_risk_v2, "tick", return_value={"httpStatus": 500}), \
                patch.object(sys, "argv", ["operate_risk_v2.py"]), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(operate_risk_v2.main(), 1)


if __name__ == "__main__":
    unittest.main()
