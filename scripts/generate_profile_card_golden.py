#!/usr/bin/env python3
"""Export the profile card, so a Rust port can be held to it.

The card is the one read model a user *publishes*. Two halves meet here and must not be confused:
the snapshot is an authenticated-owner read of a single database snapshot, and the public lookup
verifies the retained bytes against their own hash before serving them. `create_profile_card` is the
only bridge, and it exists so that nothing is public until the owner deliberately shares it.

Four things the vector is built to expose:

  * The snapshot's `asOf` is a claim about *one* read. `PROFILE_CARD_SQL` is a single statement
    precisely so that identity, the scoring ledger, the translated titles, the history and the
    highlight all come from the same instant; a port that issued several queries would produce a
    card nobody's read agrees with.
  * The metrics are three different divisions with three different denominators. Accuracy is
    `correct*100/count`, the Brier score is `brier_numerator/(10000*count)`, and the calibration
    score is `1 - error/(100*count)` — each `None` when nothing is scored, never `0`.
  * `correct` is reported as a boolean, not as SQLite's `0`/`1`: the canonical encoding of `true` is
    not the encoding of `1`, and the card's hash is taken over that encoding.
  * `sampleStatus` is a claim about the sample, not a ranking: new, provisional below ten, and
    established at ten or more.

The vector drives the reference's own `Application` over the real migrations, with a forecaster
whose history covers the correct and incorrect branches, the INVALID exclusion, and the highlight's
tie order.

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

from tests import test_web_application as fixtures  # noqa: E402

GOLDEN = ROOT / "tests/golden/profile-card-golden.json"


async def build() -> dict:
    case = fixtures.ApplicationTests(methodName="runTest")
    await case.asyncSetUp()
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

    # A forecaster with nothing scored yet: the card exists, and every metric is null.
    await call("snapshot:empty", case.app.profile_card(case.other), userId=case.other)
    await call("snapshot:unknown", case.app.profile_card("u-nobody"), userId="u-nobody")

    # The state before the question is published, because the first two cards are read then and
    # the rows below are the state after everything: a replay that restored only the final rows
    # would give the empty card a submission it did not have.
    initial = {table: [dict(row) for row in await case.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
               for table in ("users", "forecasts", "user_forecasts", "artifacts")}

    # A settled question, which is where `eligible_reputation_scores` gets its row — and with it,
    # the denominator every metric divides by. The fixture's own publish key is fixed, so the
    # question is published once, by `challenge`.
    await case.challenge(vote=True)
    forecast = {"id": (await case.db.first("SELECT id FROM forecasts ORDER BY created_at,id LIMIT 1"))["id"]}
    await call("snapshot:scored", case.app.profile_card(case.other), userId=case.other)

    # Publishing: the same card, retained as an artifact under its own hash.
    published = await call("create", case.app.create_profile_card(case.other), userId=case.other)
    await call("lookup:published", case.app.get_profile_card(published["snapshotHash"]) if published else None,
               snapshotHash=published["snapshotHash"] if published else None)
    await call("lookup:not-a-hash", case.app.get_profile_card("nope"), snapshotHash="nope")

    # A retained body that does not hash to the key it is stored under is an integrity failure, not
    # a served card — the check is what makes the hash a commitment. The store will not let an
    # artifact be edited (`immutable_artifact`), so the mismatched row is inserted instead, which is
    # the same thing a tampered store would hold.
    await case.db.execute(
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
        ("f" * 64, "profile-share-snapshot",
         json.dumps({"schemaVersion": 1}, sort_keys=True, separators=(",", ":")), "application/json", case.now))
    await call("lookup:unknown", case.app.get_profile_card("f" * 64), snapshotHash="f" * 64)

    # Tables, not views: `eligible_reputation_scores` is a view and has no rowid to order by, and a
    # replay can rebuild it from the tables it derives from.
    rows = {}
    for table in ["users", "forecasts", "user_forecasts", "artifacts"]:
        rows[table] = [dict(row) for row in await case.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
    await case.asyncTearDown()

    return {
        "description": "The profile card: the single-snapshot read, the three metric divisions, the "
                       "boolean `correct`, and the publication the public lookup verifies.",
        "now": case.now,
        "initial": initial,
        "forecastId": forecast["id"],
        "rows": rows,
        "calls": calls,
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
