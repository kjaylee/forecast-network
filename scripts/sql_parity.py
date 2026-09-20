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
# `\\[\s\S]` and not `\\.`: Rust continues a string across lines with a backslash, and `.`
# does not match a newline, so a literal written that way first failed to match and then mispaired
# every quote after it. The harness reported parity while silently skipping whole modules.
RUST_STRING = re.compile(r'"((?:[^"\\]|\\[\s\S])*)"')
# `r"..."`, `r#"..."#`, `r##"..."##` — never SQL in this codebase, and a quote inside one
# desynchronises the scanner above. The `r` needs a boundary of its own: without one, the
# trailing `r` of `"September"` opens a raw string that swallows the rest of the file.
RAW_STRING = re.compile(r'(?<![A-Za-z0-9_])r(#*)"[\s\S]*?"\1')
# Rust format holes (`{}`, `{guard_sql}`) and Python f-string expressions both stand for a
# value supplied at runtime, so both become the same marker and compare equal.
HOLE = "\x00"
MIN_STATEMENT = 20
# A statement containing a hole cannot be compared whole when Python assembles it from a
# variable with `+`. Its fixed pieces are compared instead; below this length a piece is
# too small to be evidence of anything.
MIN_FRAGMENT = 12

# Deliberate differences. Each entry is a Rust statement that is not the Python statement and is
# not meant to be; the key identifies it, the value says why, so the next reader does not have to
# reconstruct the reasoning from a diff. Anything absent from here fails the check, so adding an
# entry is a decision someone has to make on purpose.
DIVERGENCES = {
    "SELECTreserved_lamportsFROMregistry_spendWHEREday=?":
        "`reserve_daily_spend` reads the day back to confirm the reserve, because this `Database` "
        "reports no affected-row count. The statement it confirms is the reference's own: a WHERE "
        "clause that refuses the update leaves the row unchanged, and the reserve is refused. "
        "Two extra reads, the same outcome, and nothing invented to make the check possible.",
    "SELECTu.idFROMsessionssJOINusersuONu.id=":
        "`auth::user_id` reads only the id, where Python's `authenticate` builds a whole public "
        "user. The session guard is identical; the projection is narrower because the caller is.",
}


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


def drop_continuations(raw: str) -> str:
    """Rust's `\` at the end of a line removes the newline and the indentation after it.

    The harness compares source text, so without this every continuation arrives as a stray
    backslash in the middle of the statement — `JOIN x\ON` — and a statement written that way
    reads as drift when it is identical.
    """
    return re.sub(r"\\\r?\n[ \t]*", "", raw)


def rust_statements(source: str) -> list[tuple[int, str]]:
    """(line, statement) for every SQL string literal in one Rust file.

    Test code is excluded. A fixture that inserts a row to exercise a guard is not a statement
    the Worker runs, and reporting it as drift trains the reader to ignore the check.
    """
    marker = source.find("#[cfg(test)]")
    if marker >= 0:
        source = source[:marker]

    # `'"'` is the one Rust char literal whose contents can close a string and splice
    # unrelated code into a run of adjacent literals. Blanking it is the whole fix.
    source = source.replace("'\"'", "   ")
    # A raw string can contain a quote — `r#"...\"...\"..."#` is how this codebase writes a
    # regex that matches one — and the scanner below does not know it is inside one. The
    # quote then pairs with the next real literal and every statement after it is misread,
    # silently and completely: a whole file reported zero statements while containing SQL.
    # Raw strings here are patterns, never statements, so they are blanked to their own width.
    source = RAW_STRING.sub(lambda match: " " * len(match.group()), source)
    if RAW_STRING.search(source):
        # A raw string the pattern could not close means the scan below is about to misread
        # every literal after it. Failing here is the only way that stays visible.
        raise ValueError("unterminated raw string; the statement scan cannot be trusted")
    statements: list[tuple[int, str]] = []
    index = 0
    while True:
        match = RUST_STRING.search(source, index)
        if match is None:
            return statements
        parts, cursor = [drop_continuations(match.group(1))], match.end()
        while True:
            remainder = source[cursor:]
            gap = len(remainder) - len(remainder.lstrip())
            following = RUST_STRING.match(source, cursor + gap)
            if following is None:
                break
            parts.append(drop_continuations(following.group(1)))
            cursor = following.end()
        joined = " ".join(parts).strip()
        if len(joined) > MIN_STATEMENT and SQL_START.match(joined):
            statements.append((source[:match.start()].count("\n") + 1, joined))
        index = cursor


def python_statements(source: str) -> list[str]:
    """Every SQL string Python builds, with its constants folded in.

    Python assembles some statements from several pieces — implicit concatenation, `+` between
    literals, and module-level constants like the current-address subquery — and an AST fold is
    the only way to see the statement rather than the pieces. A piece that cannot be folded is a
    value supplied at runtime, so it becomes the same marker a Rust format hole becomes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    # Every assignment in the file, in tree order, so a statement finished by `+=` inside a
    # method is folded as one statement. Scoping is deliberately ignored: a name reused for two
    # different values folds to whichever came last, which can only invent a statement Python
    # does not run -- a visible failure -- never hide one it does.
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            folded = _fold(node.value, constants, 0)
            if folded is not None:
                constants[node.targets[0].id] = folded
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.op, ast.Add)
            and constants.get(node.target.id) is not None
        ):
            added = _fold(node.value, constants, 0)
            if added is not None:
                constants[node.target.id] += added
    found: list[str] = []
    for node in ast.walk(tree):
        folded = _fold(node, constants, 0)
        if folded is None:
            continue
        text = folded.strip()
        if len(text) > MIN_STATEMENT and SQL_START.match(text):
            found.append(folded)
    # The same statement is reachable from several nodes; comparing it once is enough.
    return list(dict.fromkeys(found))


def _fold(node: ast.AST, constants: dict[str, str], depth: int) -> str | None:
    if depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.FormattedValue):
        return HOLE
    if isinstance(node, ast.JoinedStr):
        return "".join(_fold(part, constants, depth + 1) or HOLE for part in node.values)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold(node.left, constants, depth + 1)
        right = _fold(node.right, constants, depth + 1)
        if left is None and right is None:
            return None
        # Inside a string concatenation an unfoldable side is a runtime value, not noise.
        return (HOLE if left is None else left) + (HOLE if right is None else right)
    return None


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
    checked = mismatched = known = 0
    for path in sorted(RUST_SOURCES.glob("*.rs")):
        for line, statement in rust_statements(path.read_text(encoding="utf-8")):
            checked += 1
            if accounted_for(statement, canon_only):
                continue
            divergence = next(
                (reason for marker, reason in DIVERGENCES.items() if marker in canonical(statement)), None)
            if divergence is not None:
                known += 1
                print(f"Known difference: {path.name}:{line} — {divergence}")
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
    detail = f", {known} known difference{'s' if known != 1 else ''}" if known else ""
    print(f"SQL parity verified: {checked} edge statements against {len(entries)} Python statements{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
