"""Actual Worker routes, cookies and application against transactional SQLite."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import re
import unittest
from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from forecast_application.errors import AppError
from forecast_application.service import Application
from forecast_application.wallet_login import WalletLogin

from tests import test_web_wallet_login as fixtures
from tests.test_web_transport import scheduled_method

ROOT = fixtures.ROOT

ORIGIN = "https://forecast.example"
SESSION = "__Host-forecast_session"
CONTEXT = "__Host-forecast_auth"


class WalletLoginTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WalletLoginTests.asyncSetUp
    asyncTearDown = fixtures.WalletLoginTests.asyncTearDown
    hash = staticmethod(fixtures.WalletLoginTests.hash)
    token = fixtures.WalletLoginTests.token
    verify_signature = fixtures.WalletLoginTests.verify_signature
    signed = staticmethod(fixtures.WalletLoginTests.signed)

    def setup_route(self):
        tree = ast.parse((ROOT / "apps/web/src/entry.py").read_text())
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"cookie_token", "api_response"}]
        scope = {"Any": Any, "SESSION_COOKIE": SESSION, "AUTH_CONTEXT_COOKIE": CONTEXT,
                 "SimpleCookie": SimpleCookie,
                 "Response": SimpleNamespace(json=lambda value, **kw: {"body": value, **kw})}
        exec(compile(ast.Module(body=helpers, type_ignores=[]), "actual_wallet_http_helpers", "exec"), scope)
        self.app = Application(self.db, None, now_ms=lambda: self.now, token_hash=self.hash,
                               random_token=self.token)
        self.entry = SimpleNamespace(application=lambda: self.app, verify_wallet_signature=self.verify_signature,
                                     env=SimpleNamespace(SESSION_SECRET="fixture-only-" + "x"*40))

        async def bounded(request, limit):
            return json.dumps(request.body).encode()

        self.route = scheduled_method("route_api", **scope, AppError=AppError, re=re, hmac=hmac,
            hashlib=hashlib, parse_qs=parse_qs, WalletLogin=WalletLogin, bounded_bytes=bounded,
            MAX_BODY_BYTES=16384, MAX_PROVIDER_BYTES=524288)

    async def dispatch(self, path, body=None, *, cookies=None, method="POST", origin=ORIGIN, client="web"):
        headers = {"content-type": "application/json", "origin": origin, "X-Forecast-Client": client,
                   "cookie": "; ".join(key+"="+value for key, value in (cookies or {}).items())}
        request = SimpleNamespace(method=method, headers=headers, body=body or {})
        return await self.route(self.entry, request, urlsplit(ORIGIN+path), path)

    def accept_cookie(self, result, cookies):
        jar = SimpleCookie()
        jar.load(result["headers"]["Set-Cookie"])
        for key, value in jar.items():
            self.assertTrue(value["secure"])
            self.assertTrue(value["httponly"])
            self.assertEqual(value["samesite"], "Strict")
            self.assertEqual(value["path"], "/")
            self.assertFalse(value["domain"])
            if value["max-age"] == "0":
                cookies.pop(key, None)
            else:
                cookies[key] = value.value

    async def prepare(self, cookies):
        result = await self.dispatch("/api/auth/wallet/context", cookies=cookies)
        self.assertEqual(set(result["body"]["data"]), {"expiresAt"})
        self.accept_cookie(result, cookies)
        result = await self.dispatch("/api/auth/wallet/challenge", {"address": self.address,
            "mode": "login", "expectedUserId": None}, cookies=cookies)
        return result["body"]["data"]

    async def test_http_sign_in_and_me_require_both_cookies_and_never_expose_secrets(self):
        self.setup_route()
        cookies = {}
        challenge = await self.prepare(cookies)
        result = await self.dispatch("/api/auth/wallet/verify", self.signed(challenge), cookies=cookies)
        self.assertEqual(set(result["body"]["data"]), {"user", "wallet", "points"})
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.accept_cookie(result, cookies)
        self.assertEqual(set(cookies), {CONTEXT, SESSION})
        for token in cookies.values():
            self.assertNotIn(token, json.dumps(result["body"]))
        me = (await self.dispatch("/api/me", method="GET", cookies=cookies))["body"]["data"]
        self.assertEqual(me["user"], result["body"]["data"]["user"])
        self.assertEqual(me["authentication"], {"method": "wallet", "address": self.address})
        for missing in (CONTEXT, SESSION):
            fewer = {key: value for key, value in cookies.items() if key != missing}
            self.assertIsNone((await self.dispatch("/api/me", method="GET", cookies=fewer))["body"]["data"]["user"])

    async def test_cancel_prevents_late_cookie_or_signature_from_restoring_login(self):
        self.setup_route()
        cookies = {}
        challenge = await self.prepare(cookies)
        verified = await self.dispatch("/api/auth/wallet/verify", self.signed(challenge), cookies=cookies)
        await self.dispatch("/api/auth/wallet/cancel", cookies=cookies)
        self.accept_cookie(verified, cookies)  # A previously delayed successful HTTP response.
        self.assertIsNone((await self.dispatch("/api/me", method="GET", cookies=cookies))["body"]["data"]["user"])
        with self.assertRaises(AppError):
            await self.dispatch("/api/auth/wallet/verify", self.signed(challenge), cookies=cookies)

    async def test_legacy_login_cancels_prior_wallet_nonce_without_revoking_replacement_session(self):
        self.setup_route()
        guest = await self.app.register("Existing profile")
        cookies = {}
        challenge = await self.prepare(cookies)
        result = await self.dispatch("/api/auth/login", {"recoveryCode": guest["recoveryCode"]}, cookies=cookies)
        self.accept_cookie(result, cookies)
        me = (await self.dispatch("/api/me", method="GET", cookies=cookies))["body"]["data"]
        self.assertEqual(me["user"]["id"], guest["user"]["id"])
        self.assertEqual(me["authentication"]["method"], "legacy")
        with self.assertRaises(AppError):
            await self.dispatch("/api/auth/wallet/verify", self.signed(challenge), cookies=cookies)

    async def test_new_guest_registration_is_disabled_and_cross_origin_auth_rejected(self):
        self.setup_route()
        with self.assertRaises(AppError) as error:
            await self.dispatch("/api/auth/register", {"displayName": "No guest"})
        self.assertEqual(error.exception.code, "wallet_login_required")
        for path in ("context", "challenge", "verify", "cancel"):
            for origin, client in (("https://other.example", "web"), (ORIGIN, None)):
                with self.subTest(path=path, origin=origin, client=client), self.assertRaises(AppError) as denied:
                    await self.dispatch("/api/auth/wallet/"+path, origin=origin, client=client)
                self.assertEqual(denied.exception.status, 403)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM users"))["n"], 0)
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM wallet_login_contexts"))["n"], 0)

    async def test_legacy_link_mutations_cannot_bypass_guarded_migration(self):
        self.setup_route()
        guest = await self.app.register("Legacy owner")
        cookies = {SESSION: guest["sessionToken"]}
        for path in ("challenge", "link", "unlink"):
            with self.subTest(path=path), self.assertRaises(AppError) as error:
                await self.dispatch("/api/wallet/"+path, {"expectedUserId": guest["user"]["id"]}, cookies=cookies)
            self.assertEqual(error.exception.code, "wallet_migration_required")
        self.assertEqual((await self.db.first("SELECT count(*) AS n FROM wallet_challenges"))["n"], 0)
