#!/usr/bin/env python3
"""Export the early-resolution path, so a Rust port can be held to it.

Three entry points, all of which read retained bytes rather than summaries:

  * `review_source_observation` decides whether one watched official article qualifies a
    question early. It is the only place a *positive* result can be reached without a
    deadline passing, so its refusals are as important as its acceptances: an unrelated
    article is dismissed by a second provider, an uncertain one is not, and both are
    recorded with the exact provider identities that decided it.
  * `propose_early_resolution` turns a qualified trigger into a resolution, judging the
    immutable YES clause against the trigger's own bytes.
  * `check_question_freshness` runs before publication, to refuse a question whose event
    a retained article already establishes.

The vector exports the payload of every call, the records, and every artifact hash. The
observation is built by the watch's own `article_content`, so the content commitment the
providers see is a real commitment over real bytes.

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

from forecast_application.ai import AIRejected, AIUnavailable, ProviderConfig  # noqa: E402
from forecast_application.source_watch import article_content  # noqa: E402
from forecast_domain.early_resolution import LockEarly  # noqa: E402
from forecast_domain.lifecycle import BeginResolution, BeginValidation, Publish, create_forecast  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402
from tests.model_fixtures import validation  # noqa: E402
from tests.test_early_resolution_domain import step  # noqa: E402
from tests.test_source_watch import URL, digest  # noqa: E402
from tests.test_web_ai import Transport, coordinator, resolution_outputs, specification  # noqa: E402

GOLDEN = ROOT / "tests/golden/ai-early-golden.json"
# The article the early-resolution tests use. The watched-feed fixture's own body carries a
# date-only publication stamp, which is a different branch: a dated article observed before its
# publication date is `publication_time_uncertain` and never reaches qualification at all.
BODY = (
    "<main>Apple officially announces Product X. The exact published official announcement "
    "and required specifications are confirmed.</main>"
)
OBSERVED_AT_MS = 2000
REVIEWED_AT_MS = 3000


def observation() -> dict:
    text, date, precision = article_content(BODY)
    content = json.dumps({"text": text, "publicationDate": date, "datePrecision": precision},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"url": URL, "artifactHash": digest(BODY), "contentHash": digest(content),
            "observedAt": OBSERVED_AT_MS}


def forecast() -> dict:
    return {"id": "forecast-observer", "specificationHash": specification().specification_hash,
            "specification": to_dict(specification())}


def counter(agrees: bool = True) -> dict:
    return {"agrees": agrees, "explanation": "Independent provider verified every exact condition.",
            "event_time_basis": "observed_upper_bound", "event_not_after_ms": OBSERVED_AT_MS}


def verified(relevant: bool = True) -> dict:
    return {"verified": True, "relevant": relevant, "explanation": "Official article with substantive source evidence."}


def qualified() -> dict:
    return {"positive_existential": True, "all_conditions_satisfied": True, "irreversible": True,
            "invalidation_clear": True,
            "explanation": "The official article establishes the exact immutable announcement conditions."}


def build_ai(outputs: list):
    transport = Transport(outputs)

    async def read(key):
        return BODY if key == digest(BODY) else None

    providers = [ProviderConfig("gemini", "test-model", "test-key"), ProviderConfig("openai", "test-model", "test-key")]
    return coordinator(transport, providers=providers, read_artifact=read), transport


def payload_of(request: dict) -> dict:
    """The user text of whichever provider envelope this request used."""
    if "contents" in request:
        raw = request["contents"][0]["parts"][0]["text"]
    elif "messages" in request:
        raw = request["messages"][-1]["content"]
    else:
        raw = request["input"][-1]["content"]
    return json.loads(raw)


def payloads_of(transport: Transport) -> list[dict]:
    return [payload_of(call["body"]) for call in transport.calls]


def schema_of(transport: Transport) -> list:
    schemas = []
    for call in transport.calls:
        body = call["body"]
        if "generationConfig" in body:
            schemas.append(body["generationConfig"]["responseJsonSchema"])
        elif "text" in body:
            schemas.append(body["text"]["format"]["schema"])
        else:
            schemas.append(body["response_format"]["json_schema"])
    return schemas


async def observation_case(name: str, outputs: list, now_ms: int) -> dict:
    ai, transport = build_ai(outputs)
    case = {"name": name, "now_ms": now_ms, "observation": observation(), "forecast": forecast(),
            "responses": outputs, "payloads": [], "schemas": []}
    try:
        result = await ai.review_source_observation(case["forecast"], case["observation"], now_ms)
    except (AIRejected, AIUnavailable) as exc:
        case["payloads"] = payloads_of(transport)
        case["schemas"] = schema_of(transport)
        case["error"] = {"code": getattr(exc, "code", None),
                         "message": str(exc), "unavailable": isinstance(exc, AIUnavailable)}
        return case
    case["payloads"] = payloads_of(transport)
    case["schemas"] = schema_of(transport)
    case["result"] = {"accepted": result["accepted"], "reason": result["reason"],
                      "dismissible": result.get("dismissible", False),
                      "dismissalProof": result.get("dismissalProof"),
                      "trigger": to_dict(result["trigger"]) if result["trigger"] is not None else None,
                      "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                                    for item in result["artifacts"]]}
    case["_trigger"] = result["trigger"]
    return case


async def proposal_case(trigger: dict, name: str = "early-proposal") -> dict:
    outputs = [resolution_outputs()[1], counter()]
    ai, transport = build_ai(outputs)
    draft = create_forecast(forecast_id="forecast-observer", creator_id="creator-watch",
                            specification=specification(), now_ms=0)
    validating = step(draft, BeginValidation(), 10).forecast
    opened = step(validating, Publish(assessment=validation(specification())), 60).forecast
    locked = step(opened, LockEarly(trigger=trigger), 3001).forecast
    resolving = step(locked, BeginResolution(), 3002).forecast
    proposal = await ai.propose_early_resolution(resolving, 3003)
    proposal.resolution.require_proposable(specification())
    return {"name": name, "forecast": to_dict(resolving), "now_ms": 3003, "responses": outputs,
            "payloads": payloads_of(transport), "schemas": schema_of(transport),
            "expect": {"resolution": to_dict(proposal.resolution),
                       "artifacts": [{"kind": item.kind, "hash": item.content_hash, "body": item.body}
                                     for item in proposal.artifacts]}}


async def freshness_case(name: str, outputs: list) -> dict:
    ai, transport = build_ai(outputs)
    case = {"name": name, "now_ms": REVIEWED_AT_MS, "specification": to_dict(specification()),
            "observation": observation(), "responses": outputs}
    try:
        artifacts = await ai.check_question_freshness(specification(), [case["observation"]], REVIEWED_AT_MS)
    except (AIRejected, AIUnavailable) as exc:
        case["payloads"] = payloads_of(transport)
        case["schemas"] = schema_of(transport)
        case["error"] = {"code": getattr(exc, "code", None), "message": str(exc)}
        return case
    case["payloads"] = payloads_of(transport)
    case["schemas"] = schema_of(transport)
    case["artifacts"] = [{"kind": item.kind, "hash": item.content_hash, "body": item.body} for item in artifacts]
    return case


async def build() -> dict:
    accepted = await observation_case("observe-accepted",
                                      [verified(), qualified(), counter()], REVIEWED_AT_MS)
    trigger = accepted.pop("_trigger")
    unrelated = await observation_case("observe-unrelated-dismissible",
                                       [verified(relevant=False), counter()], REVIEWED_AT_MS)
    disagreement = await observation_case("observe-unrelated-disagreement",
                                          [verified(relevant=False), counter(agrees=False)], REVIEWED_AT_MS)
    unverified = await observation_case("observe-not-verified",
                                        [{"verified": False, "relevant": True, "explanation": "Archive index page."}],
                                        REVIEWED_AT_MS)
    not_qualified = await observation_case("observe-not-qualified",
                                           [verified(), {**qualified(), "irreversible": False}, counter()],
                                           REVIEWED_AT_MS)

    return {
        "description": "The early-resolution path: a watched article qualified against retained bytes, "
                       "the resolution it proposes, and the publication freshness review.",
        "providers": [{"provider": "gemini", "model": "test-model", "apiKey": "test-key"},
                      {"provider": "openai", "model": "test-model", "apiKey": "test-key"}],
        "observation": observation(),
        "observationBody": BODY,
        "observationCases": [accepted, unrelated, disagreement, unverified, not_qualified],
        "proposal": await proposal_case(trigger),
        "freshnessCases": [
            await freshness_case("freshness-already-resolved",
                                 [{"status": "known_true", "monotonic_positive": True,
                                   "all_conditions_satisfied": True,
                                   "explanation": "The retained announcement already establishes every exact YES condition."}]),
            await freshness_case("freshness-uncertain",
                                 [{"status": "uncertain", "monotonic_positive": False,
                                   "all_conditions_satisfied": False,
                                   "explanation": "The official page only mentions a related product."}]),
        ],
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
