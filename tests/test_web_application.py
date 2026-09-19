"""Real SQLite/D1-contract persistence and failure-path application verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from forecast_application.ai import (
    AIRejected,
    AIUnavailable,
    Artifact,
    CompileResult,
    DisputeResult,
    ResolutionResult,
)
from forecast_application.database import SQLiteDatabase
from forecast_application.errors import AppError
from forecast_application.service import (
    CHALLENGE_MS,
    DAY_MS,
    MAX_DAILY_AI_CALLS,
    Application,
)
from forecast_application.sources import SourceUnavailable
from forecast_domain import (
    Command,
    Forecast,
    apply_command,
    content_hash,
    create_forecast,
    dumps,
    loads,
    to_dict,
)
from forecast_domain.lifecycle import (
    Archive,
    BeginValidation,
    Finalize,
    LifecycleState,
    Publish,
    SubmitForecast,
    adjudication_input_hash,
)
from forecast_domain.models import AITask, ForecastChoice, Outcome, UserForecast

from tests import model_fixtures as fixtures

ROOT = Path(__file__).resolve().parents[1]


class TestAI:
    """Only tests instantiate this deterministic adapter; never application runtime."""

    configured_providers = ("provider-a", "provider-b", "independent-provider")

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.unavailable_providers = self.configured_providers
        self.reject = False
        self.material = False
        self.outcome = Outcome.YES
        # "instant" publishes at the deadline, which is what ordinary resolutions look
        # like. Any other value omits the metadata, so the evidence is authentic but its
        # publication time cannot be placed relative to participation.
        self.publication = "instant"
        self.read_artifact = None
        self.gate = None
        self.on_resolution = None
        self.last_candidates = []
        self.last_question = None
        self.ai_forecast = None

    async def compile_question(self, question, candidates, now_ms, *, distinct_measurement_windows=False):
        self.calls += 1
        self.last_candidates = list(candidates)
        self.last_distinct_windows = distinct_measurement_windows
        self.last_question = question
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise AIUnavailable("providers failed", unavailable_providers=self.unavailable_providers)
        spec = fixtures.specification(canonical_question=question, share_title=question,
                                      open_at_ms=now_ms, close_at_ms=now_ms+100000)
        assessment = fixtures.validation(spec, validated_at_ms=now_ms)
        return CompileResult(spec, assessment, (), self.ai_forecast)

    async def propose_resolution(self, forecast, now_ms, *, publication_time_unknown=False,
                                 determined_outcome=None):
        # The service tells the judge when the evidence cannot be placed relative to
        # participation, and when a closed review has already determined the outcome.
        # This stub answers the same either way; what matters here is that the call is
        # accepted and that both facts reach it.
        self.publication_time_unknown = publication_time_unknown
        self.determined_outcome = determined_outcome
        self.calls += 1
        if self.fail:
            raise AIUnavailable("providers failed", unavailable_providers=self.unavailable_providers)
        if self.reject:
            raise AIRejected("counter judge disagrees")
        # Successful ordinary-resolution fixtures explicitly publish at the
        # deadline. Missing/earlier publication is tested as a timing-review hold.
        instant = datetime.fromtimestamp(forecast.specification.close_at_ms/1000, timezone.utc).isoformat(timespec="seconds")
        meta = f'<meta property="article:published_time" content="{instant}">' if self.publication == "instant" else ""
        body = meta + '<article>Immutable official announcement.</article>'
        evidence = fixtures.evidence(forecast.specification, content=body)
        value = fixtures.resolution(forecast.specification, forecast_id=forecast.forecast_id,
                                    proposed_at_ms=now_ms, outcome=self.outcome, evidence=(evidence,))
        if self.on_resolution:
            self.on_resolution()
        artifact = Artifact(hashlib.sha256(body.encode()).hexdigest(), "source", body, "text/plain")
        return ResolutionResult(value, (artifact,))

    async def collect_dispute_evidence(self, specification, url, now_ms):
        body = "Retained counter-evidence with product identity details."
        value = fixtures.evidence(specification, collected_at_ms=now_ms, content=body, collector=None)
        return (value,), (Artifact(hashlib.sha256(body.encode()).hexdigest(), "source", body, "text/plain"),)

    async def review_dispute(self, forecast, dispute, now_ms):
        if self.fail:
            raise AIUnavailable("providers failed", unavailable_providers=self.unavailable_providers)
        result = fixtures.review(dispute, forecast.resolution, forecast.specification,
                                 material_conflict=self.material, reviewed_at_ms=now_ms)
        return DisputeResult(result, ())


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.ai = TestAI()
        self.now = 1_800_000_000_000
        self.nonce = 0
        self.app = Application(self.db, self.ai, now_ms=lambda: self.now,
            random_token=self.random_token, token_hash=self.token_hash)
        self.account = await self.app.register("테스터")
        self.uid = self.account["user"]["id"]
        self.other = (await self.app.register("다른 사용자"))["user"]["id"]

    def random_token(self):
        self.nonce += 1
        return hashlib.sha256(str(self.nonce).encode()).hexdigest()

    @staticmethod
    def token_hash(value):
        return hmac.new(b"test-key-only-not-a-production-secret", value.encode(), hashlib.sha256).hexdigest()

    async def asyncTearDown(self):
        self.connection.close()

    async def publish(self, question="Will Acme officially announce Product X before the deadline?"):
        draft = await self.app.compile_forecast(self.uid, question)
        value = await self.app.publish_forecast(self.uid, draft["draftId"], "publish-key-123")
        return value["forecast"]

    async def challenge(self, *, vote=True):
        forecast = await self.publish()
        if vote:
            await self.app.submit_forecast(self.other, forecast["id"], "YES", 80,
                                           forecast["revision"], "vote-key-123")
        self.now = forecast["closeAt"]+1000
        result = await self.app.run_due_jobs()
        self.assertEqual(result["failed"], 0)
        value = await self.app._forecast(forecast["id"])
        self.assertEqual(value.state, LifecycleState.CHALLENGE)
        return value

    async def _points_forecast(self, suffix):
        draft = await self.app.compile_forecast(self.uid,
            f"Will Acme announce Product {suffix} before the stated deadline?")
        return (await self.app.publish_forecast(self.uid, draft["draftId"],
                                                "publish-points-" + suffix))["forecast"]

    async def test_profile_points_grant_is_reported_once_without_rewarding_profile_edits_or_login(self):
        self.assertEqual(self.account["points"]["available"], 1000)
        self.assertEqual(self.account["points"]["committed"], 0)
        self.assertTrue(self.account["points"]["onboarding"]["profile"]["completed"])
        self.assertFalse(self.account["points"]["onboarding"]["wallet"]["completed"])
        baseline = self.account["points"]["entries"]
        edited = await self.app.update_profile(self.uid, "Updated profile")
        logged_in = await self.app.login(self.account["recoveryCode"])
        profile = await self.app.me(self.uid)
        for result in (edited, logged_in, profile):
            self.assertEqual(result["points"]["available"], 1000)
            self.assertEqual(result["points"]["entries"], baseline)
            self.assertEqual(result["points"]["userId"], self.uid)
        self.assertIsNone((await self.app.me(None))["points"])

    async def test_profile_loads_one_hundred_stake_positions_with_one_bounded_query(self):
        for index in range(100):
            await self.profile_ledger_fixture(index)
        original_all = self.db.all
        queries = []
        async def count_position_queries(sql, params=()):
            if "FROM point_positions" in sql:
                queries.append((sql, params))
            return await original_all(sql, params)
        self.db.all = count_position_queries
        profile = await self.app.me(self.other)
        self.assertEqual(len(profile["myForecasts"]), 100)
        self.assertTrue(all(card["stake"]["amount"] == 0 for card in profile["myForecasts"]))
        self.assertEqual(len(queries), 1)
        self.assertLessEqual(len(queries[0][1]), 100)
        self.assertEqual(profile["points"]["available"], 1000)

    async def test_profile_grant_rolls_back_with_failed_registration_transaction(self):
        original_batch = self.db.batch
        before_users = (await self.db.first("SELECT COUNT(*) AS n FROM users"))["n"]
        before_sessions = (await self.db.first("SELECT COUNT(*) AS n FROM sessions"))["n"]
        async def fail_registration(statements):
            if any("INSERT INTO users(" in sql for sql, _ in statements):
                return await original_batch([*statements,
                    ("INSERT INTO mutation_guards(token,valid) VALUES('registration-rollback',0)", ())])
            return await original_batch(statements)
        self.db.batch = fail_registration
        with self.assertRaises(sqlite3.IntegrityError):
            await self.app.register("Must roll back with its grant")
        self.db.batch = original_batch
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM users"))["n"], before_users)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM sessions"))["n"], before_sessions)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM point_accounts"))["n"], before_users)

    async def test_stake_reservation_and_exact_retry_return_current_balance_without_double_debit(self):
        card = await self._points_forecast("reservation")
        result = await self.app.submit_forecast(self.other, card["id"], "YES", 80,
            card["revision"], "stake-reservation-key", stake_points=200)
        self.assertEqual((result["points"]["available"], result["points"]["committed"]), (800, 200))
        self.assertEqual(result["stake"]["amount"], 200)
        self.assertEqual(result["stake"]["status"], "committed")
        self.assertEqual(result["stake"]["forecastRevision"], card["revision"]+1)
        retry = await self.app.submit_forecast(self.other, card["id"], "YES", 80,
            card["revision"], "stake-reservation-key", stake_points=200)
        self.assertEqual(retry["myForecast"], result["myForecast"])
        self.assertEqual(retry["points"], result["points"])
        self.assertEqual(retry["stake"], result["stake"])
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 80,
                card["revision"], "stake-reservation-key", stake_points=300)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        detail = await self.app.forecast_detail(card["id"], self.other)
        profile = await self.app.me(self.other)
        self.assertEqual(detail["stake"], result["stake"])
        self.assertEqual(detail["points"]["available"], 800)
        self.assertEqual(profile["myForecasts"][0]["stake"], result["stake"])
        self.assertIsNone((await self.app.forecast_detail(card["id"]))["points"])

    async def test_stake_updates_adjust_only_difference_and_explicit_practice_releases_hold(self):
        card = await self._points_forecast("updates")
        revision = card["revision"]
        for index, (stake, outcome, available) in enumerate(((200, "YES", 800), (500, "NO", 500),
                                                            (100, "NO", 900), (0, "YES", 1000))):
            result = await self.app.submit_forecast(self.other, card["id"], outcome, 70, revision,
                f"stake-update-key-{index}", stake_points=stake)
            revision += 1
            self.assertEqual(result["points"]["available"], available)
            self.assertEqual(result["points"]["committed"], stake)
            self.assertEqual(result["stake"]["amount"], stake)
            self.assertEqual(result["myForecast"]["outcome"], outcome)
            self.assertEqual(result["stake"]["status"], "committed" if stake else "practice")
            self.assertEqual(result["stake"]["forecastRevision"], revision)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM user_forecasts WHERE forecast_id=?",
                                            (card["id"],)))["n"], 1)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM forecast_history WHERE forecast_id=?",
                                            (card["id"],)))["n"], 4)

    async def test_omitted_stake_preserves_legacy_request_hash_and_never_silently_releases_positive_hold(self):
        card = await self._points_forecast("legacy")
        original = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "legacy-practice-key")
        request = {"kind": "forecast", "forecastId": card["id"], "outcome": "YES", "confidence": 80, "revision": 2}
        stored = await self.db.first("SELECT request_hash FROM operations WHERE user_id=? AND operation_key=?",
                                     (self.other, "legacy-practice-key"))
        self.assertEqual(stored["request_hash"], content_hash(request))
        self.assertEqual(original["stake"]["amount"], 0)
        staked = await self.app.submit_forecast(self.other, card["id"], "NO", 60, 3,
                                               "explicit-later-stake", stake_points=200)
        retried_legacy = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "legacy-practice-key")
        self.assertEqual(retried_legacy["myForecast"], original["myForecast"])
        self.assertEqual(retried_legacy["points"], staked["points"])
        self.assertEqual(retried_legacy["stake"]["amount"], 200)
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 90, 4, "missing-stake-confirmation")
        self.assertEqual(caught.exception.code, "stake_required")
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2,
                                           "legacy-practice-key", stake_points=0)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual((await self.app._forecast(card["id"])).revision, 4)

    async def test_legacy_future_revision_cannot_release_hold_created_during_position_read(self):
        card = await self._points_forecast("legacy-future-race")
        original_position = self.app.points.position
        interleaved = False
        async def stake_after_empty_position(user_id, forecast_id):
            nonlocal interleaved
            position = await original_position(user_id, forecast_id)
            if not interleaved:
                interleaved = True
                await self.app.submit_forecast(self.other, card["id"], "YES", 80,
                    card["revision"], "concurrent-explicit-future", stake_points=300)
            return position
        self.app.points.position = stake_after_empty_position
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "NO", 60,
                card["revision"]+1, "legacy-future-race-request")
        self.app.points.position = original_position
        self.assertEqual(caught.exception.status, 409)
        # The invalid future revision must be rejected before consulting a hold.
        self.assertFalse(interleaved)
        result = await self.app.submit_forecast(self.other, card["id"], "YES", 80,
            card["revision"], "concurrent-explicit-future", stake_points=300)
        self.assertEqual((result["points"]["available"], result["points"]["committed"]), (700, 300))
        self.assertEqual(result["stake"]["status"], "committed")
        self.assertIsNone(await self.db.first("SELECT result FROM operations WHERE operation_key='legacy-future-race-request'"))

    async def test_legacy_current_revision_cas_preserves_concurrently_created_hold(self):
        card = await self._points_forecast("legacy-current-race")
        original_position = self.app.points.position
        interleaved = False
        async def stake_after_empty_position(user_id, forecast_id):
            nonlocal interleaved
            position = await original_position(user_id, forecast_id)
            if not interleaved:
                interleaved = True
                await self.app.submit_forecast(self.other, card["id"], "YES", 80,
                    card["revision"], "concurrent-explicit-current", stake_points=300)
            return position
        self.app.points.position = stake_after_empty_position
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "NO", 60,
                card["revision"], "legacy-current-race-request")
        self.app.points.position = original_position
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(interleaved)
        position = await self.app.points.position(self.other, card["id"])
        points = await self.app.points.summary(self.other)
        self.assertEqual((points["available"], points["committed"]), (700, 300))
        self.assertEqual((position["amount"], position["status"], position["outcome"]), (300, "committed", "YES"))
        self.assertEqual((await self.app._forecast(card["id"])).revision, card["revision"]+1)
        self.assertIsNone(await self.db.first("SELECT result FROM operations WHERE operation_key='legacy-current-race-request'"))

    async def test_legacy_position_read_cannot_backdate_acceptance_past_deadline(self):
        card = await self._points_forecast("legacy-closing-race")
        original_position = self.app.points.position
        async def expire_after_position_read(user_id, forecast_id):
            result = await original_position(user_id, forecast_id)
            self.now = card["closeAt"]
            return result
        self.app.points.position = expire_after_position_read
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 80,
                card["revision"], "legacy-closing-race-request")
        self.app.points.position = original_position
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual((await self.app._forecast(card["id"])).revision, card["revision"])
        self.assertEqual((await self.app.points.summary(self.other))["available"], 1000)

    async def test_insufficient_points_roll_back_forecast_snapshot_submission_history_and_operation(self):
        first = await self._points_forecast("available")
        second = await self._points_forecast("insufficient")
        await self.app.submit_forecast(self.other, first["id"], "YES", 80, 2,
                                       "reserve-most-balance", stake_points=700)
        balance = await self.app.points.summary(self.other)
        snapshot = await self.app._forecast(second["id"])
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, second["id"], "YES", 90, 2,
                                           "cannot-reserve-balance", stake_points=400)
        self.assertEqual(caught.exception.code, "insufficient_points")
        self.assertEqual(await self.app._forecast(second["id"]), snapshot)
        self.assertEqual(await self.app.points.summary(self.other), balance)
        for table in ("user_forecasts", "forecast_history"):
            self.assertEqual((await self.db.first(f"SELECT COUNT(*) AS n FROM {table} WHERE forecast_id=?",
                                                (second["id"],)))["n"], 0)
        self.assertIsNone(await self.db.first("SELECT result FROM operations WHERE operation_key='cannot-reserve-balance'"))
        self.assertEqual((await self.app.points.position(self.other, second["id"]))["amount"], 0)

    async def test_failed_sql_after_reservation_rolls_back_grant_balance_and_entire_forecast_mutation(self):
        card = await self._points_forecast("sql-rollback")
        original_batch = self.db.batch
        snapshot = await self.app._forecast(card["id"])
        points = await self.app.points.summary(self.other)
        async def fail_after_reservation(statements):
            if any("UPDATE forecasts SET snapshot=" in sql for sql, _ in statements):
                return await original_batch([*statements,
                    ("INSERT INTO mutation_guards(token,valid) VALUES('stake-rollback',0)", ())])
            return await original_batch(statements)
        self.db.batch = fail_after_reservation
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2,
                                           "stake-rollback-request", stake_points=300)
        self.db.batch = original_batch
        self.assertEqual(caught.exception.code, "forecast_storage_unavailable")
        self.assertEqual(await self.app._forecast(card["id"]), snapshot)
        self.assertEqual(await self.app.points.summary(self.other), points)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM user_forecasts"))["n"], 0)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM forecast_history"))["n"], 0)
        self.assertIsNone(await self.db.first("SELECT result FROM operations WHERE operation_key='stake-rollback-request'"))

    async def test_reservation_commit_with_lost_acknowledgement_recovers_durable_receipt(self):
        card = await self._points_forecast("lost-ack")
        original_batch = self.db.batch
        async def lose_acknowledgement(statements):
            result = await original_batch(statements)
            if any("UPDATE forecasts SET snapshot=" in sql for sql, _ in statements):
                raise RuntimeError("private transport response was lost after commit")
            return result
        self.db.batch = lose_acknowledgement
        result = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2,
                                               "reservation-lost-ack", stake_points=300)
        self.db.batch = original_batch
        self.assertEqual((result["points"]["available"], result["points"]["committed"]), (700, 300))
        self.assertEqual(result["stake"]["amount"], 300)
        retry = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2,
                                              "reservation-lost-ack", stake_points=300)
        self.assertEqual(retry["points"], result["points"])
        self.assertEqual((await self.app._forecast(card["id"])).revision, 3)

    async def test_opaque_reservation_database_failure_is_classified_from_current_available_balance(self):
        first = await self._points_forecast("opaque-first")
        second = await self._points_forecast("opaque-second")
        await self.app.submit_forecast(self.other, first["id"], "YES", 80, 2,
                                       "opaque-initial-hold", stake_points=700)
        original_batch = self.db.batch
        async def obscure_database_error(statements):
            try:
                return await original_batch(statements)
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("private adapter detail; no portable constraint message") from exc
        self.db.batch = obscure_database_error
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, second["id"], "YES", 80, 2,
                                           "opaque-insufficient-hold", stake_points=400)
        self.db.batch = original_batch
        self.assertEqual(caught.exception.code, "insufficient_points")
        self.assertNotIn("private", caught.exception.message)
        self.assertEqual((await self.app._forecast(second["id"])).revision, 2)
        self.assertEqual((await self.app.points.summary(self.other))["available"], 300)

    async def test_pre_points_practice_receipt_replays_without_new_position_or_settlement_credit(self):
        card = await self._points_forecast("pre-points")
        forecast = await self.app._forecast(card["id"])
        choice = UserForecast(forecaster_id=self.other, forecast_id=card["id"],
            specification_hash=forecast.specification_hash, outcome=ForecastChoice.YES,
            confidence=80, submitted_at_ms=self.now)
        request = {"kind": "forecast", "forecastId": card["id"], "outcome": "YES", "confidence": 80, "revision": 2}
        receipt = {"myForecast": {"outcome": "YES", "confidence": 80, "probability": 80,
                                   "submittedAt": self.now, "revision": 3}}
        await self.app._mutate(forecast, SubmitForecast(user_forecast=choice), key="legacy-before-points", extra=[
            self.app._record_artifact(choice, "user_forecast"),
            ("INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,revision,body) "
             "VALUES(?,?,?,?,?,?,?,?)", (card["id"], self.other, "YES", 80, 80, self.now, 3, dumps(choice))),
            self.app._operation(self.other, "legacy-before-points", request, card["id"], receipt)])
        result = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "legacy-before-points")
        self.assertEqual(result["myForecast"], receipt["myForecast"])
        self.assertEqual(result["stake"]["status"], "practice")
        self.assertEqual(result["points"]["available"], 1000)
        self.assertIsNone(await self.db.first("SELECT user_id FROM point_positions WHERE user_id=? AND forecast_id=?",
                                            (self.other, card["id"])))
        self.now = card["closeAt"]+1000
        await self.app.run_due_jobs()
        challenge = await self.app._forecast(card["id"])
        self.now = challenge.challenge_until_ms
        await self.app.run_due_jobs()
        self.assertEqual((await self.app.points.summary(self.other))["available"], 1000)
        self.assertEqual((await self.app.points.summary(self.other))["committed"], 0)
        self.assertEqual((await self.app.reputation(self.other))["resolvedForecasts"], 1)

    async def test_settlement_and_reputation_roll_back_together_then_retry_without_duplicate_credit(self):
        card = await self._points_forecast("settlement-rollback")
        await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2,
                                       "settlement-rollback-stake", stake_points=100)
        self.now = card["closeAt"]+1000
        await self.app.run_due_jobs()
        challenge = await self.app._forecast(card["id"])
        self.now = challenge.challenge_until_ms
        await self.app._mutate(challenge, Finalize(), key="settlement-rollback-final")
        before = await self.app.points.summary(self.other)
        original_batch = self.db.batch
        async def fail_settlement(statements):
            if any("INSERT INTO point_ledger" in sql for sql, _ in statements):
                return await original_batch([*statements,
                    ("INSERT INTO mutation_guards(token,valid) VALUES('settlement-rollback',0)", ())])
            return await original_batch(statements)
        self.db.batch = fail_settlement
        with self.assertRaises(sqlite3.IntegrityError):
            await self.app._process_outbox(50)
        self.db.batch = original_batch
        self.assertEqual(await self.app.points.summary(self.other), before)
        self.assertEqual((await self.app.reputation(self.other))["resolvedForecasts"], 0)
        pending = await self.db.first("SELECT id,status FROM outbox WHERE forecast_id=? AND kind='REPUTATION_UPDATE_REQUIRED'",
                                     (card["id"],))
        self.assertEqual(pending["status"], "pending")
        await self.app._process_outbox(50)
        settled = await self.app.points.summary(self.other)
        self.assertEqual((settled["available"], settled["committed"]), (1100, 0))
        await self.db.execute("UPDATE outbox SET status='pending',processed_at=NULL WHERE id=?", (pending["id"],))
        await self.app._process_outbox(50)
        self.assertEqual(await self.app.points.summary(self.other), settled)
        self.assertEqual((await self.app.reputation(self.other))["resolvedForecasts"], 1)

    async def test_cross_forecast_concurrent_reservations_cannot_overspend_account(self):
        first = await self._points_forecast("race-one")
        second = await self._points_forecast("race-two")
        original_batch = self.db.batch
        ready = asyncio.Event()
        reached = 0
        async def synchronize_reservations(statements):
            nonlocal reached
            if any("UPDATE forecasts SET snapshot=" in sql for sql, _ in statements):
                reached += 1
                if reached == 2:
                    ready.set()
                await ready.wait()
            return await original_batch(statements)
        self.db.batch = synchronize_reservations
        results = await asyncio.gather(
            self.app.submit_forecast(self.other, first["id"], "YES", 80, 2, "cross-stake-race-one", stake_points=800),
            self.app.submit_forecast(self.other, second["id"], "NO", 70, 2, "cross-stake-race-two", stake_points=800),
            return_exceptions=True)
        self.db.batch = original_batch
        errors = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AppError)
        self.assertEqual(errors[0].code, "insufficient_points")
        points = await self.app.points.summary(self.other)
        self.assertEqual((points["available"], points["committed"]), (200, 800))
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM user_forecasts"))["n"], 1)

    async def test_stakes_do_not_weight_crowd_probability_or_reputation(self):
        card = await self._points_forecast("unweighted")
        await self.app.submit_forecast(self.uid, card["id"], "YES", 90, 2, "large-stake-unweighted", stake_points=900)
        result = await self.app.submit_forecast(self.other, card["id"], "NO", 90, 3,
                                               "small-stake-unweighted", stake_points=1)
        self.assertEqual(result["forecast"]["crowd"], {"probability": 50.0, "count": 2})
        self.now = card["closeAt"] + 1000
        await self.app.run_due_jobs()
        challenge = await self.app._forecast(card["id"])
        self.now = challenge.challenge_until_ms
        await self.app.run_due_jobs()
        self.assertAlmostEqual((await self.app.reputation(self.uid))["brierScore"], 0.01)
        self.assertAlmostEqual((await self.app.reputation(self.other))["brierScore"], 0.81)

    async def test_settlement_is_exact_once_for_yes_no_invalid_and_retained_until_final(self):
        for outcome in (Outcome.YES, Outcome.NO, Outcome.INVALID):
            self.ai.outcome = outcome
            card = await self._points_forecast("settle-" + outcome.value)
            before_user = (await self.app.points.summary(self.uid))["available"]
            before_other = (await self.app.points.summary(self.other))["available"]
            await self.app.submit_forecast(self.uid, card["id"], "YES", 80, 2,
                                           "yes-stake-" + outcome.value, stake_points=200)
            await self.app.submit_forecast(self.other, card["id"], "NO", 70, 3,
                                           "no-stake-" + outcome.value, stake_points=100)
            self.now = card["closeAt"]+1000
            await self.app.run_due_jobs()
            challenge = await self.app._forecast(card["id"])
            self.assertEqual(challenge.state, LifecycleState.CHALLENGE)
            self.assertEqual((await self.app.points.position(self.uid, card["id"]))["status"], "committed")
            self.assertEqual((await self.app.points.summary(self.uid))["available"], before_user-200)
            self.now = challenge.challenge_until_ms
            await self.app._mutate(challenge, Finalize(), key="settlement-final-" + outcome.value)
            if outcome == Outcome.INVALID:
                finalized = await self.app._forecast(card["id"])
                await self.app._mutate(finalized, Archive(), key="settlement-archive-invalid")
            await self.app._process_outbox(50)
            user_return = 200 if outcome == Outcome.INVALID else 400 if outcome == Outcome.YES else 0
            other_return = 100 if outcome == Outcome.INVALID else 200 if outcome == Outcome.NO else 0
            user = await self.app.points.summary(self.uid)
            other = await self.app.points.summary(self.other)
            self.assertEqual((user["available"], user["committed"]), (before_user-200+user_return, 0))
            self.assertEqual((other["available"], other["committed"]), (before_other-100+other_return, 0))
            position = await self.app.points.position(self.uid, card["id"])
            self.assertEqual(position["status"], "settled")
            self.assertEqual(position["returned"], user_return)
            await self.app._process_outbox(50)
            await self.app.run_due_jobs()
            self.assertEqual(await self.app.points.summary(self.uid), user)
            self.assertEqual(await self.app.points.summary(self.other), other)

    async def test_invalid_stakes_never_write_forecasts_or_ledger(self):
        card = await self._points_forecast("invalid-amounts")
        before = await self.app.points.summary(self.other)
        for index, amount in enumerate((True, False, -1, 1001, 1.5, "100", [])):
            with self.subTest(amount=amount), self.assertRaises(AppError) as caught:
                await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2,
                    f"invalid-stake-request-{index}", stake_points=amount)
            self.assertEqual(caught.exception.code, "invalid_stake")
        self.assertEqual((await self.app._forecast(card["id"])).revision, 2)
        self.assertEqual(await self.app.points.summary(self.other), before)

    async def escalated(self):
        forecast = await self.challenge()
        self.now += 10
        await self.app.submit_dispute(self.uid, forecast.forecast_id, "판정에 중요한 반대 근거가 있습니다.",
            "https://acme.example/news", "yes-rule", "독립 심사가 필요한 근거 충돌입니다.",
            forecast.revision, "escalation-dispute-key")
        self.now += 10
        self.ai.material = True
        await self.app.run_due_jobs()
        value = await self.app._forecast(forecast.forecast_id)
        self.assertEqual(value.state, LifecycleState.ESCALATED)
        self.now += 10
        return value

    def operator_decision(self, forecast):
        instant = datetime.fromtimestamp(forecast.specification.close_at_ms/1000, timezone.utc).isoformat(timespec="seconds")
        body = f'<meta property="article:published_time" content="{instant}"><article>New retained official clarification for the independent adjudication.</article>'
        evidence = fixtures.evidence(forecast.specification, content=body,
                                     collected_at_ms=self.now-5)
        resolution = fixtures.resolution(forecast.specification, forecast_id=forecast.forecast_id,
            outcome=Outcome.NO, proposed_at_ms=self.now, evidence=(evidence,))
        adjudicator = fixtures.provenance(AITask.ADJUDICATION, provider="third-independent-provider",
            input_hash=adjudication_input_hash(forecast, resolution),
            output_hash=resolution.resolution_hash, created_at_ms=self.now)
        return resolution, adjudicator, (Artifact(evidence.content_sha256, "source", body, "text/plain"),)

    async def test_operator_adjudication_requires_fresh_full_challenge_before_finalization(self):
        forecast = await self.escalated()
        resolution, adjudicator, artifacts = self.operator_decision(forecast)
        result = await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
            artifacts, "operator-valid-key", expected_revision=forecast.revision)
        self.assertEqual(result["forecast"]["state"], "PROPOSED")
        proposed = await self.app._forecast(forecast.forecast_id)
        self.assertIsNone(proposed.finalized_outcome)
        self.assertIsNone(proposed.challenge_until_ms)
        self.assertEqual(proposed.disputes, ())
        with self.assertRaises(AppError):
            await self.app._mutate(proposed, Finalize(), key="premature-operator-final")
        self.now += 1
        await self.app.run_due_jobs()
        fresh = await self.app._forecast(forecast.forecast_id)
        self.assertEqual(fresh.state, LifecycleState.CHALLENGE)
        self.assertEqual(fresh.challenge_until_ms, self.now+CHALLENGE_MS)
        self.assertGreater(fresh.challenge_until_ms, forecast.challenge_until_ms)
        self.assertEqual((await self.app.reputation(self.uid))["totalDisputes"], 1)
        self.assertEqual((await self.app.reputation(self.uid))["successfulDisputes"], 1)
        self.assertEqual((await self.app.creator(self.uid))["creator"]["disputedMarkets"], 1)
        self.now = fresh.challenge_until_ms
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(forecast.forecast_id)).finalized_outcome, Outcome.NO)

    async def test_operator_adjudication_retry_returns_original_receipt_and_current_state(self):
        forecast = await self.escalated()
        resolution, adjudicator, artifacts = self.operator_decision(forecast)
        original = await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
            artifacts, "operator-retry-key", expected_revision=forecast.revision)
        self.now += 10
        await self.app.run_due_jobs()
        retry = await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
            artifacts, "operator-retry-key", expected_revision=forecast.revision)
        self.assertEqual(retry["adjudication"], original["adjudication"])
        self.assertEqual(retry["forecast"]["state"], "CHALLENGE")
        events = await self.db.first("SELECT COUNT(*) AS n FROM events WHERE "
            "json_extract(event,'$.command_name')='adjudicate_resolution'")
        self.assertEqual(events["n"], 1)
        with self.assertRaises(AppError) as caught:
            await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
                (), "operator-retry-key", expected_revision=forecast.revision)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_operator_adjudication_missing_artifact_and_nonindependent_provider_fail_closed(self):
        forecast = await self.escalated()
        resolution, adjudicator, artifacts = self.operator_decision(forecast)
        before = dumps(forecast)
        with self.assertRaises(AppError) as caught:
            await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
                (), "operator-missing-key", expected_revision=forecast.revision)
        self.assertEqual(caught.exception.code, "missing_resolution_artifact")
        nonindependent = replace(adjudicator, provider=forecast.resolution.judge.provider)
        with self.assertRaises(AppError) as caught:
            await self.app.adjudicate_forecast(forecast.forecast_id, resolution, nonindependent,
                artifacts, "operator-biased-key", expected_revision=forecast.revision)
        self.assertEqual(caught.exception.code, "adjudication_validation_failed")
        self.assertEqual(dumps(await self.app._forecast(forecast.forecast_id)), before)
        self.assertIsNone(await self.app.read_artifact(artifacts[0].content_hash))

    async def test_operator_adjudication_stale_revision_and_wrong_state_are_rejected(self):
        forecast = await self.escalated()
        resolution, adjudicator, artifacts = self.operator_decision(forecast)
        with self.assertRaises(AppError) as caught:
            await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
                artifacts, "operator-stale-key", expected_revision=forecast.revision-1)
        self.assertEqual(caught.exception.code, "revision_conflict")
        await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
            artifacts, "operator-first-key", expected_revision=forecast.revision)
        with self.assertRaises(AppError) as caught:
            await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
                artifacts, "operator-wrongstate-key", expected_revision=forecast.revision+1)
        self.assertEqual(caught.exception.code, "adjudication_not_allowed")

    async def test_operator_adjudication_accepts_previously_retained_source_and_rejects_future_decision(self):
        forecast = await self.escalated()
        resolution, adjudicator, artifacts = self.operator_decision(forecast)
        self.now -= 1
        with self.assertRaises(AppError) as caught:
            await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
                artifacts, "operator-future-key", expected_revision=forecast.revision)
        self.assertEqual(caught.exception.status, 400)
        self.now += 1
        await self.db.batch(self.app._artifact_sql(artifacts))
        result = await self.app.adjudicate_forecast(forecast.forecast_id, resolution, adjudicator,
            (), "operator-retained-key", expected_revision=forecast.revision)
        self.assertEqual(result["forecast"]["state"], "PROPOSED")

    async def test_authentication_stores_only_keyed_hashes_and_revokes_session(self):
        row = await self.db.first("SELECT * FROM users WHERE id=?", (self.uid,))
        session = await self.db.first("SELECT * FROM sessions WHERE user_id=?", (self.uid,))
        self.assertNotIn(self.account["recoveryCode"], json.dumps(row))
        self.assertNotIn(self.account["sessionToken"], json.dumps(session))
        self.assertEqual((await self.app.authenticate(self.account["sessionToken"]))["id"], self.uid)
        logged = await self.app.login(self.account["recoveryCode"])
        await self.app.logout(logged["sessionToken"])
        self.assertIsNone(await self.app.authenticate(logged["sessionToken"]))
        self.now += 31*DAY_MS
        self.assertIsNone(await self.app.authenticate(self.account["sessionToken"]))

    async def test_rejects_invalid_recovery_and_weak_entropy_provider(self):
        with self.assertRaises(AppError) as caught:
            await self.app.login("x"*64)
        self.assertEqual(caught.exception.status, 401)
        weak = Application(self.db, self.ai, now_ms=lambda: self.now, token_hash=self.token_hash,
                           random_token=lambda: "weak")
        with self.assertRaises(RuntimeError):
            await weak.register("weak")

    async def test_profile_update_and_anonymous_empty_state(self):
        self.assertIsNone((await self.app.me(None))["user"])
        user = (await self.app.update_profile(self.uid, "수정된 이름"))["user"]
        self.assertEqual(user["displayName"], "수정된 이름")
        empty = await self.app.me(self.uid)
        self.assertIsNone(empty["reputation"]["accuracy"])
        self.assertIsNone(empty["reputation"]["brierScore"])
        self.assertEqual(empty["myForecasts"], [])

    async def test_compile_publish_persists_immutable_domain_and_null_observations(self):
        card = await self.publish()
        self.assertEqual(card["state"], "OPEN")
        self.assertEqual(card["crowd"], {"probability": None, "count": 0})
        self.assertEqual(card["top"], {"probability": None, "count": 0})
        self.assertIsNone(card["ai"]["probability"])
        self.assertEqual(card["chain"]["status"], "unconnected")
        snapshot = await self.app._forecast(card["id"])
        self.assertEqual(snapshot.specification_hash, card["specificationHash"])
        detail = await self.app.forecast_detail(card["id"])
        self.assertEqual(len(detail["audit"]), 2)
        raw = await self.app.read_artifact(snapshot.specification_hash)
        self.assertEqual(content_hash(json.loads(raw)), snapshot.specification_hash)
        for event in detail["audit"]:
            if event["artifactHash"]:
                self.assertIsNotNone(await self.app.read_artifact(event["artifactHash"]))

    async def test_public_integrity_exposes_exact_recomputable_canonical_specification(self):
        card = await self.publish()
        record = await self.app.integrity(card["id"])
        specification = record["specification"]
        self.assertEqual(content_hash(json.loads(specification["canonicalJson"])),
                         specification["specificationHash"])
        self.assertEqual(specification["specificationHash"], card["specificationHash"])
        profile = record["commitmentProfile"]
        self.assertEqual(profile["prefix"], "forecast-network:sha256:canonical-json:v1\n")
        self.assertEqual(hashlib.sha256((profile["prefix"]+specification["canonicalJson"]).encode("utf-8")).hexdigest(),
                         specification["specificationHash"])
        self.assertIsNone(record["resolution"])
        self.assertEqual(record["revision"], card["revision"])
        self.assertEqual(record["chain"], {"status": "unconnected", "network": None, "transaction": None})

    async def test_public_integrity_exposes_resolution_commitment_from_one_snapshot(self):
        forecast = await self.challenge()
        record = await self.app.integrity(forecast.forecast_id)
        resolution = record["resolution"]
        self.assertEqual(content_hash(json.loads(resolution["canonicalJson"])), resolution["resolutionHash"])
        self.assertEqual(resolution["resolutionHash"], forecast.resolution.resolution_hash)
        self.assertEqual(hashlib.sha256((record["commitmentProfile"]["prefix"]
                                        + resolution["canonicalJson"]).encode("utf-8")).hexdigest(),
                         resolution["resolutionHash"])
        self.assertEqual(record["auditHead"], forecast.audit_head_hash)
        self.assertEqual(record["revision"], forecast.revision)

    async def test_public_integrity_rejects_unknown_and_private_draft_ids(self):
        draft = await self.app.compile_forecast(self.uid, "Will Acme announce Product X before 2030?")
        for identifier in ("unknown-public-id", draft["draftId"]):
            with self.subTest(identifier=identifier), self.assertRaises(AppError) as caught:
                await self.app.integrity(identifier)
            self.assertEqual(caught.exception.status, 404)

    async def translation_body(self, card):
        forecast = await self.app._forecast(card["id"])
        return {"specificationHash": forecast.specification_hash,
            "title": "Will Acme announce Product X?",
            "question": "Will Acme officially announce Product X before the stated deadline?",
            "rules": [{"clauseId": rule.clause_id, "condition": rule.condition}
                      for rule in forecast.specification.rules],
            "invalidationRules": list(forecast.specification.invalidation_rules),
            "aiRationale": None, "sourceLanguage": "ko", "language": "en",
            "attribution": "Forecast editorial translation"}

    async def profile_ledger_fixture(self, index, *, outcome=Outcome.YES, choice="YES", confidence=80,
                                     user_id=None, finalized_at=None, scored=True):
        """Synthetic test-only data built through every real domain lifecycle gate."""
        from forecast_domain.lifecycle import (
            BeginChallenge,
            BeginResolution,
            Lock,
            ProposeResolution,
        )

        user_id = user_id or self.other
        finalized_at = self.now-index*1000 if finalized_at is None else finalized_at
        close_at = finalized_at-CHALLENGE_MS-1001
        created_at = close_at-100000
        identifier = f"profile-record-{index:04d}"
        spec = fixtures.specification(canonical_question=f"Will Company {index} announce its product?",
            share_title=f"Company {index} product announcement", open_at_ms=created_at, close_at_ms=close_at)
        forecast = create_forecast(forecast_id=identifier, creator_id=self.uid,
                                   specification=spec, now_ms=created_at)
        events = []

        def step(payload, at):
            nonlocal forecast
            result = apply_command(forecast, Command(idempotency_key=f"fixture-{forecast.revision}",
                expected_revision=forecast.revision, payload=payload), now_ms=at)
            forecast = result.forecast
            events.extend(result.events)

        step(BeginValidation(), created_at)
        step(Publish(assessment=fixtures.validation(spec, validated_at_ms=created_at)), created_at)
        personal = UserForecast(forecast_id=identifier, forecaster_id=user_id,
            specification_hash=spec.specification_hash, outcome=ForecastChoice(choice),
            confidence=confidence, submitted_at_ms=created_at+10)
        step(SubmitForecast(user_forecast=personal), created_at+10)
        step(Lock(), close_at)
        step(BeginResolution(), close_at+1)
        resolution = fixtures.resolution(spec, forecast_id=identifier, outcome=outcome, proposed_at_ms=close_at+1000)
        step(ProposeResolution(resolution=resolution), close_at+1000)
        step(BeginChallenge(duration_ms=CHALLENGE_MS), close_at+1001)
        step(Finalize(), finalized_at)
        probability = confidence if choice == "YES" else 100-confidence
        correct = None if outcome == Outcome.INVALID else int(choice == outcome.value)
        brier = None if outcome == Outcome.INVALID else (probability/100-int(outcome == Outcome.YES))**2
        statements = [
            ("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
             "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,challenge_until,"
             "finalized_outcome,mutation_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (identifier, self.uid, "draft-"+identifier, dumps(forecast), forecast.revision, forecast.state.value,
              spec.category.value, spec.share_title, spec.canonical_question, spec.canonical_question.casefold(),
              spec.specification_hash, created_at, close_at, created_at, finalized_at, forecast.challenge_until_ms,
              outcome.value, "profile-fixture")),
            ("INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,revision,body) "
             "VALUES(?,?,?,?,?,?,?,?)", (identifier, user_id, choice, confidence, probability, created_at+10, 3, dumps(personal))),
        ]
        statements.extend(("INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                           (identifier, event.revision, content_hash(event), dumps(event), event.occurred_at_ms)) for event in events)
        if scored:
            statements.append(("INSERT INTO reputation_scores(forecast_id,user_id,category,outcome,probability,correct,brier_score,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (identifier, user_id, spec.category.value, outcome.value, probability, correct, brier,
                                          finalized_at+500)))
        await self.db.batch(statements)
        return forecast

    async def test_profile_card_empty_pending_and_invalid_metrics_keep_zero_distinct_from_null(self):
        empty = await self.app.profile_card(self.other)
        self.assertEqual(empty["sampleStatus"], "new")
        self.assertEqual(empty["metrics"]["totalForecasts"], 0)
        self.assertIsNone(empty["metrics"]["accuracy"])
        self.assertIsNone(empty["metrics"]["brierScore"])
        self.assertIsNone(empty["metrics"]["calibrationScore"])
        self.assertEqual(empty["history"], [])
        self.assertIsNone(empty["highlight"])
        pending = await self.publish()
        await self.app.submit_forecast(self.other, pending["id"], "NO", 100, pending["revision"], "private-pending-choice")
        await self.profile_ledger_fixture(0, outcome=Outcome.INVALID)
        result = await self.app.profile_card(self.other)
        self.assertEqual(result["metrics"]["totalForecasts"], 2)
        self.assertEqual(result["metrics"]["resolvedForecasts"], 0)
        self.assertEqual(result["metrics"]["invalidForecasts"], 1)
        self.assertIsNone(result["metrics"]["accuracy"])
        self.assertEqual(result["history"], [])
        self.assertNotIn(pending["id"], result["canonicalJson"])

    async def test_profile_card_uses_personal_choices_with_actual_results_and_finalization_event_time(self):
        wrong = await self.profile_ledger_fixture(0, outcome=Outcome.YES, choice="NO", confidence=100)
        result = await self.app.profile_card(self.other)
        self.assertEqual(result["metrics"]["accuracy"], 0)
        self.assertEqual(result["metrics"]["brierScore"], 1)
        self.assertEqual(result["metrics"]["calibrationScore"], 0)
        self.assertEqual(result["sampleStatus"], "provisional")
        self.assertIsNone(result["highlight"])
        item = result["history"][0]
        self.assertEqual(item["outcome"], "NO")
        self.assertEqual(item["resolvedOutcome"], "YES")
        self.assertEqual(item["confidence"], 100)
        self.assertIs(item["correct"], False)
        self.assertEqual(item["finalizedAt"], wrong.updated_at_ms)
        self.assertNotEqual(item["finalizedAt"], wrong.updated_at_ms+500)

    async def test_profile_card_perfect_score_zero_survives_publication_without_becoming_null(self):
        await self.profile_ledger_fixture(0, outcome=Outcome.YES, choice="YES", confidence=100)
        record = await self.app.create_profile_card(self.other)
        self.assertEqual(record["metrics"]["accuracy"], 100)
        self.assertEqual(record["metrics"]["brierScore"], 0)
        self.assertIsNotNone(record["metrics"]["brierScore"])
        self.assertEqual(record["metrics"]["calibrationScore"], 1)
        self.assertEqual(record["highlight"]["confidence"], 100)
        self.assertEqual((await self.app.get_profile_card(record["snapshotHash"]))["metrics"]["brierScore"], 0)

    async def test_profile_card_all_time_metrics_do_not_use_the_truncated_history(self):
        for index in range(25):
            await self.profile_ledger_fixture(index, choice="YES", outcome=Outcome.YES if index < 20 else Outcome.NO,
                                               confidence=80)
        result = await self.app.profile_card(self.other)
        self.assertEqual(result["sampleStatus"], "established")
        self.assertEqual(result["metrics"]["resolvedForecasts"], 25)
        self.assertEqual(result["metrics"]["correctForecasts"], 20)
        self.assertEqual(result["metrics"]["accuracy"], 80)
        self.assertAlmostEqual(result["metrics"]["brierScore"], (20*.04+5*.64)/25)
        self.assertEqual(result["metrics"]["calibrationScore"], 1)
        self.assertEqual(len(result["history"]), 20)
        self.assertTrue(result["historyTruncated"])
        self.assertTrue(all(item["correct"] for item in result["history"]))

    async def test_profile_highlight_selects_best_correct_and_deterministic_recent_then_id_ties(self):
        await self.profile_ledger_fixture(0, confidence=60, finalized_at=self.now)
        await self.profile_ledger_fixture(2, confidence=90, finalized_at=self.now-1000)
        await self.profile_ledger_fixture(3, confidence=90, finalized_at=self.now)
        winner = await self.profile_ledger_fixture(1, confidence=90, finalized_at=self.now)
        await self.profile_ledger_fixture(4, confidence=100, outcome=Outcome.NO)
        result = await self.app.profile_card(self.other)
        self.assertEqual(result["highlight"]["forecastId"], winner.forecast_id)
        self.assertEqual(result["highlight"]["confidence"], 90)
        self.assertEqual(result["highlight"]["specificationHash"], winner.specification_hash)
        self.assertIn("selected example", result["methodology"]["highlight"])

    async def test_profile_card_translation_is_hash_bound_and_scope_excludes_other_users_and_secrets(self):
        forecast = await self.profile_ledger_fixture(0)
        await self.profile_ledger_fixture(1, user_id=self.uid, confidence=100)
        card = await self.app._card(forecast.forecast_id)
        translation = {**await self.translation_body(card), "title": "A verified English display title"}
        await self.app.set_translation(forecast.forecast_id, translation)
        result = await self.app.profile_card(self.other)
        self.assertEqual(result["user"]["id"], self.other)
        self.assertEqual(set(result["user"]), {"id", "displayName", "handle", "createdAt"})
        self.assertEqual(result["metrics"]["totalForecasts"], 1)
        self.assertEqual(result["history"][0]["title"], translation["title"])
        self.assertEqual(result["highlight"]["title"], translation["title"])
        for forbidden in ("recovery_hash", "recoveryCode", "sessionToken", "wallet", "profile-record-0001"):
            self.assertNotIn(forbidden, result["canonicalJson"])
        with self.assertRaises(AppError) as caught:
            await self.app.profile_card("unknown-profile")
        self.assertEqual(caught.exception.status, 404)

    async def test_profile_card_uses_one_database_snapshot_despite_concurrent_ledger_growth(self):
        await self.profile_ledger_fixture(0)
        original_first = self.db.first
        reads = 0

        async def read_then_mutate(sql, params=()):
            nonlocal reads
            reads += 1
            row = await original_first(sql, params)
            await self.profile_ledger_fixture(1, outcome=Outcome.NO)
            return row

        self.db.first = read_then_mutate
        snapshot = await self.app.profile_card(self.other)
        self.db.first = original_first
        self.assertEqual(reads, 1)
        self.assertEqual(snapshot["metrics"]["totalForecasts"], 1)
        self.assertEqual(snapshot["metrics"]["resolvedForecasts"], 1)
        self.assertEqual(len(snapshot["history"]), 1)
        self.assertEqual((await self.app.profile_card(self.other))["metrics"]["resolvedForecasts"], 2)

    async def test_profile_card_is_not_public_until_owner_publication_and_hash_is_reproducible(self):
        await self.profile_ledger_fixture(0)
        private = await self.app.profile_card(self.other)
        with self.assertRaises(AppError) as caught:
            await self.app.get_profile_card(private["snapshotHash"])
        self.assertEqual(caught.exception.status, 404)
        shared = await self.app.create_profile_card(self.other)
        canonical_payload = json.loads(shared["canonicalJson"])
        self.assertNotIn("canonicalJson", canonical_payload)
        self.assertNotIn("snapshotHash", canonical_payload)
        actual = hashlib.sha256((shared["commitmentProfile"]["prefix"]+shared["canonicalJson"]).encode("utf-8")).hexdigest()
        self.assertEqual(actual, shared["snapshotHash"])
        self.assertEqual(await self.app.get_profile_card(shared["snapshotHash"]), shared)
        self.assertIn("not an on-chain certificate", shared["methodology"]["commitment"])
        with self.assertRaises(AppError):
            await self.app.create_profile_card("unknown-profile")

    async def test_published_profile_card_remains_unchanged_after_later_outcomes_and_profile_edits(self):
        await self.profile_ledger_fixture(0)
        shared = await self.app.create_profile_card(self.other)
        self.now += 1000
        await self.profile_ledger_fixture(1, outcome=Outcome.NO)
        await self.app.update_profile(self.other, "Updated public display name")
        self.assertEqual(await self.app.get_profile_card(shared["snapshotHash"]), shared)
        fresh = await self.app.create_profile_card(self.other)
        self.assertNotEqual(fresh["snapshotHash"], shared["snapshotHash"])
        self.assertEqual(fresh["metrics"]["resolvedForecasts"], 2)
        self.assertEqual(shared["metrics"]["resolvedForecasts"], 1)

    async def test_public_profile_card_denies_private_artifact_kinds_and_rejects_tampering(self):
        snapshot = await self.app.profile_card(self.other)
        await self.db.execute("INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
            (snapshot["snapshotHash"], "private-ai-review", snapshot["canonicalJson"], "application/json", self.now))
        for identifier in (snapshot["snapshotHash"], "unknown", "f"*64):
            with self.subTest(identifier=identifier), self.assertRaises(AppError) as caught:
                await self.app.get_profile_card(identifier)
            self.assertEqual(caught.exception.status, 404)
        self.now += 1
        published = await self.app.create_profile_card(self.other)
        original_first = self.db.first

        async def corrupted_read(sql, params=()):
            row = await original_first(sql, params)
            if "kind='profile-share-snapshot'" in sql and row:
                return {**row, "body": row["body"].replace('"totalForecasts":0', '"totalForecasts":999')}
            return row

        self.db.first = corrupted_read
        with self.assertRaises(AppError) as caught:
            await self.app.get_profile_card(published["snapshotHash"])
        self.assertEqual(caught.exception.code, "profile_card_integrity_failed")
        self.db.first = original_first

    async def test_finalized_but_unscored_forecasts_do_not_claim_personal_score_completion(self):
        await self.profile_ledger_fixture(0, scored=False)
        snapshot = await self.app.profile_card(self.other)
        self.assertEqual(snapshot["metrics"]["totalForecasts"], 1)
        self.assertEqual(snapshot["metrics"]["resolvedForecasts"], 0)
        self.assertEqual(snapshot["history"], [])

    async def test_translation_localizes_display_without_mutating_any_canonical_commitment(self):
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        before = await self.app.integrity(card["id"])
        original = dumps(await self.app._forecast(card["id"]))
        translation = await self.translation_body(card)
        saved = await self.app.set_translation(card["id"], translation)
        self.assertEqual(saved["forecast"]["title"], translation["title"])
        self.assertEqual(saved["forecast"]["translationLanguage"], "en")
        self.assertEqual(saved["forecast"]["sourceLanguage"], "ko")
        detail = await self.app.forecast_detail(card["id"])
        self.assertEqual(detail["displayTranslation"]["rules"], translation["rules"])
        self.assertEqual(detail["displayTranslation"]["attribution"], "Forecast editorial translation")
        self.assertEqual(detail["forecast"]["specification"]["canonicalQuestion"], card["question"])
        self.assertEqual(await self.app.integrity(card["id"]), before)
        self.assertEqual(dumps(await self.app._forecast(card["id"])), original)
        self.assertEqual((await self.app.list_forecasts(q="Product X"))["items"][0]["id"], card["id"])

    async def test_translation_retries_preserve_timestamp_and_corrections_keep_admin_audit(self):
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        translation = await self.translation_body(card)
        first = await self.app.set_translation(card["id"], translation)
        self.now += 10
        retried = await self.app.set_translation(card["id"], translation)
        self.assertEqual(first["displayTranslation"], retried["displayTranslation"])
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM forecast_translation_audit"))["n"], 1)
        await self.app.set_translation(card["id"], {**translation, "title": "Will Product X be announced?"})
        self.now += 10
        await self.app.set_translation(card["id"], translation)
        audit = await self.db.all("SELECT actor,attribution FROM forecast_translation_audit")
        self.assertEqual(len(audit), 3)
        self.assertEqual({row["actor"] for row in audit}, {"authenticated_admin"})
        self.assertEqual({row["attribution"] for row in audit}, {"Forecast editorial translation"})
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE forecast_translation_audit SET attribution='AI translation'")

    async def test_translation_rejects_private_drafts_wrong_hash_and_changed_clause_topology(self):
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        translation = await self.translation_body(card)
        draft = await self.app.compile_forecast(self.other, "Will another product launch before 2030?")
        with self.assertRaises(AppError) as caught:
            await self.app.set_translation(draft["draftId"], translation)
        self.assertEqual(caught.exception.status, 404)
        invalid_cases = [
            {**translation, "specificationHash": "a"*64},
            {**translation, "rules": translation["rules"][:-1]},
            {**translation, "rules": list(reversed(translation["rules"]))},
            {**translation, "rules": [{**translation["rules"][0], "clauseId": "invented"}, *translation["rules"][1:]]},
            {**translation, "invalidationRules": []},
            {**translation, "title": "아직 번역되지 않은 제목"},
            {**translation, "language": "fr"},
            {**translation, "attribution": "AI independently verified"},
            {**translation, "closeAt": self.now},
        ]
        for invalid_translation in invalid_cases:
            with self.subTest(value=invalid_translation), self.assertRaises(AppError):
                await self.app.set_translation(card["id"], invalid_translation)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM forecast_translations"))["n"], 0)

    async def test_translation_preserves_ai_numbers_and_attributes_only_its_rationale(self):
        self.ai.ai_forecast = {"probability": 42, "provider": "test-provider", "model": "test-model",
                               "rationale": "공식 일정에 따른 추정입니다."}
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        translation = {**await self.translation_body(card), "aiRationale": "An estimate based on the official schedule."}
        result = await self.app.set_translation(card["id"], translation)
        self.assertEqual(result["forecast"]["ai"]["probability"], 42)
        self.assertEqual(result["forecast"]["ai"]["provider"], "test-provider")
        self.assertEqual(result["forecast"]["ai"]["rationale"], translation["aiRationale"])
        self.assertEqual(result["forecast"]["ai"]["rationaleAttribution"], "Forecast editorial translation")
        raw = await self.db.first("SELECT ai_forecast FROM forecasts WHERE id=?", (card["id"],))
        self.assertEqual(json.loads(raw["ai_forecast"])["rationale"], "공식 일정에 따른 추정입니다.")

    async def test_translation_does_not_manufacture_an_absent_ai_rationale(self):
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        translation = {**await self.translation_body(card), "aiRationale": "An invented rationale."}
        with self.assertRaises(AppError):
            await self.app.set_translation(card["id"], translation)

    async def test_translation_batch_failure_rolls_back_audit_and_projection(self):
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        translation = await self.translation_body(card)
        await self.db.execute("CREATE TRIGGER fail_translation BEFORE INSERT ON forecast_translations "
                              "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.app.set_translation(card["id"], translation)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM forecast_translation_audit"))["n"], 0)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM mutation_guards"))["n"], 0)

    async def test_activity_uses_english_machine_text_and_translated_forecast_title(self):
        await self.app.follow(self.other, self.uid, True)
        card = await self.publish("아크메가 마감 전에 신제품을 공식 발표할까요?")
        translation = await self.translation_body(card)
        await self.app.set_translation(card["id"], translation)
        item = (await self.app.activity(self.other))["items"][0]
        self.assertEqual(item["title"], "New forecast from a creator you follow")
        self.assertEqual(item["body"], translation["title"])

    async def test_original_input_is_not_stripped_before_durable_ai_capture(self):
        original = "  Will Acme announce Product X before 2030?  "
        await self.app.compile_forecast(self.uid, original)
        self.assertEqual(self.ai.last_question, original)

    async def test_all_feed_modes_search_pagination_and_creator_view(self):
        card = await self.publish()
        await self.app.follow(self.other, self.uid, True)
        for sort in ("newest", "trending", "ending", "ai-gap", "following"):
            result = await self.app.list_forecasts(self.other, q="Acme", category="technology", sort=sort)
            self.assertEqual(result["items"][0]["id"], card["id"])
            self.assertEqual(result["counts"]["active"], 1)
        self.assertEqual((await self.app.list_forecasts(q="%"))["items"], [])
        self.assertEqual((await self.app.list_forecasts(category="science"))["items"], [])
        creator = await self.app.creator(self.uid, self.other)
        self.assertTrue(creator["isFollowing"])
        self.assertEqual(creator["creator"]["followerCount"], 1)
        self.assertEqual(creator["creator"]["marketsCreated"], 1)
        for cursor in ("-1", "bad", "100001"):
            with self.assertRaises(AppError):
                await self.app.list_forecasts(cursor=cursor)

    async def test_daily_selection_stable_and_contains_only_open_real_ids(self):
        card = await self.publish()
        first = await self.app.list_forecasts(self.uid)
        second = await self.app.list_forecasts(self.uid)
        self.assertEqual(first["dailyIds"], [card["id"]])
        self.assertEqual(first["dailyIds"], second["dailyIds"])
        self.now = card["closeAt"]
        self.assertEqual((await self.app.list_forecasts())["dailyIds"], [])

    async def test_latest_vote_updates_aggregate_but_retains_history(self):
        card = await self.publish()
        first = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "vote-key-first")
        second = await self.app.submit_forecast(self.other, card["id"], "NO", 70, 3, "vote-key-next")
        self.assertEqual(first["forecast"]["crowd"], {"probability": 80.0, "count": 1})
        self.assertEqual(second["forecast"]["crowd"], {"probability": 30.0, "count": 1})
        detail = await self.app.forecast_detail(card["id"], self.other)
        self.assertEqual([h["probability"] for h in detail["history"]], [80, 30])
        self.assertEqual(detail["myForecast"]["outcome"], "NO")
        self.assertEqual((await self.app.me(self.other))["reputation"]["totalForecasts"], 1)

    async def test_vote_retry_preserves_original_acceptance_after_other_mutations(self):
        card = await self.publish()
        original = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "vote-retry-key")
        await self.app.submit_forecast(self.uid, card["id"], "NO", 60, 3, "another-vote-key")
        self.now += 50
        retry = await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "vote-retry-key")
        self.assertEqual(retry["myForecast"], original["myForecast"])
        self.assertEqual(retry["forecast"]["revision"], 4)
        self.assertEqual(retry["forecast"]["crowd"]["count"], 2)
        count = await self.db.first("SELECT COUNT(*) AS n FROM forecast_history")
        self.assertEqual(count["n"], 2)

    async def test_idempotency_key_cannot_change_payload_or_actor(self):
        card = await self.publish()
        await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "shared-request-key")
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 90, 2, "shared-request-key")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        other = await self.app.submit_forecast(self.uid, card["id"], "NO", 80, 3, "shared-request-key")
        self.assertEqual(other["forecast"]["crowd"]["count"], 2)

    async def test_stale_revision_and_expired_vote_leave_snapshot_unchanged(self):
        card = await self.publish()
        before = dumps(await self.app._forecast(card["id"]))
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], "YES", 80, 1, "stale-vote-key")
        self.assertEqual(caught.exception.status, 409)
        self.now = card["closeAt"]
        with self.assertRaises(AppError):
            await self.app.submit_forecast(self.other, card["id"], "YES", 80, 2, "late-vote-key")
        self.assertEqual(dumps(await self.app._forecast(card["id"])), before)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM user_forecasts"))["n"], 0)

    async def test_concurrent_votes_accept_one_and_never_leak_second_projection(self):
        card = await self.publish()
        results = await asyncio.gather(
            self.app.submit_forecast(self.uid, card["id"], "YES", 50, 2, "race-vote-one"),
            self.app.submit_forecast(self.other, card["id"], "NO", 50, 2, "race-vote-two"),
            return_exceptions=True)
        self.assertEqual(sum(isinstance(item, AppError) for item in results), 1)
        self.assertEqual((await self.app._card(card["id"]))["crowd"]["count"], 1)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM forecast_history"))["n"], 1)

    async def test_zero_row_cas_aborts_every_statement_in_batch(self):
        card = await self.publish()
        stale = await self.app._forecast(card["id"])
        await self.app.submit_forecast(self.uid, card["id"], "YES", 80, 2, "first-cas-key")
        choice = fixtures.user_forecast(stale.specification, forecaster_id=self.other,
            forecast_id=card["id"], submitted_at_ms=self.now)
        with self.assertRaises(AppError):
            await self.app._mutate(stale, SubmitForecast(user_forecast=choice), key="stale-cas-key",
                extra=(("INSERT INTO comments(id,forecast_id,user_id,body,created_at) VALUES(?,?,?,?,?)",
                        ("should-not-exist", card["id"], self.other, "bad", self.now)),))
        self.assertIsNone(await self.db.first("SELECT * FROM comments WHERE id='should-not-exist'"))
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM mutation_guards"))["n"], 0)

    async def test_failure_after_cas_rolls_back_snapshot_event_receipt_and_projection(self):
        card = await self.publish()
        before = dumps(await self.app._forecast(card["id"]))
        forecast = loads(Forecast, before)
        choice = fixtures.user_forecast(forecast.specification, forecast_id=card["id"],
                                        forecaster_id=self.other, submitted_at_ms=self.now)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.app._mutate(forecast, SubmitForecast(user_forecast=choice), key="rollback-key",
                extra=(("INSERT INTO mutation_guards(token,valid) VALUES(?,0)", ("force-failure",)),))
        self.assertEqual(dumps(await self.app._forecast(card["id"])), before)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM events"))["n"], 2)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM command_receipts"))["n"], 2)

    async def test_published_draft_retry_and_duplicate_specification(self):
        draft = await self.app.compile_forecast(self.uid, "Will Acme officially announce Product X before 2030?")
        first = await self.app.publish_forecast(self.uid, draft["draftId"], "publish-first-key")
        retry = await self.app.publish_forecast(self.uid, draft["draftId"], "publish-first-key")
        self.assertEqual(first["forecast"]["id"], retry["forecast"]["id"])
        duplicate = await self.app.compile_forecast(self.other, "Will Acme officially announce Product X before 2030?")
        with self.assertRaises(AppError) as caught:
            await self.app.publish_forecast(self.other, duplicate["draftId"], "publish-duplicate-key")
        self.assertEqual(caught.exception.code, "duplicate_forecast")

    async def test_draft_ownership_and_expiry_are_enforced(self):
        draft = await self.app.compile_forecast(self.uid, "Will Acme officially announce Product X before 2030?")
        with self.assertRaises(AppError):
            await self.app.publish_forecast(self.other, draft["draftId"], "publish-stolen-key")
        self.now = draft["expiresAt"]
        with self.assertRaises(AppError) as caught:
            await self.app.publish_forecast(self.uid, draft["draftId"], "publish-expired-key")
        self.assertEqual(caught.exception.status, 410)

    async def test_comments_follow_notifications_and_read_state_are_idempotent(self):
        await self.app.follow(self.other, self.uid, True)
        await self.app.follow(self.other, self.uid, True)
        card = await self.publish()
        comment = await self.app.add_comment(self.other, card["id"], "근거를 공유합니다.", "comment-key-123")
        retry = await self.app.add_comment(self.other, card["id"], "근거를 공유합니다.", "comment-key-123")
        self.assertEqual(comment, retry)
        self.assertEqual((await self.app._card(card["id"]))["commentCount"], 1)
        activity = (await self.app.activity(self.other))["items"]
        self.assertEqual(len(activity), 1)
        await self.app.read_activity(self.other)
        self.assertEqual((await self.app.activity(self.other))["items"][0]["readAt"], self.now)

    async def test_share_counts_unique_authenticated_daily_intentions_only(self):
        card = await self.publish()
        await self.app.record_share(card["id"])
        self.assertEqual((await self.app._card(card["id"]))["shareCount"], 0)
        await self.app.record_share(card["id"], self.other)
        await self.app.record_share(card["id"], self.other)
        self.assertEqual((await self.app._card(card["id"]))["shareCount"], 1)

    async def test_full_scheduler_finalizes_after_challenge_scores_and_notifies_once(self):
        forecast = await self.challenge()
        self.now = forecast.challenge_until_ms-1
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(forecast.forecast_id)).state, LifecycleState.CHALLENGE)
        self.now += 1
        result = await self.app.run_due_jobs()
        self.assertEqual(result["failed"], 0)
        self.assertEqual((await self.app._forecast(forecast.forecast_id)).state, LifecycleState.FINALIZED)
        reputation = await self.app.reputation(self.other)
        self.assertEqual(reputation["resolvedForecasts"], 1)
        self.assertAlmostEqual(reputation["brierScore"], 0.04)
        self.assertEqual(reputation["accuracy"], 100)
        self.assertEqual(len((await self.app.activity(self.other))["items"]), 1)
        await self.app.run_due_jobs()
        self.assertEqual(len((await self.app.activity(self.other))["items"]), 1)
        chain = await self.db.first("SELECT status FROM outbox WHERE kind='RESOLUTION_COMMITMENT_REQUIRED'")
        self.assertEqual(chain["status"], "awaiting_adapter")

    async def held_by_an_indeterminable_review(self, outcome):
        """Drive a forecast into the state two forecasts were stuck in for a day.

        The evidence is authentic and its publication time cannot be placed, which is the
        review reason no retry can clear. Time advances well past the retry backoff so the
        scheduler, not the test, decides how many attempts it takes.
        """
        forecast = await self.publish()
        await self.app.submit_forecast(self.other, forecast["id"], "YES", 80, forecast["revision"], "held-vote", 300)
        self.now = forecast["closeAt"]+1000
        self.ai.publication = "none"
        self.ai.outcome = outcome
        accounts = await self.db.first("SELECT available, committed FROM point_accounts WHERE user_id=?", (self.other,))
        for _ in range(5):
            await self.app.run_due_jobs()
            record = await self.app._forecast(forecast["id"])
            if record.state == LifecycleState.FINALIZED:
                break
            # Long enough to clear both the six-hour retry cap and the 48-hour challenge
            # window, so the number of attempts is the scheduler's decision, not the test's.
            self.now = max(self.now, record.challenge_until_ms or 0) + 3*24*60*60*1000
        return record, accounts

    async def test_a_forecast_deadlocked_by_an_indeterminable_review_finalizes_invalid(self):
        record, accounts = await self.held_by_an_indeterminable_review(Outcome.INVALID)

        self.assertEqual(record.state, LifecycleState.FINALIZED)
        self.assertEqual(record.finalized_outcome, Outcome.INVALID)
        closure = await self.db.first("SELECT determination, reason FROM resolution_timing_closures WHERE forecast_id=?",
                                      (record.forecast_id,))
        self.assertEqual((closure["determination"], closure["reason"]), ("INVALID", "publication_time_unknown"))
        self.assertIsNone(await self.db.first("SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (record.forecast_id,)))
        # Nothing was credited from evidence that could not be placed relative to participation.
        score = await self.db.first("SELECT correct, brier_score FROM reputation_scores WHERE forecast_id=?", (record.forecast_id,))
        self.assertIsNone(score["correct"])
        self.assertIsNone(score["brier_score"])
        # The stake came back, because INVALID takes nothing from anyone.
        after = await self.db.first("SELECT available, committed FROM point_accounts WHERE user_id=?", (self.other,))
        self.assertEqual(after["committed"], 0)
        self.assertGreater(after["available"], accounts["available"])

    async def test_the_same_review_does_not_licence_a_reward(self):
        # The closure released the blocker, not the evidence. A rewarded outcome stays
        # refused, so the forecast retries rather than paying out on an unplaceable source.
        record, _ = await self.held_by_an_indeterminable_review(Outcome.YES)
        self.assertEqual(record.state, LifecycleState.RESOLVING)
        self.assertIsNone(record.finalized_outcome)
        row = await self.db.first("SELECT job_error FROM forecasts WHERE id=?", (record.forecast_id,))
        self.assertIn("determines INVALID", row["job_error"])
        score = await self.db.first("SELECT 1 FROM reputation_scores WHERE forecast_id=?", (record.forecast_id,))
        self.assertIsNone(score)

    async def test_finalization_race_emits_one_set_of_effects(self):
        forecast = await self.challenge()
        self.now = forecast.challenge_until_ms
        results = await asyncio.gather(
            self.app._mutate(forecast, Finalize(), key="final-race-one"),
            self.app._mutate(forecast, Finalize(), key="final-race-two"), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, AppError) for item in results), 1)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM outbox"))["n"], 3)
        await self.app._process_outbox(10)
        await self.app._process_outbox(10)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM reputation_scores"))["n"], 1)

    async def test_provider_outage_pauses_and_real_success_recovers(self):
        card = await self.publish()
        self.now = card["closeAt"]+1000
        self.ai.fail = True
        result = await self.app.run_due_jobs()
        self.assertEqual(result["failed"], 1)
        paused = await self.app._forecast(card["id"])
        self.assertEqual(paused.state, LifecycleState.PAUSED)
        self.assertIsNone(paused.finalized_outcome)
        self.now += 60000
        self.ai.fail = False
        recovered = await self.app.run_due_jobs()
        self.assertEqual(recovered["failed"], 0)
        self.assertEqual((await self.app._forecast(card["id"])).state, LifecycleState.PROPOSED)
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(card["id"])).state, LifecycleState.CHALLENGE)

    async def test_unresolved_initial_evidence_never_becomes_a_proposal_or_final(self):
        card = await self.publish()
        self.now = card["closeAt"]+1000
        self.ai.reject = True
        await self.app.run_due_jobs()
        value = await self.app._forecast(card["id"])
        self.assertEqual(value.state, LifecycleState.RESOLVING)
        self.assertIsNone(value.resolution)
        self.assertIsNone(value.finalized_outcome)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM outbox"))["n"], 0)

    async def test_dispute_retention_obeys_original_deadline_and_scores_only_after_finalization(self):
        forecast = await self.challenge()
        self.now += 10
        submitted = await self.app.submit_dispute(self.uid, forecast.forecast_id, "제품 식별이 다릅니다.",
            "https://acme.example/news", "yes-rule", "원래 규칙을 다시 검토해야 합니다.",
            forecast.revision, "dispute-valid-key")
        self.assertEqual(submitted["forecast"]["state"], "DISPUTED")
        self.now += 10
        self.assertEqual((await self.app.run_due_jobs())["failed"], 0)
        retained = await self.app._forecast(forecast.forecast_id)
        self.assertEqual(retained.state, LifecycleState.CHALLENGE)
        self.assertEqual(retained.challenge_until_ms, forecast.challenge_until_ms)
        self.assertEqual((await self.app.reputation(self.other))["resolvedForecasts"], 0)

    async def test_material_dispute_escalates_without_automatic_biased_finalization(self):
        forecast = await self.challenge()
        self.now += 10
        await self.app.submit_dispute(self.uid, forecast.forecast_id, "공식 발표가 잘못 해석되었습니다.",
            "https://acme.example/news", "yes-rule", "원래 규칙과 충돌하는 문서입니다.",
            forecast.revision, "dispute-material-key")
        self.now += 10
        self.ai.material = True
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(forecast.forecast_id)).state, LifecycleState.ESCALATED)
        self.now += 2*CHALLENGE_MS
        await self.app.run_due_jobs()
        self.assertIsNone((await self.app._forecast(forecast.forecast_id)).finalized_outcome)

    async def test_late_dispute_fails_without_fetch_or_snapshot_change(self):
        forecast = await self.challenge()
        self.now = forecast.challenge_until_ms
        before = dumps(forecast)
        with self.assertRaises(AppError) as caught:
            await self.app.submit_dispute(self.uid, forecast.forecast_id, "늦은 이의 제기입니다.",
                "https://acme.example/news", "yes-rule", "마감이 지난 근거입니다.", forecast.revision, "late-dispute-key")
        self.assertEqual(caught.exception.code, "challenge_closed")
        self.assertEqual(dumps(await self.app._forecast(forecast.forecast_id)), before)

    async def test_expired_job_lease_cannot_commit(self):
        card = await self.publish()
        forecast = await self.app._forecast(card["id"])
        await self.db.execute("UPDATE forecasts SET job_token='old',job_until=? WHERE id=?", (self.now-1, card["id"]))
        choice = fixtures.user_forecast(forecast.specification, forecast_id=card["id"],
                                        forecaster_id=self.other, submitted_at_ms=self.now)
        with self.assertRaises(AppError):
            await self.app._mutate(forecast, SubmitForecast(user_forecast=choice), key="expired-lease", job_token="old")
        self.assertEqual((await self.app._forecast(card["id"])).revision, 2)

    async def test_rate_limit_and_ai_lease_prevent_duplicate_expensive_calls(self):
        await self.app.rate_limit("test-scope", 1, DAY_MS)
        with self.assertRaises(AppError) as caught:
            await self.app.rate_limit("test-scope", 1, DAY_MS)
        self.assertEqual(caught.exception.status, 429)
        self.ai.gate = asyncio.Event()
        task = asyncio.create_task(self.app.compile_forecast(self.uid, "Will Acme announce Product X before 2030?"))
        await asyncio.sleep(0)
        with self.assertRaises(AppError) as caught:
            await self.app.compile_forecast(self.uid, "Will Acme announce Product Y before 2030?")
        self.assertEqual(caught.exception.code, "ai_work_in_progress")
        self.assertEqual(self.ai.calls, 1)
        self.ai.gate.set()
        await task

    async def test_failed_compilation_releases_lease_and_does_not_publish(self):
        self.ai.fail = True
        with self.assertRaises(AppError):
            await self.app.compile_forecast(self.uid, "Will Acme announce Product X before 2030?")
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM drafts"))["n"], 0)
        self.assertIsNone(await self.db.first("SELECT * FROM ai_leases"))

    async def test_safe_ai_errors_distinguish_source_output_deadline_and_semantic_failures(self):
        private_detail = "https://private.example/prompt?api_key=secret-private-user-question"
        cases = (
            ("source_rejected", 502, "source_not_usable", "official text page"),
            ("ai_output_incomplete", 502, "ai_response_invalid", "try again"),
            ("ai_output_json", 502, "ai_response_invalid", "try again"),
            ("ai_output_size", 502, "ai_response_invalid", "try again"),
            ("compiler_domain_validation", 502, "ai_response_invalid", "try again"),
            ("compiler_deadline_timezone", 422, "deadline_clarification_required", "time zone"),
            ("compiler_deadline_mismatch", 422, "deadline_clarification_required", "deadline"),
            ("compiler_not_publishable", 422, "specification_needs_review", "Clarify"),
            ("ai_rejected", 502, "ai_review_incomplete", "try again"),
            (private_detail, 502, "ai_review_incomplete", "try again"),
        )
        for reason, status, public_code, instruction in cases:
            with self.subTest(reason=reason):
                error = self.app._ai_error(AIRejected(private_detail, code=reason))
                self.assertEqual(error.status, status)
                self.assertEqual(error.code, public_code)
                self.assertIn(instruction, error.message)
                self.assertNotIn(private_detail, error.message)
                self.assertNotIn("secret", error.message)
                self.assertNotEqual(error.code, "not_objectively_resolvable")

    async def test_source_transport_outage_is_not_mislabeled_as_semantic_question_failure(self):
        failure = Artifact("a"*64, "source-failure", "private source diagnostic")
        unavailable = AIUnavailable("https://private.example/internal", (failure,))
        error = self.app._ai_error(unavailable)
        self.assertEqual(error.status, 503)
        self.assertEqual(error.code, "source_temporarily_unavailable")
        self.assertIn("official text page", error.message)
        self.assertNotIn("private", error.message)
        direct = AIUnavailable("private transport details")
        direct.__cause__ = SourceUnavailable("private transport details")
        self.assertEqual(self.app._ai_error(direct).code, "source_temporarily_unavailable")
        provider = self.app._ai_error(AIUnavailable("private provider credential error"))
        self.assertEqual(provider.code, "ai_unavailable")
        self.assertNotIn("private", provider.message)

    async def test_global_ai_budget_blocks_provider_call_and_releases_lease(self):
        await self.db.execute("INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES(?,?,?,?)",
            ("ai:global", self.now//DAY_MS, MAX_DAILY_AI_CALLS, self.now+DAY_MS))
        with self.assertRaises(AppError) as caught:
            await self.app.compile_forecast(self.uid, "Will Acme announce Product X before 2030?")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.ai.calls, 0)
        self.assertIsNone(await self.db.first("SELECT * FROM ai_leases"))

    async def test_partial_provider_or_source_outage_does_not_claim_full_outage(self):
        card = await self.publish()
        self.now = card["closeAt"]+1000
        self.ai.fail = True
        self.ai.unavailable_providers = ("provider-a",)
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(card["id"])).state, LifecycleState.RESOLVING)
        self.assertIsNotNone((await self.app.forecast_detail(card["id"]))["forecast"]["pauseReason"])

    async def test_dispute_provider_recovery_extends_challenge_window(self):
        forecast = await self.challenge()
        self.now += 10
        await self.app.submit_dispute(self.uid, forecast.forecast_id, "다른 문서가 있습니다.",
            "https://acme.example/news", "yes-rule", "관련 판정 규칙의 재검토가 필요합니다.",
            forecast.revision, "recovery-dispute-key")
        self.now += 10
        self.ai.fail = True
        await self.app.run_due_jobs()
        paused = await self.app._forecast(forecast.forecast_id)
        self.assertEqual(paused.state, LifecycleState.PAUSED)
        pause_time = self.now
        self.now += 60000
        self.ai.fail = False
        await self.app.run_due_jobs()
        recovered = await self.app._forecast(forecast.forecast_id)
        self.assertEqual(recovered.state, LifecycleState.DISPUTED)
        self.assertEqual(recovered.challenge_until_ms, forecast.challenge_until_ms+self.now-pause_time)
        await self.app.run_due_jobs()
        self.assertEqual((await self.app._forecast(forecast.forecast_id)).state, LifecycleState.CHALLENGE)

    async def test_invalid_types_are_rejected_as_client_errors(self):
        card = await self.publish()
        with self.assertRaises(AppError) as caught:
            await self.app.submit_forecast(self.other, card["id"], [], 50, 2, "invalid-choice-key")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(AppError):
            await self.app.submit_forecast(self.other, card["id"], "YES", True, 2, "invalid-bool-key")
        with self.assertRaises(AppError):
            await self.app.publish_forecast(self.uid, {}, "invalid-draft-key")

    async def test_editorial_seed_retries_before_new_ai_calls_and_cannot_be_name_hijacked(self):
        await self.app.update_profile(self.uid, "Forecast Editorial")
        question = "Will Acme officially announce Product X before the deadline?"
        first = await self.app.seed(question)
        original_calls = self.ai.calls
        self.now += 100
        second = await self.app.seed(question)
        self.assertEqual(first["forecast"]["id"], second["forecast"]["id"])
        self.assertEqual(self.ai.calls, original_calls)
        self.assertEqual(first["forecast"]["creator"]["id"], "system_editorial")
        self.assertEqual(first["forecast"]["crowd"]["count"], 0)
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM drafts"))["n"], 1)

    async def test_long_valid_ai_workflow_commits_within_lease_and_does_not_repeat(self):
        card = await self.publish()
        self.now = card["closeAt"]+1000

        def advance_long_call():
            self.now += 200000  # Previously exceeded the old 180-second lease.

        self.ai.on_resolution = advance_long_call
        result = await self.app.run_due_jobs()
        self.assertEqual(result["failed"], 0)
        self.assertEqual((await self.app._forecast(card["id"])).state, LifecycleState.CHALLENGE)
        completed_calls = self.ai.calls
        await self.app.run_due_jobs()
        self.assertEqual(self.ai.calls, completed_calls)

    async def test_ai_result_past_overall_cap_is_rejected_before_proposal_and_backed_off(self):
        card = await self.publish()
        self.now = card["closeAt"]+1000

        def exceed_workflow_cap():
            self.now += 241000

        self.ai.on_resolution = exceed_workflow_cap
        result = await self.app.run_due_jobs()
        self.assertEqual(result["failed"], 1)
        forecast = await self.app._forecast(card["id"])
        self.assertEqual(forecast.state, LifecycleState.RESOLVING)
        self.assertIsNone(forecast.resolution)
        failed_calls = self.ai.calls
        await self.app.run_due_jobs()
        self.assertEqual(self.ai.calls, failed_calls)
        self.assertIsNone(await self.db.first("SELECT * FROM ai_leases"))

    async def test_overall_timeout_cancels_provider_and_releases_compile_lease(self):
        self.ai.gate = asyncio.Event()
        with patch("forecast_application.service.AI_WORKFLOW_TIMEOUT_SECONDS", 0.001):
            with self.assertRaises(AppError) as caught:
                await self.app.compile_forecast(self.uid, "Will Acme announce Product X before 2030?")
        self.assertEqual(caught.exception.code, "ai_workflow_timeout")
        self.assertIsNone(await self.db.first("SELECT * FROM ai_leases"))
        self.assertEqual((await self.db.first("SELECT COUNT(*) AS n FROM drafts"))["n"], 0)

    async def test_large_korean_candidate_corpus_is_bounded_before_provider_calls(self):
        from forecast_application.ai import MAX_CANDIDATE_CONTEXT_BYTES, MAX_CANDIDATES

        question = "애플이 2030년 전에 새 제품을 공식 발표할까요?"
        # Synthetic records exist only in this test database. Every snapshot is
        # constructed with the public domain engine and strict commitments.
        for number in range(151):
            identifier = "corpus-" + str(number)
            canonical = question if number == 0 else f"독립 기업 {number}이 신제품을 발표할까요?"
            spec = fixtures.specification(canonical_question=canonical,
                invalidation_rules=("공개된 공식 근거가 없으면 판정을 보류합니다. "*150,),
                open_at_ms=self.now, close_at_ms=self.now+100000)
            aggregate = create_forecast(forecast_id=identifier, creator_id=self.uid,
                                        specification=spec, now_ms=self.now)
            validating = apply_command(aggregate, Command(idempotency_key="validation-"+str(number),
                expected_revision=0, payload=BeginValidation()), now_ms=self.now).forecast
            published = apply_command(validating, Command(idempotency_key="publish-"+str(number),
                expected_revision=1, payload=Publish(assessment=fixtures.validation(spec, validated_at_ms=self.now))),
                now_ms=self.now).forecast
            await self.db.execute(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
                "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, self.uid, "draft-"+str(number), dumps(published), 2, "OPEN", "TECHNOLOGY",
                 spec.share_title, canonical, canonical.casefold(), spec.specification_hash,
                 spec.open_at_ms, spec.close_at_ms, self.now+number, self.now, "test-fixture"))
        result = await self.app.compile_forecast(self.other, question)
        self.assertIsNotNone(result["draftId"])
        self.assertGreater(len(self.ai.last_candidates), 0)
        self.assertLessEqual(len(self.ai.last_candidates), MAX_CANDIDATES)
        payload = [{"forecast_id": candidate.forecast_id,
                    "specification_hash": candidate.specification_hash,
                    "specification": to_dict(candidate.specification)}
                   for candidate in self.ai.last_candidates]
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()),
                             MAX_CANDIDATE_CONTEXT_BYTES)
        # The oldest exact match wins over 150 newer unrelated publications.
        self.assertEqual(self.ai.last_candidates[0].forecast_id, "corpus-0")
        related = await self.app._candidate_forecasts("애플이 2030년까지 새로운 기기를 선보일까요?")
        self.assertEqual(related[0].forecast_id, "corpus-0")

    async def test_artifact_size_hash_and_immutability_fail_closed(self):
        bad = Artifact("a"*64, "source", "wrong bytes", "text/plain")
        with self.assertRaises(AppError):
            self.app._artifact_sql((bad,))
        payload = "a"*524289
        oversized = Artifact(hashlib.sha256(payload.encode()).hexdigest(), "source", payload, "text/plain")
        with self.assertRaises(AppError):
            self.app._artifact_sql((oversized,))
        card = await self.publish()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE artifacts SET body='changed' WHERE hash=?", (card["specificationHash"],))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE events SET event='{}' WHERE forecast_id=?", (card["id"],))

    async def test_new_application_instance_reads_same_durable_state(self):
        card = await self.publish()
        restarted = Application(SQLiteDatabase(self.connection), self.ai, now_ms=lambda: self.now,
                                random_token=self.random_token, token_hash=self.token_hash)
        self.assertEqual((await restarted.list_forecasts())["items"][0]["id"], card["id"])
        self.assertEqual((await restarted.authenticate(self.account["sessionToken"]))["id"], self.uid)

    async def test_invalidation_excluded_from_brier_accuracy(self):
        self.ai.outcome = Outcome.INVALID
        forecast = await self.challenge()
        self.now = forecast.challenge_until_ms
        await self.app.run_due_jobs()
        result = await self.app.reputation(self.other)
        self.assertEqual(result["invalidForecasts"], 1)
        self.assertEqual(result["resolvedForecasts"], 0)
        self.assertIsNone(result["brierScore"])
        self.assertIsNone(result["accuracy"])
