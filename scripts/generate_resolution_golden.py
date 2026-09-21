#!/usr/bin/env python3
"""Export what Python's resolution pipeline decides, so a Rust port can be held to it.

`AiCoordinator.propose_resolution` collects evidence, asks a verifier, a judge and a
counter-judge, and then commits a `Resolution` whose every hash is bound into the lifecycle.
It is the centre of the system: the outcome it proposes is what a reward is eventually paid
against.

The vector is deliberately forensic rather than outcome-shaped. It exports the payload each
of the three calls was given, the retained source bytes, and every artifact the pipeline
produced — because a port that reaches the same verdict through a different conversation has
not been ported, it has been reimplemented, and the difference would surface only when a
forecast resolved by one worker was read by the other.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_domain.lifecycle import (  # noqa: E402
    BeginResolution,
    BeginValidation,
    Lock,
    Publish,
    create_forecast,
)
from forecast_domain.serialization import to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests.lifecycle_fixtures import step  # noqa: E402
from tests.test_web_ai import (  # noqa: E402
    BODY,
    NOW,
    Transport,
    compile_outputs,
    coordinator,
    resolution_outputs,
)

GOLDEN = ROOT / "tests/golden/ai-resolution-golden.json"
RESOLVED_AT_MS = 400000
QUESTION = "Will Apple announce Product X before the stated deadline?"


async def build() -> dict:
    compiling = Transport(compile_outputs())
    compiled = await coordinator(compiling).compile_question(QUESTION, (), NOW)
    forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai",
                               specification=compiled.specification, now_ms=NOW)
    forecast = step(forecast, BeginValidation(), NOW).forecast
    forecast = step(forecast, Publish(assessment=compiled.assessment), NOW).forecast
    forecast = step(forecast, Lock(), RESOLVED_AT_MS).forecast
    before = step(forecast, BeginResolution(), RESOLVED_AT_MS).forecast

    transport = Transport(resolution_outputs())
    transport.source_body = BODY
    result = await coordinator(transport).propose_resolution(before, RESOLVED_AT_MS)
    result.resolution.require_proposable(before.specification)

    payloads = []
    for call in transport.calls:
        request = call["body"]
        raw = (request["contents"][0]["parts"][0]["text"] if "contents" in request
               else request["messages"][-1]["content"])
        payloads.append(json.loads(raw))

    return {
        "description": "AiCoordinator.propose_resolution: evidence collection, source verification, "
                       "the judge and the counter-judge, committing a CLEAR YES at 9,400 bp.",
        "now_ms": RESOLVED_AT_MS,
        "publication_time_unknown": False,
        "determined_outcome": None,
        "providers": [{"provider": "gemini", "model": "test-model", "apiKey": "test-key"}],
        "before": to_dict(before),
        "sources": [{"url": url, "status": 200, "contentType": "text/html", "body": BODY}
                    for url in transport.source_calls],
        "responses": resolution_outputs(),
        "payloads": payloads,
        "expect": {
            "resolution": to_dict(result.resolution),
            "payloads": payloads,
            "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                          for item in result.artifacts],
        },
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
