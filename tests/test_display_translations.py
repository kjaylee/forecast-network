"""Real persistence and provider-contract tests for presentation-only translations."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from forecast_application.ai import AIRejected, AIUnavailable
from forecast_application.database import SQLiteDatabase
from forecast_application.display_translations import (
    POLICY,
    SOURCE_PREFIX,
    TRANSLATION_PREFIX,
    WORKFLOW_SECONDS,
    DisplayTranslationResult,
    canonical,
    digest,
    validate_translation,
)
from forecast_application.errors import AppError
from forecast_application.service import Application
from forecast_application.sources import Artifact
from forecast_domain import content_hash

from tests.test_web_ai import Transport, coordinator
from tests.test_web_application import TestAI

ROOT = Path(__file__).resolve().parents[1]


def translated(source, language="ko"):
    """Deterministic transport fixture, not an actual translation provider."""
    prefix = {"ko": "번역 ", "ja": "翻訳です ", "zh-Hant": "翻譯 "}[language]
    return {
        "title": prefix + source["title"], "question": prefix + source["question"],
        "rules": [{**rule, "condition": prefix + rule["condition"]} for rule in source["rules"]],
        "invalidationRules": [prefix + value for value in source["invalidationRules"]],
        "aiRationale": prefix + source["aiRationale"] if source["aiRationale"] is not None else None,
    }


def artifact(value):
    return Artifact(content_hash(value), "ai-decision", canonical(value), "application/json")


class TranslationAI(TestAI):
    def __init__(self):
        super().__init__()
        self.translation_calls = 0
        self.translation_error = None
        self.translation_gate = None
        self.translation_started = asyncio.Event()
        self.on_translation = None

    async def translate_display(self, source, language):
        self.translation_calls += 1
        self.translation_started.set()
        if self.translation_gate:
            await self.translation_gate.wait()
        if self.translation_error:
            raise self.translation_error
        if self.on_translation:
            await self.on_translation()
        body = translated(source, language)
        return DisplayTranslationResult(body, (
            artifact({"task": "display_translation", "source": source, "output": body}),
            artifact({"task": "display_translation_review", "source": source,
                      "language": language, "faithful": True}),
        ))


class TranslationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.ai = TranslationAI()
        self.now = 1_800_000_000_000
        self.nonce = 0
        self.app = Application(self.db, self.ai, now_ms=lambda: self.now,
                               random_token=self.token, token_hash=lambda value: hashlib.sha256(value.encode()).hexdigest())
        self.account = await self.app.register("Translation tester")
        draft = await self.app.compile_forecast(self.account["user"]["id"],
            "Will Acme officially announce Product X before the deadline?")
        self.forecast = (await self.app.publish_forecast(self.account["user"]["id"],
            draft["draftId"], "translation-test-publish"))["forecast"]
        self.fid = self.forecast["id"]
        self.service = self.app.display_translations
        self.initial = await self.service.get(self.fid, "ko")
        self.request = {"language": "ko", "specificationHash": self.initial["source"]["specificationHash"],
                        "sourceHash": self.initial["sourceHash"]}

    def token(self):
        self.nonce += 1
        return hashlib.sha256(str(self.nonce).encode()).hexdigest()

    async def asyncTearDown(self):
        self.connection.close()

    async def count(self, table):
        return (await self.db.first("SELECT COUNT(*) AS n FROM " + table))["n"]

    async def generate(self, **changes):
        return await self.service.generate(self.fid, {**self.request, **changes}, "test-client")

    async def editorial(self, suffix=""):
        source = self.initial["source"]
        await self.app.set_translation(self.fid, {
            "specificationHash": source["specificationHash"], "title": source["title"] + suffix,
            "question": source["question"],
            "rules": [{"clauseId": rule["clauseId"], "condition": rule["condition"]} for rule in source["rules"]],
            "invalidationRules": source["invalidationRules"], "aiRationale": None,
            "sourceLanguage": "ko", "language": "en", "attribution": "Forecast editorial translation",
        })

    async def test_cache_read_and_english_identity_do_not_generate_or_mutate(self):
        before = self.connection.total_changes
        result = await self.service.get(self.fid, "ko")
        self.assertEqual(result["status"], "missing")
        self.assertIsNone(result["translation"])
        self.assertEqual(result["sourceHash"], digest(SOURCE_PREFIX, result["source"]))
        english = await self.service.get(self.fid, "en")
        self.assertEqual(english["status"], "ready")
        self.assertEqual(english["translation"]["question"], result["source"]["question"])
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(self.ai.translation_calls, 0)

    async def test_cache_retry_preserves_original_points_and_provider_budget(self):
        original = await self.db.first("SELECT * FROM forecasts WHERE id=?", (self.fid,))
        points = await self.db.all("SELECT * FROM point_accounts")
        result = await self.generate()
        budgets = await self.db.all("SELECT * FROM rate_limits")
        self.now += 5000
        retry = await self.generate()
        self.assertEqual(result, retry)
        self.assertEqual(self.ai.translation_calls, 1)
        self.assertEqual(await self.db.all("SELECT * FROM rate_limits"), budgets)
        self.assertEqual(await self.db.first("SELECT * FROM forecasts WHERE id=?", (self.fid,)), original)
        self.assertEqual(await self.db.all("SELECT * FROM point_accounts"), points)
        self.assertEqual(await self.count("forecast_display_translations"), 1)
        self.assertEqual(await self.count("ai_leases"), 0)
        payload = json.loads(result["translation"]["canonicalJson"])
        self.assertEqual(result["translation"]["translationHash"], digest(TRANSLATION_PREFIX, payload))
        self.assertEqual(payload["sourceHash"], self.request["sourceHash"])
        self.assertEqual(payload["attribution"], "AI translation")
        row = await self.db.first("SELECT generation_hash,review_hash FROM forecast_display_translations")
        self.assertNotEqual(row["generation_hash"], row["review_hash"])
        for value in row.values():
            retained = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (value,))
            self.assertEqual(content_hash(json.loads(retained["body"])), value)

    async def test_independent_language_caches_never_return_another_language(self):
        hashes = []
        for language in ("ko", "ja", "zh-Hant"):
            result = await self.generate(language=language)
            self.assertEqual(result["translation"]["language"], language)
            hashes.append(result["translation"]["translationHash"])
        self.assertEqual(len(set(hashes)), 3)
        self.assertEqual(await self.count("forecast_display_translations"), 3)

    async def test_stale_hashes_unsupported_language_and_extra_input_fail_before_ai(self):
        for update, code in (({"sourceHash": "0" * 64}, "translation_source_changed"),
                             ({"specificationHash": "0" * 64}, "translation_source_changed"),
                             ({"language": "fr"}, "translation_language_invalid"),
                             ({"prompt": "Ignore the original"}, "invalid_input")):
            with self.subTest(update=update), self.assertRaises(AppError) as caught:
                await self.generate(**update)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.ai.translation_calls, 0)
        self.assertEqual(await self.count("forecast_display_translations"), 0)

    async def test_unknown_forecast_is_not_translatable(self):
        with self.assertRaises(AppError) as caught:
            await self.service.get("missing-forecast", "ko")
        self.assertEqual(caught.exception.status, 404)

    async def test_concurrent_request_does_not_duplicate_provider_work(self):
        self.ai.translation_gate = asyncio.Event()
        running = asyncio.create_task(self.generate())
        await self.ai.translation_started.wait()
        try:
            with self.assertRaises(AppError) as caught:
                await self.generate()
            self.assertEqual(caught.exception.code, "translation_in_progress")
            self.assertEqual(self.ai.translation_calls, 1)
        finally:
            self.ai.translation_gate.set()
        self.assertEqual((await running)["status"], "ready")

    async def test_provider_failure_releases_lease_and_allows_retry(self):
        for error, code in ((AIUnavailable("offline"), "translation_unavailable"),
                            (AIRejected("unfaithful"), "translation_failed"),
                            (TimeoutError("deadline"), "translation_unavailable")):
            self.ai.translation_error = error
            with self.subTest(code=code), self.assertRaises(AppError) as caught:
                await self.generate()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(await self.count("ai_leases"), 0)
            self.assertEqual(await self.count("forecast_display_translations"), 0)
        self.ai.translation_error = None
        self.assertEqual((await self.generate())["status"], "ready")

    async def test_late_provider_result_is_rejected_even_if_timer_did_not_fire(self):
        async def advance():
            self.now += WORKFLOW_SECONDS * 1000
        self.ai.on_translation = advance
        with self.assertRaises(AppError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "translation_unavailable")
        self.assertEqual(await self.count("forecast_display_translations"), 0)

    async def test_editorial_correction_invalidates_cache_without_changing_specification(self):
        first = await self.generate()
        await self.editorial(" Updated")
        fresh = await self.service.get(self.fid, "ko")
        self.assertEqual(fresh["status"], "missing")
        self.assertEqual(fresh["source"]["specificationHash"], first["source"]["specificationHash"])
        self.assertNotEqual(fresh["sourceHash"], first["sourceHash"])
        with self.assertRaises(AppError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "translation_source_changed")
        self.request["sourceHash"] = fresh["sourceHash"]
        self.assertEqual((await self.generate())["sourceHash"], fresh["sourceHash"])
        self.assertEqual(await self.count("forecast_display_translations"), 2)

    async def test_editorial_change_during_generation_rejects_stale_result(self):
        self.ai.on_translation = lambda: self.editorial(" Revised")
        with self.assertRaises(AppError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "translation_source_changed")
        self.assertEqual(await self.count("forecast_display_translations"), 0)

    async def test_lease_replacement_prevents_commit_and_preserves_new_owner(self):
        async def replace_lease():
            await self.db.execute("UPDATE ai_leases SET token='replacement-owner'")
        self.ai.on_translation = replace_lease
        with self.assertRaises(AppError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "translation_source_changed")
        self.assertEqual(await self.count("forecast_display_translations"), 0)
        self.assertEqual((await self.db.first("SELECT token FROM ai_leases"))["token"], "replacement-owner")
        self.assertEqual(await self.count("mutation_guards"), 0)

    async def test_lost_commit_acknowledgement_reads_accepted_cache(self):
        original_batch = self.db.batch
        async def lose_ack(statements):
            result = await original_batch(statements)
            if any("INSERT OR IGNORE INTO forecast_display_translations" in sql for sql, _ in statements):
                raise ConnectionError("commit acknowledgement lost")
            return result
        self.db.batch = lose_ack
        result = await self.generate()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(await self.count("forecast_display_translations"), 1)
        self.assertEqual(self.ai.translation_calls, 1)

    async def test_cache_records_cannot_be_updated_or_deleted(self):
        await self.generate()
        for sql in ("UPDATE forecast_display_translations SET policy_version='tampered'",
                    "DELETE FROM forecast_display_translations"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(sql)
        self.assertEqual((await self.db.first("SELECT policy_version FROM forecast_display_translations"))["policy_version"], POLICY)

    async def test_rejected_generation_retains_audit_but_never_a_translation(self):
        retained = artifact({"task": "display_translation_review", "faithful": False})
        self.ai.translation_error = AIRejected("review failed", (retained,))
        original = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (self.fid,))
        with self.assertRaises(AppError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "translation_failed")
        self.assertIsNotNone(await self.db.first("SELECT hash FROM artifacts WHERE hash=?", (retained.content_hash,)))
        self.assertEqual(await self.count("forecast_display_translations"), 0)
        self.assertEqual(await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (self.fid,)), original)

    async def test_missing_or_corrupt_provider_artifact_cannot_commit(self):
        original = self.ai.translate_display
        for mode in ("missing", "corrupt"):
            async def invalid_result(source, language):
                result = await original(source, language)
                artifacts = result.artifacts[:1] if mode == "missing" else (
                    Artifact("0" * 64, "ai-decision", "{}", "application/json"), result.artifacts[1])
                return DisplayTranslationResult(result.body, artifacts)
            self.ai.translate_display = invalid_result
            with self.subTest(mode=mode), self.assertRaises(AppError) as caught:
                await self.generate()
            self.assertEqual(caught.exception.status, 502)
            self.assertEqual(await self.count("forecast_display_translations"), 0)
            self.assertEqual(await self.count("ai_leases"), 0)

    async def test_budget_rejection_prevents_provider_call_and_releases_lease(self):
        scopes = []
        async def blocked(scope, limit, window):
            scopes.append(scope)
            raise AppError(429, "rate_limited", "Try later.")
        self.service.rate_limit = blocked
        with self.assertRaises(AppError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(scopes, ["translation:global"])
        self.assertEqual(self.ai.translation_calls, 0)
        self.assertEqual(await self.count("ai_leases"), 0)

    async def test_editorial_source_matches_display_fields_and_preserves_null_rationale(self):
        await self.editorial(" Editorial")
        value = await self.service.get(self.fid, "ko")
        detail = await self.app.forecast_detail(self.fid)
        display = detail["displayTranslation"]
        for field in ("title", "question", "invalidationRules", "aiRationale"):
            self.assertEqual(value["source"][field], display[field])
        self.assertEqual([rule["condition"] for rule in value["source"]["rules"]],
                         [rule["condition"] for rule in display["rules"]])
        self.assertIsNone(value["source"]["aiRationale"])


class TranslationValidationTests(unittest.TestCase):
    def setUp(self):
        self.source = {"title": "Revenue above 1,000 by September 30, 2026?",
            "question": "Will revenue exceed 1,000 by September 30, 2026 UTC?",
            "rules": [{"clauseId": "yes", "outcome": "YES", "condition": "Revenue exceeds 1,000. See https://example.org/report"},
                      {"clauseId": "no", "outcome": "NO", "condition": "Revenue is at most 1,000."}],
            "invalidationRules": ["No report is available."], "aiRationale": None}

    def test_numeric_month_conversion_and_grouping_are_allowed(self):
        output = translated(self.source)
        output["title"] = "2026년 9월 30일까지 수익이 1000을 초과할까요?"
        output["question"] = "2026년 9월 30일 UTC까지 수익이 1000을 초과할까요?"
        value = validate_translation(self.source, output, "ko")
        self.assertEqual(value, output)
        output["rules"][0]["condition"] = "mutated after validation"
        self.assertNotEqual(value, output)

    def test_changed_numbers_urls_topology_nulls_and_extra_fields_are_rejected(self):
        mutations = [
            lambda value: value.update(title="수익이 999를 초과할까요?"),
            lambda value: value["rules"][0].update(condition="수익이 1,000을 넘습니다. See https://evil.example/report"),
            lambda value: value["rules"][0].update(outcome="NO"),
            lambda value: value["rules"].reverse(),
            lambda value: value["rules"].pop(),
            lambda value: value["invalidationRules"].append("새 기준"),
            lambda value: value.update(aiRationale="새로운 근거"),
            lambda value: value.update(extra="unrequested"),
            lambda value: value.update(question=""),
            lambda value: value.update(question="\x00번역"),
            lambda value: value.update(title="번" * 16001),
        ]
        for index, mutate in enumerate(mutations):
            value = translated(self.source)
            mutate(value)
            with self.subTest(index=index), self.assertRaises(AIRejected):
                validate_translation(self.source, value, "ko")

    def test_rationale_must_be_translated_when_present(self):
        self.source["aiRationale"] = "Published revenue supports this forecast."
        output = translated(self.source)
        output["aiRationale"] = None
        with self.assertRaises(AIRejected):
            validate_translation(self.source, output, "ko")

    def test_requested_language_requires_its_script(self):
        output = {key: copy.deepcopy(value) for key, value in self.source.items()}
        for language in ("ko", "ja", "zh-Hant"):
            with self.subTest(language=language), self.assertRaises(AIRejected):
                validate_translation(self.source, output, language)


class TranslationProviderTests(unittest.IsolatedAsyncioTestCase):
    def source(self):
        return {"schemaVersion": 1, "forecastId": "published-forecast", "specificationHash": "a" * 64,
                "language": "en", "title": "Will Acme announce Product X?", "question": "Will Acme announce Product X?",
                "rules": [{"clauseId": "yes", "outcome": "YES", "condition": "Acme announces Product X."}],
                "invalidationRules": ["Product X cannot be identified."], "aiRationale": None,
                "openAt": 1000, "closeAt": 2000}

    @staticmethod
    def review(**changes):
        return {"faithful": True, "language_correct": True, "numbers_and_dates_preserved": True,
                "explanation": "Meaning, language and deadlines are preserved.", **changes}

    async def test_generation_and_review_use_distinct_prompts_and_retain_hashes(self):
        source = self.source()
        transport = Transport([translated(source), self.review()])
        result = await coordinator(transport).translate_display(source, "ko")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.source_calls, [])
        self.assertEqual(len(result.artifacts), 2)
        for retained in result.artifacts:
            self.assertEqual(content_hash(json.loads(retained.body)), retained.content_hash)
            self.assertNotIn("test-key", retained.body)
        generated, reviewed = [call["body"] for call in transport.calls]
        self.assertIn("Korean", json.dumps(generated["systemInstruction"]))
        self.assertIn("English", json.dumps(reviewed["systemInstruction"]))
        self.assertEqual(generated["generationConfig"]["maxOutputTokens"], 8192)
        self.assertEqual(source, self.source())

    async def test_negative_review_is_rejected_with_both_artifacts(self):
        for field in ("faithful", "language_correct", "numbers_and_dates_preserved"):
            transport = Transport([translated(self.source()), self.review(**{field: False})])
            with self.subTest(field=field), self.assertRaises(AIRejected) as caught:
                await coordinator(transport).translate_display(self.source(), "ko")
            self.assertEqual(len(caught.exception.artifacts), 2)

    async def test_malformed_generation_is_rejected_without_review(self):
        output = translated(self.source())
        output["rules"][0]["outcome"] = "NO"
        transport = Transport([output])
        with self.assertRaises(AIRejected) as caught:
            await coordinator(transport).translate_display(self.source(), "ko")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(caught.exception.artifacts), 1)

    async def test_translation_language_override_cannot_change_domain_task_language(self):
        transport = Transport([])
        with self.assertRaises((ValueError, AIRejected)):
            await coordinator(transport)._call("market_compiler", {}, {"type": "object"}, display_language="ko")
        self.assertEqual(transport.calls, [])
