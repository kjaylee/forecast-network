#!/usr/bin/env python3
"""Export Python's two canonical-JSON encodings, so a Rust port can be held to both.

`json.dumps(value, sort_keys=True, separators=(",", ":"))` is one line of Python with one keyword
argument deciding its output, and the reference uses it *both ways*:

  * `ensure_ascii=False` for commitments — `forecast_domain.serialization.canonical_bytes` and the
    `sort_keys` calls that spell the flag out. Every character travels as its own UTF-8 bytes.
  * The **default** `ensure_ascii=True` for audit bodies and the hashes that key rows — a wallet
    sign-in's audit, a wallet sign-in's profile commitment, `markets._hash`, `points`, the timing
    reviews' proof hashes, and `participation_holds`' request hash. Every character outside ASCII
    becomes `\\uXXXX`, and above the BMP a surrogate pair.

They agree on every ASCII value, which is why picking the wrong one survives a whole test suite and
then produces a different digest for the same request the first time someone writes an accent.

The values below are chosen for what they make visible: a BMP character, an astral one that has to
become *two* escapes, a non-ASCII object *key* (keys are escaped too), the ASCII characters each
encoding treats specially, the one ASCII character neither escapes, and the line separator that
`ensure_ascii` escapes even though it is neither ASCII nor a control character.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_domain import dumps  # noqa: E402

GOLDEN = ROOT / "tests/golden/canonical-json-golden.json"

CASES = [
    ("ascii", {"b": 1, "a": [1, 2, 3], "c": None}),
    ("accent", {"text": "café"}),
    ("astral", {"emoji": "😀", "tail": "x"}),
    ("unicode-key", {"clé": 1, "a": 2}),
    ("escapes", {"text": "quote \" backslash \\ newline \n tab \t carriage \r delete \x7f"}),
    ("line-separator", {"text": "\u2028separator \u00a0 space \u007f delete"}),
    ("control", {"text": "\u0000\u001f\u0080"}),
    ("nested", {"outer": {"inner": ["é", {"深": "度"}]}}),
    ("numbers", {"big": 9007199254740991, "small": -9007199254740991, "zero": 0, "truthy": True}),
    ("empty", {"object": {}, "array": []}),
]


def build() -> dict:
    cases = []
    for name, value in CASES:
        cases.append({
            "name": name,
            "value": value,
            # The two rules, spelled the way the reference spells them at each call site.
            "raw": json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            "escaped": json.dumps(value, sort_keys=True, separators=(",", ":")),
            # `forecast_domain.dumps` is the commitment rule, and it has to agree with the
            # hand-written one above — two code paths for one rule is how they drift.
            "commitment": dumps(value),
        })
    return {
        "description": "Python's two canonical-JSON encodings: the commitment rule that keeps "
                       "non-ASCII raw, and json.dumps's default that escapes it.",
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
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
