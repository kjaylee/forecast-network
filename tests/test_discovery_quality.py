"""Ranking evidence, manipulation limits and honest cold-start recommendations."""

from __future__ import annotations

import unittest

from forecast_application.discovery import (
    DAY_MS,
    _tie_coefficients,
    rank_forecasts,
    recommendations,
    score_forecast,
    tie_break,
)

NOW = 100 * DAY_MS + 1234


def candidate(identifier="a", **changes):
    return {"id": identifier, "category": "SCIENCE", "state": "OPEN", "created_at": 99 * DAY_MS,
            "open_at": 99 * DAY_MS, "close_at": 110 * DAY_MS, "eligible": 1,
            "ambiguity_score_bp": 1000, "participant_count": 0, "comment_count": 0, "share_count": 0,
            **changes}


class DiscoveryQualityTests(unittest.TestCase):
    def test_score_can_be_recomputed_from_retained_evidence(self):
        quality = score_forecast(candidate(), as_of_ms=NOW)
        self.assertEqual(quality["componentsBp"], {"clarity": 9000, "creator": 5000,
                         "adjudication": 5000, "engagement": 0, "freshness": 5000})
        self.assertEqual(quality["scoreBp"], 5850)
        self.assertEqual(quality["creatorSampleStatus"], "new")
        inputs = quality["inputs"]
        independent = (4000 * (10000 - inputs["ambiguityScoreBp"])
            + 2000 * ((inputs["creatorFinalized"] - inputs["creatorInvalid"] + 2) * 10000 // (inputs["creatorFinalized"] + 4))
            + 1500 * ((inputs["reviewedDisputes"] - inputs["materialDisputes"] + 2) * 10000 // (inputs["reviewedDisputes"] + 4))
            + 1500 * ((inputs["participantsCapped"] * 3 + inputs["commentsCapped"] + inputs["sharesCapped"]) * 10000 // 190)
            + 1000 * (10000 // (1 + inputs["ageDays"]))) // 10000
        self.assertEqual(independent, quality["scoreBp"])

    def test_popularity_cannot_contribute_more_than_fifteen_percent(self):
        quiet = score_forecast(candidate(), as_of_ms=NOW)
        noisy = score_forecast(candidate(participant_count=10**12, comment_count=10**12, share_count=10**12), as_of_ms=NOW)
        capped = score_forecast(candidate(participant_count=50, comment_count=20, share_count=20), as_of_ms=NOW)
        self.assertEqual(noisy, capped)
        self.assertEqual(noisy["scoreBp"] - quiet["scoreBp"], 1500)

    def test_invalid_and_material_adjudications_reduce_quality(self):
        good = candidate(creator_finalized_count=10, creator_invalid_count=0,
                         creator_reviewed_disputes=10, creator_material_disputes=0)
        bad = {**good, "creator_invalid_count": 10, "creator_material_disputes": 10}
        self.assertGreater(score_forecast(good, as_of_ms=NOW)["scoreBp"], score_forecast(bad, as_of_ms=NOW)["scoreBp"])
        with self.assertRaises(ValueError):
            score_forecast(candidate(creator_invalid_count=1), as_of_ms=NOW)

    def test_held_closed_ineligible_not_open_yet_are_excluded(self):
        rows = [candidate(), candidate("held", participation_hold={"reason": "review"}),
                candidate("closed", close_at=NOW), candidate("invalid", eligible=0),
                candidate("future", open_at=NOW + 1), candidate("resolved", state="FINALIZED"),
                candidate("created-future", created_at=NOW + 1), candidate("unknown", eligible=None)]
        self.assertEqual([row["id"] for row in recommendations(rows, as_of_ms=NOW)], ["a"])

    def test_daily_ties_stable_for_reloads_input_order_and_personalized(self):
        rows = [candidate(str(i)) for i in range(15)]
        first = rank_forecasts(rows, as_of_ms=NOW, user_id="alice")
        reload = rank_forecasts(reversed(rows), as_of_ms=NOW + 10000, user_id="alice")
        self.assertEqual([row["id"] for row in first], [row["id"] for row in reload])
        self.assertNotEqual([row["id"] for row in first], [row["id"] for row in rank_forecasts(rows, as_of_ms=NOW, user_id="bob")])
        self.assertEqual({row["quality"]["scoreBp"] for row in first}, {5850})

    def test_clear_cold_start_has_reserved_slot_and_category_diversity(self):
        rows = [candidate(str(i), creator_finalized_count=100, participant_count=50,
                          category="SCIENCE" if i < 8 else "POLITICS") for i in range(10)]
        rows += [candidate("new", category="ECONOMICS")]
        result = recommendations(rows, as_of_ms=NOW)
        self.assertEqual(len(result), 5)
        self.assertIn("new", [row["id"] for row in result])
        self.assertGreaterEqual(len({row["category"] for row in result}), 2)
        self.assertEqual(len({row["id"] for row in result}), 5)

    def test_cold_start_does_not_reserve_space_for_ambiguous_questions(self):
        rows = [candidate(str(i), creator_finalized_count=100) for i in range(10)]
        rows += [candidate("ambiguous", ambiguity_score_bp=9000)]
        self.assertNotIn("ambiguous", [row["id"] for row in recommendations(rows, as_of_ms=NOW)])

    def test_snapshot_ambiguity_and_empty_inventory(self):
        row = candidate()
        del row["ambiguity_score_bp"]
        row["snapshot"] = '{"specification":{"ambiguity_score_bp":1000}}'
        self.assertEqual(score_forecast(row, as_of_ms=NOW)["componentsBp"]["clarity"], 9000)
        self.assertEqual(recommendations([], as_of_ms=NOW), [])
        with self.assertRaises(ValueError):
            rank_forecasts([candidate(), candidate()], as_of_ms=NOW)
        with self.assertRaises(ValueError):
            recommendations([], as_of_ms=NOW, limit=-1)


class TieBreakParityTests(unittest.TestCase):
    """The same vectors are asserted in apps/web-rs/src/discovery.rs.

    Discovery order is decided by this key, and three implementations compute it: this one,
    the Rust edge that serves /api/forecasts, and the SQL expression. They have to agree, or
    the same database answers differently depending on which one asked. Nothing was checking
    that until the Rust side got tests, and the first version of those tests disagreed with
    this implementation for a non-ASCII user id.
    """

    VECTORS = (
        ("f_BHbIOkOLWiMNIEGD3sjxrOSf", 1_800_000_000_000, None, 275240),
        ("f_BHbIOkOLWiMNIEGD3sjxrOSf", 1_800_000_000_000, "user-a", 314780),
        ("f_tkqvmogdeeITz0c_XMNJTB68", 1_800_086_400_000, None, 274888),
        ("f_tkqvmogdeeITz0c_XMNJTB68", 1_800_086_400_000, "user-b", 386187),
        ("f_kFOj9FjHp6oP2C-h3B4mNNZy", 1_700_000_000_000, "user-a", 337443),
        ("", 1_800_000_000_000, None, 0),
        ("short", 1_800_000_000_000, None, 75374),
        ("f_abcdefghijklmnopqrstuvwxyz0123456789extra", 1_800_000_000_000, None, 417510),
        ("질문-식별자-테스트", 1_800_000_000_000, "사용자", 49845078),
        ("boundary", 1_800_086_399_999, None, 110263),
    )

    def test_the_tie_key_matches_the_vectors_the_rust_edge_asserts(self):
        for identifier, as_of_ms, user_id, expected in self.VECTORS:
            with self.subTest(identifier=identifier, user_id=user_id):
                self.assertEqual(tie_break(identifier, as_of_ms, user_id), expected)

    def test_an_escaped_seed_ties_differently_which_is_why_both_sides_serialize_raw(self):
        # The seed is serialized raw on both sides. This is what escaping would cost: the
        # literal text of an escaped id is a different string, so it ties differently, and
        # the Rust edge serializes raw and cannot reproduce the escaped form.
        raw_id = "사용자"
        escaped_id = "\\uc0ac\\uc6a9\\uc790"
        self.assertNotEqual(raw_id, escaped_id)
        self.assertNotEqual(tie_break("x", 1_800_000_000_000, raw_id),
                            tie_break("x", 1_800_000_000_000, escaped_id))
        self.assertNotEqual(_tie_coefficients(1_800_000_000_000, raw_id),
                            _tie_coefficients(1_800_000_000_000, escaped_id))
