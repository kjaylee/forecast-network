"""Real SQLite CAS and actual AI adapter provenance for canonical refresh."""

from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from forecast_application.errors import AppError
from forecast_application.risk_feed import approve_binding, revoke_binding
from forecast_application.risk_refresh import refresh_bound_prediction
from forecast_domain.models import Category, EvidenceSnapshot
from forecast_domain.risk_feed import RiskFeedBinding
from forecast_domain.serialization import from_dict

from tests import test_web_application as fixtures
from tests.test_web_ai import Transport, coordinator, specification


class RiskRefreshTests(unittest.IsolatedAsyncioTestCase):
    random_token = fixtures.ApplicationTests.random_token
    token_hash = staticmethod(fixtures.ApplicationTests.token_hash)
    asyncTearDown = fixtures.ApplicationTests.asyncTearDown
    publish = fixtures.ApplicationTests.publish

    async def asyncSetUp(self):
        await fixtures.ApplicationTests.asyncSetUp(self)
        original_spec = fixtures.fixtures.specification
        policy = specification().source_policy
        with patch.object(fixtures.fixtures, "specification", side_effect=lambda **kw:
                          original_spec(**{**kw, "source_policy": policy})):
            card = await self.publish()
        self.original = await self.db.first("SELECT * FROM forecasts WHERE id=?", (card["id"],))
        self.binding = RiskFeedBinding(binding_id="refresh-test", forecast_id=card["id"],
            specification_hash=self.original["specification_hash"], channel="depegRisk1d", horizon_hours=24,
            asset="USDC", category=Category(self.original["category"]),
            valid_from_ms=self.original["open_at"], valid_until_ms=self.original["close_at"])
        await approve_binding(self.db, feed_id="risk", binding=self.binding,
                              approved_by="test-operator", now_ms=self.now)
        self.now += 1000
        self.transport = Transport([{"yesProbabilityBp": 180, "rationale": "Retained context; substantial tail uncertainty."}])
        self.real_ai = coordinator(self.transport)
        self.app.ai = self.real_ai

    async def refresh(self):
        return await refresh_bound_prediction(self.app, self.binding.binding_id)

    async def row(self):
        return await self.db.first("SELECT * FROM forecasts WHERE id=?", (self.binding.forecast_id,))

    async def test_fresh_sources_and_estimate_retained_without_changing_specification(self):
        result = await self.refresh()
        row = await self.row()
        for field in ("snapshot", "revision", "specification_hash", "open_at", "close_at", "state"):
            self.assertEqual(row[field], self.original[field])
        estimate = json.loads(row["ai_forecast"])
        self.assertEqual(estimate, result["aiForecast"])
        self.assertEqual(estimate["asOf"], self.now)
        self.assertEqual(estimate["probability"], 1.8)
        retained = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (estimate["artifactHash"],))
        self.assertEqual(json.loads(retained["body"])["as_of_ms"], self.now)
        self.assertTrue(self.transport.source_calls)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((await self.db.first("SELECT count(*) n FROM mutation_guards"))["n"], 0)

    async def test_older_estimate_is_preserved_after_a_real_second_refresh(self):
        first = (await self.refresh())["aiForecast"]
        self.now += 1000
        self.transport.outputs.append({"yesProbabilityBp": 210, "rationale": "Newly collected evidence remains uncertain."})
        second = (await self.refresh())["aiForecast"]
        self.assertGreater(second["asOf"], first["asOf"])
        self.assertNotEqual(second["artifactHash"], first["artifactHash"])
        self.assertIsNotNone(await self.db.first("SELECT hash FROM artifacts WHERE hash=?", (first["artifactHash"],)))

    async def test_retained_provenance_distinguishes_collection_evaluation_and_storage(self):
        captured = self.now
        original_text, original_json = self.transport.text, self.transport.json
        async def text(*args):
            value = await original_text(*args)
            self.now += 100
            return value
        async def model(*args):
            value = await original_json(*args)
            self.now += 100
            return value
        self.transport.text = text
        self.transport.json = model
        self.app.ai = coordinator(self.transport)
        result = await self.refresh()
        row = await self.db.first("SELECT body,created_at FROM artifacts WHERE hash=?",
                                  (result["aiForecast"]["artifactHash"],))
        estimate = json.loads(row["body"])
        self.assertEqual(estimate["as_of_ms"], captured+100)
        self.assertEqual(row["created_at"], captured+200)
        provenance = json.loads((await self.db.first("SELECT body FROM artifacts WHERE hash=?",
                                  (estimate["source_provenance_hash"],)))["body"])
        snapshot = from_dict(EvidenceSnapshot, provenance["snapshots"][0])
        self.assertEqual(snapshot.collected_at_ms, captured)
        raw = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (snapshot.content_sha256,))
        self.assertEqual(hashlib.sha256(raw["body"].encode()).hexdigest(), snapshot.content_sha256)
        self.assertEqual(provenance["collection"]["retained_sources"][0]["evidence_hash"], snapshot.evidence_hash)
        decision = json.loads((await self.db.first("SELECT body FROM artifacts WHERE hash=?",
                               (estimate["decision_artifact_hash"],)))["body"])
        self.assertEqual(decision["input"]["source_provenance_hash"], estimate["source_provenance_hash"])

    async def test_lease_takeover_fences_old_response_and_preserves_new_owner(self):
        async def takeover(_):
            await self.db.execute("UPDATE ai_leases SET token='new-owner' WHERE owner=?",
                                  ("risk-prediction:"+self.binding.forecast_id,))
        await self.race_after_prediction(takeover)
        with self.assertRaises(AppError) as error:
            await self.refresh()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual((await self.db.first("SELECT token FROM ai_leases"))["token"], "new-owner")
        self.assertEqual((await self.row())["ai_forecast"], self.original["ai_forecast"])

    async def test_revoked_or_unknown_binding_rejects_before_ai(self):
        await revoke_binding(self.db, binding_id=self.binding.binding_id, revoked_by="test-operator",
                             now_ms=self.now, reason="Withdraw canonical admission")
        with self.assertRaises(AppError) as error:
            await self.refresh()
        self.assertEqual(error.exception.code, "risk_binding_not_current")
        self.assertEqual(self.transport.calls, [])

    async def race_after_prediction(self, mutation):
        async def predict(*args):
            result = await self.real_ai.refresh_prediction(*args)
            self.rejected_artifact = result.ai_forecast["artifactHash"]
            await mutation(result)
            return result
        self.app.ai = SimpleNamespace(refresh_prediction=predict)

    async def test_slow_result_cannot_overwrite_newer_pointer(self):
        async def newer(result):
            self.newer = {**result.ai_forecast, "asOf": self.now+1, "artifactHash": "e"*64}
            await self.db.execute("UPDATE forecasts SET ai_forecast=? WHERE id=?",
                                  (json.dumps(self.newer), self.binding.forecast_id))
        await self.race_after_prediction(newer)
        with self.assertRaises(AppError) as error:
            await self.refresh()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(json.loads((await self.row())["ai_forecast"]), self.newer)
        self.assertIsNone(await self.db.first("SELECT hash FROM artifacts WHERE hash=?", (self.rejected_artifact,)))

    async def test_revocation_during_ai_does_not_install_prediction(self):
        async def revoke(_):
            await revoke_binding(self.db, binding_id=self.binding.binding_id, revoked_by="test-operator",
                                 now_ms=self.now, reason="Revoked while model was in flight")
        await self.race_after_prediction(revoke)
        with self.assertRaises(AppError) as error:
            await self.refresh()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual((await self.row())["ai_forecast"], self.original["ai_forecast"])

    async def test_hold_during_ai_does_not_install_prediction(self):
        async def hold(_):
            await self.app.participation_holds.change(self.binding.forecast_id, {
                "action": "hold", "expectedRevision": 0, "expectedHoldId": None,
                "specificationHash": self.binding.specification_hash, "reason": "known_outcome_review",
                "evidenceUrl": "https://www.apple.com/newsroom/", "idempotencyKey": "refresh-hold-001"})
        await self.race_after_prediction(hold)
        with self.assertRaises(AppError) as error:
            await self.refresh()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual((await self.row())["ai_forecast"], self.original["ai_forecast"])

    async def test_deadline_during_ai_does_not_install_prediction(self):
        async def close(_):
            self.now = self.original["close_at"]
        await self.race_after_prediction(close)
        with self.assertRaises(AppError):
            await self.refresh()
        self.assertEqual((await self.row())["ai_forecast"], self.original["ai_forecast"])

    async def test_unavailable_provider_preserves_last_prediction_without_fallback(self):
        self.transport.outputs = [RuntimeError("temporary provider outage")]
        with self.assertRaises(AppError):
            await self.refresh()
        self.assertEqual((await self.row())["ai_forecast"], self.original["ai_forecast"])
        self.assertEqual((await self.db.first("SELECT count(*) n FROM ai_leases"))["n"], 0)
