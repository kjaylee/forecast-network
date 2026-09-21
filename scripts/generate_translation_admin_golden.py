#!/usr/bin/env python3
"""Export the operator's display-translation write, so a Rust port can be held to it.

`set_translation` is the one write where an operator's prose reaches a published forecast, and
its refusals are the whole of its safety. What a port has to reproduce is therefore not the
happy path — that is a batch of four statements — but the eight ways this is refused, and the
two properties of the happy path that make it auditable at all:

  * repeating identical content preserves the timestamp and appends no audit row, while any
    correction appends one and replaces only the current display translation; and
  * the write is made under a guard that re-reads the published specification in the same
    batch, so a forecast republished between the read and the write cannot be translated
    against the version the caller was shown.

The fixture is `TranslationPersistenceTests`, which publishes a real forecast over the real
migrations: the tables and their triggers are part of what this vector holds a port to.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT), str(ROOT / "tests")]

from forecast_application.errors import AppError  # noqa: E402
from test_display_translations import TranslationPersistenceTests  # noqa: E402

GOLDEN = ROOT / "tests/golden/translation-admin-golden.json"


WRITTEN = ("forecast_translations", "forecast_translation_audit", "mutation_guards")


async def dump(db, connection) -> dict:
    """The fixture's state, in rowid order, as the Rust harness reads its own store.

    Every table, so a case is self-contained and the comparison is total: the write reads a
    published forecast, and a vector that did not carry the forecast row would be a vector whose
    every case is a 404. The Rust harness restores this state into a database built from the same
    migrations and then compares *its whole store* against `rows`, so a table left out here is a
    table whose contents nothing would check.
    """
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: await db.all(f"SELECT * FROM {name} ORDER BY rowid") for name in names}


def editorial(source, **changes) -> dict:
    """A complete, documented translation of the published source."""
    body = {
        "specificationHash": source["specificationHash"],
        "title": "Will Acme officially announce Product X?",
        "question": "Will Acme officially announce Product X before the closing deadline?",
        "rules": [{"clauseId": rule["clauseId"], "condition": rule["condition"]} for rule in source["rules"]],
        "invalidationRules": list(source["invalidationRules"]),
        "aiRationale": None,
        "sourceLanguage": "ko",
        "language": "en",
        "attribution": "Forecast editorial translation",
    }
    body.update(changes)
    return body


async def build() -> dict:
    case = TranslationPersistenceTests(methodName="runTest")
    await case.asyncSetUp()
    source = case.initial["source"]
    calls: list[dict] = []

    async def call(name: str, body, forecast_id: str | None = None) -> object:
        """One case, carrying its own fixture and its own tokens.

        The token stream is what makes the *count* and the *order* part of the vector: this write
        takes two (the guard and the audit id), and a port that took one or took them the other way
        round would be caught by the audit row it names.
        """
        entry: dict = {"call": name, "forecastId": forecast_id or case.fid, "body": body,
                       "now": case.now, "compare": ["result"], "initial": await dump(case.db, case.connection)}
        before = case.nonce
        try:
            entry["result"] = await case.app.set_translation(forecast_id or case.fid, body)
        except AppError as error:
            entry["error"] = {"status": error.status, "code": error.code, "message": error.message}
        entry["tokens"] = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(before + 1, case.nonce + 1)]
        entry["rows"] = await dump(case.db, case.connection)
        calls.append(entry)
        return entry.get("result")

    # --- the refusals, before anything is written.
    await call("set:missing-key", {key: value for key, value in editorial(source).items() if key != "attribution"})
    await call("set:extra-key", {**editorial(source), "extra": 1})
    await call("set:wrong-language", editorial(source, language="fr"))
    await call("set:wrong-source-language", editorial(source, sourceLanguage="en"))
    await call("set:wrong-attribution", editorial(source, attribution="machine"))
    await call("set:unknown-forecast", editorial(source), forecast_id="f-nobody")
    await call("set:specification-mismatch", editorial(source, specificationHash="b" * 64))
    await call("set:rule-count", editorial(source, rules=editorial(source)["rules"][:2]))
    await call("set:rule-keys", editorial(
        source, rules=[{"clauseId": rule["clauseId"], "condition": rule["condition"], "outcome": "YES"}
                       for rule in editorial(source)["rules"]]))
    await call("set:rule-order", editorial(source, rules=list(reversed(editorial(source)["rules"]))))
    await call("set:invalidation-count", editorial(source, invalidationRules=[]))
    await call("set:rationale-without-ai", editorial(source, aiRationale="A translated rationale."))
    await call("set:korean-text", editorial(source, title="제목"))
    await call("set:no-latin", editorial(source, title="12345"))
    await call("set:too-large", editorial(source, question="x" * 3000 + " Will Acme announce it?"))

    # --- and the write itself. Identical content again is a replay: the timestamp stands and no
    # second audit row is appended.
    first = await call("set:ok", editorial(source))
    if first is None:
        raise SystemExit("the first editorial translation was refused; the fixture is wrong")
    await call("set:replay", editorial(source))
    # A correction changes one field, which is a new audit row and the same current translation row.
    await call("set:corrected", editorial(source, title="Will Acme announce Product X this quarter?"))

    await case.asyncTearDown()

    return {
        "description": "Application.set_translation: the eight refusals, the replay that preserves "
                       "the timestamp, and the correction that appends an audit row.",
        "now_ms": case.now,
        "source": source,
        "cases": calls,
        "counts": {
            "audit": len(calls[-1]["rows"]["forecast_translation_audit"]),
            "translations": len(calls[-1]["rows"]["forecast_translations"]),
            "guards": len(calls[-1]["rows"]["mutation_guards"]),
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
