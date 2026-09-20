#!/usr/bin/env python3
"""Export the outbox and the sweep that drains it, so a Rust port can be held to them.

`_process_outbox` is where a finalized question's consequences are *delivered* — the reputation it
scored, the points it settled, the notification a participant reads. Three things about it are the
whole rule, and each is a place where a plausible port is a different behaviour:

  * It is **exactly-once by selection, not by a flag**. Every statement carries
    `EXISTS(SELECT 1 FROM outbox WHERE id=? AND status='pending')` in its own WHERE clause, and the
    status flips in the same batch. A replay selects nothing because the row is no longer pending —
    so a port that checked a flag it read earlier would settle twice, and one that flipped the
    status first would settle never.
  * A **blocked** forecast is not drained at all: the selection excludes it, so a question whose
    evidence review is still open cannot be scored, settled or announced while it waits.
  * `RESOLUTION_COMMITMENT_REQUIRED` is not a delivery — it *hands the row to the chain adapter*
    and marks it `awaiting_adapter`, which is a different status from `processed` because it means
    a different thing is now responsible.

The settlement statement is the other half and is compared as SQL: it is one set-based insert whose
id is derived from the forecast and the user, so a second run names the same rows and inserts none.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_application.points import MAX_BALANCE, settlement_sql  # noqa: E402

from tests import test_web_application as fixtures  # noqa: E402

GOLDEN = ROOT / "tests/golden/outbox-golden.json"


async def tables(case) -> dict:
    names = [
        row["name"]
        for row in await case.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: [dict(row) for row in await case.db.all(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


async def fixture(cls=fixtures.ApplicationTests):
    case = cls(methodName="runTest")
    original = case.random_token
    case.produced = []

    def token():
        value = original()
        case.produced.append(value)
        return value

    case.random_token = token
    await case.asyncSetUp()
    return case


async def record(case, action, name: str, **inputs) -> dict:
    compare = inputs.pop("compare", [])
    produced = getattr(case, "produced", None)
    start = len(produced) if produced is not None else 0
    entry: dict = {"call": name, "input": inputs, "compare": compare}
    if case is not None:
        entry["initial"] = await tables(case)
    try:
        entry["result"] = await action(case)
    except AppError as error:
        entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
    # The clock is read *after* the call, because an action may move it: the reference's own
    # `_sweep` sets `self.now` before running, and a replay has to be at the instant the pass ran.
    entry["now"] = case.now if case is not None else None
    if produced is not None:
        entry["tokens"] = produced[start:]
    if case is not None:
        entry["rows"] = await tables(case)
    return entry


async def finalized(case):
    """A question carried all the way to FINALIZED, with one scored participant.

    Driven through the application rather than assembled, because the rows a finalize leaves
    behind are the *input* the outbox works from: a fixture that wrote them by hand would be
    testing its own idea of what a finalize produces.
    """
    forecast = await case.challenge()
    case.now = forecast.challenge_until_ms + 1
    await case.app.run_due_jobs()
    return forecast


async def build() -> dict:
    cases: list[dict] = []

    # --- settlement_sql: one set-based insert, and its three refusals.
    for label, forecast_id, now in [
        ("valid", "f-abcd", 1_800_000_000_000),
        ("empty-id", "", 1_800_000_000_000),
        ("control-char", "f\x01id", 1_800_000_000_000),
        ("too-long", "f" * 129, 1_800_000_000_000),
        ("negative-time", "f-abcd", -1),
        ("over-ceiling", "f-abcd", MAX_BALANCE + 1),
        ("wrong-type", "f-abcd", True),
    ]:
        entry: dict = {"call": f"settlement:{label}", "input": {"forecastId": forecast_id, "now": now},
                       "compare": ["result"], "now": None}
        try:
            entry["result"] = [{"sql": sql, "params": list(params)} for sql, params in settlement_sql(forecast_id, now)]
        except AppError as error:
            entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
        cases.append(entry)

    # --- _process_outbox: nothing pending is nothing done.
    case = await fixture()
    cases.append(await record(case, lambda c: c.app._process_outbox(15), "outbox:empty", limit=15,
                              compare=["result"]))
    case.connection.close()

    # A commitment row is handed to the adapter and left awaiting it, which is not `processed`.
    case = await fixture()
    forecast = await case.publish()
    await case.db.execute(
        "INSERT INTO outbox(id,forecast_id,kind,created_at,status) VALUES('ob-commit','%s','RESOLUTION_COMMITMENT_REQUIRED',?, 'pending')"
        % forecast["id"], (case.now,))

    async def commitment(c):
        first = await c.app._process_outbox(15)
        second = await c.app._process_outbox(15)
        return {"first": first, "second": second}

    cases.append(await record(case, commitment, "outbox:commitment", forecastId=forecast["id"], limit=15,
                              compare=["result"]))
    case.connection.close()

    # A kind nothing knows is left pending rather than marked done: the row is a claim this
    # version cannot honour, and marking it processed would lose it.
    case = await fixture()
    forecast = await case.publish()
    await case.db.execute(
        "INSERT INTO outbox(id,forecast_id,kind,created_at,status) VALUES('ob-unknown',?, 'SOMETHING_ELSE',?, 'pending')",
        (forecast["id"], case.now))
    cases.append(await record(case, lambda c: c.app._process_outbox(15), "outbox:unknown",
                              forecastId=forecast["id"], limit=15, compare=["result"]))
    case.connection.close()

    # A blocked forecast is not drained at all — not scored, not settled, not announced.
    case = await fixture()
    forecast = await finalized(case)
    await case.db.execute(
        "INSERT INTO outbox(id,forecast_id,kind,created_at,status) VALUES('ob-blocked',?, 'REPUTATION_UPDATE_REQUIRED',?, 'pending')",
        (forecast.forecast_id, case.now))
    # A timing review is what the blocker view reads, and its trigger hash is a foreign key onto
    # the retained artifacts — a review nobody kept the evidence for is not a review.
    await case.db.execute(
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'early-resolution-trigger','{}','application/json',?)",
        ("a" * 64, case.now))
    await case.db.execute(
        "INSERT INTO forecast_timing_reviews(forecast_id,specification_hash,trigger_hash,event_at,event_time_basis,created_at) "
        "VALUES(?,(SELECT specification_hash FROM forecasts WHERE id=?),?,0,'observed_upper_bound',?)",
        (forecast.forecast_id, forecast.forecast_id, "a" * 64, case.now))
    cases.append(await record(case, lambda c: c.app._process_outbox(15), "outbox:blocked",
                              forecastId=forecast.forecast_id, limit=15, compare=["result"]))
    case.connection.close()

    # The reputation and the settlement, on a finalized question with a scored participant.
    case = await fixture()
    forecast = await finalized(case)
    # The sweep already drained the rows a finalize writes; this is the same work asked for again
    # on a fresh row, which is what pins the exactly-once selection rather than the flag.
    await case.db.execute(
        "INSERT INTO outbox(id,forecast_id,kind,created_at,status) VALUES('ob-rep',?, 'REPUTATION_UPDATE_REQUIRED',?, 'pending')",
        (forecast.forecast_id, case.now))
    cases.append(await record(case, lambda c: c.app._process_outbox(15), "outbox:reputation",
                              forecastId=forecast.forecast_id, limit=15, compare=["result"]))
    case.connection.close()

    # The notification, on the same shape of row.
    case = await fixture()
    forecast = await finalized(case)
    await case.db.execute(
        "INSERT INTO outbox(id,forecast_id,kind,created_at,status) VALUES('ob-note',?, 'RESULT_NOTIFICATION_REQUIRED',?, 'pending')",
        (forecast.forecast_id, case.now))
    cases.append(await record(case, lambda c: c.app._process_outbox(15), "outbox:notification",
                              forecastId=forecast.forecast_id, limit=15, compare=["result"]))
    case.connection.close()

    # A reputation row on a question that has not finalized inserts nothing — the statement's own
    # WHERE clause refuses — and is still marked processed, because there is nothing left to do.
    case = await fixture()
    forecast = await case.publish()
    await case.db.execute(
        "INSERT INTO outbox(id,forecast_id,kind,created_at,status) VALUES('ob-early',?, 'REPUTATION_UPDATE_REQUIRED',?, 'pending')",
        (forecast["id"], case.now))
    cases.append(await record(case, lambda c: c.app._process_outbox(15), "outbox:not-finalized",
                              forecastId=forecast["id"], limit=15, compare=["result"]))
    case.connection.close()

    # --- the sweep: the whole pass, including what it cleans up and what it reports.
    case = await fixture()
    forecast = await case.challenge()
    cases.append(await record(
        case,
        lambda c: _sweep(c, forecast.challenge_until_ms - 1),
        "sweep:before-deadline", forecastId=forecast.forecast_id, challengeUntil=forecast.challenge_until_ms,
        compare=["result"]))
    cases.append(await record(
        case,
        lambda c: _sweep(c, forecast.challenge_until_ms + 1),
        "sweep:finalize", forecastId=forecast.forecast_id, challengeUntil=forecast.challenge_until_ms,
        compare=["result"]))
    case.connection.close()

    # An expired session, lease and rate limit are all cleared by the same pass.
    case = await fixture()
    forecast = await case.publish()
    await case.db.execute(
        "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES('stale',?,?,?)",
        (case.uid, case.now - 1, case.now - 100))
    await case.db.execute(
        "INSERT INTO ai_leases(owner,token,expires_at) VALUES('stale-owner','stale-token',?)", (case.now - 1,))
    await case.db.execute(
        "INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES('stale-scope',0,1,?)",
        (case.now - 86_400_000 - 1,))
    cases.append(await record(case, lambda c: c.app.run_due_jobs(), "sweep:cleanup",
                              forecastId=forecast["id"], compare=["result"]))
    case.connection.close()

    # --- the sweep's failure handling: three ways an attempt can end without advancing.
    #
    # A blocked question is a *refusal*: the evidence review is still open, so the pass records the
    # mapped reason and grows the bounded backoff like any other failure.
    case = await fixture()
    forecast = await case.challenge(vote=False)
    case.now = forecast.challenge_until_ms + 1
    await case.db.execute(
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'early-resolution-trigger','{}','application/json',?)",
        ("b" * 64, case.now))
    await case.db.execute(
        "INSERT INTO forecast_timing_reviews(forecast_id,specification_hash,trigger_hash,event_at,event_time_basis,created_at) "
        "VALUES(?,(SELECT specification_hash FROM forecasts WHERE id=?),?,0,'observed_upper_bound',?)",
        (forecast.forecast_id, forecast.forecast_id, "b" * 64, case.now))
    cases.append(await record(case, lambda c: c.app.run_due_jobs(), "sweep:blocked",
                              forecastId=forecast.forecast_id, compare=["result"]))
    case.connection.close()

    # A chain that has not reached its own deadline is a *schedule*, not a fault: the pass must not
    # count it as a failure, and must re-arm the question for the instant the chain named rather
    # than for the failure backoff. This is the difference the whole `not_before_ms` distinction
    # exists for, and it is the only case here whose outcome the local record cannot explain.
    from forecast_application.service import Application  # noqa: E402

    from tests.test_solana_registry import RegistryTests  # noqa: E402

    case = await fixture()
    forecast = await case.challenge(vote=False)
    registry, transport = RegistryTests.registry(case)
    await registry.enable(forecast.forecast_id)
    case.now = forecast.challenge_until_ms + 1000
    chain_deadline = case.now + 86_400_000
    await RegistryTests.chain_account(case, registry, transport, forecast, deadline=chain_deadline)
    transport.chain_time = case.now
    # Read the chain once before the case, so the state the sweep starts from is a state the
    # reference actually produced rather than one this fixture assembled. It raises the deferral,
    # which is the answer the case then drives.
    try:
        await registry.prepare_finalization(forecast.forecast_id)
    except Exception:  # noqa: BLE001 - the deferral is the point
        pass
    chained = Application(case.db, case.ai, now_ms=lambda: case.now, token_hash=case.token_hash,
                          random_token=case.random_token, registry=registry)
    cases.append(await record(case, lambda c: chained.run_due_jobs(), "sweep:chain-deferred",
                              forecastId=forecast.forecast_id, chainDeadline=chain_deadline,
                              compare=["result"]))
    case.connection.close()

    return {
        "description": "The outbox and the sweep that drains it: the exactly-once selection, the "
                       "blocked forecast, the adapter hand-off, the set-based settlement, and the "
                       "three ways an attempt can end without advancing.",
        "maxBalance": MAX_BALANCE,
        "cases": cases,
    }


async def _sweep(case, now: int):
    case.now = now
    return await case.app.run_due_jobs()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(asyncio.run(build()), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
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
