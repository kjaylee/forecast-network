#!/usr/bin/env python3
"""Export what Python's dispute review decides, so a Rust port can be held to it.

`AiCoordinator.review_dispute` decides whether a challenge to a resolution stands, and the
answer it produces is a `DisputeReview` whose hashes are committed by the lifecycle. A port
that agrees on the disposition but not on the provenance chain is a second opinion about what
happened, not a reproduction of it — so this exports the whole result, including every
artifact hash, alongside the exact inputs that produced it.

Three things make the vector useful rather than decorative:

  * The dispute evidence is collected through the real `SourceCollector` against a fake
    transport, so the snapshot digests are real digests of real bytes.
  * The retained bodies are exported alongside their digests, so the Rust test can serve the
    same bytes rather than inventing its own.
  * The three model answers are exported raw, in call order, so the Rust test replays the
    same conversation instead of asserting against a different one.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.ai import ProviderConfig  # noqa: E402
from forecast_application.sources import Artifact  # noqa: E402
from forecast_domain.models import Dispute  # noqa: E402
from forecast_domain.serialization import content_hash, to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests.test_web_ai import BODY, Transport, challenge_fixture, coordinator  # noqa: E402

GOLDEN = ROOT / "tests/golden/ai-dispute-golden.json"

DISPUTE_URL = "https://www.apple.com/newsroom/correction/"
DISPUTE_BODY = "Apple Newsroom correction: the announced product is Product Y, not Product X. " * 3
NOW_MS = 402000
SUBMITTED_AT_MS = 401000
PROVIDERS = (ProviderConfig("gemini", "test-model", "test-key"), ProviderConfig("cloudflare", "@cf/model"))
RESPONSES = [
    {"evidence_validated": True, "explanation": "Usable retained counterevidence."},
    {"material_conflict": True, "reason_summary": "Exact product identity is materially disputed."},
    {"agrees": True, "explanation": "Independent provider agrees with material conflict."},
]


async def build() -> dict:
    forecast = await challenge_fixture()

    collecting = Transport([])
    collecting.source_body = DISPUTE_BODY
    snapshots, artifacts = await coordinator(collecting).collect_dispute_evidence(
        forecast.specification, DISPUTE_URL, SUBMITTED_AT_MS)
    resolution_hash = forecast.resolution.resolution_hash
    dispute = Dispute(
        dispute_id="dispute-ai", disputant_id="user-ai", forecast_id=forecast.forecast_id,
        specification_hash=forecast.specification_hash, resolution_hash=resolution_hash,
        claim="Product name differs.", evidence=snapshots, rule_clause_id="yes-rule",
        explanation="The referenced exact name may identify another product.",
        submitted_at_ms=SUBMITTED_AT_MS)

    # The original resolution's own bytes, which the review must re-read rather than trust.
    original = Artifact(hashlib.sha256(BODY.encode()).hexdigest(), "source", BODY, "text/html")
    retained = {item.content_hash: item.body for item in (*artifacts, original)}

    transport = Transport(list(RESPONSES))
    ai = coordinator(transport, providers=PROVIDERS,
                     read_artifact=lambda digest: _read(retained, digest))
    result = await ai.review_dispute(forecast, dispute, NOW_MS)
    result.review.require_valid_for(dispute, forecast.resolution, forecast.specification)

    # The payloads the three calls were actually given. The two retained readings are the point
    # of the exercise, so the exact excerpt that reached the model is part of the vector.
    payloads = []
    for call in transport.calls:
        request = call["body"]
        raw = (request["contents"][0]["parts"][0]["text"] if "contents" in request
               else request["messages"][-1]["content"])
        payloads.append(json.loads(raw))

    return {
        "description": "AiCoordinator.review_dispute, from the challenge fixture through "
                       "material conflict to an independent re-judge that agrees.",
        "now_ms": NOW_MS,
        "providers": [{"provider": p.provider, "model": p.model, "apiKey": p.api_key} for p in PROVIDERS],
        "forecast": to_dict(forecast),
        "dispute": to_dict(dispute),
        "bodies": retained,
        "responses": RESPONSES,
        "payloads": payloads,
        "expect": {
            "disposition": result.review.disposition.value,
            "evidence_validated": result.review.evidence_validated,
            "material_conflict": result.review.material_conflict,
            "reason_summary": result.review.reason_summary,
            "independent_judge": result.review.independent_judge.provider,
            "evidence_validation": result.review.evidence_validation.provider,
            "counter_analysis": result.review.counter_analysis.provider,
            "review_hash": content_hash(to_dict(result.review)),
            "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                          for item in result.artifacts],
            "source_calls": collecting.source_calls,
        },
    }


async def _read(retained: dict[str, str], digest: str) -> str | None:
    return retained.get(digest)


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
