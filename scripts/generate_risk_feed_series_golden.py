#!/usr/bin/env python3
"""Export the recurring-episode scheduler, so a Rust port can be held to it.

A series is an operator-approved template that publishes and binds the next question *before* the
window it asks about begins. The interesting part is not the cadence arithmetic — it is that
nothing here bypasses the ordinary canonical seed path or the ordinary typed-target approval, and
that a failure is recorded and retried on the next tick rather than inside the tick that failed.

Four things the vector is built to expose:

  * `next_episode_start`. With a latest episode it is that start plus one cadence while that is still
    ahead — a missed successor is skipped for the next boundary after now; without one it is
    the next cadence boundary, and a `now` that already *is* a boundary advances rather than
    returning itself. Off-by-one here publishes an episode a cadence early or repeats one.
  * `episode_question`. The template's four named fields are filled from the episode's own window,
    and the result is bounded — the compiler refuses a longer question, so a port that formatted one
    differently would fail at the *next* stage with a worse message.
  * The retry backoff. A failed attempt within `RETRY_MS` is not retried, and the tick reports when
    it will be: what fails here costs AI budget, so the pace is the budget's and not the loop's.
  * `episode_binding`. The typed target comes from the *published* question's own measurement
    interval, and the two hashes commit to both the series and the question it produced.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_application.risk_feed_series import (  # noqa: E402
    configure_series,
    create_due_episodes,
    episode_question,
    next_episode_start,
)
from forecast_application.risk_feed_v2 import (  # noqa: E402
    admit_definition,
    admit_profile,
    operational_bindings_v2,
)
from forecast_domain.errors import ValidationError  # noqa: E402
from forecast_domain.models import Category  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests import test_web_application as fixtures  # noqa: E402
from tests.test_risk_feed_v2_contract import (  # noqa: E402
    golden_definition,
    golden_profile,
    golden_series,
)
from tests.test_web_ai import specification  # noqa: E402

GOLDEN = ROOT / "tests/golden/risk-feed-series-golden.json"
HOUR = 3_600_000

TABLES = [
    "users", "forecasts", "artifacts", "risk_feed_definitions_v2", "risk_feed_profiles_v2",
    "risk_feed_series_v2", "risk_feed_series_log_v2", "risk_feed_bindings_v2",
    "risk_feed_binding_revocations_v2", "risk_feed_heads",
]


class Fixture:
    def __init__(self, case) -> None:
        self.case = case
        self.calls: list[dict] = []
        self.questions: list[str] = []

    @property
    def db(self):
        return self.case.db

    async def call(self, name: str, kind: str, awaitable, **inputs) -> object:
        entry: dict = {"call": name, "kind": kind, "input": inputs}
        try:
            result = await awaitable
        except (AppError, ValidationError) as error:
            entry["error"] = {"type": type(error).__name__,
                              "message": getattr(error, "message", None) or str(error)}
            self.calls.append(entry)
            return None
        if kind == "episode_question":
            self.questions.append(result)
        entry["result"] = result
        self.calls.append(entry)
        return result

    async def rows(self) -> dict:
        return {table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in TABLES}


async def build() -> dict:
    case = fixtures.ApplicationTests(methodName="runTest")
    await case.asyncSetUp()
    fixture = Fixture(case)
    db = fixture.db

    definition, profile = golden_definition(), golden_profile()
    await admit_definition(db, feed_id="risk-v2", definition=definition, approved_by="a", now_ms=case.now)
    await admit_profile(db, feed_id="risk-v2", profile=profile, approved_by="a", now_ms=case.now)
    series = replace(golden_series(), feed_id="risk-v2")

    # --- configure: the series has to agree with the records it cites, and cite records that
    # were admitted under the same feed.
    await fixture.call("configure:unadmitted-definition", "configure_series",
                       configure_series(db, series=replace(series, definition_hash="0" * 64), enabled=True,
                                        configured_by="a", now_ms=case.now),
                       feedId="risk-v2")
    await fixture.call("configure:bad-actor", "configure_series",
                       configure_series(db, series=series, enabled=True, configured_by=" ", now_ms=case.now),
                       feedId="risk-v2")
    # A malformed feed id cannot even be constructed: the domain record validates its own, so
    # `_feed`'s check is reachable only from the HTTP layer, which passes a plain string. Recorded
    # as a note rather than a case, because a case that cannot be built tests nothing.
    await fixture.call("configure:ok", "configure_series",
                       configure_series(db, series=series, enabled=True, configured_by="a", now_ms=case.now),
                       feedId="risk-v2")
    await fixture.call("configure:again", "configure_series",
                       configure_series(db, series=series, enabled=True, configured_by="a", now_ms=case.now),
                       feedId="risk-v2")

    # --- the cadence arithmetic, against fixed instants rather than the clock.
    cadence = series.cadence_ms
    for name, latest, now in [
        ("next:no-latest-mid", None, 1_800_000_000_123),
        ("next:no-latest-boundary", None, 1_800_000_000_000 - (1_800_000_000_000 % cadence)),
        ("next:after-latest", 1_800_000_000_000, 1_800_000_000_123),
        # The successor of the latest start has itself passed: the next boundary after now, not
        # the missed one. The instant is two cadences and a bit past the latest start.
        ("next:after-latest-missed", 1_800_000_000_000, 1_800_000_000_000 + 2 * cadence + 5_000),
        # Exactly at the successor, the successor has started.
        ("next:after-latest-at-successor", 1_800_000_000_000, 1_800_000_000_000 + cadence),
    ]:
        decision = next_episode_start(series, latest_start_ms=latest, now_ms=now)
        fixture.calls.append({"call": name, "kind": "next_episode_start",
                              "input": {"latestStartMs": latest, "nowMs": now}, "result": decision})

    # The episode the scheduler will choose, taken from the reference's own answer above rather
    # than assumed: the boundary is cadence-aligned, and a hand-picked start is simply a different
    # instant that no tick would have selected.
    start = next_episode_start(series, latest_start_ms=None, now_ms=1_800_000_000_123)
    question = episode_question(series, start)
    fixture.questions.append(question)
    fixture.calls.append({"call": "question:first", "kind": "episode_question",
                          "input": {"startMs": start}, "result": question})

    # --- a tick with the lead window open: one episode, published through the real seed path.
    case.now = start - series.lead_ms + 1000
    seeded: list[dict] = []

    async def seed(text):
        # The seed path is the application's, not this module's, so what it *returns* is part of
        # the vector: a port replays the tick against the card the reference was given.
        seeded.append({"question": text})
        end = int(datetime.strptime(text.split(", ")[1].split(")")[0], "%Y-%m-%dT%H:%M:%SZ")
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)
        original_spec = fixtures.fixtures.specification
        policy = specification().source_policy
        with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw: original_spec(
                **{**kw, "source_policy": policy, "close_at_ms": end, "category": Category.CRYPTO})):
            draft = await case.app.compile_forecast(case.uid, text)
            card = (await case.app.publish_forecast(case.uid, draft["draftId"], "publish-series"))["forecast"]
            seeded[-1]["card"] = card
            return {"forecast": card}

    await fixture.call("tick:first", "create_due_episodes",
                       create_due_episodes(db, now_ms=case.now, seed=seed), nowMs=case.now)
    await fixture.call("operational", "operational_bindings_v2",
                       operational_bindings_v2(db, feed_id="risk-v2", now_ms=start + 1000), feedId="risk-v2")
    # The next tick, a moment later, must not create a second episode for the same start.
    await fixture.call("tick:again", "create_due_episodes",
                       create_due_episodes(db, now_ms=case.now, seed=seed), nowMs=case.now)

    # --- a failing seed is logged and backed off rather than retried inside the tick.
    failure = {"kind": "ValidationError", "message": "the compiler refused this episode", "code": None}

    async def failing_seed(text):
        # The question is recorded with the others, so a replay knows which seed call failed as
        # well as how. The failure itself is the seed's own: the reference records the exception's
        # type and message rather than a name it chose.
        seeded.append({"question": text})
        raise ValidationError(failure["message"])

    # A second series whose seed always fails: the tick records the failure and reports when it
    # will retry. Its template differs from the first's, so the seed path can tell the two apart —
    # a vector where both ask the same question cannot replay a seed that fails for one of them.
    other = replace(series, series_id="usdc-depeg-7d-w48",
                    question_template=series.question_template.replace("USDC/USD", "USDT/USD"))
    await db.execute(
        "INSERT INTO risk_feed_series_v2(series_id,feed_id,series_hash,series_json,enabled,configured_by,updated_at)"
        " VALUES(?,?,?,?,1,'a',?)",
        (other.series_id, other.feed_id, "0" * 64, json.dumps(to_dict(other)), case.now))
    await fixture.call("tick:failing", "create_due_episodes",
                       create_due_episodes(db, now_ms=case.now, seed=failing_seed), nowMs=case.now)
    await fixture.call("tick:backoff", "create_due_episodes",
                       create_due_episodes(db, now_ms=case.now + 1000, seed=failing_seed), nowMs=case.now + 1000)

    rows = await fixture.rows()
    await case.asyncTearDown()

    return {
        "description": "The recurring-episode scheduler: the cadence arithmetic, the templated "
                       "question, the tick that publishes through the real seed path, and the "
                       "backoff after a failure.",
        "series": to_dict(series),
        "questions": fixture.questions,
        "failedSeed": failure,
        "seeded": seeded,
        "asked": [entry["question"] for entry in seeded],
        "calls": fixture.calls,
        "rows": rows,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__, default=to_dict))
