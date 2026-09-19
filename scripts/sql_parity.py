#!/usr/bin/env python3
"""Every SQL statement the edge Worker runs must still be the one Python runs.

`apps/web-rs` serves forecast.eastsea.xyz and mirrors the Python queries by hand, thousands
of lines of SQL and response shapes with no gate between them. A change to one side would
ship as a silent divergence — the two Workers would answer the same request differently and
nothing would say so. This compares them.

It compares the statement as SQL rather than as text, because the two languages do not
write the same statement the same way and none of that changes what the database does:
Python wraps a query over several implicitly concatenated literals and sometimes joins
fragments with `+`, Rust joins adjacent literals and interpolates with `format!`, and either
language may put a line break where the other put a comma-space. None of it is meaningful.
What is compared is the token sequence, with whitespace between tokens removed and string
literals left exactly as written, since whitespace inside a literal is meaningful.

No dependencies: the Python side is read through `ast`, which has already folded implicit
concatenation, so it sees the statement Python builds rather than the pieces in the file.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST_SOURCES = ROOT / "apps/web-rs/src"
PYTHON_ROOTS = ("packages/application/src", "packages/domain/src", "apps/web/src")

SQL_START = re.compile(r"^(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.I)
RUST_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
# Rust format holes (`{}`, `{guard_sql}`) and Python f-string expressions both stand for a
# value supplied at runtime, so both become the same marker and compare equal.
HOLE = "\x00"
MIN_STATEMENT = 20
# A statement containing a hole cannot be compared whole when Python assembles it from a
# variable with `+`. Its fixed pieces are compared instead; below this length a piece is
# too small to be evidence of anything.
MIN_FRAGMENT = 12


def canonical(sql: str) -> str:
    """The token sequence the database executes, minus the whitespace between tokens."""
    sql = re.sub(r"\{[^{}]*\}", HOLE, sql)
    out: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            # A SQL string literal: its contents, whitespace included, are part of the query.
            end = sql.find("'", index + 1)
            end = len(sql) - 1 if end < 0 else end
            out.append(sql[index:end + 1])
            index = end + 1
        elif char.isspace():
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def rust_statements(source: str) -> list[tuple[int, str]]:
    """(line, statement) for every SQL string literal in one Rust file."""
    # `'"'` is the one Rust char literal whose contents can close a string and splice
    # unrelated code into a run of adjacent literals. Blanking it is the whole fix.
    source = source.replace("'\"'", "   ")
    statements: list[tuple[int, str]] = []
    index = 0
    while True:
        match = RUST_STRING.search(source, index)
        if match is None:
            return statements
        parts, cursor = [match.group(1)], match.end()
        while True:
            remainder = source[cursor:]
            gap = len(remainder) - len(remainder.lstrip())
            following = RUST_STRING.match(source, cursor + gap)
            if following is None:
                break
            parts.append(following.group(1))
            cursor = following.end()
        joined = " ".join(parts).strip()
        if len(joined) > MIN_STATEMENT and SQL_START.match(joined):
            statements.append((source[:match.start()].count("\n") + 1, joined))
        index = cursor


def python_statements(source: str) -> list[str]:
    """Every SQL string Python builds, already folded across implicit concatenation."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            found.append("".join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                                 else "{}" for part in node.values))
    return [text for text in found if len(text.strip()) > MIN_STATEMENT and SQL_START.match(text.strip())]


def corpus() -> list[tuple[str, str, str]]:
    """(file, raw, canonical) for every SQL statement in the Python Worker."""
    entries: list[tuple[str, str, str]] = []
    for directory in PYTHON_ROOTS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            for text in python_statements(path.read_text(encoding="utf-8")):
                entries.append((str(path.relative_to(ROOT)), text, canonical(text)))
    return entries


def fragments(statement: str) -> list[str]:
    """The fixed pieces of a statement that carries a runtime hole."""
    return [piece for piece in canonical(statement).split(HOLE) if len(piece) >= MIN_FRAGMENT]


def accounted_for(statement: str, corpus_canon: list[str]) -> bool:
    """Whether Python still runs this statement, whole or in its fixed pieces.

    The fragment branch is weaker than the whole-statement one: it proves every piece still
    exists, not that Python assembles them into this statement. It is only reached for
    statements containing a runtime hole, and those are assembled by the caller in both
    languages, so the whole-shape check would be blind to them anyway.
    """
    key = canonical(statement)
    if any(key in other for other in corpus_canon):
        return True
    pieces = fragments(statement)
    return bool(pieces) and all(any(piece in other for other in corpus_canon) for piece in pieces)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when a statement has drifted from Python")
    args = parser.parse_args()
    if not args.check:
        parser.error("this script only reports drift; pass --check")

    entries = corpus()
    canon_only = [entry[2] for entry in entries]
    checked = mismatched = 0
    for path in sorted(RUST_SOURCES.glob("*.rs")):
        for line, statement in rust_statements(path.read_text(encoding="utf-8")):
            checked += 1
            if accounted_for(statement, canon_only):
                continue
            mismatched += 1
            print(f"\n{path.name}:{line} is not a query the Python Worker runs:", file=sys.stderr)
            key = canonical(statement)
            closest = max(entries, key=lambda e: difflib.SequenceMatcher(None, key, e[2]).ratio())
            ratio = difflib.SequenceMatcher(None, key, closest[2]).ratio()
            print(f"  closest is {closest[0]} at {ratio:.0%} similarity", file=sys.stderr)
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, key, closest[2]).get_opcodes():
                if tag != "equal":
                    print(f"    {tag:8} rust ...{key[max(0, i1 - 30):i2 + 30]}...", file=sys.stderr)
                    print(f"    {'':8} py   ...{closest[2][max(0, j1 - 30):j2 + 30]}...", file=sys.stderr)

    if mismatched:
        print(f"\n{mismatched} of {checked} edge Worker statements are not in the Python Worker.", file=sys.stderr)
        return 1
    print(f"SQL parity verified: {checked} edge statements against {len(entries)} Python statements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
