"""The dead-man's switch reports when configured, and stays out of the way when it is not."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts.heartbeat import checks, ping, target


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "heartbeat.json"

    def tearDown(self):
        self.directory.cleanup()

    def write(self, value):
        self.path.write_text(json.dumps(value) if not isinstance(value, str) else value)

    def test_an_absent_config_file_configures_nothing(self):
        self.assertEqual(checks(self.path), {})
        self.assertIsNone(target("operator", path=self.path))

    def test_a_malformed_config_file_configures_nothing_rather_than_raising(self):
        for content in ("{", "[]", '"string"', "null", '{"operator": 7}', '{"operator": "ftp://x"}'):
            with self.subTest(content=content):
                self.write(content)
                self.assertEqual(checks(self.path), {})

    def test_only_http_urls_survive_and_the_rest_are_dropped(self):
        self.write({"operator": "https://hc-ping.com/abc", "bad": "ftp://hc-ping.com/abc"})
        self.assertEqual(checks(self.path), {"operator": "https://hc-ping.com/abc"})

    def test_a_failure_ping_goes_to_the_fail_suffix(self):
        self.write({"operator": "https://hc-ping.com/abc"})
        self.assertEqual(target("operator", path=self.path), "https://hc-ping.com/abc")
        self.assertEqual(target("operator", failed=True, path=self.path), "https://hc-ping.com/abc/fail")

    def test_a_trailing_slash_never_produces_a_double_slash(self):
        self.write({"operator": "https://hc-ping.com/abc/"})
        self.assertEqual(target("operator", failed=True, path=self.path), "https://hc-ping.com/abc/fail")

    def test_an_unconfigured_check_is_a_no_op_and_never_reaches_the_network(self):
        with patch("urllib.request.urlopen") as opened:
            self.assertFalse(ping("operator", path=self.path))
            opened.assert_not_called()


class PingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "heartbeat.json"
        self.path.write_text(json.dumps({"operator": "https://hc-ping.com/abc"}))

    def tearDown(self):
        self.directory.cleanup()

    def sent(self, **kwargs):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["timeout"] = timeout
            return Response()

        with patch("urllib.request.urlopen", urlopen):
            sent = ping("operator", path=self.path, **kwargs)
        return sent, captured

    def test_a_success_ping_posts_to_the_check_url(self):
        sent, captured = self.sent(note="tick ok")
        self.assertTrue(sent)
        self.assertEqual(captured["url"], "https://hc-ping.com/abc")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], b"tick ok")

    def test_a_failure_ping_posts_to_the_fail_url(self):
        sent, captured = self.sent(failed=True, note="http 500")
        self.assertTrue(sent)
        self.assertEqual(captured["url"], "https://hc-ping.com/abc/fail")

    def test_a_long_note_is_truncated_rather_than_refused(self):
        _, captured = self.sent(note="x" * 5000)
        self.assertEqual(len(captured["body"]), 400)

    def test_the_monitoring_service_being_down_never_propagates(self):
        for error in (urllib.error.URLError("refused"), TimeoutError(), OSError("socket"), ValueError("bad")):
            with self.subTest(error=type(error).__name__):
                def urlopen(request, timeout=None):
                    raise error

                with patch("urllib.request.urlopen", urlopen):
                    self.assertFalse(ping("operator", path=self.path))


if __name__ == "__main__":
    unittest.main()
