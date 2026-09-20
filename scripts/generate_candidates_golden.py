#!/usr/bin/env python3
"""Export the compiler's candidate selection, so a Rust port can be held to it.

`_candidate_forecasts` decides what the model is even allowed to *see* before it compiles a
question. The wire contract — how the chosen candidates are presented — already has a vector; this
is the half before it, and it is three orderings stacked:

  * The lexical shortlist ranks an exact normalized match above a term match, then by recency, then
    by id. A port that reordered the `ORDER BY` would hand the compiler a different corpus, and the
    model's answer to "which existing question is this a duplicate of" would change with it.
  * The terms themselves: ASCII words of three or more letters or digits, or Hangul of two or more,
    minus a fixed stopword list, deduplicated, longest first, then alphabetical, capped at ten.
  * The byte budget, applied **twice** — once against `spec_bytes + 320` so the first pass does not
    fetch snapshots the second would discard, and again against the encoded form. The reserve of 2
    is the enclosing `[]`.

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

from tests import test_web_application as fixtures  # noqa: E402

GOLDEN = ROOT / "tests/golden/candidates-golden.json"

# Questions chosen so the ranking is exercised rather than assumed: one exact repeat of the query,
# several sharing a single term, several sharing none, and one that matches a term twice.
CORPUS = [
    "Will Acme officially announce Product X before the deadline?",
    "Will Acme ship the Product Y update before the deadline?",
    "Will Beta Systems announce a new product before the deadline?",
    "Will the Gamma merger complete before the deadline?",
    "Will Delta report earnings before the deadline?",
    "이 회사는 신제품을 발표할까?",
]
QUERIES = [
    "Will Acme officially announce Product X before the deadline?",
    "Will Acme announce something before the deadline?",
    "product",
    "이 회사는",
    "nothing matches this at all",
    "",
]


async def build() -> dict:
    case = fixtures.ApplicationTests(methodName="runTest")
    await case.asyncSetUp()

    published = []
    for index, question in enumerate(CORPUS):
        draft = await case.app.compile_forecast(case.uid, question)
        result = await case.app.publish_forecast(case.uid, draft["draftId"], f"candidate-key-{index}")
        published.append({"question": question, "forecastId": result["forecast"]["id"]})

    calls = []
    for query in QUERIES:
        chosen = await case.app._candidate_forecasts(query)
        calls.append({
            "call": "candidates", "input": {"question": query},
            "result": [forecast.forecast_id for forecast in chosen],
        })

    # The rows the selection reads, so a replay ranks the same corpus rather than one it built.
    rows = {table: [dict(row) for row in await case.db.all(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("users", "forecasts")}
    await case.asyncTearDown()
    return {
        "description": "The compiler's candidate selection: the lexical ranking, the term "
                       "extraction, and the two-pass byte budget.",
        # Note what this corpus does *not* exercise: six small specifications are nowhere near the
        # 128 KiB context bound, so both budget passes accept every candidate. Reaching the bound
        # would need specifications of tens of kilobytes, and the bound is held by the two constants
        # and the reserve of 2 rather than by a row here.
        "corpus": published,
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
