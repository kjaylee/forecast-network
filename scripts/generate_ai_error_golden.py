#!/usr/bin/env python3
"""Export the AI-failure → HTTP-failure table, so a Rust port can be held to it.

Every refusal an AI task produces becomes a status and a message a person reads. Two things about
that table make it worth a vector rather than a reading:

  * The *unknown* codes are deliberately neutral. A rejection this layer does not recognise is a
    502 whose message says nothing about the exception, because echoing model output to a caller
    turns a diagnostic into an oracle. A port that fell through to the exception's own text would
    pass every test written from a known code.
  * "Say more" and "try again later" are different failures. The deadline codes and
    `compiler_not_publishable` are 422s — the user has to change the question — while a malformed
    answer is a 502. Collapsing them tells a caller to retry a request that can never succeed.

The cases are every branch of the reference's table, plus three codes it has never heard of, plus
the `source_temporarily_unavailable` branch reached both ways (`__cause__` and a retained
`source-failure` artifact).

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import AIRejected, AIUnavailable  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402
from forecast_application.service import Application  # noqa: E402
from forecast_application.sources import Artifact, SourceUnavailable  # noqa: E402
from golden_cli import golden_main  # noqa: E402

GOLDEN = ROOT / "tests/golden/ai-error-golden.json"

# Every code the table names, the codes it does not, and one empty code.
CODES = [
    "compiler_deadline_timezone", "compiler_deadline_invalid", "compiler_deadline_mismatch",
    "compiler_deadline_range", "question_already_resolved", "source_rejected",
    "resolution_domain_rejected", "compiler_not_publishable", "ai_output_size", "ai_output_json",
    "ai_output_type", "ai_output_enum", "ai_output_fields", "ai_output_text_length",
    "ai_output_range", "ai_output_incomplete", "compiler_domain_validation",
    "ai_rejected", "", "something_new", "SOURCE_REJECTED",
]


def artifact(kind: str) -> Artifact:
    return Artifact(content_hash="0" * 64, kind=kind, body="{}", media_type="application/json")


def case(name: str, error: Exception) -> dict:
    entry: dict = {"name": name}
    try:
        mapped = Application._ai_error(error)
        entry["result"] = {"status": mapped.status, "code": mapped.code, "message": mapped.message}
    except AppError as mapped:  # pragma: no cover - `_ai_error` returns rather than raises
        entry["result"] = {"status": mapped.status, "code": mapped.code, "message": mapped.message}
    return entry


def build() -> dict:
    cases = []
    for code in CODES:
        cases.append(case(f"rejected:{code or '<empty>'}", AIRejected("model text", code=code)))
    # The two ways the unavailable branch learns the failure was the *source* rather than the
    # provider: the exception's cause, and a retained artifact.
    unavailable = AIUnavailable("no provider answered")
    unavailable.__cause__ = SourceUnavailable("the page did not open")
    cases.append(case("unavailable:source-cause", unavailable))
    cases.append(case("unavailable:source-artifact", AIUnavailable("no provider", (artifact("source-failure"),))))
    cases.append(case("unavailable:provider", AIUnavailable("no provider")))
    cases.append(case("not-an-ai-error", ValueError("something else entirely")))
    return {
        "description": "The AI failure → HTTP failure table: the deadline and output code sets, the "
                       "deliberately neutral fallthrough, and the two ways a source failure is "
                       "recognised.",
        "cases": cases,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
