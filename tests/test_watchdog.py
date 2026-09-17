"""The off-host watchdog names what is wrong, and stays quiet when nothing is."""

from __future__ import annotations

import unittest

from scripts.watchdog import FEED_STALE_MS, evaluate

NOW = 1_800_000_000_000


def feed(*, status="current", issued=NOW, server=NOW, with_payload=True):
    payload = {"issued_at_ms": issued, "sequence": 1} if with_payload else {}
    return {"status": status, "serverTime": server, "envelope": {"payload": payload}}


def observed(*, health=None, status=None, feed_body=None):
    return {"health": health, "status": status, "feed": feed_body}


HEALTHY = {"ok": True}
STATUS = {"version": "0.13.0"}


class WatchdogTests(unittest.TestCase):
    def test_a_current_feed_on_answering_endpoints_reports_nothing(self):
        self.assertEqual(evaluate(observed(health=HEALTHY, status=STATUS, feed_body=feed()),
                                  now_ms=NOW), [])

    def test_a_feed_that_stopped_being_issued_is_reported_with_its_age(self):
        problems = evaluate(observed(health=HEALTHY, status=STATUS, feed_body=feed(issued=NOW - 900_000)),
                            now_ms=NOW)
        self.assertEqual(len(problems), 1)
        self.assertIn("900s ago", problems[0])

    def test_the_age_uses_the_feed_clock_not_the_observer_clock(self):
        # A runner whose own clock is wrong must not manufacture an outage, and a
        # runner whose clock is right must not hide one behind a wrong server clock.
        self.assertEqual(evaluate(observed(health=HEALTHY, status=STATUS,
                                           feed_body=feed(issued=NOW, server=NOW - 900_000)),
                                  now_ms=NOW), [])
        problems = evaluate(observed(health=HEALTHY, status=STATUS,
                                     feed_body=feed(issued=NOW - 900_000, server=NOW)),
                            now_ms=NOW + 86_400_000)
        self.assertTrue(any("900s ago" in problem for problem in problems))

    def test_the_staleness_boundary_is_inclusive_of_the_boundary_itself(self):
        self.assertEqual(evaluate(observed(health=HEALTHY, status=STATUS,
                                           feed_body=feed(issued=NOW - FEED_STALE_MS)),
                                  now_ms=NOW), [])
        self.assertTrue(evaluate(observed(health=HEALTHY, status=STATUS,
                                          feed_body=feed(issued=NOW - FEED_STALE_MS - 1)),
                                 now_ms=NOW))

    def test_a_feed_that_is_not_current_is_reported_by_its_own_word(self):
        problems = evaluate(observed(health=HEALTHY, status=STATUS, feed_body=feed(status="expired")),
                            now_ms=NOW)
        self.assertEqual(len(problems), 1)
        self.assertIn("'expired'", problems[0])

    def test_a_feed_without_an_issuance_time_is_reported_rather_than_assumed_fresh(self):
        problems = evaluate(observed(health=HEALTHY, status=STATUS,
                                     feed_body=feed(with_payload=False)), now_ms=NOW)
        self.assertEqual(problems, ["the risk feed carries no issuance time"])

    def test_every_unreachable_surface_is_named_separately(self):
        problems = evaluate(observed(), now_ms=NOW)
        self.assertEqual(problems, ["the health endpoint did not answer",
                                    "the status endpoint did not answer",
                                    "the risk feed did not answer"])

    def test_a_health_endpoint_that_answers_that_it_is_unwell_is_not_treated_as_up(self):
        problems = evaluate(observed(health={"ok": False}, status=STATUS, feed_body=feed()), now_ms=NOW)
        self.assertEqual(problems, ["the health endpoint answered that it is not ok"])

    def test_an_unreachable_feed_is_reported_once_and_not_compounded(self):
        problems = evaluate(observed(health=HEALTHY, status=STATUS, feed_body=None), now_ms=NOW)
        self.assertEqual(problems, ["the risk feed did not answer"])


if __name__ == "__main__":
    unittest.main()
