"""Provider transports are mocked only in tests; returned domain records are real."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/application/src"))

from forecast_application.ai import (
    COMPILER_WIRE_VERSION,
    MAX_CANDIDATE_CONTEXT_BYTES,
    MAX_CANDIDATES,
    MAX_DECISION_ARTIFACT_BYTES,
    AiCoordinator,
    AIRejected,
    AIUnavailable,
    ProviderConfig,
)
from forecast_application.sources import (
    MAX_EXCERPT_BYTES,
    MAX_SOURCE_BYTES,
    Artifact,
    SourceCollector,
    SourceRejected,
    SourceUnavailable,
    TextResponse,
    evidence_excerpt,
    validate_public_url,
)
from forecast_domain.lifecycle import (
    BeginChallenge,
    BeginResolution,
    BeginValidation,
    Lock,
    ProposeResolution,
    Publish,
    create_forecast,
)
from forecast_domain.models import AITask, Dispute, ReviewDisposition, Source, SourcePolicy
from forecast_domain.serialization import canonical_bytes, content_hash, dumps, loads, to_dict

from tests import model_fixtures as model
from tests.lifecycle_fixtures import step

NOW = 1000
BODY = "<html><body>Apple Newsroom. Official Product X announcement, published before the deadline. " + "source context " * 10 + "</body></html>"


def specification():
    spec = model.specification(open_at_ms=NOW, close_at_ms=400000,
        source_policy=SourcePolicy(primary_sources=(Source(source_id="apple-news", name="Apple Newsroom",
            url="https://www.apple.com/newsroom/", is_official=True),)))
    return replace(spec, canonical_question="Will Apple announce Product X by 1970-01-01T00:06:40Z?",
        rules=tuple(replace(rule, condition=rule.condition + " Deadline: 1970-01-01T00:06:40Z.")
                    if rule.outcome.value in {"YES", "NO"} else rule for rule in spec.rules))


def compiler_wire():
    wire = to_dict(specification())
    del wire["close_at_ms"]
    wire["close_at_utc"] = "1970-01-01T00:06:40Z"
    wire["compiler_wire_version"] = COMPILER_WIRE_VERSION
    return wire


class Transport:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.source_calls: list[str] = []
        self.source_body = BODY

    async def json(self, url, method, headers, body):
        self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        if "generativelanguage" in url:
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}],
                    "modelVersion": "gemini-tested-revision"}
        if url.startswith("workers-ai:"):
            return {"response": value}
        return {"status": "completed", "model": "openai-tested-revision",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}]}

    async def text(self, url, method, headers):
        self.source_calls.append(url)
        return TextResponse(200, self.source_body, {"content-type": "text/html"})


def coordinator(transport, *, providers=None, **kwargs):
    return AiCoordinator(providers or (ProviderConfig("gemini", "test-model", "test-key"),),
                         transport.json, transport.text, **kwargs)


def compile_outputs():
    return [compiler_wire(), {"objectively_resolvable": True, "ambiguity_passed": True,
        "sources_appropriate": True, "english_language_passed": True, "intent_preserved": True,
        "title_consistent": True,
        "explanation": "Objective and source-verifiable criteria; faithful English translation."},
        {"check_completed": True, "candidates_accurate": True, "explanation": "All candidates assessed."},
        {"yesProbabilityBp": 6200, "rationale": "Available source context suggests 62%, with launch uncertainty."}]


def resolution_outputs(*, agrees=True):
    return [{"verified": True, "explanation": "Official retained source with substantive evidence."},
        {"proposed_outcome": "YES", "confidence_bp": 9400, "rule_matches": ["yes-rule"],
         "rule_conflicts": [], "reason_summary": "Official evidence satisfies the unchanged YES clause.",
         "conflict_status": "CLEAR", "conflict_explanation": None},
        {"agrees": agrees, "explanation": "Independent challenge of the exact judge decision."}]


async def challenge_fixture():
    transport = Transport(compile_outputs() + resolution_outputs())
    ai = coordinator(transport)
    compiled = await ai.compile_question("Will Apple announce Product X before the stated deadline?", (), NOW)
    forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai",
                              specification=compiled.specification, now_ms=NOW)
    forecast = step(forecast, BeginValidation(), NOW).forecast
    forecast = step(forecast, Publish(assessment=compiled.assessment), NOW).forecast
    forecast = step(forecast, Lock(), 400000).forecast
    forecast = step(forecast, BeginResolution(), 400000).forecast
    result = await ai.propose_resolution(forecast, 400000)
    forecast = step(forecast, ProposeResolution(resolution=result.resolution), 400000).forecast
    return step(forecast, BeginChallenge(duration_ms=100000), 400000).forecast


class AITests(unittest.IsolatedAsyncioTestCase):
    async def test_compiler_returns_publishable_exact_domain_commitments(self):
        transport = Transport(compile_outputs())
        ai = coordinator(transport)
        result = await ai.compile_question("Will Apple announce Product X before 2027?", (), NOW)
        result.assessment.require_publishable(result.specification)
        self.assertEqual(result.ai_forecast["probability"], 62)
        self.assertEqual(result.ai_forecast["specificationHash"], result.specification.specification_hash)
        self.assertTrue(any(item.content_hash == result.ai_forecast["artifactHash"] for item in result.artifacts))
        self.assertEqual(result.assessment.compiler.output_hash, result.specification.specification_hash)
        self.assertEqual(result.assessment.ambiguity_judge.input_hash, result.specification.specification_hash)
        self.assertEqual(result.assessment.compiler.model_version, "gemini-tested-revision")
        self.assertEqual(result.assessment.compiler.task, AITask.MARKET_COMPILER)
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(ai.configured_providers, ("gemini",))
        self.assertEqual(loads(type(result.assessment), dumps(result.assessment)), result.assessment)
        for artifact in result.artifacts:
            if artifact.kind == "source":
                digest = hashlib.sha256(artifact.body.encode()).hexdigest()
            else:
                digest = content_hash(json.loads(artifact.body))
            self.assertEqual(digest, artifact.content_hash)
            self.assertNotIn("test-key", artifact.body)

    async def test_deadline_epoch_computed_from_utc_string_and_raw_normalization_retained(self):
        now = 1790000000000
        outputs = compile_outputs()
        wire = outputs[0]
        wire["open_at_ms"] = now
        wire["close_at_utc"] = "2026-12-31T23:59:00Z"
        wire["canonical_question"] = "Will Apple announce Product X by 2026-12-31T23:59:00Z?"
        for rule in wire["rules"]:
            rule["condition"] = rule["condition"].replace("1970-01-01T00:06:40Z", wire["close_at_utc"])
        result = await coordinator(Transport(outputs)).compile_question(
            "Apple이 2026년 12월 31일 23:59 UTC까지 Product X를 발표할까요?", (), now)
        expected = int(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc).timestamp()) * 1000
        self.assertEqual(result.specification.close_at_ms, expected)
        self.assertNotEqual(result.specification.close_at_ms, 1798857540000)
        raw = next(json.loads(item.body) for item in result.artifacts if item.kind == "ai-decision")
        self.assertNotIn("close_at_ms", raw["output"])
        self.assertEqual(raw["output"]["close_at_utc"], "2026-12-31T23:59:00Z")
        normalized = next(json.loads(item.body) for item in result.artifacts if item.kind == "compiler-normalization")
        self.assertEqual(normalized["close_at_ms"], expected)
        self.assertEqual(normalized["specification_hash"], result.specification.specification_hash)

    async def test_missing_timezone_invalid_calendar_and_human_deadline_mismatch_rejected(self):
        for timestamp, text in (
            ("1970-01-01T00:06:40", None),
            ("1970-01-01T00:06:40+09:00", None),
            ("1970-02-30T00:06:40Z", None),
            ("1970-01-02T00:06:40Z", None),
            ("1970-01-01T00:06:40Z", "1970년 1월 2일까지 발표? (1970-01-01T00:06:40Z)"),
        ):
            outputs = compile_outputs()
            outputs[0]["close_at_utc"] = timestamp
            if text:
                outputs[0]["canonical_question"] = text
            transport = Transport(outputs)
            with self.subTest(timestamp=timestamp, text=text), self.assertRaises(AIRejected) as caught:
                await coordinator(transport).compile_question("Will Apple announce X before the deadline?", (), NOW)
            self.assertTrue(caught.exception.artifacts)
            self.assertEqual(transport.source_calls, [])

    async def test_equivalent_korean_utc_deadline_normalized_before_publication(self):
        now = 1790000000000
        outputs = compile_outputs()
        wire = outputs[0]
        wire["open_at_ms"] = now
        wire["close_at_utc"] = "2027-01-31T23:59:00Z"
        wire["canonical_question"] = "Will Apple announce Product X by 2027년 1월 31일 23:59:00Z?"
        for rule in wire["rules"]:
            rule["condition"] = rule["condition"].replace("1970-01-01T00:06:40Z", "2027년 1월 31일 23:59:00Z")
            if "2027년" not in rule["condition"]:
                rule["condition"] += " Deadline: 2027년 1월 31일 23:59:00Z."
        result = await coordinator(Transport(outputs)).compile_question(
            "Apple이 2027년 1월 31일 23:59 UTC까지 Product X를 발표할까요?", (), now)
        self.assertIn("2027-01-31T23:59:00Z", result.specification.canonical_question)
        self.assertTrue(all("2027-01-31T23:59:00Z" in rule.condition for rule in result.specification.rules))
        self.assertIn("2027년 1월 31일 23:59:00Z", wire["canonical_question"])
        self.assertTrue(all("2027년 1월 31일 23:59:00Z" in rule["condition"] for rule in wire["rules"]))
        expected = int(datetime(2027, 1, 31, 23, 59, tzinfo=timezone.utc).timestamp()) * 1000
        self.assertEqual(result.specification.close_at_ms, expected)
        raw = next(json.loads(item.body) for item in result.artifacts if item.kind == "ai-decision")
        self.assertIn("2027년", raw["output"]["canonical_question"])
        normalized = next(json.loads(item.body) for item in result.artifacts if item.kind == "compiler-normalization")
        self.assertIn("2027-01-31T23:59:00Z", normalized["normalized_specification"]["canonical_question"])

    async def test_conflicting_korean_z_timestamp_in_any_rule_rejected(self):
        for index in range(3):
            outputs = compile_outputs()
            outputs[0]["rules"][index]["condition"] += " 다른 마감: 1970년 1월 2일 00:06:40Z."
            transport = Transport(outputs)
            with self.subTest(rule=index), self.assertRaises(AIRejected) as caught:
                await coordinator(transport).compile_question("Will Apple announce X before the deadline?", (), NOW)
            self.assertEqual(caught.exception.code, "compiler_deadline_mismatch")
            self.assertEqual(transport.source_calls, [])

    async def test_invalid_model_raw_output_retained_with_safe_code(self):
        transport = Transport([{"approved": True}])
        with self.assertRaises(AIRejected) as caught:
            await coordinator(transport).compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertEqual(caught.exception.code, "ai_output_fields")
        record = json.loads(caught.exception.artifacts[0].body)
        self.assertIn('"approved": true', record["raw_output"])
        self.assertEqual(record["reason_code"], "ai_output_fields")
        self.assertNotIn("test-key", caught.exception.artifacts[0].body)
        self.assertNotIn("headers", record)

    async def test_compiler_cannot_change_explicit_user_utc_deadline_even_with_consistent_echo(self):
        outputs = compile_outputs()
        outputs[0]["open_at_ms"] = 1790000000000
        outputs[0]["close_at_utc"] = "2027-01-02T23:59:00Z"
        outputs[0]["canonical_question"] = "Will Apple announce Product X by 2027-01-02T23:59:00Z?"
        for rule in outputs[0]["rules"]:
            rule["condition"] = rule["condition"].replace("1970-01-01T00:06:40Z", "2027-01-02T23:59:00Z")
        transport = Transport(outputs)
        with self.assertRaises(AIRejected) as caught:
            await coordinator(transport).compile_question(
                "Apple이 2026년12월31일23:59 UTC까지 Product X를 발표할까요?", (), 1790000000000)
        self.assertEqual(caught.exception.code, "compiler_deadline_mismatch")
        self.assertEqual(transport.source_calls, [])

    async def test_korean_input_produces_english_preview_and_retains_exact_original_without_extra_calls(self):
        original = "  애플이 2026년 12월 31일 23:59 UTC까지 Product X를 발표할까요?  "
        now = 1790000000000
        outputs = compile_outputs()
        wire = outputs[0]
        wire["open_at_ms"] = now
        wire["close_at_utc"] = "2026-12-31T23:59:00Z"
        wire["canonical_question"] = "Will Apple announce Product X by 2026-12-31T23:59:00Z?"
        for rule in wire["rules"]:
            rule["condition"] = rule["condition"].replace("1970-01-01T00:06:40Z", wire["close_at_utc"])
        transport = Transport(outputs)
        result = await coordinator(transport).compile_question(original, (), now)
        self.assertEqual(result.specification.canonical_question, wire["canonical_question"])
        self.assertEqual(result.specification.close_at_ms,
                         int(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc).timestamp()) * 1000)
        self.assertEqual(len(transport.calls), 4)
        records = [json.loads(item.body) for item in result.artifacts if item.kind == "ai-decision"]
        self.assertEqual(records[0]["input"]["question"], original)
        self.assertEqual(records[1]["input"]["original_question"], original)
        self.assertEqual(records[0]["input"]["output_language"], "en")
        system = transport.calls[0]["body"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("in English regardless of the input language", system)
        self.assertIn("without adding or", system)
        self.assertNotIn("non-monetary forecasting service", system)
        self.assertIn("Never introduce purchasable or transferable", system)
        self.assertIn("shown to the user for review before publication", records[0]["input"]["policy"])
        prediction = records[-1]["input"]
        expected_utc = datetime.fromtimestamp(now // 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(prediction["as_of_utc"], expected_utc)
        self.assertEqual(prediction["as_of_ms"], now)
        self.assertIn("prior or historical knowledge", prediction["policy"])
        self.assertIn("unless the retained source documents support it", prediction["policy"])

    async def test_english_sentence_can_preserve_quoted_korean_or_japanese_proper_name(self):
        for name in ("새봄", "未来"):
            outputs = compile_outputs()
            wire = outputs[0]
            wire["canonical_question"] = f'Will Apple announce product "{name}" by 1970-01-01T00:06:40Z?'
            wire["share_title"] = f'Will Apple announce "{name}"?'
            for rule in wire["rules"]:
                rule["condition"] = rule["condition"].replace("Product X", f'product "{name}"')
            result = await coordinator(Transport(outputs)).compile_question(
                f'Will Apple announce product "{name}" before the stated deadline?', (), NOW)
            result.assessment.require_publishable(result.specification)
            self.assertIn(f'"{name}"', result.specification.canonical_question)
            self.assertIn(f'"{name}"', result.specification.share_title)

    async def test_untranslated_korean_public_prose_is_blocked_before_publication(self):
        for field in ("canonical_question", "share_title", "invalidation_rules"):
            outputs = compile_outputs()
            if field == "canonical_question":
                outputs[0][field] = "애플이 1970-01-01T00:06:40Z까지 새 제품을 발표할까요?"
            elif field == "share_title":
                outputs[0][field] = "애플이 새 제품을 발표할까요?"
            else:
                outputs[0][field] = ["필요한 공식 자료가 없으면 무효입니다."]
            transport = Transport(outputs)
            with self.subTest(field=field), self.assertRaises(AIRejected) as caught:
                await coordinator(transport).compile_question("애플이 마감 시각까지 Product X를 발표할까요?", (), NOW)
            self.assertEqual(caught.exception.code, "ai_output_language")
            self.assertNotRegex(str(caught.exception), r"[가-힣]")
            self.assertEqual(transport.source_calls, [])

    async def test_untranslated_optional_rationale_does_not_leak_into_public_prediction(self):
        outputs = compile_outputs()
        outputs[-1]["rationale"] = "현재 자료를 보면 발표할 가능성이 높습니다."
        result = await coordinator(Transport(outputs)).compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertIsNone(result.ai_forecast)
        rejections = [json.loads(item.body) for item in result.artifacts if item.kind == "ai-rejection"]
        self.assertEqual(rejections[0]["reason_code"], "ai_output_language")

    async def test_temporal_share_titles_rejected_without_changing_deadline_or_spending_on_more_calls(self):
        for title in (
            "Apple to announce OLED iPad mini by end of 2027?",
            "Will Apple launch next year?", "Will Apple launch before July?",
            "Will Apple launch in 2027?", "Will Apple launch by Q2?",
            "Will Apple launch on 2027-06-30?", "Will Apple launch by year's end?",
            "Will Apple launch within 12 months?", "Will Apple launch soon?",
        ):
            outputs = compile_outputs()
            outputs[0]["share_title"] = title
            original_close = outputs[0]["close_at_utc"]
            transport = Transport(outputs)
            with self.subTest(title=title), self.assertRaises(AIRejected) as caught:
                await coordinator(transport).compile_question("Will Apple announce X before the deadline?", (), NOW)
            self.assertEqual(caught.exception.code, "compiler_title_deadline")
            self.assertEqual({item.kind for item in caught.exception.artifacts}, {"ai-decision", "compiler-normalization"})
            self.assertEqual(outputs[0]["close_at_utc"], original_close)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.source_calls, [])

    async def test_timeless_titles_preserve_valid_product_numbers_and_thresholds(self):
        for title in ("Will Apple announce an M6 Mac?", "Will NVIDIA launch RTX 5090?",
                      "Will Apple announce iPhone 17?", "Will model 2027 launch?",
                      "Will Product X outperform Product Y by 10%?"):
            outputs = compile_outputs()
            outputs[0]["share_title"] = title
            result = await coordinator(Transport(outputs)).compile_question("Will Apple announce X before the deadline?", (), NOW)
            self.assertEqual(result.specification.share_title, title)
            self.assertEqual(result.specification.close_at_ms, 400000)

    async def test_semantically_wrong_timeless_title_still_requires_existing_judge_approval(self):
        outputs = compile_outputs()
        outputs[0]["share_title"] = "Will Apple announce an unrelated product?"
        outputs[1]["title_consistent"] = False
        outputs[1]["explanation"] = "The title changes the requested product identity."
        transport = Transport(outputs)
        with self.assertRaises(AIRejected) as caught:
            await coordinator(transport).compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertEqual(caught.exception.code, "compiler_not_publishable")
        self.assertEqual(len(transport.calls), 3)
        payload = json.loads(transport.calls[1]["body"]["contents"][0]["parts"][0]["text"])
        self.assertIn("set title_consistent=false", payload["policy"])

    async def test_source_failure_preserves_compiler_and_actionable_failure_artifacts(self):
        transport = Transport(compile_outputs())
        async def missing(url, method, headers):
            return TextResponse(404, "missing", {"content-type": "text/html"})
        ai = AiCoordinator((ProviderConfig("gemini", "model", "test-key"),), transport.json, missing)
        with self.assertRaises(AIRejected) as caught:
            await ai.compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertEqual(caught.exception.code, "source_rejected")
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertEqual({item.kind for item in caught.exception.artifacts},
                         {"ai-decision", "compiler-normalization", "source-failure"})

    async def test_openai_structured_transport(self):
        transport = Transport(compile_outputs())
        ai = coordinator(transport, providers=(ProviderConfig("openai", "explicit-model", "test-key"),))
        result = await ai.compile_question("Will Apple announce Product X before 2027?", (), NOW)
        self.assertEqual(result.assessment.compiler.provider, "openai")
        request = transport.calls[0]
        self.assertFalse(request["body"]["store"])
        self.assertTrue(request["body"]["text"]["format"]["strict"])

    async def test_gemini_flash_thinking_is_bounded_without_applying_flash_controls_to_pro(self):
        for name, thinking in (("gemini-2.5-flash", {"thinkingBudget": 512}),
                               ("gemini-2.5-pro", None), ("gemini-3.1-pro-preview", None)):
            transport = Transport(compile_outputs())
            ai = coordinator(transport, providers=(ProviderConfig("gemini", name, "test-key"),))
            await ai.compile_question("Will Apple announce Product X before the deadline?", (), NOW)
            compiler_config = transport.calls[0]["body"]["generationConfig"]
            self.assertEqual(compiler_config["maxOutputTokens"], 8192)
            self.assertEqual(compiler_config.get("thinkingConfig"), thinking)
            self.assertEqual(transport.calls[1]["body"]["generationConfig"]["maxOutputTokens"], 4096)

    async def test_valid_primary_does_not_fetch_optional_unauthorized_fallback(self):
        outputs = compile_outputs()
        outputs[0]["source_policy"]["fallback_sources"] = [to_dict(Source(source_id="reuters",
            name="Reuters", url="https://www.reuters.com/technology/", is_official=False))]
        transport = Transport(outputs)
        calls = []
        async def source(url, method, headers):
            calls.append(url)
            if "reuters" in url:
                return TextResponse(401, "Unauthorized", {"content-type": "text/plain"})
            return TextResponse(200, BODY, {"content-type": "text/html"})
        ai = AiCoordinator((ProviderConfig("gemini", "model", "test-key"),), transport.json, source)
        result = await ai.compile_question("Will Apple announce X before the deadline?", (), NOW)
        result.assessment.require_publishable(result.specification)
        self.assertEqual(calls, ["https://www.apple.com/newsroom/"])
        context = next(json.loads(item.body) for item in result.artifacts if item.kind == "source-collection")
        self.assertFalse(context["used_fallback"])
        self.assertEqual(context["unfetched_fallback_source_ids"], ["reuters"])

    async def test_failed_primary_uses_available_fallback_and_retains_failure_context(self):
        outputs = compile_outputs()
        outputs[0]["source_policy"]["fallback_sources"] = [to_dict(Source(source_id="ap",
            name="AP", url="https://apnews.com/technology", is_official=False))]
        transport = Transport(outputs)
        calls = []
        async def source(url, method, headers):
            calls.append(url)
            return (TextResponse(404, "Missing", {"content-type": "text/plain"}) if "apple" in url
                    else TextResponse(200, BODY, {"content-type": "text/html"}))
        ai = AiCoordinator((ProviderConfig("gemini", "model", "test-key"),), transport.json, source)
        result = await ai.compile_question("Will Apple announce X before the deadline?", (), NOW)
        result.assessment.require_publishable(result.specification)
        self.assertEqual(calls, ["https://www.apple.com/newsroom/", "https://apnews.com/technology"])
        self.assertTrue(any(item.kind == "source-failure" for item in result.artifacts))
        payload = json.loads(transport.calls[1]["body"]["contents"][0]["parts"][0]["text"])
        self.assertTrue(payload["source_collection"]["used_fallback"])
        self.assertEqual(payload["source_collection"]["unavailable_sources"][0]["source_id"], "apple-news")
        self.assertEqual(payload["source_documents"][0]["url"], "https://apnews.com/technology")

    async def test_all_primary_and_fallback_failures_block_and_retain_each_failure(self):
        outputs = compile_outputs()
        outputs[0]["source_policy"]["fallback_sources"] = [to_dict(Source(source_id="reuters",
            name="Reuters", url="https://www.reuters.com/technology/", is_official=False))]
        transport = Transport(outputs)
        async def source(url, method, headers):
            return TextResponse(404 if "apple" in url else 401, "Missing", {"content-type": "text/plain"})
        ai = AiCoordinator((ProviderConfig("gemini", "model", "test-key"),), transport.json, source)
        with self.assertRaises(AIRejected) as caught:
            await ai.compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertEqual(caught.exception.code, "source_rejected")
        failures = [json.loads(item.body) for item in caught.exception.artifacts if item.kind == "source-failure"]
        self.assertEqual({item["source_id"] for item in failures}, {"apple-news", "reuters"})
        self.assertEqual(len(transport.calls), 1)

    async def test_missing_required_source_remains_visible_and_unresolved_blocks_proposal(self):
        spec = specification()
        spec = replace(spec, source_policy=SourcePolicy(primary_sources=(
            *spec.source_policy.primary_sources,
            Source(source_id="required-second", name="Second official page",
                   url="https://www.apple.com/newsroom/required-proof/", is_official=True))))
        forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai", specification=spec, now_ms=NOW)
        outputs = resolution_outputs()
        outputs[1].update(proposed_outcome="NO", rule_matches=["no-rule"], conflict_status="UNRESOLVED",
                          conflict_explanation="The required archive is unavailable, so absence is not proved.")
        transport = Transport(outputs)
        async def source(url, method, headers):
            return (TextResponse(404, "Missing", {"content-type": "text/plain"}) if "required-proof" in url
                    else TextResponse(200, BODY, {"content-type": "text/html"}))
        ai = AiCoordinator((ProviderConfig("gemini", "model", "test-key"),), transport.json, source)
        with self.assertRaises(AIRejected):
            await ai.propose_resolution(forecast, 400000)
        payload = json.loads(transport.calls[1]["body"]["contents"][0]["parts"][0]["text"])
        self.assertEqual(payload["source_collection"]["unavailable_sources"][0]["source_id"], "required-second")
        self.assertIn("UNRESOLVED for missing coverage", payload["policy"])
        self.assertIsNone(forecast.resolution)

    async def test_optional_prediction_failure_leaves_null_without_losing_validation(self):
        for last_output in (RuntimeError("provider down"), {"yesProbabilityBp": 12000, "rationale": "invalid"}):
            outputs = compile_outputs()
            outputs[-1] = last_output
            result = await coordinator(Transport(outputs)).compile_question("Will Apple announce X before 2027?", (), NOW)
            self.assertIsNone(result.ai_forecast)
            result.assessment.require_publishable(result.specification)
            self.assertFalse(any(item.kind == "ai-forecast" for item in result.artifacts))

    async def test_cloudflare_model_is_explicit_and_version_unreported(self):
        transport = Transport(compile_outputs())
        ai = coordinator(transport, providers=(ProviderConfig("cloudflare", "@cf/vendor/model"),))
        result = await ai.compile_question("Will Apple announce Product X before 2027?", (), NOW)
        self.assertEqual(result.assessment.compiler.model_version, "unreported:@cf/vendor/model")
        self.assertEqual(transport.calls[0]["url"], "workers-ai://@cf/vendor/model")

    async def test_provider_outage_raises_no_hidden_result(self):
        ai = coordinator(Transport([RuntimeError("secret provider error text")]))
        with self.assertRaises(AIUnavailable) as caught:
            await ai.compile_question("Will Apple announce Product X before 2027?", (), NOW)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(caught.exception.unavailable_providers, ("gemini",))
        failure = json.loads(caught.exception.artifacts[0].body)
        self.assertEqual(failure["exception_type"], "RuntimeError")
        self.assertEqual(failure["failure_category"], "transport")
        self.assertIsNone(failure["http_status"])
        self.assertNotIn("secret", caught.exception.artifacts[0].body)
        self.assertNotIn("test-key", caught.exception.artifacts[0].body)

    async def test_outage_fallback_records_actual_provider(self):
        transport = Transport([OSError("outage"), *compile_outputs()])
        ai = coordinator(transport, providers=(ProviderConfig("openai", "model", "key"),
                                              ProviderConfig("gemini", "model", "key")))
        result = await ai.compile_question("Will Apple announce Product X before 2027?", (), NOW)
        self.assertEqual(result.assessment.compiler.provider, "gemini")
        compiler_record = next(json.loads(item.body) for item in result.artifacts if item.kind == "ai-decision")
        self.assertEqual(compiler_record["preceding_provider_failures"][0]["provider"], "openai")
        self.assertEqual(compiler_record["preceding_provider_failures"][0]["exception_type"], "OSError")

    async def test_all_provider_failures_retain_only_safe_typed_status_and_actual_attempts(self):
        class ProviderHTTPError(RuntimeError):
            def __init__(self, status):
                super().__init__("Authorization: Bearer PRIVATE-KEY; body=PRIVATE-RESPONSE")
                self.http_status = status
        transport = Transport([ProviderHTTPError(429), ProviderHTTPError("401")])
        ai = coordinator(transport, providers=(ProviderConfig("gemini", "model", "PRIVATE-KEY"),
                                              ProviderConfig("cloudflare", "@cf/model")))
        with self.assertRaises(AIUnavailable) as caught:
            await ai.compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertEqual(caught.exception.unavailable_providers, ("gemini", "cloudflare"))
        self.assertEqual(len(transport.calls), 2)
        failures = [json.loads(item.body) for item in caught.exception.artifacts]
        self.assertEqual([item["provider"] for item in failures], ["gemini", "cloudflare"])
        self.assertEqual([item["http_status"] for item in failures], [429, None])
        for artifact in caught.exception.artifacts:
            self.assertLess(len(artifact.body.encode("utf-8")), 1000)
            self.assertNotIn("PRIVATE-", artifact.body)
            self.assertNotIn("Authorization", artifact.body)
            self.assertEqual(content_hash(json.loads(artifact.body)), artifact.content_hash)

    async def test_semantic_rejection_after_provider_failure_keeps_failure_accounting(self):
        transport = Transport([RuntimeError("sensitive error"), {"invalid": True}])
        ai = coordinator(transport, providers=(ProviderConfig("gemini", "model", "test-key"),
                                              ProviderConfig("cloudflare", "@cf/model")))
        with self.assertRaises(AIRejected) as caught:
            await ai.compile_question("Will Apple announce X before the deadline?", (), NOW)
        self.assertEqual([item.kind for item in caught.exception.artifacts], ["provider-failure", "ai-rejection"])
        self.assertEqual(json.loads(caught.exception.artifacts[0].body)["provider"], "gemini")
        self.assertNotIn("sensitive error", caught.exception.artifacts[0].body)

    async def test_malformed_missing_extra_or_float_output_rejected(self):
        for altered in ({"approved": True}, {**compiler_wire(), "unknown": True},
                        {**compiler_wire(), "ambiguity_score_bp": 0.1}):
            with self.subTest(altered=altered), self.assertRaises(AIRejected):
                await coordinator(Transport([altered])).compile_question("Will Apple announce X before 2027?", (), NOW)

    async def test_ambiguity_or_source_assessment_failure_blocks_publication(self):
        for field in ("objectively_resolvable", "ambiguity_passed", "sources_appropriate",
                      "english_language_passed", "intent_preserved", "title_consistent"):
            outputs = compile_outputs()
            outputs[1][field] = False
            with self.subTest(field=field), self.assertRaises(AIRejected) as caught:
                await coordinator(Transport(outputs)).compile_question("Will Apple announce X before 2027?", (), NOW)
            self.assertTrue(caught.exception.artifacts)

    async def test_duplicate_omission_rejected(self):
        outputs = compile_outputs()
        outputs[2]["candidates_accurate"] = False
        with self.assertRaises(AIRejected):
            await coordinator(Transport(outputs)).compile_question("Will Apple announce X before 2027?", (), NOW)

    async def test_model_cannot_invent_duplicate_identity(self):
        outputs = compile_outputs()
        outputs[0]["duplicate_candidates"] = [{"schema_version": 1, "forecast_id": "invented",
            "specification_hash": "a" * 64, "similarity_bp": 9000,
            "materially_different_rules": True, "explanation": "Invented comparison."}]
        with self.assertRaises(AIRejected):
            await coordinator(Transport(outputs)).compile_question("Will Apple announce X before 2027?", (), NOW)

    async def test_past_or_invented_open_time_rejected(self):
        for field, value in (("open_at_ms", 999), ("close_at_utc", "1970-01-01T00:00:02Z")):
            outputs = compile_outputs()
            outputs[0][field] = value
            with self.subTest(field=field), self.assertRaises(AIRejected):
                await coordinator(Transport(outputs)).compile_question("Will Apple announce X before 2027?", (), NOW)

    async def test_candidate_count_and_utf8_budget_rejected_before_provider_cost(self):
        for count, padding in ((MAX_CANDIDATES + 1, 0), (MAX_CANDIDATES, 2000)):
            candidates = [create_forecast(forecast_id=f"candidate-{i}", creator_id="creator-ai",
                specification=model.specification(canonical_question="candidate " + "가" * padding),
                now_ms=0) for i in range(count)]
            transport = Transport([])
            with self.subTest(count=count, padding=padding), self.assertRaises(AIRejected):
                await coordinator(transport).compile_question("Will Apple announce X before 2027?", candidates, NOW)
            self.assertEqual(transport.calls, [])
            self.assertEqual(transport.source_calls, [])

    async def test_bounded_candidate_context_keeps_retained_decisions_below_storage_limit(self):
        candidates = [create_forecast(forecast_id=f"candidate-{i}", creator_id="creator-ai",
            specification=model.specification(canonical_question=f"Candidate {i}: " + "topic " * 200),
            now_ms=0) for i in range(MAX_CANDIDATES)]
        transport = Transport(compile_outputs())
        result = await coordinator(transport).compile_question("Will Apple announce X before 2027?", candidates, NOW)
        request = json.loads(transport.calls[0]["body"]["contents"][0]["parts"][0]["text"])
        self.assertEqual(len(request["candidates"]), MAX_CANDIDATES)
        self.assertLessEqual(len(canonical_bytes(request["candidates"])), MAX_CANDIDATE_CONTEXT_BYTES)
        for artifact in result.artifacts:
            self.assertLessEqual(len(artifact.body.encode("utf-8")), MAX_DECISION_ARTIFACT_BYTES)

    async def test_model_private_source_rejected_before_fetch(self):
        outputs = compile_outputs()
        outputs[0]["source_policy"]["primary_sources"][0]["url"] = "https://127.0.0.1/secret"
        transport = Transport(outputs)
        with self.assertRaises(AIRejected):
            await coordinator(transport).compile_question("Will Apple announce X before 2027?", (), NOW)
        self.assertEqual(transport.source_calls, [])

    async def test_real_resolution_passes_lifecycle_and_roundtrip(self):
        forecast = await challenge_fixture()
        self.assertEqual(forecast.resolution.judge.provider, "gemini")
        self.assertEqual(forecast.resolution.counter_judge.provider, "gemini")
        self.assertEqual(loads(type(forecast), dumps(forecast)), forecast)

    async def test_counterjudge_disagreement_never_becomes_proposal(self):
        forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai",
                                  specification=specification(), now_ms=NOW)
        with self.assertRaises(AIRejected) as caught:
            await coordinator(Transport(resolution_outputs(agrees=False))).propose_resolution(forecast, 400000)
        self.assertTrue(caught.exception.artifacts)
        self.assertIsNone(forecast.resolution)

    async def test_low_confidence_or_unresolved_evidence_blocks(self):
        for field, value in (("confidence_bp", 7000), ("conflict_status", "UNRESOLVED")):
            outputs = resolution_outputs()
            outputs[1][field] = value
            forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai",
                                      specification=specification(), now_ms=NOW)
            with self.subTest(field=field), self.assertRaises(AIRejected):
                await coordinator(Transport(outputs)).propose_resolution(forecast, 400000)

    async def test_invented_rule_reference_fails_domain_guard(self):
        outputs = resolution_outputs()
        outputs[1]["rule_matches"] = ["invented-rule"]
        forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai",
                                  specification=specification(), now_ms=NOW)
        with self.assertRaises(AIRejected):
            await coordinator(Transport(outputs)).propose_resolution(forecast, 400000)

    async def test_resolution_before_expiry_makes_no_calls(self):
        transport = Transport([])
        forecast = create_forecast(forecast_id="forecast-ai", creator_id="creator-ai",
                                  specification=specification(), now_ms=NOW)
        with self.assertRaises(AIRejected):
            await coordinator(transport).propose_resolution(forecast, NOW)
        self.assertEqual(transport.calls, [])
        self.assertEqual(transport.source_calls, [])

    async def _dispute(self):
        forecast = await challenge_fixture()
        transport = Transport([])
        transport.source_body = "Apple Newsroom correction: the announced product is Product Y, not Product X. " * 3
        ai = coordinator(transport)
        snapshots, artifacts = await ai.collect_dispute_evidence(forecast.specification,
            "https://www.apple.com/newsroom/correction/", 401000)
        dispute = Dispute(dispute_id="dispute-ai", disputant_id="user-ai", forecast_id=forecast.forecast_id,
            specification_hash=forecast.specification_hash, resolution_hash=forecast.resolution.resolution_hash,
            claim="Product name differs.", evidence=snapshots, rule_clause_id="yes-rule",
            explanation="The referenced exact name may identify another product.", submitted_at_ms=401000)
        original = Artifact(hashlib.sha256(BODY.encode()).hexdigest(), "source", BODY, "text/html")
        return forecast, dispute, (*artifacts, original)

    async def test_dispute_requires_independent_provider(self):
        forecast, dispute, _ = await self._dispute()
        with self.assertRaises(AIUnavailable):
            await coordinator(Transport([])).review_dispute(forecast, dispute, 402000)

    async def test_dispute_review_is_independent_and_uses_retained_bytes(self):
        forecast, dispute, artifacts = await self._dispute()
        async def reader(digest):
            return next((item.body for item in artifacts if item.content_hash == digest), None)
        transport = Transport([{"evidence_validated": True, "explanation": "Usable retained counterevidence."},
            {"material_conflict": True, "reason_summary": "Exact product identity is materially disputed."},
            {"agrees": True, "explanation": "Independent provider agrees with material conflict."}])
        ai = coordinator(transport, providers=(ProviderConfig("gemini", "model", "key"),
            ProviderConfig("cloudflare", "@cf/model")), read_artifact=reader)
        result = await ai.review_dispute(forecast, dispute, 402000)
        result.review.require_valid_for(dispute, forecast.resolution, forecast.specification)
        self.assertEqual(result.review.disposition, ReviewDisposition.MATERIAL_CONFLICT)
        self.assertEqual(result.review.independent_judge.provider, "cloudflare")
        self.assertEqual(result.review.counter_analysis.provider, "gemini")
        self.assertEqual(transport.source_calls, [])
        for call in transport.calls:
            request = call["body"]
            raw = (request["contents"][0]["parts"][0]["text"] if "contents" in request
                   else request["messages"][-1]["content"])
            payload = json.loads(raw)
            self.assertIn("Official Product X announcement", payload["original_resolution_evidence"][0]["retained_text"])
            self.assertIn("Product Y, not Product X", payload["submitted_evidence"][0]["retained_text"])

    async def test_missing_or_corrupted_original_resolution_evidence_blocks_dispute_review(self):
        forecast, dispute, artifacts = await self._dispute()
        original_digest = forecast.resolution.evidence[0].content_sha256
        for original in (None, "changed original evidence"):
            async def reader(digest):
                if digest == original_digest:
                    return original
                return next((item.body for item in artifacts if item.content_hash == digest), None)
            transport = Transport([])
            ai = coordinator(transport, providers=(ProviderConfig("gemini", "model", "key"),
                ProviderConfig("cloudflare", "@cf/model")), read_artifact=reader)
            with self.subTest(original=original), self.assertRaises(AIUnavailable):
                await ai.review_dispute(forecast, dispute, 402000)
            self.assertEqual(transport.calls, [])
            self.assertEqual(transport.source_calls, [])

    async def test_corrupted_dispute_bytes_never_refetched_or_judged(self):
        forecast, dispute, _ = await self._dispute()
        async def reader(digest):
            return "changed source body"
        transport = Transport([])
        ai = coordinator(transport, providers=(ProviderConfig("gemini", "model", "key"),
            ProviderConfig("cloudflare", "@cf/model")), read_artifact=reader)
        with self.assertRaises(AIUnavailable):
            await ai.review_dispute(forecast, dispute, 402000)
        self.assertEqual(transport.calls, [])
        self.assertEqual(transport.source_calls, [])

    async def test_timeout_has_no_fake_fallback(self):
        async def wait(*args):
            import asyncio
            await asyncio.sleep(1)
        ai = AiCoordinator((ProviderConfig("gemini", "model", "key"),), wait, Transport([]).text,
                           timeout_seconds=0.001)
        with self.assertRaises(AIUnavailable):
            await ai.compile_question("Will Apple announce Product X before 2027?", (), NOW)


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def test_excerpt_limit_counts_utf8_bytes_and_preserves_retained_source(self):
        body = "<p>" + "한글근거 " * 10000 + "</p>"
        excerpt = evidence_excerpt(body)
        self.assertLessEqual(len(excerpt.encode("utf-8")), MAX_EXCERPT_BYTES)
        self.assertTrue(excerpt.startswith("한글근거"))
        self.assertNotIn("\ufffd", excerpt)

    def test_urls_are_exact_registered_public_https(self):
        for url in ("https://127.0.0.1/a", "http://www.apple.com/a", "https://www.apple.com:8443/a",
                    "https://www.apple.com.evil.com/a", "https://user@www.apple.com/a",
                    "https://www.apple.com./a", "https://www.apple.com/a#fragment",
                    "https://www.apple.com\\@evil.com/a", "https://[::1]/a",
                    "https://www.apple.com\n/a", "https://internal.local/a"):
            with self.subTest(url=url), self.assertRaises(SourceRejected):
                validate_public_url(url)
        self.assertEqual(validate_public_url("https://www.apple.com/newsroom/"), "www.apple.com")

    async def test_redirect_revalidated_before_second_fetch(self):
        calls = []
        async def transport(url, method, headers):
            calls.append(url)
            return TextResponse(302, "", {"Location": "https://127.0.0.1/secrets"})
        with self.assertRaises(SourceRejected):
            await SourceCollector(transport).collect(specification().source_policy.sources[0], NOW)
        self.assertEqual(len(calls), 1)

    async def test_same_host_redirect_retains_final_url_and_raw_body(self):
        calls = []
        async def transport(url, method, headers):
            calls.append(url)
            return (TextResponse(302, "", {"Location": "/newsroom/announcement/"}) if len(calls) == 1
                    else TextResponse(200, BODY, {"Content-Type": "text/html"}))
        result = await SourceCollector(transport).collect(specification().source_policy.sources[0], NOW)
        self.assertEqual(result.snapshot.url, "https://www.apple.com/newsroom/announcement/")
        self.assertEqual(result.artifact.body, BODY)
        self.assertEqual(result.snapshot.content_sha256, hashlib.sha256(BODY.encode()).hexdigest())

    async def test_deleted_empty_oversize_binary_or_unreadable_rejected(self):
        for response in (TextResponse(404, "gone", {}), TextResponse(200, "", {"content-type": "text/html"}),
            TextResponse(200, "x" * (MAX_SOURCE_BYTES + 1), {"content-type": "text/plain"}),
            TextResponse(200, BODY, {"content-type": "application/pdf"}),
            TextResponse(200, "<script>" + "x" * 200 + "</script>", {"content-type": "text/html"})):
            async def transport(url, method, headers):
                return response
            with self.subTest(status=response.status), self.assertRaises(SourceRejected):
                await SourceCollector(transport).collect(specification().source_policy.sources[0], NOW)

    async def test_source_outage_is_retryable(self):
        async def transport(url, method, headers):
            return TextResponse(503, "unavailable", {})
        with self.assertRaises(SourceUnavailable):
            await SourceCollector(transport).collect(specification().source_policy.sources[0], NOW)


if __name__ == "__main__":
    unittest.main()
