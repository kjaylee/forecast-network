"""The fallback acts only once the primary has already failed."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

from scripts import feed_fallback

NOW_MS = 1_800_000_000_000


def feed(*, issued=NOW_MS, server=NOW_MS, with_issued=True):
    payload = {"issued_at_ms": issued, "sequence": 1} if with_issued else {}
    return {"data": {"serverTime": server, "status": "current", "envelope": {"payload": payload}}}


def run(argv=(), env=None, responses=()):
    """Run main() with the network replaced. Returns (exit code, printed record, calls)."""
    calls: list[tuple[str, dict]] = []
    queue = list(responses)

    def fetch(url, *, data=None, headers=None, timeout=None):
        calls.append((url, {"data": data, "headers": headers or {}}))
        outcome = queue.pop(0) if queue else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    output = io.StringIO()
    with patch.object(feed_fallback, "fetch", fetch), \
            patch.object(sys, "argv", ["feed_fallback.py", *argv]), \
            patch("sys.stdout", output), \
            patch.dict(os.environ, env or {}, clear=False):
        code = feed_fallback.main()
    return code, json.loads(output.getvalue()), calls


class AgeTests(unittest.TestCase):
    def test_the_age_comes_from_the_feed_clock_not_the_observer_clock(self):
        # The observer's own clock must not be able to invent staleness or hide it,
        # so the same feed reads the same however wrong the runner's clock is.
        stale = feed(issued=NOW_MS - 900_000, server=NOW_MS)
        for observer in (NOW_MS, 0, NOW_MS + 86_400_000):
            with self.subTest(observer=observer):
                self.assertEqual(feed_fallback.feed_age_seconds(stale, observer), 900.0)

    def test_a_server_clock_behind_the_issuance_reads_as_not_stale(self):
        self.assertEqual(feed_fallback.feed_age_seconds(feed(issued=NOW_MS, server=NOW_MS - 900_000), NOW_MS), -900.0)

    def test_an_absent_clock_falls_back_to_the_observer(self):
        self.assertEqual(feed_fallback.feed_age_seconds(feed(issued=NOW_MS - 60_000, server=None), NOW_MS), 60.0)

    def test_a_payload_without_an_issuance_time_is_unknown_rather_than_fresh(self):
        self.assertIsNone(feed_fallback.feed_age_seconds(feed(with_issued=False), NOW_MS))
        for malformed in (None, {}, {"data": None}, {"data": {"envelope": {}}}, {"data": {"envelope": {"payload": {"issued_at_ms": "soon"}}}}):
            with self.subTest(malformed=malformed):
                self.assertIsNone(feed_fallback.feed_age_seconds(malformed, NOW_MS))


class BehaviourTests(unittest.TestCase):
    def test_a_healthy_feed_is_read_once_and_left_alone(self):
        code, record, calls = run(responses=[feed(issued=NOW_MS)])
        self.assertEqual(code, 0)
        self.assertFalse(record["acted"])
        self.assertEqual(len(calls), 1, "the healthy path must cost exactly one read")
        self.assertEqual(calls[0][0], feed_fallback.ORIGIN + feed_fallback.FEED_PATH)

    def test_a_stale_feed_is_triggered_with_the_scheduler_credential(self):
        token = "s" * 64
        code, record, calls = run(env={"SCHEDULER_TOKEN": token},
                                  responses=[feed(issued=NOW_MS - 900_000), {"data": {"status": "ticked"}}])
        self.assertEqual(code, 0)
        self.assertTrue(record["acted"])
        self.assertEqual(record["ageSeconds"], 900.0)
        self.assertEqual(calls[1][0], feed_fallback.ORIGIN + feed_fallback.OPERATE_PATH)
        self.assertEqual(calls[1][1]["headers"]["Authorization"], "Bearer " + token)
        self.assertEqual(calls[1][1]["data"], b"{}")

    def test_a_stale_feed_without_a_credential_fails_rather_than_passing_quietly(self):
        code, record, calls = run(env={"SCHEDULER_TOKEN": ""}, responses=[feed(issued=NOW_MS - 900_000)])
        self.assertEqual(code, 1)
        self.assertFalse(record["acted"])
        self.assertEqual(len(calls), 1, "nothing should be attempted without a credential")

    def test_a_short_credential_is_refused_like_a_missing_one(self):
        code, record, _ = run(env={"SCHEDULER_TOKEN": "s" * 31}, responses=[feed(issued=NOW_MS - 900_000)])
        self.assertEqual((code, record["reason"]), (1, "scheduler credential unavailable"))

    def test_the_boundary_is_not_treated_as_stale(self):
        code, record, calls = run(env={"SCHEDULER_TOKEN": "s" * 64},
                                  responses=[feed(issued=NOW_MS - 180_000)])
        self.assertEqual(code, 0)
        self.assertFalse(record["acted"])
        self.assertEqual(len(calls), 1)

    def test_an_unobservable_feed_does_not_fail_this_workflow(self):
        # Alerting belongs to the watchdog. A Cloudflare blip is not two problems.
        code, record, calls = run(responses=[urllib.error.URLError("refused")])
        self.assertEqual(code, 0)
        self.assertFalse(record["observed"])
        self.assertEqual(len(calls), 1)

    def test_a_rescue_that_failed_is_reported(self):
        code, record, _ = run(env={"SCHEDULER_TOKEN": "s" * 64},
                              responses=[feed(issued=NOW_MS - 900_000), urllib.error.URLError("refused")])
        self.assertEqual(code, 1)
        self.assertTrue(record["acted"])


if __name__ == "__main__":
    unittest.main()
