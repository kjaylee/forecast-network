"""Exercise deployed transport methods without loading the browser/Wasm SDK on the host."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from forecast_application.errors import AppError


def scheduled_method(name: str = "scheduled", **extra: Any) -> Any:
    source = ast.parse((Path(__file__).resolve().parents[1] / "apps/web/src/entry.py").read_text())
    entry = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "Default")
    method = next(node for node in entry.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    module = ast.Module(body=[method], type_ignores=[])
    scope: dict[str, Any] = {"Any": Any, "json": json, "javascript": lambda value: value, **extra}
    exec(compile(module, "actual_worker_scheduled", "exec"), scope)
    return scope[name]


class ProfilePublicationTransportTests(unittest.TestCase):
    def run_publication(self, body: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        self.publications: list[str] = []

        async def bounded_bytes(request: Any, limit: int) -> bytes:
            return json.dumps(body).encode()

        async def rate_limit(*args: Any) -> None:
            pass

        async def authenticate(token: Any, context: Any = None) -> dict[str, str] | None:
            return {"id": user_id} if user_id else None

        async def create_profile_card(owner: str) -> dict[str, Any]:
            self.publications.append(owner)
            return {"user": {"id": owner}}

        app = SimpleNamespace(rate_limit=rate_limit, authenticate=authenticate,
                              create_profile_card=create_profile_card)
        entry = SimpleNamespace(application=lambda: app,
                                env=SimpleNamespace(SESSION_SECRET="test-session-key-not-a-credential"))
        request = SimpleNamespace(method="POST", headers={"content-type": "application/json",
            "origin": "https://forecast.example", "X-Forecast-Client": "web"})
        route = scheduled_method("route_api", Response=Any, AppError=AppError, re=re, hmac=hmac,
            hashlib=hashlib, parse_qs=parse_qs, AUTH_CONTEXT_COOKIE="__Host-forecast_auth", cookie_token=lambda request, name=None: None,
            bounded_bytes=bounded_bytes, MAX_BODY_BYTES=16384, MAX_PROVIDER_BYTES=524288,
            api_response=lambda data, **kwargs: {"data": data, **kwargs})
        return asyncio.run(route(entry, request, urlsplit("https://forecast.example/api/me/share-card"),
                                 "/api/me/share-card"))

    def test_cookie_account_change_is_rejected_before_publication(self) -> None:
        with self.assertRaises(AppError) as raised:
            self.run_publication({"expectedUserId": "user-a"}, "user-b")
        self.assertEqual(raised.exception.code, "profile_owner_changed")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.publications, [])

    def test_displayed_owner_is_only_a_precondition_not_identity_authority(self) -> None:
        result = self.run_publication({"expectedUserId": "user-a"}, "user-a")
        self.assertEqual(self.publications, ["user-a"])
        self.assertEqual(result["status"], 201)
        with self.assertRaises(AppError) as raised:
            self.run_publication({"expectedUserId": "user-a"}, None)
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(self.publications, [])

    def test_missing_precondition_and_client_metrics_cannot_publish(self) -> None:
        for body in ({}, {"expectedUserId": 7}, {"expectedUserId": "user-a", "accuracy": 100}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.run_publication(body, "user-a")
            self.assertEqual(self.publications, [])


class ScheduledTransportTests(unittest.TestCase):
    @staticmethod
    def entrypoint(env: Any) -> Any:
        # workers-runtime-sdk's WorkerEntrypoint(ctx, env) initializes instance
        # bindings. Exercise real Python bound-method dispatch, rather than the
        # module-style free-function call that hid the production failure.
        class ConstructedEntrypoint:
            scheduled = scheduled_method()

            def __init__(self, ctx: Any, bindings: Any):
                self.ctx, self.env = ctx, bindings

        return ConstructedEntrypoint(SimpleNamespace(), env)

    def test_scheduled_dispatch_uses_authenticated_fetch_for_placement(self) -> None:
        calls: list[tuple[Any, ...]] = []

        async def fetch(resource: str, **options: Any) -> Any:
            # SDK _FetcherWrapper delegates to workers.fetch(resource, **options).
            # A JavaScript-style second positional options argument must fail.
            calls.append((resource, options))
            return SimpleNamespace(status=200)

        env = SimpleNamespace(ADMIN_TOKEN="test-operator-secret-" + "x"*40,
                              APP_ORIGIN="https://forecast.example", SCHEDULED_JOBS=SimpleNamespace(fetch=fetch))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            entry = self.entrypoint(env)
            asyncio.run(entry.scheduled(SimpleNamespace(cron="*/5 * * * *"), SimpleNamespace(), entry.ctx))
        self.assertEqual(len(calls), 1)
        url, options = calls[0]
        self.assertEqual(url, "https://forecast.example/api/admin/sweep")
        self.assertEqual(options["method"], "POST")
        self.assertEqual(options["body"], "{}")
        self.assertEqual(options["headers"]["Authorization"], "Bearer " + env.ADMIN_TOKEN)
        self.assertNotIn(env.ADMIN_TOKEN, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["httpStatus"], 200)

    def test_per_minute_cron_dispatches_only_the_risk_v2_operation(self) -> None:
        calls: list[tuple[Any, ...]] = []

        async def fetch(resource: str, **options: Any) -> Any:
            calls.append((resource, options))
            return SimpleNamespace(status=200)

        env = SimpleNamespace(ADMIN_TOKEN="test-operator-secret-" + "x"*40,
                              APP_ORIGIN="https://forecast.example", SCHEDULED_JOBS=SimpleNamespace(fetch=fetch))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            entry = self.entrypoint(env)
            asyncio.run(entry.scheduled(SimpleNamespace(cron="* * * * *"), SimpleNamespace(), entry.ctx))
        self.assertEqual([url for url, _ in calls], ["https://forecast.example/api/admin/risk/v2/operate"])
        self.assertEqual(json.loads(output.getvalue())["event"], "scheduled_risk_v2")

    def test_missing_operator_secret_never_dispatches(self) -> None:
        for token in (None, "short"):
            with self.subTest(token=token), self.assertRaises(RuntimeError):
                entry = self.entrypoint(SimpleNamespace(ADMIN_TOKEN=token))
                asyncio.run(entry.scheduled(None, SimpleNamespace(ADMIN_TOKEN="positional-shadow-"+"x"*40), entry.ctx))

    def test_positional_bindings_cannot_override_the_initialized_entrypoint_environment(self) -> None:
        calls = []
        async def trusted_fetch(resource: str, **options: Any) -> Any:
            calls.append((resource, options))
            return SimpleNamespace(status=200)
        async def unexpected_fetch(resource: str, **options: Any) -> Any:
            self.fail("Scheduled dispatch used a positional environment instead of its instance bindings")
        env = SimpleNamespace(ADMIN_TOKEN="instance-test-token-"+"a"*40,
            APP_ORIGIN="https://forecast.example", SCHEDULED_JOBS=SimpleNamespace(fetch=trusted_fetch))
        shadow = SimpleNamespace(ADMIN_TOKEN="positional-test-token-"+"b"*40,
            APP_ORIGIN="https://unrelated.example", SCHEDULED_JOBS=SimpleNamespace(fetch=unexpected_fetch))
        entry = self.entrypoint(env)
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(entry.scheduled(SimpleNamespace(), shadow, entry.ctx))
        self.assertEqual(calls[0][0], "https://forecast.example/api/admin/sweep")
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer "+env.ADMIN_TOKEN)

    def test_failed_dispatch_is_reported_as_a_failed_scheduled_run(self) -> None:
        async def fetch(resource: str, **options: Any) -> Any:
            return SimpleNamespace(status=503)

        env = SimpleNamespace(ADMIN_TOKEN="test-operator-secret-" + "x"*40,
                              APP_ORIGIN="https://forecast.example", SCHEDULED_JOBS=SimpleNamespace(fetch=fetch))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            entry = self.entrypoint(env)
            asyncio.run(entry.scheduled(None, None, entry.ctx))


class PointsTransportTests(unittest.TestCase):
    def dispatch(self, path: str, body: dict[str, Any], user_id: str | None = "user-b",
                 method: str = "POST") -> dict[str, Any]:
        self.actions: list[tuple[Any, ...]] = []
        actions = self.actions

        async def bounded_bytes(request: Any, limit: int) -> bytes:
            return json.dumps(body).encode()

        async def rate_limit(*args: Any) -> None:
            pass

        async def authenticate(token: Any, context: Any = None) -> dict[str, str] | None:
            return {"id": user_id} if user_id else None

        async def submit_forecast(*args: Any, **kwargs: Any) -> dict[str, Any]:
            actions.append(("forecast", args[0], kwargs["stake_points"]))
            return {"accepted": True}

        class Points:
            def __init__(self, db: Any):
                pass

            async def summary(self, owner: str) -> dict[str, Any]:
                return {"userId": owner, "available": 1000}

        class Wallets:
            def __init__(self, *args: Any, **kwargs: Any):
                pass

            async def challenge(self, owner: str, address: str) -> dict[str, Any]:
                actions.append(("challenge", owner, address))
                return {"address": address}

            async def link(self, owner: str, proof: dict[str, Any]) -> dict[str, Any]:
                self.assert_proof(proof)
                actions.append(("link", owner, proof))
                return {"wallet": {"address": proof["address"]}, "points": {"userId": owner}}

            @staticmethod
            def assert_proof(proof: dict[str, Any]) -> None:
                if set(proof) != {"challengeId", "address", "signature"}:
                    raise ValueError("Unexpected signature body")

            async def unlink(self, owner: str) -> dict[str, Any]:
                actions.append(("unlink", owner))
                return {"wallet": None}

        source = ast.parse((Path(__file__).resolve().parents[1] / "apps/web/src/entry.py").read_text())
        guard = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                     and node.name == "require_expected_user")
        guard_scope: dict[str, Any] = {"Any": Any, "AppError": AppError}
        exec(compile(ast.Module(body=[guard], type_ignores=[]), "actual_account_guard", "exec"), guard_scope)
        app = SimpleNamespace(db=object(), now_ms=lambda: 1000, rate_limit=rate_limit,
                              authenticate=authenticate, submit_forecast=submit_forecast)
        entry = SimpleNamespace(application=lambda: app, verify_wallet_signature=lambda *args: True,
                                env=SimpleNamespace(SESSION_SECRET="test-session-key-not-a-credential", WALLET_LOGIN_REQUIRED="false"))
        request = SimpleNamespace(method=method, headers={"content-type": "application/json",
            "origin": "https://forecast.example", "X-Forecast-Client": "web"})
        route = scheduled_method("route_api", Response=Any, AppError=AppError, re=re, hmac=hmac,
            hashlib=hashlib, parse_qs=parse_qs, AUTH_CONTEXT_COOKIE="__Host-forecast_auth", cookie_token=lambda request, name=None: None,
            require_expected_user=guard_scope["require_expected_user"], PointsService=Points,
            WalletService=Wallets, bounded_bytes=bounded_bytes, MAX_BODY_BYTES=16384,
            MAX_PROVIDER_BYTES=524288, api_response=lambda data, **kwargs: {"data": data, **kwargs})
        return asyncio.run(route(entry, request, urlsplit("https://forecast.example" + path), path.split("?")[0]))

    def test_balance_is_private_and_query_identity_is_not_authority(self) -> None:
        with self.assertRaises(AppError) as error:
            self.dispatch("/api/points", {}, None, "GET")
        self.assertEqual(error.exception.status, 401)
        result = self.dispatch("/api/points?userId=user-a", {}, "user-b", "GET")
        self.assertEqual(result["data"]["userId"], "user-b")

    def test_stake_and_wallet_actions_reject_changed_accounts_before_mutating(self) -> None:
        cases = [
            ("/api/forecasts/f_example/forecast", {"stakePoints": 50}),
            ("/api/wallet/challenge", {"address": "address"}),
            ("/api/wallet/link", {"challengeId": "challenge", "address": "address", "signature": "signature"}),
            ("/api/wallet/unlink", {}),
        ]
        for path, body in cases:
            with self.subTest(path=path), self.assertRaises(AppError) as error:
                self.dispatch(path, {**body, "expectedUserId": "user-a"})
            self.assertEqual(error.exception.code, "account_changed")
            self.assertEqual(self.actions, [])

    def test_explicit_stakes_require_an_owner_and_integer_including_practice_zero(self) -> None:
        path = "/api/forecasts/f_example/forecast"
        with self.assertRaises(AppError) as error:
            self.dispatch(path, {"stakePoints": 50})
        self.assertEqual(error.exception.code, "account_precondition_required")
        for amount in (None, 50.0, True, "50"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.dispatch(path, {"expectedUserId": "user-b", "stakePoints": amount})
            self.assertEqual(self.actions, [])
        self.dispatch(path, {"expectedUserId": "user-b", "stakePoints": 0})
        self.assertEqual(self.actions, [("forecast", "user-b", 0)])
        self.dispatch(path, {})
        self.assertEqual(self.actions, [("forecast", "user-b", None)])

    def test_wallet_precondition_is_not_forwarded_into_the_signature_proof(self) -> None:
        proof = {"challengeId": "challenge", "address": "address", "signature": "signature"}
        result = self.dispatch("/api/wallet/link", {**proof, "expectedUserId": "user-b"})
        self.assertEqual(self.actions, [("link", "user-b", proof)])
        self.assertEqual(result["data"]["points"]["userId"], "user-b")
