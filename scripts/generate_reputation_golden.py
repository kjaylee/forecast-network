#!/usr/bin/env python3
"""Export the quality projections and the two cohorts a signed feed may weight.

`risk_feed._signals` does not invent its cohorts — it asks `reputation.qualified_cohorts` who
counts as an expert and who is in the top decile by Brier loss, and then weights their submissions
into a *signed* probability. So this vector is about arithmetic that ends up inside a signature,
where being approximately right is the same as being wrong.

Four things the corpus is built to expose:

  * The two gates are separate. `expert` needs twenty samples across three distinct finalization
    days *and* a Brier at or below 0.20 *and* a calibration at or above 0.70; `top` needs only ten
    samples and ranks on Brier alone. A forecaster can be in one and not the other, which is why
    the two cohorts overlap without being equal.
  * `top` is the first `(n + 9) // 10` of a ranking, so it is *empty* below eleven ranked users.
    A port that reached for "the best tenth" by a different rounding would return a different
    cohort and a different signed probability.
  * The ranking sorts `(brierScore, user_id)`, and the user id is a real tiebreak — two users with
    identical histories must come out in a determinate order or the signature stops being stable.
  * The target forecast is excluded from the history that qualifies it, and a row whose identifier
    or `(user, forecast)` pair is malformed *raises* where a row with a bad timestamp is merely
    skipped. Which of the two a bad row gets is not a detail: skipping where the reference raised
    reports a quality score for a corpus that could not be scored.

Every fixture travels once, under a name, and each case names the one it read. Inlining the corpus
per call would make a vector nobody reads; a case whose rows were only implicit in the caller is
one a port cannot replay.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.reputation import (  # noqa: E402
    eligible_history,
    qualified_cohorts,
    reputation_quality,
)
from golden_cli import golden_main  # noqa: E402

GOLDEN = ROOT / "tests/golden/reputation-golden.json"
NOW = 1_800_000_000_000
DAY = 86_400_000


def row(user, forecast, probability, outcome, finalized_at, *, category="CRYPTO",
        state="FINALIZED", eligible=1, finalized_outcome=None) -> dict:
    """One `forecast_quality_history` row, with the joined columns the view supplies."""
    return {
        "user_id": user, "forecast_id": forecast, "category": category, "probability": probability,
        "outcome": outcome, "finalized_outcome": finalized_outcome or outcome, "state": state,
        "eligible": eligible,
        "submitted_at": finalized_at - 1000,
        "finalized_at": finalized_at,
        "eligibility_at": finalized_at + 500,
    }


def sample(user, probability, outcome, count, *, days=3, category="CRYPTO",
           first=NOW - 12 * DAY) -> list[dict]:
    """`count` finalized results for one forecaster, spread over `days` distinct days.

    The spread is not cosmetic: `_qualification` counts *distinct finalization days*, so twenty
    rows written on one day are twenty samples and one day, and that is not an expert.
    """
    return [
        row(user, f"{user}-f{index:03d}", probability, outcome, first + (index % days) * DAY,
            category=category)
        for index in range(count)
    ]


def build() -> dict:
    # A dozen forecasters with ten or more samples each, so the top decile is two people and not
    # one: a cohort rule that only ever selects the single best forecaster looks correct on a
    # corpus that cannot tell the difference.
    corpus: list[dict] = []
    for index in range(12):
        # Probability drives the Brier loss, so a lower probability against a YES outcome is a
        # worse forecaster — and that ordering is what the cohort is ranked on.
        corpus.extend(sample(f"u-ranked-{index:02d}", 95 - index, "YES", 10 + index))
    # Two forecasters with identical histories: the ranking has to break the tie on the id.
    corpus.extend(sample("u-tie-b", 70, "YES", 11))
    corpus.extend(sample("u-tie-a", 70, "YES", 11))
    # An expert on the gates: 24 samples over 4 days, Brier 0.01, calibration 0.9.
    corpus.extend(sample("u-expert", 90, "YES", 24, days=4))
    # Twenty-two samples in *another* category: enough there, not for CRYPTO.
    corpus.extend(sample("u-other-category", 90, "YES", 22, days=4, category="SPORTS"))
    # Nine samples: below the expert floor and below `top`'s as well.
    corpus.extend(sample("u-thin", 99, "YES", 9))
    # Eleven samples on one day: eleven samples and one distinct finalization day.
    corpus.extend(sample("u-one-day", 99, "YES", 11, days=1))
    # Old enough to fall outside the expert window, still recent enough to be ranked.
    corpus.extend(sample("u-stale", 99, "YES", 12, first=NOW - 400 * DAY))
    # Rows the projection must skip rather than count, five ways.
    corpus.append(row("u-skip", "u-skip-open", 80, "YES", NOW - 2 * DAY, state="OPEN"))
    corpus.append(row("u-skip", "u-skip-nomatch", 80, "NO", NOW - 2 * DAY, finalized_outcome="YES"))
    corpus.append(row("u-skip", "u-skip-future", 80, "YES", NOW + DAY))
    corpus.append(row("u-skip", "u-skip-prob", 101, "YES", NOW - 2 * DAY))
    corpus.append(row("u-skip", "u-skip-hold", 80, "YES", NOW - 2 * DAY, eligible=0))

    single = sample("u-solo", 90, "YES", 12, days=4)
    pair = single + sample("u-second", 80, "YES", 12, days=4)
    # The same forecaster and forecast twice, which the reference refuses outright.
    duplicated = [
        row("u-dup", "u-dup-f", 80, "YES", NOW - 3 * DAY),
        row("u-dup", "u-dup-f", 70, "YES", NOW - 2 * DAY),
    ]
    # The empty fixture is here rather than passed inline: a case whose rows were not recorded
    # is a case a port cannot replay, and an empty corpus is the one nobody thinks to record.
    fixtures = {"corpus": corpus, "single": single, "pair": pair, "duplicated": duplicated, "empty": []}
    target = "u-ranked-00-f000"
    cases = [
        # The history projection, at a cutoff, inside a window, and excluding the target forecast.
        ("history:all", "corpus", eligible_history, {"as_of_ms": NOW}, lambda: eligible_history(corpus, as_of_ms=NOW)),
        ("history:window", "corpus", eligible_history, {"as_of_ms": NOW, "window_ms": 10 * DAY},
         lambda: eligible_history(corpus, as_of_ms=NOW, window_ms=10 * DAY)),
        ("history:exclude", "corpus", eligible_history, {"as_of_ms": NOW, "exclude_forecast_id": target},
         lambda: eligible_history(corpus, as_of_ms=NOW, exclude_forecast_id=target)),
        # The two arguments the reference type-checks, and the values it refuses.
        ("history:as-of-not-an-int", "corpus", eligible_history, {"as_of_ms": "now"},
         lambda: eligible_history(corpus, as_of_ms="now")),
        ("history:as-of-negative", "corpus", eligible_history, {"as_of_ms": -1},
         lambda: eligible_history(corpus, as_of_ms=-1)),
        ("history:window-zero", "corpus", eligible_history, {"as_of_ms": NOW, "window_ms": 0},
         lambda: eligible_history(corpus, as_of_ms=NOW, window_ms=0)),
        # One forecaster and forecast twice: the reference refuses the corpus, not the row.
        ("history:duplicate", "duplicated", eligible_history, {"as_of_ms": NOW},
         lambda: eligible_history(duplicated, as_of_ms=NOW)),
        ("history:identifier-not-a-string", "duplicated", eligible_history, {"as_of_ms": NOW, "user_id": 7},
         lambda: eligible_history([{**duplicated[0], "user_id": 7}], as_of_ms=NOW)),
        ("history:identifier-empty", "duplicated", eligible_history, {"as_of_ms": NOW, "forecast_id": ""},
         lambda: eligible_history([{**duplicated[0], "forecast_id": ""}], as_of_ms=NOW)),

        ("quality:one-user", "single", reputation_quality, {"as_of_ms": NOW},
         lambda: reputation_quality(single, as_of_ms=NOW)),
        ("quality:category", "single", reputation_quality, {"as_of_ms": NOW, "category": "crypto"},
         lambda: reputation_quality(single, as_of_ms=NOW, category="crypto")),
        ("quality:category-unknown", "single", reputation_quality,
         {"as_of_ms": NOW, "category": "SPORTS"},
         lambda: reputation_quality(single, as_of_ms=NOW, category="SPORTS")),
        ("quality:two-users", "pair", reputation_quality, {"as_of_ms": NOW},
         lambda: reputation_quality(pair, as_of_ms=NOW)),
        ("quality:category-not-a-string", "single", reputation_quality,
         {"as_of_ms": NOW, "category": 7},
         lambda: reputation_quality(single, as_of_ms=NOW, category=7)),

        ("cohorts:crypto", "corpus", qualified_cohorts,
         {"forecast_id": target, "category": "CRYPTO", "as_of_ms": NOW},
         lambda: qualified_cohorts(corpus, forecast_id=target, category="CRYPTO", as_of_ms=NOW)),
        ("cohorts:lowercase", "corpus", qualified_cohorts,
         {"forecast_id": target, "category": "crypto", "as_of_ms": NOW},
         lambda: qualified_cohorts(corpus, forecast_id=target, category="crypto", as_of_ms=NOW)),
        ("cohorts:sports", "corpus", qualified_cohorts,
         {"forecast_id": target, "category": "SPORTS", "as_of_ms": NOW},
         lambda: qualified_cohorts(corpus, forecast_id=target, category="SPORTS", as_of_ms=NOW)),
        ("cohorts:no-exclusion", "corpus", qualified_cohorts,
         {"forecast_id": "nothing", "category": "CRYPTO", "as_of_ms": NOW},
         lambda: qualified_cohorts(corpus, forecast_id="nothing", category="CRYPTO", as_of_ms=NOW)),
        # Below eleven ranked forecasters the top decile rounds to nobody, which is the reason the
        # corpus has twelve.
        ("cohorts:few", "pair", qualified_cohorts,
         {"forecast_id": target, "category": "CRYPTO", "as_of_ms": NOW},
         lambda: qualified_cohorts(pair, forecast_id=target, category="CRYPTO", as_of_ms=NOW)),
        ("cohorts:empty", "empty", qualified_cohorts,
         {"forecast_id": target, "category": "CRYPTO", "as_of_ms": NOW},
         lambda: qualified_cohorts([], forecast_id=target, category="CRYPTO", as_of_ms=NOW)),
        ("cohorts:category-not-a-string", "corpus", qualified_cohorts,
         {"forecast_id": target, "category": None, "as_of_ms": NOW},
         lambda: qualified_cohorts(corpus, forecast_id=target, category=None, as_of_ms=NOW)),
    ]

    recorded = []
    for name, fixture, function, arguments, call_it in cases:
        entry: dict = {
            "call": name,
            "kind": function.__name__,
            "input": {"rows": fixture, **arguments},
        }
        try:
            entry["result"] = call_it()
        except (ValueError, TypeError) as error:
            entry["error"] = {"type": type(error).__name__, "message": str(error)}
        recorded.append(entry)

    return {
        "description": "The quality projections and the two cohorts a signed risk feed weights: "
                       "eligibility at a cutoff, per-window consistency, expertise gates, and the "
                       "top decile by Brier loss.",
        "now": NOW,
        "fixtures": fixtures,
        "calls": recorded,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
