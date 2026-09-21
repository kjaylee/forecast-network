#!/usr/bin/env python3
"""Export `adjudicate_forecast`, so a Rust port can be held to it.

This is the exceptional ADMIN path — the one place a supplied, reviewed verdict enters the record
without a model having produced it. Four things about it are the whole rule:

  * It **never manufactures a verdict, modifies provenance, or finalizes**. A successful
    adjudication enters PROPOSED, and the ordinary scheduler then opens a fresh, complete challenge
    window. That is why the port has to be held to the *state after*, not to the returned record:
    a port that let the operator's decision finalize directly would look identical at the call site.
  * The decision's timestamp is bounded on both sides — it must follow the current record and be
    within the last fifteen minutes — because a prepared decision travels over HTTP without
    rewriting its hash-bound timestamps.
  * Every piece of evidence the replacement resolution cites has to be *retained* and to hash to
    what the resolution claims. Supplying it with the request is allowed; claiming it without
    either is `missing_resolution_artifact`.
  * The receipt is written inside the same batch as the command, so a retry after a lost response
    returns the original receipt — and the same key with different content is a conflict rather
    than a second adjudication.

The vector records the prepared decision itself (the resolution and the adjudicator record), so the
replay parses the reference's own values rather than a fixture that re-derived them.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests import test_web_application as fixtures  # noqa: E402

GOLDEN = ROOT / "tests/golden/adjudication-golden.json"


async def tables(case) -> dict:
    names = [
        row["name"]
        for row in await case.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: [dict(row) for row in await case.db.all(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


async def fixture():
    case = fixtures.ApplicationTests(methodName="runTest")
    original = case.random_token
    case.produced = []

    def token():
        value = original()
        case.produced.append(value)
        return value

    case.random_token = token
    await case.asyncSetUp()
    return case


async def record(case, action, name: str, decision: dict | None = None, **inputs) -> dict:
    compare = inputs.pop("compare", [])
    produced = getattr(case, "produced", None)
    start = len(produced) if produced is not None else 0
    entry: dict = {"call": name, "input": inputs, "compare": compare,
                   # An action may move the clock part-way through, so the instant it *started* at
                   # travels with the case as well as the one it ended at.
                   "nowBefore": case.now}
    entry["initial"] = await tables(case)
    entry["decision"] = decision
    try:
        entry["result"] = await action(case, decision)
    except AppError as error:
        entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
    entry["now"] = case.now
    if produced is not None:
        entry["tokens"] = produced[start:]
    entry["rows"] = await tables(case)
    return entry


async def build() -> dict:
    cases: list[dict] = []

    # The success path: the operator's decision is accepted and the forecast enters PROPOSED.
    case = await fixture()
    forecast = await case.escalated()
    resolution, adjudicator, artifacts = case.operator_decision(forecast)
    decision = {"resolution": to_dict(resolution), "adjudicator": to_dict(adjudicator),
                "artifacts": [{"hash": item.content_hash, "kind": item.kind,
                               "body": item.body, "mediaType": item.media_type} for item in artifacts],
                "expectedRevision": forecast.revision, "forecastId": forecast.forecast_id}
    cases.append(await record(
        case,
        lambda c, d: c.app.adjudicate_forecast(d["forecastId"], resolution, adjudicator, artifacts,
                                               "operator-valid-key", expected_revision=d["expectedRevision"]),
        "adjudicate:ok", decision, compare=["result"]))
    # And the state it leaves: PROPOSED, with no outcome and no window — the scheduler opens one.
    cases[-1]["stateAfter"] = (await case.app._forecast(forecast.forecast_id)).state.value
    cases[-1]["rows"] = await tables(case)
    case.connection.close()

    # A retry of the same key returns the original receipt; the same key with different content is
    # a conflict rather than a second adjudication.
    case = await fixture()
    forecast = await case.escalated()
    resolution, adjudicator, artifacts = case.operator_decision(forecast)
    decision = {"resolution": to_dict(resolution), "adjudicator": to_dict(adjudicator),
                "artifacts": [{"hash": item.content_hash, "kind": item.kind,
                               "body": item.body, "mediaType": item.media_type} for item in artifacts],
                "expectedRevision": forecast.revision, "forecastId": forecast.forecast_id}

    async def retried(c, d):
        first = await c.app.adjudicate_forecast(d["forecastId"], resolution, adjudicator, artifacts,
                                                "operator-retry-key", expected_revision=d["expectedRevision"])
        c.now += 10
        await c.app.run_due_jobs()
        retry = await c.app.adjudicate_forecast(d["forecastId"], resolution, adjudicator, artifacts,
                                                "operator-retry-key", expected_revision=d["expectedRevision"])
        return {"first": first, "retry": retry,
                "events": (await c.db.first(
                    "SELECT COUNT(*) AS n FROM events WHERE json_extract(event,'$.command_name')"
                    "='adjudicate_resolution'"))["n"]}

    cases.append(await record(case, retried, "adjudicate:retry", decision, compare=["result"]))
    cases.append(await record(
        case,
        lambda c, d: c.app.adjudicate_forecast(d["forecastId"], resolution, adjudicator, (),
                                               "operator-retry-key", expected_revision=d["expectedRevision"]),
        "adjudicate:idempotency-conflict", decision))
    case.connection.close()

    # Evidence that is neither supplied nor retained cannot back a replacement resolution.
    case = await fixture()
    forecast = await case.escalated()
    resolution, adjudicator, artifacts = case.operator_decision(forecast)
    decision = {"resolution": to_dict(resolution), "adjudicator": to_dict(adjudicator),
                "artifacts": [{"hash": item.content_hash, "kind": item.kind,
                               "body": item.body, "mediaType": item.media_type} for item in artifacts],
                "expectedRevision": forecast.revision, "forecastId": forecast.forecast_id}
    cases.append(await record(
        case,
        lambda c, d: c.app.adjudicate_forecast(d["forecastId"], resolution, adjudicator, (),
                                               "operator-missing-key", expected_revision=d["expectedRevision"]),
        "adjudicate:missing-artifact", decision))
    # An adjudicator from a provider that already decided one side is not independent.
    nonindependent = replace(adjudicator, provider=forecast.resolution.judge.provider)
    cases.append(await record(
        case,
        lambda c, d: c.app.adjudicate_forecast(d["forecastId"], resolution, nonindependent, artifacts,
                                               "operator-biased-key", expected_revision=d["expectedRevision"]),
        "adjudicate:not-independent", decision))
    case.connection.close()

    # A stale revision, and a question that is not awaiting adjudication at all.
    case = await fixture()
    forecast = await case.escalated()
    resolution, adjudicator, artifacts = case.operator_decision(forecast)
    decision = {"resolution": to_dict(resolution), "adjudicator": to_dict(adjudicator),
                "artifacts": [{"hash": item.content_hash, "kind": item.kind,
                               "body": item.body, "mediaType": item.media_type} for item in artifacts],
                "expectedRevision": forecast.revision, "forecastId": forecast.forecast_id}
    cases.append(await record(
        case,
        lambda c, d: c.app.adjudicate_forecast(d["forecastId"], resolution, adjudicator, artifacts,
                                               "operator-stale-key",
                                               expected_revision=d["expectedRevision"] - 1),
        "adjudicate:stale-revision", decision))
    case.connection.close()

    # A decision prepared more than fifteen minutes ago: the clock moves rather than the record,
    # because a decision that *predates the record* cannot be built at all — the domain refuses a
    # proposal that predates its own counter-judge, and forging one would test the forging.
    case = await fixture()
    forecast = await case.escalated()
    resolution, adjudicator, artifacts = case.operator_decision(forecast)
    case.now += 16 * 60 * 1000
    stale = resolution
    decision = {"resolution": to_dict(stale), "adjudicator": to_dict(adjudicator),
                "artifacts": [{"hash": item.content_hash, "kind": item.kind,
                               "body": item.body, "mediaType": item.media_type} for item in artifacts],
                "expectedRevision": forecast.revision, "forecastId": forecast.forecast_id}
    cases.append(await record(
        case,
        lambda c, d: c.app.adjudicate_forecast(d["forecastId"], stale, adjudicator, artifacts,
                                               "operator-backdated-key",
                                               expected_revision=d["expectedRevision"]),
        "adjudicate:backdated", decision))
    case.connection.close()

    return {
        "description": "`adjudicate_forecast`: the operator's prepared decision, the receipt that "
                       "makes a retry a retry, and the four ways it fails closed.",
        "cases": cases,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
