"""Exercise actual Worker routing: public absence is honest; signing stays operator-only."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from forecast_application.errors import AppError
from forecast_domain.risk_feed import RiskFeedBinding
from forecast_domain.serialization import content_hash, from_dict, to_dict

from tests.test_risk_feed_contract import golden_payload
from tests.test_web_transport import scheduled_method


class RiskFeedHttpTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, path, *, method="GET", body=None, admin=True, latest=None, retained=None):
        self.body_reads = 0
        self.approve = AsyncMock()
        self.publisher = AsyncMock()
        self.latest = AsyncMock(return_value=latest)
        self.revoker = AsyncMock()
        self.refresher = AsyncMock(return_value={"status": "refreshed"})
        self.db = SimpleNamespace(first=AsyncMock(return_value=retained), execute=AsyncMock())
        self.sign = AsyncMock()
        self.seed = AsyncMock(return_value={"forecast": {"id": "compiled-and-published"}})
        app = SimpleNamespace(db=self.db, now_ms=lambda: 1_800_000_000_000, seed=self.seed)
        worker = SimpleNamespace(env=SimpleNamespace(ADMIN_TOKEN="a" * 64), application=lambda: app,
                                 relayer_public_key=lambda: bytes(range(32)), sign_registry_message=self.sign)

        async def read(_request, _limit):
            self.body_reads += 1
            return json.dumps(body or {}).encode()

        actual = scheduled_method("route_api", Response=Any, AppError=AppError, hmac=hmac, re=re,
            hashlib=hashlib, MAX_PROVIDER_BYTES=524288, MAX_BODY_BYTES=16384, bounded_bytes=read,
            latest_feed=self.latest, approve_binding=self.approve, publish_feed=self.publisher,
            refresh_bound_prediction=self.refresher,
            revoke_binding=self.revoker, RiskFeedBinding=RiskFeedBinding, from_dict=from_dict, to_dict=to_dict,
            content_hash=content_hash, api_response=lambda data, status=200: {"data": data, "status": status})
        headers = {"Content-Type": "application/json", "content-type": "application/json"}
        if admin:
            headers["authorization"] = "Bearer " + "a" * 64
        request = SimpleNamespace(method=method, headers=headers)
        return await actual(worker, request, urlsplit("https://forecast.eastsea.xyz" + path), path)

    async def test_public_unavailable_feed_has_no_fake_envelope_or_signature(self):
        result = await self.call("/api/risk/feeds/stable-risk", admin=False)
        self.assertEqual(result["data"]["status"], "unavailable")
        self.assertIsNone(result["data"]["envelope"])
        self.sign.assert_not_called()
        self.publisher.assert_not_called()
        self.assertEqual(self.body_reads, 0)

    async def test_unauthorized_publication_fails_before_body_database_or_signing(self):
        with self.assertRaises(AppError) as error:
            await self.call("/api/admin/risk/feeds/stable-risk/publish", method="POST", admin=False)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.body_reads, 0)
        self.db.first.assert_not_called()
        self.sign.assert_not_called()
        self.publisher.assert_not_called()

    async def test_approval_actor_is_authenticated_context_not_supplied_by_client(self):
        binding = golden_payload().bindings[0]
        result = await self.call("/api/admin/risk/bindings", method="POST",
                                 body={"feedId": "stable-risk", "binding": to_dict(binding)})
        self.assertEqual(result["status"], 201)
        self.assertEqual(self.approve.call_args.kwargs["approved_by"], "authenticated-operator")
        self.assertEqual(self.approve.call_args.kwargs["binding"], binding)
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/bindings", method="POST", body={
                "feedId": "stable-risk", "binding": to_dict(binding), "approved_by": "forged"})
        self.approve.assert_not_called()

    async def test_unadmitted_weight_reference_cannot_reach_signer(self):
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/feeds/stable-risk/publish", method="POST", body={
                "weightSetHash": "a" * 64, "weightSetVersion": "source-calibration-v1"})
        self.publisher.assert_not_called()
        self.sign.assert_not_called()

    async def test_refresh_authenticates_and_uses_only_approved_binding_identity(self):
        result = await self.call("/api/admin/risk/bindings/canonical-001/refresh", method="POST")
        self.assertEqual(result["data"]["status"], "refreshed")
        self.assertEqual(self.refresher.call_args.args[1], "canonical-001")
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/bindings/canonical-001/refresh", method="POST", body={"asOf": 10})
        self.refresher.assert_not_called()
        with self.assertRaises(AppError):
            await self.call("/api/admin/risk/bindings/canonical-001/refresh", method="POST", admin=False)
        self.refresher.assert_not_called()

    async def test_operator_risk_seed_retains_compiler_pipeline_and_normal_seed_band(self):
        question = "Will a defined USDC tail-risk event occur within the published window?"
        result = await self.call("/api/admin/risk/seed", method="POST", body={"question": question})
        self.assertEqual(result["status"], 201)
        self.seed.assert_awaited_once_with(question, uncertainty_band=None, canonical_risk=True)
        await self.call("/api/admin/seed", method="POST", body={"question": question})
        self.seed.assert_awaited_once_with(question)
        with self.assertRaises(AppError):
            await self.call("/api/admin/risk/seed", method="POST", body={"question": question}, admin=False)
        self.seed.assert_not_called()
