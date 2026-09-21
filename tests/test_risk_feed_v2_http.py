"""Actual Worker routing for the v2 registry: operator-only writes, server-side actor, honest public reads."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import unittest
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from forecast_application.errors import AppError
from forecast_domain.risk_feed import (
    CanonicalRiskDefinitionV2,
    RiskFeedBinding,
    RiskFeedBindingV2,
    RiskFeedSeriesV2,
    RiskMappingProfileV2,
)
from forecast_domain.serialization import content_hash, from_dict, to_dict

from tests.risk_feed_fixtures import (
    golden_definition,
    golden_payload,
    golden_profile,
    golden_series,
)
from tests.test_risk_feed_v2_contract import golden_envelope
from tests.test_web_transport import scheduled_method


class RiskFeedV2HttpTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, path, *, method="GET", body=None, admin=True, latest=None, retained=None,
                   scheduler=None, credential=None):
        self.body_reads = 0
        mocks = dict(
            admit_definition=AsyncMock(return_value="1" * 64), admit_profile=AsyncMock(return_value="2" * 64),
            approve_binding_v2=AsyncMock(), publish_feed_v2=AsyncMock(return_value=golden_envelope()),
            latest_feed_v2=AsyncMock(return_value=latest),
            revoke_binding_v2=AsyncMock(), refresh_bound_prediction_v2=AsyncMock(return_value={"status": "refreshed"}),
            configure_operation=AsyncMock(), operate_feeds_v2=AsyncMock(return_value=[{"feedId": "risk-v2"}]),
            configure_series=AsyncMock(return_value="3" * 64),
            approve_binding=AsyncMock(), publish_feed=AsyncMock(), latest_feed=AsyncMock(return_value=None),
            revoke_binding=AsyncMock(), refresh_bound_prediction=AsyncMock(),
        )
        self.mocks = mocks
        self.db = SimpleNamespace(first=AsyncMock(return_value=retained), execute=AsyncMock())
        self.sign = AsyncMock()
        self.app_seed = AsyncMock(return_value={"forecast": {"id": "f"}})
        app = SimpleNamespace(db=self.db, now_ms=lambda: 1_800_000_000_000, seed=self.app_seed,
                              run_automation=AsyncMock(return_value={"sources": {"polled": 0}}), registry=None)
        env = SimpleNamespace(ADMIN_TOKEN="a" * 64)
        if scheduler is not None:
            env.SCHEDULER_TOKEN = scheduler
        worker = SimpleNamespace(env=env, application=lambda: app,
                                 relayer_public_key=lambda: bytes(range(32)), sign_registry_message=self.sign)

        async def read(_request, _limit):
            self.body_reads += 1
            return json.dumps(body or {}).encode()

        scope = dict(Response=Any, AppError=AppError, hmac=hmac, re=re, hashlib=hashlib, time=time, MAX_PROVIDER_BYTES=524288,
                     MAX_BODY_BYTES=16384, bounded_bytes=read, RiskFeedBinding=RiskFeedBinding,
                     RiskFeedBindingV2=RiskFeedBindingV2, CanonicalRiskDefinitionV2=CanonicalRiskDefinitionV2,
                     RiskMappingProfileV2=RiskMappingProfileV2, RiskFeedSeriesV2=RiskFeedSeriesV2,
                     from_dict=from_dict, to_dict=to_dict,
                     content_hash=content_hash, api_response=lambda data, status=200: {"data": data, "status": status},
                     **mocks)
        worker.route_risk_v2 = MethodType(scheduled_method("route_risk_v2", **scope), worker)
        worker.publish_risk_v2 = MethodType(scheduled_method("publish_risk_v2", **scope), worker)
        actual = scheduled_method("route_api", **scope)
        headers = {"Content-Type": "application/json", "content-type": "application/json"}
        if admin:
            headers["authorization"] = "Bearer " + (credential if credential is not None else "a" * 64)
        request = SimpleNamespace(method=method, headers=headers)
        return await actual(worker, request, urlsplit("https://forecast.eastsea.xyz" + path), path)

    async def test_public_v2_feed_is_honest_when_unavailable_and_labels_protocol(self):
        result = await self.call("/api/risk/v2/feeds/stable-risk-v2", admin=False)
        self.assertEqual((result["data"]["status"], result["data"]["protocol"]),
                         ("unavailable", "forecast-risk-feed-v2"))
        self.assertIsNone(result["data"]["envelope"])
        self.assertEqual(self.body_reads, 0)
        self.sign.assert_not_called()

    async def test_writes_require_operator_before_body_database_or_signing(self):
        for path in ["/api/admin/risk/v2/definitions", "/api/admin/risk/v2/profiles", "/api/admin/risk/v2/bindings",
                     "/api/admin/risk/v2/bindings/b1/refresh", "/api/admin/risk/v2/bindings/b1/revoke",
                     "/api/admin/risk/v2/feeds/f1/publish"]:
            with self.subTest(path):
                with self.assertRaises(AppError) as error:
                    await self.call(path, method="POST", admin=False)
                self.assertEqual(error.exception.status, 403)
                self.assertEqual(self.body_reads, 0)
        self.db.first.assert_not_called()
        self.sign.assert_not_called()
        for mock in self.mocks.values():
            mock.assert_not_called()

    async def test_a_scheduler_credential_reaches_the_two_triggers(self):
        scheduler = "s" * 64
        ticked = await self.call("/api/admin/risk/v2/operate", method="POST",
                                 scheduler=scheduler, credential=scheduler)
        self.assertEqual((ticked["status"], ticked["data"]["status"]), (200, "ticked"))
        swept = await self.call("/api/admin/sweep", method="POST", scheduler=scheduler, credential=scheduler)
        self.assertEqual(swept["status"], 200)
        self.assertEqual(swept["data"]["sources"], {"polled": 0})
        # The sweep fails with 1101 on most live calls and nothing recorded where the time
        # went, so the failure was attributed to whatever seemed likeliest.
        phases = swept["data"]["phaseMs"]
        self.assertIs(type(phases["automation"]), int)
        self.assertIs(type(phases["total"]), int)
        self.assertGreaterEqual(phases["total"], phases["automation"])

    async def test_a_scheduler_credential_cannot_administer(self):
        scheduler = "s" * 64
        for path in ("/api/admin/risk/v2/definitions", "/api/admin/risk/v2/profiles",
                     "/api/admin/risk/v2/bindings", "/api/admin/risk/v2/feeds/f1/publish",
                     "/api/admin/risk/v2/series", "/api/admin/risk/v2/health"):
            with self.subTest(path):
                with self.assertRaises(AppError) as error:
                    await self.call(path, method="POST", scheduler=scheduler, credential=scheduler)
                self.assertEqual(error.exception.status, 403)
                self.assertEqual(self.body_reads, 0)

    async def test_an_unset_or_short_scheduler_credential_authorizes_nobody(self):
        for scheduler in (None, "s" * 31, ""):
            with self.subTest(scheduler=scheduler):
                with self.assertRaises(AppError) as error:
                    await self.call("/api/admin/risk/v2/operate", method="POST",
                                    scheduler=scheduler, credential="s" * 64)
                self.assertEqual(error.exception.status, 403)

    async def test_the_scheduler_credential_never_substitutes_for_the_operator_secret(self):
        scheduler = "s" * 64
        with self.assertRaises(AppError) as error:
            await self.call("/api/admin/risk/v2/operate", method="POST",
                            scheduler=scheduler, credential="a" * 63 + "b")
        self.assertEqual(error.exception.status, 403)

    async def test_definition_profile_and_binding_carry_server_side_actor(self):
        definition, profile = golden_definition(), golden_profile()
        result = await self.call("/api/admin/risk/v2/definitions", method="POST",
                                 body={"feedId": "risk-v2", "definition": to_dict(definition)})
        self.assertEqual((result["status"], result["data"]["definitionHash"]), (201, "1" * 64))
        self.assertEqual(self.mocks["admit_definition"].call_args.kwargs["definition"], definition)
        self.assertEqual(self.mocks["admit_definition"].call_args.kwargs["approved_by"], "authenticated-operator")
        result = await self.call("/api/admin/risk/v2/profiles", method="POST",
                                 body={"feedId": "risk-v2", "profile": to_dict(profile)})
        self.assertEqual(self.mocks["admit_profile"].call_args.kwargs["profile"], profile)
        binding = golden_payload().bindings[0]
        result = await self.call("/api/admin/risk/v2/bindings", method="POST",
                                 body={"feedId": "risk-v2", "binding": to_dict(binding)})
        self.assertEqual(result["data"]["bindingId"], binding.binding_id)
        self.assertEqual(self.mocks["approve_binding_v2"].call_args.kwargs["binding"], binding)
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/bindings", method="POST", body={
                "feedId": "risk-v2", "binding": to_dict(binding), "approved_by": "forged"})
        self.mocks["approve_binding_v2"].assert_not_called()
        with self.assertRaises(Exception):
            await self.call("/api/admin/risk/v2/bindings", method="POST", body={
                "feedId": "risk-v2", "binding": {**to_dict(binding), "version": "canonical-risk-binding-v1"}})
        self.mocks["approve_binding_v2"].assert_not_called()

    async def test_publish_requires_admitted_weights_and_cohort_before_signer(self):
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/feeds/risk-v2/publish", method="POST", body={
                "weightSetHash": "a" * 64, "weightSetVersion": "source-calibration-v2"})
        self.mocks["publish_feed_v2"].assert_not_called()
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/feeds/risk-v2/publish", method="POST", body={
                "weightSetHash": "a" * 64, "weightSetVersion": "source-calibration-v2",
                "calibrationCohortId": "cohort"})
        self.mocks["publish_feed_v2"].assert_not_called()
        self.sign.assert_not_called()
        document = {"version": "source-calibration-v2", "weights": {}}
        digest = content_hash(document)
        result = await self.call("/api/admin/risk/v2/feeds/risk-v2/publish", method="POST",
                                 body={"weightSetHash": digest, "weightSetVersion": "source-calibration-v2",
                                       "calibrationCohortId": "cohort"},
                                 retained={"body": json.dumps(document)})
        self.assertEqual(result["status"], 201)
        kwargs = self.mocks["publish_feed_v2"].call_args.kwargs
        self.assertEqual((kwargs["feed_id"], kwargs["calibration_cohort_id"], kwargs["weight_set_hash"]),
                         ("risk-v2", "cohort", digest))
        self.assertTrue(kwargs["key_id"].startswith("forecast-relayer-"))

    async def test_operation_config_and_tick_are_operator_only_and_server_actor(self):
        with self.assertRaises(AppError):
            await self.call("/api/admin/risk/v2/feeds/risk-v2/operate", method="POST", admin=False)
        with self.assertRaises(AppError):
            await self.call("/api/admin/risk/v2/operate", method="POST", admin=False)
        self.mocks["configure_operation"].assert_not_called()
        self.mocks["operate_feeds_v2"].assert_not_called()
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/feeds/risk-v2/operate", method="POST", body={
                "weightSetHash": "a" * 64, "weightSetVersion": "v", "calibrationCohortId": "c", "enabled": "yes"})
        result = await self.call("/api/admin/risk/v2/feeds/risk-v2/operate", method="POST", body={
            "weightSetHash": "a" * 64, "weightSetVersion": "source-calibration-v2", "calibrationCohortId": "c",
            "enabled": True})
        self.assertEqual(result["data"], {"status": "configured", "feedId": "risk-v2", "enabled": True})
        self.assertEqual(self.mocks["configure_operation"].call_args.kwargs["configured_by"], "authenticated-operator")
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/operate", method="POST", body={"force": True})
        result = await self.call("/api/admin/risk/v2/operate", method="POST")
        self.assertEqual(result["data"], {"status": "ticked", "feeds": [{"feedId": "risk-v2"}]})
        kwargs = self.mocks["operate_feeds_v2"].call_args.kwargs
        self.assertEqual(kwargs["now_ms"], 1_800_000_000_000)
        await kwargs["refresh"]("episode-1")
        self.assertEqual(self.mocks["refresh_bound_prediction_v2"].call_args.args[1], "episode-1")

    async def test_series_configuration_and_tick_seed_wiring(self):
        series = golden_series()
        with self.assertRaises(AppError):
            await self.call("/api/admin/risk/v2/series", method="POST", admin=False)
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/series", method="POST", body={"series": to_dict(series)})
        result = await self.call("/api/admin/risk/v2/series", method="POST",
                                 body={"series": to_dict(series), "enabled": True})
        self.assertEqual(result["data"], {"status": "configured", "seriesId": series.series_id,
                                          "seriesHash": "3" * 64, "enabled": True})
        kwargs = self.mocks["configure_series"].call_args.kwargs
        self.assertEqual((kwargs["series"], kwargs["configured_by"]), (series, "authenticated-operator"))
        await self.call("/api/admin/risk/v2/operate", method="POST")
        seed = self.mocks["operate_feeds_v2"].call_args.kwargs["seed"]
        app_seed = self.app_seed
        await seed("During [a, b), will X?")
        app_seed.assert_awaited_once_with("During [a, b), will X?", uncertainty_band=None, canonical_risk=True)

    async def test_refresh_and_revoke_use_only_approved_binding_identity(self):
        result = await self.call("/api/admin/risk/v2/bindings/episode-1/refresh", method="POST")
        self.assertEqual(result["data"]["status"], "refreshed")
        self.assertEqual(self.mocks["refresh_bound_prediction_v2"].call_args.args[1], "episode-1")
        with self.assertRaises(ValueError):
            await self.call("/api/admin/risk/v2/bindings/episode-1/refresh", method="POST", body={"asOf": 10})
        self.mocks["refresh_bound_prediction_v2"].assert_not_called()
        result = await self.call("/api/admin/risk/v2/bindings/episode-1/revoke", method="POST",
                                 body={"reason": "wrong mapping"})
        self.assertEqual(result["data"], {"status": "revoked", "bindingId": "episode-1"})
        self.assertEqual(self.mocks["revoke_binding_v2"].call_args.kwargs["revoked_by"], "authenticated-operator")
        with self.assertRaises(AppError):
            await self.call("/api/admin/risk/v2/unknown", method="POST")


if __name__ == "__main__":
    unittest.main()
