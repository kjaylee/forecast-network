#!/usr/bin/env python3
"""Export the participation-points read models, so a Rust port can be held to them.

`points.py` had no vector, and porting it to the `Database` trait is what made one possible: until
its queries could run against SQLite-over-the-real-migrations, the only place they could be wrong
was production.

Three things the vector is built to expose:

  * `summary` assembles one history UNION from four optional tables, and *which* branches are
    spliced in depends on a probe. Over the deployed migrations all four exist, so that is what the
    vector pins — the grant and reservation branches with rows behind them, and the assembled SQL
    compared as SQL by `sql_parity`, which is the half a fixture cannot reach. The market-ledger,
    eligibility-adjustment and void branches have no rows here: reaching them needs a settled
    market, and their SQL is what the parity check holds.
  * The onboarding block distinguishes four states from three flags: an awarded wallet, an address
    that was already rewarded, a linked wallet with nothing to show, and no wallet at all. Collapsing
    any two of them tells a user to connect a wallet they already connected.
  * `positions` refuses more than a hundred identifiers and *validates* each one. A malformed
    identifier is a refusal, not a missing position — "you have no stake" and "you asked about
    something that is not an identifier" are different answers.

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
from forecast_application.service import Application  # noqa: E402

from tests.test_points import PointsTests  # noqa: E402

GOLDEN = ROOT / "tests/golden/points-golden.json"
NOW = 1_800_000_000_000
USER = "u-points"
WALLET = "Wa11et111111111111111111111111111111111111"


async def build() -> dict:
    """Driven through the points suite's own fixture.

    That fixture exists because the tables enforce the invariants: a ledger entry whose
    `available_after` does not follow from the account and the delta is refused by a trigger, and a
    grant whose amount does not match the policy is refused too. Reconstructing a consistent state
    by hand is how a fixture ends up testing the constraints instead of the reader.
    """
    case = PointsTests(methodName="runTest")
    await case.asyncSetUp()
    points = case.points
    calls: list[dict] = []

    async def call(name: str, awaitable, **inputs) -> object:
        entry: dict = {"call": name, "input": inputs}
        try:
            result = await awaitable
        except AppError as error:
            entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
            calls.append(entry)
            return None
        entry["result"] = result
        calls.append(entry)
        return result

    await case.opened("f-staked")
    await case.reserve("user-a", "f-staked", 300)

    await call("summary:granted", points.summary("user-a"), userId="user-a")
    await call("summary:empty", points.summary("user-b"), userId="user-b")
    await call("summary:unknown", points.summary("u-nobody"), userId="u-nobody")
    await call("summary:empty-id", points.summary(""), userId="")
    await call("summary:control-character", points.summary("bad\x01id"), userId="bad\x01id")
    await call("summary:too-long", points.summary("u" * 129), userId="u" * 129)

    await call("position:staked", points.position("user-a", "f-staked"), userId="user-a", forecastId="f-staked")
    await call("position:absent", points.position("user-a", "f-none"), userId="user-a", forecastId="f-none")
    await call("positions:many", points.positions("user-a", ["f-staked", "f-none", "f-staked"]),
               userId="user-a", forecastIds=["f-staked", "f-none", "f-staked"])
    await call("positions:empty", points.positions("user-a", []), userId="user-a", forecastIds=[])
    await call("positions:too-many", points.positions("user-a", [f"f-{index}" for index in range(101)]),
               userId="user-a", forecastIds="101 identifiers")
    await call("positions:invalid-id", points.positions("user-a", ["f-staked", ""]),
               userId="user-a", forecastIds=["f-staked", ""])
    await call("positions:not-a-string", points.positions("user-a", [7]),
               userId="user-a", forecastIds=[7])

    # Everything the reader and the ledger's own triggers need. The grant entries and the account
    # are left to the award trigger, which writes them from the awards.
    tables = ["users", "forecasts", "user_forecasts", "point_positions", "point_accounts",
              "point_awards", "point_ledger"]
    # --- the evidence reward: the earliest held report whose evidence the trigger cites.
    #
    # Each case runs on its own fixture, because the reward is once-per-forecast and a shared store
    # would make the second case a replay of the first.
    reward_cases = []
    for name, hashes, status in [
        ("reward:none-cited", [], "held"),
        ("reward:no-report", ["c" * 64], "held"),
        ("reward:not-held", ["b" * 64], "received"),
        ("reward:ok", ["b" * 64], "held"),
        ("reward:again", ["b" * 64], "held"),
    ]:
        paid = PointsTests(methodName="runTest")
        await paid.asyncSetUp()
        await paid.opened("f-reward")
        await paid.db.execute(
            "INSERT INTO evidence_reports(id,forecast_id,user_id,url,status,artifact_hash,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("er-1", "f-reward", "user-b", "https://www.apple.com/newsroom/x", status,
             "b" * 64, paid.now - 500, paid.now - 500))
        clock = paid
        shim = type("App", (), {"db": paid.db, "now_ms": staticmethod(lambda: clock.now),
                                "random_token": staticmethod(lambda: "t" * 32)})()
        result = await Application.reward_evidence_report(shim, "f-reward", hashes)
        if name == "reward:again":
            # The `NOT EXISTS` guard is per forecast, not per report: a second qualifying report
            # cannot pay again.
            await paid.db.execute(
                "INSERT INTO evidence_reports(id,forecast_id,user_id,url,status,artifact_hash,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("er-2", "f-reward", "user-c", "https://www.apple.com/newsroom/y", "held",
                 "b" * 64, paid.now - 400, paid.now - 400))
            result = await Application.reward_evidence_report(shim, "f-reward", hashes)
        reward_cases.append({
            # The status the report *starts* with: the rows below are the state after the call,
            # and a `reward:ok` report reads `rewarded` there, which is not what the query matched.
            "call": name, "input": {"forecastId": "f-reward", "evidenceHashes": hashes,
                                    "reportStatus": status}, "result": result,
            "rows": {table: [dict(row) for row in await paid.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in ("point_accounts", "point_evidence_rewards", "evidence_reports")},
        })
        paid.connection.close()

    rows = {table: [dict(row) for row in await case.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables}
    return {
        "description": "The participation-points read models: the assembled history, the four "
                       "onboarding states, the identifier rules, and the evidence reward.",
        "now": case.now,
        "rows": rows,
        "calls": calls,
        "rewards": reward_cases,
    }


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
