#!/usr/bin/env python3
"""Export the compiler's wire contract, so a Rust port can be held to it.

`compile_question` is the front door: every published question, and therefore every reward,
descends from a specification this produced. Three pieces decide whether that happens at all,
and each is exported here rather than described:

  * `_spec_schema` — the contract the model is shown. It removes `pattern` and `uniqueItems`,
    flattens `$ref`s, turns `const` into a single-valued `enum`, replaces the arithmetic
    `close_at_ms` with a human-readable `close_at_utc`, and swaps the verified
    `forecast_id`/`specification_hash` pair for a bounded `candidate_ref`.
  * `_candidate_context` — the same candidates the model may refer to, and the commitment that
    the set did not change while it was thinking.
  * `_normalize_compiler_output` — where a model's prose is turned into the exact UTC instant the
    lifecycle commits. This is the one place where a model's date is checked against the user's,
    in either language and either spelling, and it is where the reference is least forgiving.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import (  # noqa: E402
    COMPILER_WIRE_VERSION,
    AIRejected,
    _assert_candidate_context,
    _candidate_context,
    _normalize_compiler_output,
    _spec_schema,
)
from forecast_domain.lifecycle import create_forecast  # noqa: E402
from forecast_domain.serialization import content_hash, to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests import model_fixtures as model  # noqa: E402

GOLDEN = ROOT / "tests/golden/ai-compiler-golden.json"
DEADLINE = "2026-09-21T12:00:00Z"
WINDOW = "[2026-09-01T00:00:00Z, 2026-09-21T12:00:00Z)"
LATER_WINDOW = "[2026-09-02T00:00:00Z, 2026-09-21T12:00:00Z)"


def candidate(index: int, *, question: str | None = None):
    changes = {"canonical_question": question} if question is not None else {}
    return create_forecast(forecast_id=f"forecast-{index}", creator_id="creator-1",
                           specification=model.specification(**changes), now_ms=100)


def rule(clause_id: str, outcome: str, condition: str) -> dict:
    return {"schema_version": 1, "clause_id": clause_id, "outcome": outcome, "condition": condition}


def compiler_output(question: str | None = None, *,
                    rules: list | None = None, invalidation: list | None = None,
                    duplicates: list | None = None, wire: str = COMPILER_WIRE_VERSION,
                    close_at_utc: str = DEADLINE) -> dict:
    return {
        "schema_version": 1,
        # The reference requires the exact closing instant in the question and in each YES/NO
        # clause, so a fixture that leaves it out is testing the mismatch, not the compiler.
        "canonical_question": question if question is not None else f"Will Acme announce Product X before {DEADLINE}?",
        "rules": rules if rules is not None else [
            rule("yes-rule", "YES", f"An official announcement dated before {close_at_utc} names Product X."),
            rule("no-rule", "NO", f"No qualifying announcement exists at {close_at_utc}."),
            rule("invalid-rule", "INVALID", "The named company or product cannot be uniquely identified."),
        ],
        "open_at_ms": 100,
        "close_at_utc": close_at_utc,
        "source_policy": to_dict(model.specification().source_policy),
        "invalidation_rules": invalidation if invalidation is not None else [
            "Apply INVALID if the named product has multiple incompatible identities."],
        "category": "TECHNOLOGY",
        "share_title": "Will Acme announce Product X?",
        "ambiguity_score_bp": 500,
        "duplicate_candidates": duplicates if duplicates is not None else [],
        "compiler_wire_version": wire,
    }


def normalize_case(name: str, question: str, output: dict, candidates: list | None = None,
                   *, distinct_windows: bool = False) -> dict:
    context = _candidate_context(candidates or [])
    case = {"name": name, "question": question, "output": output, "candidates": context,
            "distinct_windows": distinct_windows}
    try:
        case["normalized"] = _normalize_compiler_output(output, question, context,
                                                        distinct_windows=distinct_windows)
    except AIRejected as exc:
        case["error"] = {"code": exc.code, "message": str(exc)}
    return case


def commitment_case(name: str, candidates: list, expected_from: list, expected_to: list) -> dict:
    expected = content_hash(_candidate_context(expected_to))
    case = {"name": name, "expected": expected}
    try:
        _assert_candidate_context(candidates, expected)
        case["accepted"] = True
    except AIRejected as exc:
        case["accepted"] = False
        case["code"] = exc.code
    return case


def build() -> dict:
    one, other = candidate(0), candidate(1)
    later = candidate(1, question=f"During {LATER_WINDOW} will Acme announce Product X?")
    same = candidate(1, question=f"During {WINDOW} will Acme announce Product X?")

    valid_reference = {"schema_version": 1, "candidate_ref": "c0", "similarity_bp": 9000,
                       "materially_different_rules": False, "explanation": "Same product, same deadline."}
    outside_window = f"During {WINDOW} Acme will announce Product X by 14:30."

    windowed_rules = [
        rule("yes-rule", "YES", f"An announcement inside {WINDOW} names Product X."),
        rule("no-rule", "NO", f"No announcement inside {WINDOW} names Product X."),
        rule("invalid-rule", "INVALID", "The named company or product cannot be uniquely identified."),
    ]

    cases = [
        normalize_case("plain", f"Will Acme announce Product X before {DEADLINE}?", compiler_output()),
        normalize_case("windowed", f"During {WINDOW} will Acme announce Product X?",
                       compiler_output(f"During {WINDOW} will Acme announce Product X.", rules=windowed_rules)),
        normalize_case("korean-deadline", "애크미가 마감 전에 제품 X를 발표할까요?",
                       compiler_output("Acme will announce Product X before 2026년 9월 21일 12:00 UTC.", rules=[
                           rule("yes-rule", "YES",
                                "An official announcement before 2026년 9월 21일 12:00 UTC names Product X."),
                           rule("no-rule", "NO", f"No qualifying announcement exists at {DEADLINE}."),
                           rule("invalid-rule", "INVALID", "Unidentifiable."),
                       ])),
        normalize_case("korean-deadline-mismatch", "애크미가 마감 전에 제품 X를 발표할까요?",
                       compiler_output("Acme will announce Product X before 2026년 9월 22일 12:00 UTC.")),
        normalize_case("korean-deadline-invalid", "애크미가 마감 전에 제품 X를 발표할까요?",
                       compiler_output("Acme will announce Product X before 2026년 13월 21일 12:00 UTC.")),
        normalize_case("korean-deadline-fully-spelled", "애크미가 마감 전에 제품 X를 발표할까요?",
                       compiler_output("Acme will announce Product X before 2026년 9월 21일 12:00:00 UTC.")),
        normalize_case("conflicting-iso-date", f"Will Acme announce Product X before {DEADLINE}?", compiler_output(rules=[
            rule("yes-rule", "YES", f"An announcement before {DEADLINE} names Product X."),
            rule("no-rule", "NO", "No qualifying announcement exists at 2026-09-22T12:00:00Z."),
            rule("invalid-rule", "INVALID", "Unidentifiable."),
        ])),
        normalize_case("offset-spelling-is-not-the-instant", f"Will Acme announce Product X before {DEADLINE}?", compiler_output(rules=[
            rule("yes-rule", "YES", f"An announcement before {DEADLINE} names Product X."),
            rule("no-rule", "NO", "No qualifying announcement exists at 2026-09-21T21:00:00+09:00."),
            rule("invalid-rule", "INVALID", "Unidentifiable."),
        ])),
        normalize_case("conflicting-plain-date", f"Will Acme announce Product X before {DEADLINE}?", compiler_output(rules=[
            rule("yes-rule", "YES", f"An announcement before {DEADLINE} names Product X."),
            rule("no-rule", "NO", "No qualifying announcement exists on 2026/09/22."),
            rule("invalid-rule", "INVALID", "Unidentifiable."),
        ])),
        normalize_case("wire-version", f"Will Acme announce Product X before {DEADLINE}?", compiler_output(wire="compiler-utc-candidate-ref-v2")),
        normalize_case("deadline-without-zone", f"Will Acme announce Product X before {DEADLINE}?", compiler_output(close_at_utc="2026-09-21T12:00:00")),
        normalize_case("deadline-impossible", f"Will Acme announce Product X before {DEADLINE}?", compiler_output(close_at_utc="2026-02-30T12:00:00Z")),
        normalize_case("two-intervals", "Will Acme during [2026-09-01T00:00:00Z, 2026-09-02T00:00:00Z) and "
                                        "[2026-09-03T00:00:00Z, 2026-09-04T00:00:00Z) announce X?",
                       compiler_output()),
        normalize_case("window-moved", f"During {WINDOW} Acme will announce Product X?",
                       compiler_output(f"During {LATER_WINDOW} Acme will announce Product X.", rules=windowed_rules)),
        normalize_case("window-end-not-deadline", f"During {WINDOW} Acme will announce Product X?",
                       compiler_output(f"During {WINDOW} Acme will announce Product X.", rules=windowed_rules,
                                       close_at_utc="2026-09-22T12:00:00Z")),
        normalize_case("time-outside-window", f"During {WINDOW} Acme will announce Product X by 14:30?",
                       compiler_output(outside_window, rules=windowed_rules)),
        normalize_case("window-omitted", f"During {WINDOW} Acme will announce Product X?",
                       compiler_output("Acme will announce Product X.")),
        normalize_case("candidate-valid", f"Will Acme announce Product X before {DEADLINE}?",
                       compiler_output(duplicates=[dict(valid_reference)]), [one, other]),
        normalize_case("candidate-unknown-ref", f"Will Acme announce Product X before {DEADLINE}?",
                       compiler_output(duplicates=[{**valid_reference, "candidate_ref": "c7"}]), [one, other]),
        normalize_case("candidate-repeated-ref", f"Will Acme announce Product X before {DEADLINE}?",
                       compiler_output(duplicates=[dict(valid_reference), dict(valid_reference)]), [one, other]),
        normalize_case("candidate-wrong-fields", f"Will Acme announce Product X before {DEADLINE}?",
                       compiler_output(duplicates=[{**valid_reference, "forecast_id": "forecast-0"}]), [one, other]),
        normalize_case("candidate-error-case", f"Will Acme announce Product X before {DEADLINE}?",
                       compiler_output(duplicates=[dict(valid_reference)]), [one]),
        normalize_case("distinct-measurement-windows", f"During {WINDOW} will Acme announce Product X?",
                       compiler_output(f"During {WINDOW} will Acme announce Product X.", rules=windowed_rules,
                                       duplicates=[{**valid_reference, "candidate_ref": "c1"}]),
                       [one, later], distinct_windows=True),
        normalize_case("same-measurement-windows", f"During {WINDOW} will Acme announce Product X?",
                       compiler_output(f"During {WINDOW} will Acme announce Product X.", rules=windowed_rules,
                                       duplicates=[{**valid_reference, "candidate_ref": "c1"}]),
                       [one, same], distinct_windows=True),
        normalize_case("distinct-windows-not-asked-for", f"During {WINDOW} will Acme announce Product X?",
                       compiler_output(f"During {WINDOW} will Acme announce Product X.", rules=windowed_rules,
                                       duplicates=[{**valid_reference, "candidate_ref": "c1"}]),
                       [one, later], distinct_windows=False),
    ]

    return {
        "description": "The compiler wire: the schema the model sees, the candidate context it may "
                       "refer to, and the normalization that turns its prose into the committed instant.",
        "spec_schema": {str(count): _spec_schema([candidate(index) for index in range(count)])
                        for count in (0, 1, 3)},
        "candidate_context": {"empty": _candidate_context([]),
                              "one": _candidate_context([one]),
                              "two": _candidate_context([one, other])},
        # The forecasts themselves, so a port can build a context rather than only read one.
        "candidate_forecasts": {"one": to_dict(one), "two": [to_dict(one), to_dict(other)]},
        "commitment": [commitment_case("unchanged", [one, other], [one, other], [one, other]),
                       commitment_case("changed", [one, other], [one, other], [one])],
        "normalize": cases,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
