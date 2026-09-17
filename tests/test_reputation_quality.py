"""Independent examples for qualified cohorts and cutoff-aware history."""

from __future__ import annotations

import unittest

from forecast_application.reputation import (
    DAY_MS,
    cohort_statistics,
    eligible_history,
    reputation_quality,
)

NOW = 400 * DAY_MS


def result(number, *, user="alice", category="SCIENCE", probability=90, outcome="YES", age=1):
    timestamp = NOW - age * DAY_MS
    return {"user_id": user, "forecast_id": f"f-{number}", "category": category,
            "probability": probability, "outcome": outcome, "finalized_outcome": outcome,
            "state": "FINALIZED", "submitted_at": timestamp - 1000,
            "finalized_at": timestamp, "eligibility_at": timestamp, "eligible": 1}


def sample(*, user="alice", category="SCIENCE", count=20, probability=90):
    return [result(number, user=user, category=category, probability=probability, age=number + 1)
            for number in range(count)]


def submission(user, probability=70, **changes):
    return {"forecast_id": "target", "user_id": user, "yes_probability": probability,
            "submitted_at": NOW - 1000, "eligibility_at": NOW - 1000, "revision": 1,
            "eligible": 1, **changes}


class ReputationQualityTests(unittest.TestCase):
    def test_export_requires_observed_eligible_finalized_binary_history(self):
        valid = result(0)
        changes = [{"eligible": 0}, {"eligible": 1.0}, {"outcome": "INVALID", "finalized_outcome": "INVALID"},
                   {"state": "CLOSED"}, {"finalized_outcome": "NO"}, {"finalized_at": NOW + 1},
                   {"submitted_at": NOW + 1}, {"eligibility_at": NOW + 1},
                   {"eligibility_at": None}, {"probability": True}, {"probability": 101}]
        rows = [valid] + [{**valid, "forecast_id": f"bad-{i}", **change} for i, change in enumerate(changes)]
        exported = eligible_history(rows, as_of_ms=NOW)
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0], {"userId": "alice", "forecastId": "f-0", "category": "science",
            "probabilityBp": 9000, "outcome": "YES", "submittedAt": NOW - DAY_MS - 1000,
            "finalizedAt": NOW - DAY_MS, "eligibilityAt": NOW - DAY_MS})
        self.assertEqual(eligible_history([valid], as_of_ms=NOW, exclude_forecast_id="f-0"), [])
        self.assertEqual(eligible_history([valid], as_of_ms=NOW, window_ms=1000), [])

    def test_duplicate_ledger_rows_cannot_inflate_samples(self):
        with self.assertRaises(ValueError):
            eligible_history([result(0), result(0)], as_of_ms=NOW)

    def test_category_qualification_is_asof_and_not_self_declared_credentials(self):
        rows = sample()
        quality = reputation_quality(rows, as_of_ms=NOW)
        expert = quality["expertise"][0]
        self.assertEqual(expert["status"], "qualified")
        self.assertEqual(expert["brierScore"], 0.01)
        self.assertEqual(expert["calibrationScore"], 0.9)
        self.assertEqual(reputation_quality(rows, as_of_ms=NOW, category="POLITICS")["expertise"][0]["status"], "new")
        self.assertFalse(reputation_quality(rows[:19], as_of_ms=NOW)["expertise"][0]["qualified"])
        self.assertFalse(reputation_quality([result(i, age=1) for i in range(20)], as_of_ms=NOW)["expertise"][0]["qualified"])
        self.assertEqual(reputation_quality(rows, as_of_ms=NOW - 30 * DAY_MS)["expertise"], [])

    def test_skill_requires_good_calibration_and_excludes_stale_results(self):
        poor = reputation_quality(sample(probability=60), as_of_ms=NOW)["expertise"][0]
        self.assertEqual(poor["status"], "not-qualified")  # Brier .16 passes, calibration .60 fails.
        stale = [{**row, "finalized_at": NOW - 366 * DAY_MS,
                  "submitted_at": NOW - 366 * DAY_MS - 1000, "eligibility_at": NOW - 366 * DAY_MS}
                 for row in sample()]
        self.assertFalse(reputation_quality(stale, as_of_ms=NOW)["expertise"][0]["qualified"])

    def test_consistency_is_temporal_stability_not_accuracy(self):
        wrong = [result(f"{age}-{i}", probability=0, age=age) for age in (15, 45, 75) for i in range(5)]
        report = reputation_quality(wrong, as_of_ms=NOW)
        self.assertEqual(report["consistencyScore"], 1.0)
        self.assertEqual([window["brierScore"] for window in report["consistency"]["windows"]], [1.0] * 3)
        mixed = [result(f"{age}-{i}", probability=probability, age=age)
                 for age, probability in ((75, 100), (45, 50), (15, 0)) for i in range(5)]
        self.assertEqual(reputation_quality(mixed, as_of_ms=NOW)["consistencyScore"], 0.0)
        self.assertIsNone(reputation_quality(wrong[:-1], as_of_ms=NOW)["consistencyScore"])
        self.assertIsNone(reputation_quality(sample(), as_of_ms=NOW)["consistencyScore"])

    def test_window_edges_neither_duplicate_nor_lose_results(self):
        rows = [result(age, age=age) for age in (0, 30, 60, 90, 91)]
        windows = reputation_quality(rows, as_of_ms=NOW)["consistency"]["windows"]
        self.assertEqual([window["count"] for window in windows], [1, 1, 2])

    def test_independent_cohorts_and_latest_receipt(self):
        history = sample() + sample(user="bob", category="POLITICS", probability=100)
        votes = [submission("alice", 60), submission("alice", 80, revision=2), submission("bob", 20),
                 submission("new", 50), submission("future", 99, submitted_at=NOW + 1),
                 submission("void", 100, eligible=0)]
        report = cohort_statistics(votes, history, forecast_id="target", category="science", as_of_ms=NOW,
                                   ai={"probability": 65, "created_at": NOW, "provider": "p", "model": "m"})
        self.assertEqual(report["crowd"], {"probability": 50.0, "count": 3})
        self.assertEqual(report["expert"], {"probability": 80.0, "count": 1})
        self.assertEqual(report["top"], {"probability": 20.0, "count": 1})
        self.assertEqual(report["ai"]["probability"], 65)
        self.assertEqual(report["ai"]["count"], 1)

    def test_target_result_does_not_qualify_its_own_forecaster(self):
        history = sample(count=19) + [{**result(20, age=25), "forecast_id": "target"}]
        report = cohort_statistics([submission("alice")], history, forecast_id="target", category="science", as_of_ms=NOW)
        self.assertEqual(report["expert"], {"probability": None, "count": 0})

    def test_empty_and_invalid_ai_states_are_honest(self):
        for ai in (None, {"probability": 70}, {"probability": True, "created_at": NOW},
                   {"probability": 70, "created_at": NOW + 1, "provider": "p", "model": "m"}):
            report = cohort_statistics([], [], forecast_id="target", category="science", as_of_ms=NOW, ai=ai)
            for name in ("crowd", "top", "expert", "ai"):
                self.assertIsNone(report[name]["probability"])
                self.assertEqual(report[name]["count"], 0)

    def test_conflicting_receipts_and_mixed_profile_users_rejected(self):
        with self.assertRaises(ValueError):
            cohort_statistics([submission("alice", 30), submission("alice", 90)], [],
                              forecast_id="target", category="science", as_of_ms=NOW)
        with self.assertRaises(ValueError):
            reputation_quality([result(0), result(1, user="bob")], as_of_ms=NOW)
