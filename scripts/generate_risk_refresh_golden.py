#!/usr/bin/env python3
"""Export the bound-prediction refresh, so a Rust port can be held to it.

This is the last live piece of the risk pipeline: an operator asks for a fresh AI estimate of an
exact approved binding, and the estimate is written under a guard. Everything interesting is about
the *await* in the middle — the AI call is the one effect here with a network behind it, and during
it the binding can be revoked, the forecast paused, or a participation hold placed.

Five things the vector is built to expose:

  * The currency query. A binding that is revoked, paused, held, or outside its own authorization
    window is not refreshable, and each of those is a different reason reached the same way.
  * The guard batch. It re-runs the currency query *and* checks the lease inside the transaction,
    with `f.revision` and `f.ai_forecast` pinned to the values the estimate was built from. A port
    that dropped those from the guard would overwrite an estimate that landed in between.
  * The clock artifacts. One refresh retains *every* role separately — when the source was captured
    against when the evaluation finished — and never edits the estimate artifact, which keeps saying
    what *it* was as-of.
  * The two stale-estimate rules. An estimate whose `asOf` predates the start of the workflow, or
    which is not newer than the one already stored, is a conflict rather than a refresh.
  * The deadline. The reference checks the wall clock after the work returns, because a suspended
    Worker comes back with a result the 240-second rule has already invalidated.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_application.risk_refresh import (  # noqa: E402
    _clock_statements,
    refresh_bound_prediction_v2,
)
from forecast_application.sources import Artifact  # noqa: E402
from forecast_domain.serialization import content_hash  # noqa: E402
from golden_cli import golden_main, skipped  # noqa: E402

from tests.risk_feed_fixtures import GENESIS  # noqa: E402

GOLDEN = ROOT / "tests/golden/risk-refresh-golden.json"

try:
    # The producer fixture signs with a real Ed25519 key; without `cryptography` the vector
    # cannot be built, and the check passes here so that the full job is where it is held.
    from tests.test_risk_feed_v2_producer import RiskFeedV2ProducerTests  # noqa: E402
except unittest.SkipTest as reason:  # pragma: no cover
    raise SystemExit(skipped(GOLDEN, reason))

# The tables the refresh writes, and the ones its currency query reads. `users` is in the list
# because `forecasts.creator_id` references it: a port replaying the fixture has to restore the
# whole chain, not just the rows the query selects.
TABLES = [
    "users", "artifacts", "forecasts", "risk_prediction_clocks_v2", "risk_feed_bindings_v2",
    "risk_feed_binding_revocations_v2",
]


def artifact(kind: str, body: dict) -> Artifact:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return Artifact(content_hash=content_hash(body), kind=kind, body=text, media_type="application/json")


async def build() -> dict:
    """Driven through the v2 producer's own fixture: a refresh needs an approved binding on a
    published question whose sources the transport can actually serve, and that fixture is the one
    that already builds it."""
    case = RiskFeedV2ProducerTests(methodName="runTest")
    await case.asyncSetUp()
    db, app, binding = case.db, case.app, case.binding

    estimate = artifact("ai-forecast", {"as_of_ms": case.now, "specification_hash": binding.specification_hash})
    sources = artifact("risk-prediction-sources", {"bundle": "x"})
    clocks = _clock_statements([estimate, sources], forecast_id=binding.forecast_id,
                               specification_hash=binding.specification_hash,
                               started_ms=case.now - 5000, completed_ms=case.now)
    # The *inputs* travel with the statements: a clock is a record of four hashes and five
    # instants, and a port held to the statements but not to the values would be reproducing the
    # shape rather than the clock.
    clock_inputs = {
        "estimateHash": estimate.content_hash, "sourceHash": sources.content_hash,
        "specificationHash": binding.specification_hash, "forecastId": binding.forecast_id,
        "asOfMs": case.now, "startedMs": case.now - 5000, "completedMs": case.now,
    }
    calls = [
        {"call": "clock:both", "kind": "clock_statements",
         "input": {"kinds": ["ai-forecast", "risk-prediction-sources"], **clock_inputs},
         "result": [[row[0], list(row[1])] for row in clocks]},
        # The estimate alone is not enough: without the source bundle there is no clock to retain,
        # and the reference returns nothing rather than recording half of one.
        {"call": "clock:estimate-only", "kind": "clock_statements",
         "input": {"kinds": ["ai-forecast"]}, "result": []},
    ]

    async def refresh(name: str, *, binding_id: str | None = None, mutate=None) -> None:
        if mutate is not None:
            await mutate()
        target = binding.binding_id if binding_id is None else binding_id
        try:
            result = await refresh_bound_prediction_v2(app, target)
        except AppError as error:
            calls.append({"call": name, "kind": "refresh_bound_prediction_v2", "input": {"bindingId": target},
                          "error": {"status": error.status, "code": error.code, "message": error.message}})
            return
        calls.append({"call": name, "kind": "refresh_bound_prediction_v2", "input": {"bindingId": target},
                      "result": result})

    # The state the first call sees, recorded *before* it runs. The rows below are the final
    # state, and a port that restored those would be replaying a refresh against an estimate the
    # refresh itself wrote — which is how this vector first caught its own fixture.
    initial = {table: [dict(row) for row in await db.all(f"SELECT * FROM {table} ORDER BY rowid")]
               for table in TABLES}

    # The producer's fixture sets the clock to the operational start; currency needs the binding
    # inside its *authorization* window, which opened with the question.
    case.now = case.start + 1000
    await refresh("refresh:unknown-binding", binding_id="no-such-binding")
    await refresh("refresh:ok")
    # The clock has not moved, so the coordinator stamps the same `asOf` the stored estimate has:
    # an estimate that is not newer is a conflict, not a refresh.
    await refresh("refresh:not-newer")

    async def paused() -> None:
        await db.execute("UPDATE forecasts SET state='PAUSED' WHERE id=?", (binding.forecast_id,))

    async def resumed() -> None:
        await db.execute("UPDATE forecasts SET state='OPEN' WHERE id=?", (binding.forecast_id,))

    async def revoked() -> None:
        await db.execute(
            "INSERT INTO risk_feed_binding_revocations_v2(binding_id,revoked_by,revoked_at,reason) VALUES(?,?,?,?)",
            (binding.binding_id, "admin", case.now, "operator withdrew it"))

    await refresh("refresh:paused", mutate=paused)
    await refresh("refresh:resumed", mutate=resumed)
    await refresh("refresh:revoked", mutate=revoked)
    await refresh("refresh:after-revocation")

    rows = {table: [dict(row) for row in await db.all(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES}
    await case.asyncTearDown()

    return {
        "description": "The bound-prediction refresh: the currency query, the clock roles, and the "
                       "stale-estimate rule — over the v2 producer's own approved binding.",
        "genesis": GENESIS,
        "clockArtifacts": {"estimate": estimate.body, "sources": sources.body},
        "initial": initial,
        "calls": calls,
        "rows": rows,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__, default=lambda value: getattr(value, "body", str(value))))
