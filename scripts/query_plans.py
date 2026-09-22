#!/usr/bin/env python3
"""Ask SQLite how the edge's statements would be executed, and name the ones that scan.

The D1 free tier grants 5,000,000 rows read a day. On 2026-09-22 the service was spending
them before lunch, after which the risk feed answered `stale` until midnight. The tick was
the obvious suspect and the row counters cleared it — one tick reads 51 rows over 20
statements — so the budget is going to ordinary traffic, at an average near 300 rows a
statement. That average is what a scan looks like.

A query plan does not need the production database. The schema in `apps/web/migrations` is
the schema D1 runs, and SQLite's planner chooses the same shape of plan for it: a statement
that scans a table here scans it there. So this builds the schema in memory, runs
`EXPLAIN QUERY PLAN` over every statement the edge crate contains, and reports each one
that SCANs a table rather than SEARCHing it.

A scan is not automatically a defect — a small table read whole is cheaper than an index —
so the report is evidence to read, not a gate to pass. `--check` exists for the day a
budget is set.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import re

from sql_parity import ROOT, RUST_SOURCES, rust_statements  # noqa: E402

MIGRATIONS = ROOT / "apps/web/migrations"

# A statement with a runtime hole cannot be prepared as written. `sql_parity` keeps the hole
# so the two sides can be compared; here it has to become something SQLite will parse.
HOLES = (("{}", "?"), ("{placeholders}", "?"), ("{columns}", "*"), ("{assignments}", "x=?"))

LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
SKIPPABLE = re.compile(r"(?:\s|,|//[^\n]*\n)*")


def joined_concats(source: str) -> list[tuple[int, str]]:
    """(line, statement) for each `concat!(...)` of string literals, joined as Rust joins them.

    `sql_parity` reads every literal separately, which is right for comparing text but wrong
    for a query plan: the first piece of a statement written across several literals is
    usually a complete-looking `SELECT ... JOIN ...` with its `WHERE` in the next piece, and
    planning that piece alone reports a scan the Worker never performs. That false positive
    is worse than no report, because it is the kind a reader learns to skim past.

    The pieces are taken by walking literals forward from the macro rather than by matching
    its closing parenthesis: these statements contain `(` in their own SQL, so a parenthesis
    match stops in the middle of a subquery and fragments the statement it was meant to join.
    """
    marker = source.find("#[cfg(test)]")
    if marker >= 0:
        source = source[:marker]
    found = []
    for match in re.finditer(r"concat!\s*\(", source):
        at = match.end()
        pieces = []
        while True:
            at += len(SKIPPABLE.match(source, at).group())
            literal = LITERAL.match(source, at)
            if literal is None:
                break
            pieces.append(literal.group(1))
            at = literal.end()
        if pieces:
            line = source.count("\n", 0, match.start()) + 1
            found.append((line, "".join(pieces).replace("\\n", "\n").replace("\\\"", '"')))
    return found


def statements(source: str) -> list[tuple[int, str]]:
    """Every statement in one file, with multi-literal ones joined rather than fragmented."""
    concats = joined_concats(source)
    covered = {piece for _, text in concats for piece in text.split()}
    singles = [
        (line, text)
        for line, text in rust_statements(source)
        # A literal whose every word already appears in a joined statement is one of its pieces.
        if not (text.split() and all(word in covered for word in text.split()))
    ]
    return sorted(concats + singles)


def schema() -> sqlite3.Connection:
    """The deployed schema, in memory, from the migrations D1 was given."""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    for path in sorted(MIGRATIONS.glob("*.sql")):
        connection.executescript(path.read_text(encoding="utf-8"))
    return connection


def preparable(statement: str) -> str | None:
    """The statement as SQLite can prepare it, or None when it cannot be."""
    text = statement.strip()
    if not text.upper().startswith(("SELECT", "WITH", "UPDATE", "DELETE")):
        return None  # only reads and read-driven writes have a plan worth naming
    for hole, filler in HOLES:
        text = text.replace(hole, filler)
    if "{" in text or "}" in text:
        return None
    return text


def plan(connection: sqlite3.Connection, statement: str) -> list[str] | None:
    """The plan's detail lines, or None when the statement does not prepare."""
    try:
        prepared = connection.execute("EXPLAIN QUERY PLAN " + statement, [None] * statement.count("?"))
    except sqlite3.Error:
        return None
    return [row[3] for row in prepared.fetchall()]


def scans(detail: list[str]) -> list[str]:
    """The plan lines that read a table whole.

    `SCAN table` is the whole-table read. `SCAN table USING ... INDEX` is a covering-index
    scan, which is cheaper but still proportional to the table, so it is reported too and
    left for the reader to judge.
    """
    return [line for line in detail if line.startswith("SCAN")]


def survey() -> list[tuple[str, int, str, list[str]]]:
    """(file, line, statement, scan lines) for every edge statement whose plan scans."""
    connection = schema()
    found: list[tuple[str, int, str, list[str]]] = []
    for path in sorted(RUST_SOURCES.rglob("*.rs")):
        for line, statement in statements(path.read_text(encoding="utf-8")):
            text = preparable(statement)
            if text is None:
                continue
            detail = plan(connection, text)
            if detail is None:
                continue
            scanning = scans(detail)
            if scanning:
                found.append((str(path.relative_to(ROOT)), line, " ".join(statement.split()), scanning))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 when any statement scans")
    parser.add_argument("--limit", type=int, default=0, help="Print at most this many statements")
    args = parser.parse_args()
    found = survey()
    shown = found[: args.limit] if args.limit else found
    for path, line, statement, scanning in shown:
        print(f"{path}:{line}")
        for scan in scanning:
            print(f"    {scan}")
        print(f"    {statement[:200]}")
    print(f"{len(found)} statements scan a table")
    return 1 if args.check and found else 0


if __name__ == "__main__":
    raise SystemExit(main())
