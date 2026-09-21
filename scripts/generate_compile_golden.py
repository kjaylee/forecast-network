#!/usr/bin/env python3
"""Export a full `compile_question` run, so a Rust port can be held to it.

This is the longest task in the pipeline, and the one where a port is most likely to be
"nearly right": four model calls, a collected source, a normalized specification, a
validation assessment and a probability estimate, each with its own provenance chain. So
the vector exports all of it — the payloads each call was given, the specification and
assessment as records, every artifact hash, and the estimate.

The payloads are also how the prompts are transcribed into Rust. They are data, and this
file is the only place they are written down twice on purpose: the Rust test compares the
payload it sends against the payload here, so a policy sentence that lost a space at a
line join fails the build rather than quietly asking the model something slightly else.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import AIRejected  # noqa: E402
from forecast_domain.lifecycle import create_forecast  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402

from tests import model_fixtures as model  # noqa: E402
from tests.test_web_ai import (  # noqa: E402
    BODY,
    NOW,
    Transport,
    compile_outputs,
    compiler_wire,
    coordinator,
    measurement_compile_outputs,
    specification,
)

GOLDEN = ROOT / "tests/golden/ai-compile-golden.json"
QUESTION = "Will Apple announce Product X before the stated deadline?"
WINDOW = "[1970-01-01T00:02:00Z, 1970-01-02T00:06:40Z)"


def candidate(index: int):
    return create_forecast(forecast_id=f"forecast-{index}", creator_id="creator-1",
                           specification=model.specification(), now_ms=NOW)


def payloads_of(transport: Transport) -> list[dict]:
    payloads = []
    for call in transport.calls:
        request = call["body"]
        raw = (request["contents"][0]["parts"][0]["text"] if "contents" in request
               else request["messages"][-1]["content"])
        payloads.append(json.loads(raw))
    return payloads


async def case(name: str, candidates: list, *, similarity_bp: int = 8000,
               outputs: list | None = None, question: str = QUESTION) -> dict:
    outputs = outputs if outputs is not None else compile_outputs()
    question = question
    if candidates:
        outputs[0] = dict(outputs[0], duplicate_candidates=[
            {"schema_version": 1, "candidate_ref": f"c{index}", "similarity_bp": similarity_bp,
             "materially_different_rules": False, "explanation": f"Candidate {index} is the same event."}
            for index in range(len(candidates))])
    transport = Transport(outputs)
    transport.source_body = BODY
    try:
        compiled = await coordinator(transport).compile_question(question, candidates, NOW)
    except AIRejected as exc:
        return {"name": name, "question": question, "now_ms": NOW,
                "distinct_measurement_windows": False,
                "candidates": [to_dict(item) for item in candidates],
                "sources": [{"url": url, "status": 200, "contentType": "text/html", "body": BODY}
                            for url in transport.source_calls],
                "responses": outputs, "payloads": payloads_of(transport),
                "error": {"code": exc.code, "message": str(exc)}}

    return {
        "name": name,
        "question": question,
        "now_ms": NOW,
        "distinct_measurement_windows": False,
        "candidates": [to_dict(item) for item in candidates],
        "sources": [{"url": url, "status": 200, "contentType": "text/html", "body": BODY}
                    for url in transport.source_calls],
        "responses": outputs,
        "payloads": payloads_of(transport),
        "expect": {
            "specification": to_dict(compiled.specification),
            "assessment": to_dict(compiled.assessment),
            "aiForecast": compiled.ai_forecast,
            "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                          for item in compiled.artifacts],
        },
    }


async def refresh_case() -> dict:
    """A fresh estimate for an open forecast: the clock is read again after the fetch."""
    outputs = [compile_outputs()[3]]
    transport = Transport(outputs)
    transport.source_body = BODY
    spec = specification()
    evaluated_at = 2500
    result = await coordinator(transport).refresh_prediction(spec, NOW + 1000, lambda: evaluated_at)
    return {"name": "refresh-prediction", "specification": to_dict(spec),
            "now_ms": NOW + 1000, "evaluated_at_ms": evaluated_at,
            "sources": [{"url": url, "status": 200, "contentType": "text/html", "body": BODY}
                        for url in transport.source_calls],
            "responses": outputs, "payloads": payloads_of(transport),
            "expect": {"aiForecast": result.ai_forecast,
                       "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                                     for item in result.artifacts]}}


async def build() -> dict:
    return {
        "description": "compile_question end to end: the compiler call, source collection, the "
                       "ambiguity judge, the duplicate detector and the probability estimate.",
        "providers": [{"provider": "gemini", "model": "test-model", "apiKey": "test-key"}],
        "wireVersion": compiler_wire()["compiler_wire_version"],
        "cases": [await case("no-candidates", []),
                  await case("two-candidates", [candidate(0), candidate(1)]),
                  # A declared candidate at or above the similarity threshold that is not
                  # materially different is a duplicate of a published question, and publishing
                  # it is exactly what the threshold exists to prevent.
                  await case("materially-equal-candidate", [candidate(0), candidate(1)], similarity_bp=9000),
                  # A declared measurement interval: the question names one explicit half-open
                  # window and every criterion must carry it back unchanged.
                  await refresh_case(),
                  await case("measurement-window", [],
                             outputs=measurement_compile_outputs(),
                             question=f"During {WINDOW}, will Apple officially announce Product X?")],
    }


def main() -> int:
    import asyncio

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
