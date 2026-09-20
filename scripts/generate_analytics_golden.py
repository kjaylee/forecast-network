#!/usr/bin/env python3
"""Export the product KPI aggregation, so a Rust port can be held to it.

`product_analytics` is one read batch; `aggregate_product_analytics` is pure and replayable
from a retained snapshot. The pure half is what a port has to reproduce, and it is not
arithmetic so much as a set of definitions:

  * a *predictor* is someone who submitted an accepted forecast in the window, after the
    identity merge, after the exclusions, and only on a forecast that is itself admitted;
  * a *cohort* is keyed by the UTC day of first activation, and a retention target is only
    reported when it has matured — an immature window reports no value rather than a zero,
    because the difference between "nobody came back" and "it is too early to know" is the
    whole point of the number;
  * and an operating expense does not cease to exist when its operator is excluded from
    human engagement metrics.

The corpus is synthetic and deliberately small, but every denominator in it is non-zero
and every status — available, immature, no_denominator, out_of_range — is exercised, so a
port that gets a definition wrong cannot pass by producing zeroes.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.analytics import (  # noqa: E402
    MAX_SAFE_INTEGER,
    NON_PARTICIPANT_IDS,
    VERSION,
    _ratio,
    _window,
    aggregate_product_analytics,
)

GOLDEN = ROOT / "tests/golden/analytics-golden.json"
DAY = 86_400_000
AS_OF = 10 * DAY
WINDOW = (3 * DAY, 6 * DAY)
COHORTS = (0, 10 * DAY)


def user(user_id: str, created_at: int) -> dict:
    return {"id": user_id, "created_at": created_at}


def forecast(fid: str, creator: str, created: int, *, outcome=None, finalized_at=None,
             clarity=8000, has_ai=1, open_at=0, close_at=10 * DAY, on_hold=0, state="OPEN") -> dict:
    return {"id": fid, "creator_id": creator, "created_at": created, "open_at": open_at, "close_at": close_at,
            "finalized_outcome": outcome, "has_ai": has_ai, "clarity_bp": clarity, "on_hold": on_hold,
            "state_at": state, "finalized_at": finalized_at}


def ratio(numerator: int, denominator: int, unit: str = "share") -> dict:
    return _ratio(numerator, denominator, unit=unit)


def snapshot() -> dict:
    """A corpus where every metric has a non-zero denominator.

    The one exclusion of a forecast is on a question nothing else depends on, because
    excluding a question that carries the activity would leave the report full of zeroes —
    which a port that got every definition wrong would also produce.
    """
    users = [
        user("u-new", 3 * DAY),          # created inside the window, matured by as_of
        user("u-old", 0),                # created before the window
        user("u-late", 9 * DAY),         # created inside the window, not yet matured
        user("staff:ops", 0),            # excluded by namespace
        user("system_editorial", 0),     # reserved non-participant
        user("u-alias", 0),              # linked to u-old
        user("u-blocked", 0),            # excluded by an explicit request
        user("creator-1", 0),            # a creator whose questions were finalized
        user("creator-2", 0),
    ]
    forecasts = [
        forecast("f-in", "creator-1", 3 * DAY),                                 # created in the window
        forecast("f-old", "creator-1", 0, outcome="YES", finalized_at=4 * DAY),
        forecast("f-invalid", "creator-2", 0, outcome="INVALID", finalized_at=4 * DAY),
        forecast("f-pending", "creator-1", 0, finalized_at=4 * DAY),            # no outcome yet
        forecast("f-late", "creator-2", 9 * DAY),                               # created in the window
        forecast("f-hold", "creator-1", 0, on_hold=1),
        forecast("f-excluded", "creator-2", 0),                                 # excluded below
        forecast("test:fixture", "creator-1", 0),                               # excluded by namespace
    ]
    firsts = [
        {"user_id": "u-old", "forecast_id": "f-old", "first_at": 3 * DAY + 1000},
        {"user_id": "u-new", "forecast_id": "f-old", "first_at": 3 * DAY + 2000},
        {"user_id": "u-new", "forecast_id": "test:fixture", "first_at": 3 * DAY + 3000},
        {"user_id": "staff:ops", "forecast_id": "f-old", "first_at": 4 * DAY + 1000},
    ]
    activity = [
        {"user_id": "u-old", "forecast_id": "f-old", "day_ms": 3 * DAY, "first_at": 3 * DAY + 100, "submissions": 2},
        {"user_id": "u-old", "forecast_id": "f-old", "day_ms": 4 * DAY, "first_at": 4 * DAY + 100, "submissions": 1},
        {"user_id": "u-new", "forecast_id": "f-old", "day_ms": 3 * DAY, "first_at": 3 * DAY + 200, "submissions": 1},
        # An alias of an active user is that user, not a second predictor.
        {"user_id": "u-alias", "forecast_id": "f-old", "day_ms": 3 * DAY, "first_at": 3 * DAY + 300, "submissions": 1},
        {"user_id": "u-blocked", "forecast_id": "f-old", "day_ms": 3 * DAY, "first_at": 3 * DAY + 350, "submissions": 1},
        # The creator's own submission on their own question is not external participation.
        {"user_id": "creator-1", "forecast_id": "f-in", "day_ms": 3 * DAY, "first_at": 3 * DAY + 400, "submissions": 1},
        {"user_id": "u-old", "forecast_id": "f-excluded", "day_ms": 3 * DAY, "first_at": 3 * DAY + 500, "submissions": 1},
        {"user_id": "u-old", "forecast_id": "test:fixture", "day_ms": 3 * DAY, "first_at": 3 * DAY + 600, "submissions": 1},
    ]
    # The reference keys submitted disputes by `artifact_hash` and then looks a review up by
    # `dispute_hash`, so a review only counts when the two coincide for its dispute. That is a
    # coupling in the reference rather than in this corpus — the vector reproduces it, and the
    # rows here are shaped so the branch is exercised rather than silently empty.
    disputes = [
        {"command_name": "submit_dispute", "forecast_id": "f-old", "created_at": 4 * DAY + 10,
         "artifact_hash": "h1", "dispute_hash": "h1", "user_id": "u-old", "material_conflict": 1},
        {"command_name": "review_dispute", "forecast_id": "f-old", "created_at": 4 * DAY + 20,
         "artifact_hash": "d2", "dispute_hash": "h1", "user_id": None, "material_conflict": 1},
        {"command_name": "submit_dispute", "forecast_id": "f-old", "created_at": 4 * DAY + 30,
         "artifact_hash": "h2", "dispute_hash": "h2", "user_id": "staff:ops", "material_conflict": 0},
        {"command_name": "review_dispute", "forecast_id": "f-old", "created_at": 4 * DAY + 40,
         "artifact_hash": "d4", "dispute_hash": "h2", "user_id": None, "material_conflict": 0},
    ]
    exclusions = [
        {"subject_kind": "user", "subject_id": "u-blocked", "reason": "request"},
        {"subject_kind": "forecast", "subject_id": "f-excluded", "reason": "duplicate"},
    ]
    # One admitted link. Excluding a user whose identity had been merged into a participant
    # would exclude the participant too, which the reference documents — and which would leave
    # the report full of zeroes rather than exercising it.
    identities = [{"alias_user_id": "u-alias", "canonical_user_id": "u-old"}]
    costs = [
        {"source_kind": "provider", "population_kind": "application", "forecast_id": None,
         "status": "known", "amount_atomic": 100, "operation_id": "op-1"},
        {"source_kind": "provider", "population_kind": "application", "forecast_id": "f-in",
         "status": "estimated", "amount_atomic": 50, "operation_id": "op-2"},
        {"source_kind": "provider", "population_kind": "fixture", "forecast_id": None,
         "status": "known", "amount_atomic": 999, "operation_id": "op-3"},
        {"source_kind": "chain", "population_kind": "application", "forecast_id": "f-in",
         "status": "known", "amount_atomic": 7, "operation_id": "sig-1"},
        {"source_kind": "chain", "population_kind": "application", "forecast_id": "f-excluded",
         "status": "known", "amount_atomic": 999, "operation_id": "sig-2"},
    ]
    deliveries = [{"signature": "sig-1", "operation_id": "sig-1", "forecast_id": "f-in"},
                  {"signature": "sig-9", "operation_id": "sig-9", "forecast_id": "f-in"},
                  {"signature": "sig-9", "operation_id": "sig-9", "forecast_id": "f-excluded"}]
    return {"users": users, "forecasts": forecasts, "firsts": firsts, "activity": activity,
            "disputes": disputes, "exclusions": exclusions, "identities": identities,
            "costs": costs, "deliveries": deliveries, "reserved": [{"reserved": 42}]}


def build() -> dict:
    corpus = snapshot()
    cases = []
    for name, changes in [
        ("application", {}),
        ("fixture", {"population_kind": "fixture"}),
        ("with-excluded-users", {"excluded_user_ids": ("u-new",)}),
    ]:
        arguments = {"as_of_ms": AS_OF, "window_start_ms": WINDOW[0], "window_end_ms": WINDOW[1],
                     "cohort_start_ms": COHORTS[0], "cohort_end_ms": COHORTS[1], **changes}
        population = arguments.pop("population_kind", "application")
        cases.append({"name": name, "arguments": arguments, "populationKind": population,
                      "report": aggregate_product_analytics(corpus, population_kind=population, **arguments)})

    # The window rules, one refusal each.
    windows = []
    for start, end, as_of in [(3 * DAY, 6 * DAY, AS_OF), (0, 0, AS_OF), (DAY + 1, 2 * DAY, AS_OF),
                              (0, 400 * DAY, 500 * DAY), (-1, DAY, AS_OF), (0, DAY, DAY - 1)]:
        entry = {"startMs": start, "endMs": end, "asOfMs": as_of}
        try:
            _window(start, end, as_of)
            entry["accepted"] = True
        except ValueError as error:
            entry["error"] = str(error)
        windows.append(entry)

    return {
        "description": "Product KPI aggregation: 7 users, 7 questions and 5 cost rows, with every "
                       "metric status exercised and every denominator non-zero.",
        "version": VERSION,
        "maxSafeInteger": MAX_SAFE_INTEGER,
        "nonParticipantIds": sorted(NON_PARTICIPANT_IDS),
        "snapshot": corpus,
        "cases": cases,
        "windows": windows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    if arguments.write:
        GOLDEN.write_text(document)
        print(f"wrote {GOLDEN.relative_to(ROOT)}")
        return 0
    if arguments.check:
        current = GOLDEN.read_text() if GOLDEN.exists() else ""
        if current != document:
            print(f"{GOLDEN.relative_to(ROOT)} is stale; regenerate with --write", file=sys.stderr)
            return 1
        print(f"{GOLDEN.relative_to(ROOT)} is current")
        return 0
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
