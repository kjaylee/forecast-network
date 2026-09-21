#!/usr/bin/env python3
"""Export the scheduler's *early* proposal, so a Rust port can be held to it.

One arm of `_advance_job` is not reachable from any other vector: a question upgraded by an
observed event proposes through `propose_early_resolution` rather than `propose_resolution`, and
every golden that drives `run_due_jobs` drives it over a v1 snapshot. The port had that branch
wrong — it narrowed the snapshot to its base before the branch that distinguishes them, so an
upgraded question was proposed against the question it used to be — and nothing said so.

This is the vector that would have. It seeds the early-resolution fixture's own `RESOLVING` v2
snapshot into a migrated database and drives one scheduler pass over it, and it compares what the
two languages send to the model rather than only what they decide: the early judge's payload carries
a `trigger` and the ordinary judge's does not, so a port that took the wrong branch fails on the
conversation rather than on an outcome that might be identical.

Three things had to be true for the fixture to work at all, and each was found by a probe:

  * the evidence has to be **retained as an artifact row**, because `Application` installs its own
    reader over `ai` and that reader reads the table rather than whatever the coordinator was built
    with — an unretained fixture refuses without ever calling the reader it was handed;
  * the clock has to be past the trigger's `qualified_at_ms`, or the eligibility gate answers
    "Evidence review has not completed" before anything runs; and
  * the seeded row must satisfy the due-job selection exactly, which is `job_until`, `retry_at` and
    the state — a row inserted a column out is a row the pass never looks at, and the pass reports
    that as quiet success (`processed: 0, failed: 0`) rather than as an error.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import ProviderConfig  # noqa: E402
from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_application.service import Application  # noqa: E402
from tests.test_web_ai import Transport, coordinator  # noqa: E402

GOLDEN = ROOT / "tests/golden/sweep-early-golden.json"
EARLY = ROOT / "tests/golden/ai-early-golden.json"

PROVIDERS = (ProviderConfig("gemini", "test-model", "test-key"), ProviderConfig("openai", "test-model", "test-key"))


def payload_of(call: dict) -> dict:
    """The user text of whichever provider envelope a call used."""
    envelope = call["body"]
    raw = envelope["contents"][0]["parts"][0]["text"] if "contents" in envelope else envelope["input"]
    if isinstance(raw, list):
        raw = raw[-1]["content"]
    return json.loads(raw)


async def dump(db, connection) -> dict:
    """Every table in the deployed schema, in rowid order."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: await db.all(f"SELECT * FROM {name} ORDER BY rowid") for name in names}


async def build() -> dict:
    early = json.loads(EARLY.read_text())
    proposal = early["proposal"]
    forecast = proposal["forecast"]
    specification = forecast["specification"]
    trigger = forecast["early_trigger"]
    body = early["observationBody"]
    now_ms = proposal["now_ms"]

    connection = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
        connection.executescript(migration.read_text())
    db = SQLiteDatabase(connection)

    # Foreign keys off for the seed, for the reason the Rust harness's `restore` gives: this
    # restores an already-consistent state rather than replaying the actions that produced it, and
    # the fixture's forecast arrives without the draft, the observations and the rest of its history.
    connection.execute("PRAGMA foreign_keys=OFF")
    await db.execute(
        "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
        "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,challenge_until,"
        "mutation_key,job_token,job_until,retry_at,failure_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,0,0)",
        (forecast["forecast_id"], forecast["creator_id"], "draft-sweep", json.dumps(forecast),
         forecast["revision"], forecast["state"], specification["category"], specification["share_title"],
         specification["canonical_question"], specification["canonical_question"],
         forecast["specification_hash"], specification["open_at_ms"], specification["close_at_ms"],
         forecast["created_at_ms"], forecast["updated_at_ms"],
         forecast.get("challenge_until_ms") or 0, "mutation-sweep"),
    )
    await db.execute(
        "INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
        (trigger["evidence"][0]["content_sha256"], "source", body, "text/plain", now_ms),
    )
    connection.execute("PRAGMA foreign_keys=ON")
    initial = await dump(db, connection)

    transport = Transport(list(proposal["responses"]))
    counter = {"n": 0}
    produced: list[str] = []

    def token() -> str:
        counter["n"] += 1
        value = hashlib.sha256(str(counter["n"]).encode()).hexdigest()
        produced.append(value)
        return value

    app = Application(
        db,
        coordinator(transport, providers=list(PROVIDERS)),
        now_ms=lambda: now_ms,
        random_token=token,
        token_hash=lambda value: hashlib.sha256(value.encode()).hexdigest(),
    )
    sweep = await app.run_due_jobs()
    rows = await dump(db, connection)
    connection.close()

    return {
        "description": "The scheduler's early proposal: one pass over an upgraded question's "
                       "RESOLVING snapshot, judged by the early pipeline.",
        "now_ms": now_ms,
        "providers": [{"provider": item.provider, "model": item.model, "apiKey": item.api_key}
                      for item in PROVIDERS],
        "initial": initial,
        "responses": list(proposal["responses"]),
        "tokens": produced,
        "expect": {
            "sweep": {"processed": sweep["processed"], "failed": sweep["failed"]},
            "payloads": [payload_of(call) for call in transport.calls],
            "rows": rows,
        },
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
