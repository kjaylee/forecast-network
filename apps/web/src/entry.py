"""Cloudflare transport: trusted clock/cookies, bounded network I/O and D1 bridge."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import traceback
from collections.abc import Mapping, Sequence
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from forecast_application.ai import AiCoordinator, ProviderConfig
from forecast_application.analytics import product_analytics
from forecast_application.errors import AppError
from forecast_application.points import PointsService
from forecast_application.risk_feed import (
    approve_binding,
    latest_feed,
    publish_feed,
    revoke_binding,
)
from forecast_application.risk_feed_series import configure_series
from forecast_application.risk_feed_v2 import (
    admit_definition,
    admit_profile,
    approve_binding_v2,
    configure_operation,
    latest_feed_v2,
    operate_feeds_v2,
    operations_health,
    publish_feed_v2,
    revoke_binding_v2,
    training_export,
)
from forecast_application.risk_refresh import refresh_bound_prediction, refresh_bound_prediction_v2
from forecast_application.service import CHALLENGE_MS, Application
from forecast_application.solana_registry import RegistryError, SolanaRegistry, reserve_daily_spend
from forecast_application.solana_rpc import SolanaRpcError, SolanaRpcTransport
from forecast_application.solana_wire import base58_decode
from forecast_application.sources import (
    MAX_SOURCE_BYTES,
    Artifact,
    SourceRejected,
    SourceUnavailable,
    TextResponse,
)
from forecast_application.wallet_login import WalletLogin
from forecast_application.wallets import WalletService
from forecast_domain.models import AIProvenance, Resolution
from forecast_domain.risk_feed import (
    CanonicalRiskDefinitionV2,
    RiskFeedBinding,
    RiskFeedBindingV2,
    RiskFeedSeriesV2,
    RiskMappingProfileV2,
)
from forecast_domain.serialization import content_hash, from_dict, to_dict
from js import Object, Uint8Array
from js import crypto as web_crypto
from js import fetch as js_fetch

try:
    from js import AbortSignal as js_abort_signal
except ImportError:  # pragma: no cover - the Workers runtime is expected to provide it
    js_abort_signal = None
from pyodide.ffi import JsException, to_js
from workers import Response, WorkerEntrypoint

SESSION_COOKIE = "__Host-forecast_session"
AUTH_CONTEXT_COOKIE = "__Host-forecast_auth"
# D1 read-replication bookmark: a browser always reads at or after its own last write.
BOOKMARK_COOKIE = "__Host-forecast_d1"
BOOKMARK_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,256}")
GEMINI_HOST = "generativelanguage.googleapis.com"
MAX_BODY_BYTES = 16 * 1024
MAX_PROVIDER_BYTES = 512 * 1024
VERSION = "0.12.22"


def python_value(value: Any, _depth: int = 0) -> Any:
    if _depth > 32:
        raise ValueError("Runtime value exceeds structural depth")
    if value is None:
        return None
    value = value.to_py() if hasattr(value, "to_py") else value
    # Workers bindings may return JsDict/JsArray mapping wrappers, unlike fetch's
    # native JS objects. Normalize recursively before strict domain/AI validation.
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("Runtime object contains a non-string key")
        return {key: python_value(item, _depth+1) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [python_value(item, _depth+1) for item in value]
    return value


def javascript(value: Any) -> Any:
    return to_js(value, dict_converter=Object.fromEntries)


def require_expected_user(body: dict[str, Any], user_id: str) -> None:
    expected = body.get("expectedUserId")
    if not isinstance(expected, str):
        raise AppError(400, "account_precondition_required",
                       "Reload your profile before changing points or wallet settings.")
    if expected != user_id:
        raise AppError(409, "account_changed",
                       "Your signed-in account changed. Reload your profile before continuing.")


def database_session(binding: Any, *, bookmark: str | None, primary: bool) -> Any:
    """Sessions API when the binding offers it; the plain binding otherwise (local dev)."""
    if not hasattr(binding, "withSession"):
        return binding
    if primary:
        return binding.withSession("first-primary")
    return binding.withSession(bookmark if bookmark else "first-unconstrained")


def attach_bookmark(response: Any, session: Any, previous: str | None) -> None:
    """Persist the session bookmark so this browser's next read sees this request's writes."""
    get_bookmark = getattr(session, "getBookmark", None)
    if get_bookmark is None:
        return
    current = get_bookmark()
    if not isinstance(current, str) or current == previous or not BOOKMARK_PATTERN.fullmatch(current):
        return
    response.js_object.headers.append(
        "Set-Cookie", f"{BOOKMARK_COOKIE}={current}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=86400")


class D1Database:
    def __init__(self, binding: Any):
        self.binding = binding

    def statement(self, sql: str, params: tuple[Any, ...]) -> Any:
        prepared = self.binding.prepare(sql)
        return prepared.bind(*params) if params else prepared

    async def first(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return python_value(await self.statement(sql, params).first())

    async def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        result = python_value(await self.statement(sql, params).all())
        return result["results"]

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        return python_value(await self.statement(sql, params).run())

    async def batch(self, statements: Any) -> list[dict[str, Any]]:
        prepared = [self.statement(sql, params) for sql, params in statements]
        return python_value(await self.binding.batch(prepared))


async def bounded_bytes(message: Any, limit: int) -> bytes:
    length = message.headers.get("content-length")
    if length and str(length).isdigit() and int(length) > limit:
        raise ValueError("Payload exceeds size limit")
    body = message.body
    # A bodiless response (304, manual redirect, HEAD) arrives as JS null. Newer Pyodide
    # builds surface that as a JsNull proxy rather than None, so test for the stream API.
    if body is None or not hasattr(body, "getReader"):
        return b""
    reader = body.getReader()
    result = bytearray()
    try:
        while True:
            part = await reader.read()
            if part.done:
                break
            chunk = bytes(python_value(part.value))
            if len(result) + len(chunk) > limit:
                await reader.cancel()
                raise ValueError("Payload exceeds size limit")
            result.extend(chunk)
    finally:
        reader.releaseLock()
    return bytes(result)


def cookie_token(request: Any, name: str = SESSION_COOKIE) -> str | None:
    raw = request.headers.get("cookie")
    if not raw or len(str(raw)) > 8192:
        return None
    try:
        jar: SimpleCookie[str] = SimpleCookie()
        jar.load(str(raw))
        return jar[name].value if name in jar else None
    except Exception:
        return None


def api_response(data: Any, *, status: int = 200, session: str | None = None,
                 clear_session: bool = False, error: bool = False,
                 context: str | None = None) -> Response:
    headers = {
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    }
    if session is not None:
        headers["Set-Cookie"] = (
            f"{SESSION_COOKIE}={session}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000"
        )
    if clear_session:
        headers["Set-Cookie"] = (
            f"{SESSION_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
        )
    if context is not None:
        if session is not None or clear_session:
            raise ValueError("Context bootstrap and session mutation use separate responses")
        headers["Set-Cookie"] = (
            f"{AUTH_CONTEXT_COOKIE}={context}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=2592000"
        )
    return Response.json({"error" if error else "data": data}, status=status, headers=headers)


class Default(WorkerEntrypoint):
    async def registry_rpc(self, method: str, params: list[Any]) -> Any:
        if method not in {"getGenesisHash", "getAccountInfo", "getSignatureStatuses", "getSlot",
                          "getBlockTime", "getLatestBlockhash", "getBlockHeight", "getBalance",
                          "getMinimumBalanceForRentExemption", "getFeeForMessage",
                          "simulateTransaction", "sendTransaction", "isBlockhashValid"}:
            raise ValueError("Unsupported registry RPC method")
        url = str(getattr(self.env, "SOLANA_RPC_URL", ""))
        if url != "https://api.devnet.solana.com":
            raise ValueError("Registry RPC must use pinned Devnet endpoint")
        headers = {"Content-Type": "application/json",
                   "User-Agent": "Forecast-Registry/0.9 (+https://forecast.eastsea.xyz)"}
        proxy = str(getattr(self.env, "SOLANA_RPC_PROXY_URL", "") or "")
        if proxy:
            # This owned gateway forwards only the fixed Devnet RPC. It holds no
            # signing keys; SolanaRpcTransport still verifies the chain genesis.
            if proxy != "https://forecast-rpc.eastsea.xyz/rpc":
                raise ValueError("Unapproved registry RPC gateway")
            token = str(getattr(self.env, "SOLANA_RPC_PROXY_TOKEN", "") or "")
            if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                raise ValueError("Registry RPC gateway credential unavailable")
            url = proxy
            headers["X-Forecast-RPC-Token"] = token
        keyed = str(getattr(self.env, "SOLANA_DEVNET_RPC_KEYED", "") or "")
        if keyed:
            # The public Devnet endpoint answers Cloudflare's egress with HTTP 403
            # ("your IP or provider is blocked"), which is why an owned gateway exists
            # at all. An authenticated provider is not refused that way. The trailing
            # slash is what makes this a host check rather than a prefix that
            # "rpc.ankr.com.somewhere-else" would also satisfy.
            if not keyed.startswith("https://rpc.ankr.com/"):
                raise ValueError("Unapproved keyed Devnet provider")
            url = keyed
        options: dict[str, Any] = {"method": "POST", "redirect": "manual", "headers": headers,
            "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})}
        # Bound the call on the JS side. asyncio.wait_for would create a second Python
        # task, and every route already runs inside one, so Pyodide refused the whole
        # call: "Cannot enter a promising task from inside another running promising task".
        try:
            options["signal"] = js_abort_signal.timeout(20_000)
        except Exception:
            # No AbortSignal.timeout in this runtime, so the fetch is bounded only by the
            # Worker's own limits. Logged so the weaker guarantee is visible.
            print(json.dumps({"event": "registry_rpc_timeout_unavailable", "method": method}))

        async def request() -> Any:
            response = await js_fetch(url, javascript(options))
            if response.status != 200:
                print(json.dumps({"event": "registry_rpc_http_error", "method": method,
                                  "httpStatus": int(response.status)}))
                raise RuntimeError("Registry RPC unavailable")
            result = json.loads((await bounded_bytes(response, 262144)).decode("utf-8"))
            if (type(result) is not dict or result.get("jsonrpc") != "2.0"
                    or type(result.get("id")) is not int or result["id"] != 1
                    or "result" not in result or "error" in result):
                raise RuntimeError("Registry RPC unsuccessful")
            return result["result"]
        return await request()

    async def sign_registry_message(self, message: bytes) -> bytes:
        encoded = str(getattr(self.env, "SOLANA_RELAYER_SEED", ""))
        seed = base64.b64decode(encoded, validate=True)
        if len(seed) != 32:
            raise ValueError("Registry signing key unavailable")
        # Only the hot relayer seed is deployed. Cold/admin/program keys stay in Keychain.
        pkcs8 = bytes.fromhex("302e020100300506032b657004220420") + seed
        key = await web_crypto.subtle.importKey("pkcs8", Uint8Array.new(javascript(list(pkcs8))),
                                               "Ed25519", False, javascript(["sign"]))
        signature = bytes(python_value(Uint8Array.new(await web_crypto.subtle.sign(
            "Ed25519", key, Uint8Array.new(javascript(list(message)))))))
        public_key = base58_decode(str(self.env.SOLANA_RELAYER), length=32)
        if not await self.verify_wallet_signature(public_key, message, signature):
            raise ValueError("Registry signing identity mismatch")
        return signature

    def registry(self, db: D1Database) -> SolanaRegistry | None:
        if str(getattr(self.env, "SOLANA_REGISTRY_ENABLED", "false")).lower() != "true":
            return None
        program = base58_decode(str(self.env.SOLANA_PROGRAM_ID), length=32)
        relayer = base58_decode(str(self.env.SOLANA_RELAYER), length=32)
        async def authorize_spend(amount: int) -> None:
            await reserve_daily_spend(db, amount, int(time.time() * 1000), 50_000_000)
        transport = SolanaRpcTransport(self.registry_rpc, self.sign_registry_message,
            program_id=program, relayer=relayer,
            expected_genesis_hash="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
            authorize_spend=authorize_spend)
        return SolanaRegistry(db, transport, program_id=program, relayer=relayer,
                              now_ms=lambda: int(time.time() * 1000),
                              random_token=lambda: secrets.token_urlsafe(24))

    async def verify_wallet_signature(self, public_key: bytes, message: bytes,
                                      signature: bytes) -> bool:
        # Verify server-retained challenge bytes with the runtime's Ed25519 implementation.
        key_bytes = Uint8Array.new(javascript(list(public_key)))
        signature_bytes = Uint8Array.new(javascript(list(signature)))
        message_bytes = Uint8Array.new(javascript(list(message)))
        key = await web_crypto.subtle.importKey(
            "raw", key_bytes, "Ed25519", False, javascript(["verify"]))
        return bool(await web_crypto.subtle.verify("Ed25519", key, signature_bytes, message_bytes))

    def application(self) -> Application:
        session_secret = getattr(self.env, "SESSION_SECRET", None)
        if not isinstance(session_secret, str) or len(session_secret) < 32:
            raise AppError(503, "configuration_unavailable", "Service configuration is temporarily unavailable.")
        providers = []
        # The Gemini credential lives only in the relay Worker; this Worker sends a
        # placeholder that the relay replaces. A direct key is honoured only without a relay.
        gemini = "relay" if self.gemini_relay() else str(getattr(self.env, "GEMINI_API_KEY", "") or "")
        if gemini:
            providers.append(ProviderConfig("gemini", str(self.env.GEMINI_MODEL), gemini))
        if getattr(self.env, "AI", None) is not None:
            providers.append(ProviderConfig("cloudflare", str(self.env.CLOUDFLARE_AI_MODEL)))
        ai = AiCoordinator(providers, self.request_json, self.request_text, timeout_seconds=45)
        key = session_secret.encode()
        db = D1Database(getattr(self, "db_binding", None) or self.env.DB)
        return Application(
            db, ai, now_ms=lambda: int(time.time() * 1000),
            token_hash=lambda token: hmac.new(key, token.encode(), hashlib.sha256).hexdigest(),
            random_token=lambda: secrets.token_urlsafe(32),
            source_watch_enabled=str(getattr(self.env, "SOURCE_WATCH_ENABLED", "false")).lower() == "true",
            live_markets_enabled=str(getattr(self.env, "LIVE_MARKETS_ENABLED", "false")).lower() == "true",
            billing_sandbox_enabled=str(getattr(self.env, "BILLING_SANDBOX_ENABLED", "false")).lower() == "true",
            registry=self.registry(db),
            attestation_relayer=self.relayer_public_key(),
            attestation_sign=self.sign_registry_message if self.relayer_public_key() else None,
            mainnet_rpc=self.mainnet_rpc if self.mainnet_rpc_urls() else None,
        )

    def mainnet_rpc_urls(self) -> list[str]:
        """A keyed endpoint (secret) first, then the public failover list from the vars."""
        keyed = getattr(self.env, "SOLANA_MAINNET_RPC_KEYED", None)
        public = str(getattr(self.env, "SOLANA_MAINNET_RPC", "") or "")
        urls = [str(keyed)] if isinstance(keyed, str) and keyed else []
        return urls + [url.strip() for url in public.split(",") if url.strip()]

    async def mainnet_rpc(self, method: str, params: list[Any]) -> Any:
        """Read-only mainnet JSON-RPC. Public endpoints throttle Cloudflare egress, so each
        configured URL is tried in turn and a throttled or failing one is skipped."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        last: Exception | None = None
        for url in self.mainnet_rpc_urls():
            try:
                response = await js_fetch(url, javascript({
                    "method": "POST", "headers": {"Content-Type": "application/json"}, "body": body,
                }))
            except JsException as exc:
                last = RuntimeError("mainnet RPC transport failed")
                last.__cause__ = exc
                continue
            if response.status != 200:
                print(json.dumps({"event": "mainnet_rpc_status", "method": method, "status": response.status,
                                  "endpoint": urlsplit(url).hostname}))
                last = RuntimeError(f"mainnet RPC status {response.status}")
                continue
            payload = json.loads(await response.text())
            if "error" in payload:
                code = (payload["error"] or {}).get("code") if isinstance(payload["error"], dict) else None
                print(json.dumps({"event": "mainnet_rpc_error", "method": method, "code": code,
                                  "endpoint": urlsplit(url).hostname}))
                last = RuntimeError("mainnet RPC error " + str(code))
                continue
            return payload.get("result")
        raise last or RuntimeError("no mainnet RPC endpoint is configured")

    def relayer_public_key(self) -> bytes | None:
        """The hot relayer pays attestation fees; only present when its seed is deployed."""
        relayer = str(getattr(self.env, "SOLANA_RELAYER", "") or "")
        seed = getattr(self.env, "SOLANA_RELAYER_SEED", None)
        if not relayer or not isinstance(seed, str) or not seed:
            return None
        try:
            return base58_decode(relayer, length=32)
        except ValueError:
            return None

    def gemini_relay(self) -> tuple[str, str] | None:
        proxy = str(getattr(self.env, "AI_PROXY_URL", "") or "")
        token = getattr(self.env, "AI_PROXY_TOKEN", None)
        if proxy and isinstance(token, str) and len(token) >= 32:
            return proxy, token
        return None

    async def request_json(self, url: str, method: str, headers: dict[str, str],
                           body: dict[str, Any]) -> dict[str, Any]:
        if url.startswith("workers-ai://"):
            try:
                result = await self.env.AI.run(url.removeprefix("workers-ai://"), javascript(body))
            except JsException as exc:
                print(json.dumps({"event": "ai_transport_unavailable", "provider": "cloudflare"}))
                raise RuntimeError("Workers AI could not complete the request") from exc
            return python_value(result)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {GEMINI_HOST, "api.openai.com"}:
            raise ValueError("Unapproved AI endpoint")
        fetch_url, fetch_headers = url, dict(headers)
        relay = self.gemini_relay()
        if parsed.hostname == GEMINI_HOST and relay:
            # Gemini rejects some default execution locations; the relay Worker is region-placed
            # and holds the only Gemini credential, so no key header leaves this Worker.
            fetch_url = relay[0]
            fetch_headers = {name: value for name, value in fetch_headers.items() if name.lower() != "x-goog-api-key"}
            fetch_headers.update({"X-Forecast-Proxy-Target": url, "Authorization": "Bearer " + relay[1]})
        try:
            response = await js_fetch(fetch_url, javascript({
                "method": method, "headers": fetch_headers, "body": json.dumps(body), "redirect": "manual",
            }))
        except JsException as exc:
            raise RuntimeError("AI transport could not complete the request") from exc
        if not 200 <= response.status < 300:
            # Never include upstream URLs, headers, credentials or raw error bodies in logs.
            error_status = None
            error_category = "unclassified"
            try:
                rejected = json.loads((await bounded_bytes(response, 16384)).decode("utf-8"))
                details = rejected.get("error", {})
                candidate = details.get("status")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Z_]{1,60}", candidate):
                    error_status = candidate
                message = str(details.get("message", "")).lower()
                for category, phrases in (
                    ("location_restricted", ("location is not supported", "region is not supported")),
                    ("schema_rejected", ("schema", "generation_config", "generationconfig")),
                    ("quota_exceeded", ("quota", "rate limit")),
                    ("credential_rejected", ("api key", "permission")),
                ):
                    if any(phrase in message for phrase in phrases):
                        error_category = category
                        break
            except (ValueError, UnicodeError, TypeError, AttributeError):
                pass
            print(json.dumps({"event": "ai_provider_http_error", "provider": "gemini"
                              if parsed.hostname == GEMINI_HOST else "openai", "proxied": fetch_url != url,
                              "httpStatus": int(response.status), "errorStatus": error_status,
                              "category": error_category}))
            failure = RuntimeError("AI provider returned an unsuccessful HTTP response")
            failure.http_status = int(response.status)
            raise failure
        raw = await bounded_bytes(response, MAX_PROVIDER_BYTES)
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("AI provider response must be an object")
        return result

    async def request_text(self, url: str, method: str, headers: dict[str, str]) -> TextResponse:
        try:
            response = await js_fetch(url, javascript({"method": method, "headers": headers,
                                                       "redirect": "manual"}))
        except JsException as exc:
            raise SourceUnavailable("Official source connection could not complete") from exc
        relevant = {name: str(response.headers.get(name)) for name in
                    ("content-type", "content-length", "location", "etag", "last-modified") if response.headers.get(name)}
        try:
            raw = await bounded_bytes(response, MAX_SOURCE_BYTES)
            text = raw.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise SourceRejected("The source exceeds the size limit or is not valid UTF-8.") from exc
        return TextResponse(status=int(response.status), body=text, headers=relevant)

    async def fetch(self, request: Any) -> Response:
        parsed = urlsplit(str(request.url))
        path = unquote(parsed.path).rstrip("/") or "/"
        if not path.startswith("/api/"):
            return await self.env.ASSETS.fetch(request)
        bookmark = cookie_token(request, BOOKMARK_COOKIE)
        if bookmark is not None and not BOOKMARK_PATTERN.fullmatch(bookmark):
            bookmark = None
        # Writes and operator work read from the primary; browser GETs read from the nearest
        # replica at or after their own bookmark.
        self.db_binding = database_session(
            self.env.DB, bookmark=bookmark,
            primary=str(request.method).upper() != "GET" or path.startswith("/api/admin/"))
        response = await self.api(request, parsed, path)
        attach_bookmark(response, self.db_binding, bookmark)
        return response

    async def api(self, request: Any, parsed: Any, path: str) -> Response:
        try:
            return await asyncio.create_task(self.route_api(request, parsed, path))
        except AppError as error:
            if error.status >= 500 and error.__cause__ is not None:
                cause = error.__cause__
                print(json.dumps({"event": "application_error", "errorType": type(cause).__name__,
                                  "reasonCode": getattr(cause, "code", None),
                                  "frames": [{"function": f.name, "line": f.lineno}
                                             for f in traceback.extract_tb(cause.__traceback__)[-4:]]}))
            return api_response({"code": error.code, "message": error.message},
                                status=error.status, error=True)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return api_response({"code": "invalid_request", "message": "Check the input format and length."},
                                status=400, error=True)
        except Exception as error:
            # Exception class is enough for tracing without disclosing query data or secrets.
            print(json.dumps({"event": "request_failed", "path": path[:100],
                              "errorType": type(error).__name__,
                              "frames": [{"function": f.name, "line": f.lineno}
                                         for f in traceback.extract_tb(error.__traceback__)[-4:]]}))
            return api_response({"code": "service_unavailable", "message": "Please try again shortly."},
                                status=503, error=True)

    async def route_risk_v2(self, app: Any, path: str, body: Any) -> Any:
        """Operator-authenticated v2 registry: typed targets, reviewed profiles, separate clocks."""
        actor = "authenticated-operator"
        if path == "/api/admin/risk/v2/definitions":
            if set(body) != {"feedId", "definition"}:
                raise ValueError("Feed identity and canonical definition are required")
            digest = await admit_definition(app.db, feed_id=body["feedId"],
                                            definition=from_dict(CanonicalRiskDefinitionV2, body["definition"]),
                                            approved_by=actor, now_ms=app.now_ms())
            return api_response({"status": "admitted", "definitionHash": digest}, status=201)
        if path == "/api/admin/risk/v2/profiles":
            if set(body) != {"feedId", "profile"}:
                raise ValueError("Feed identity and mapping profile are required")
            digest = await admit_profile(app.db, feed_id=body["feedId"],
                                         profile=from_dict(RiskMappingProfileV2, body["profile"]),
                                         approved_by=actor, now_ms=app.now_ms())
            return api_response({"status": "admitted", "profileHash": digest}, status=201)
        if path == "/api/admin/risk/v2/bindings":
            if set(body) != {"feedId", "binding"}:
                raise ValueError("Feed identity and canonical binding are required")
            binding = from_dict(RiskFeedBindingV2, body["binding"])
            await approve_binding_v2(app.db, feed_id=body["feedId"], binding=binding, approved_by=actor,
                                     now_ms=app.now_ms())
            return api_response({"status": "approved", "bindingId": binding.binding_id}, status=201)
        refresh = re.fullmatch(r"/api/admin/risk/v2/bindings/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/refresh", path)
        if refresh:
            if body:
                raise ValueError("Refresh uses the approved immutable question")
            return api_response(await refresh_bound_prediction_v2(app, refresh[1]))
        revoke = re.fullmatch(r"/api/admin/risk/v2/bindings/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/revoke", path)
        if revoke:
            if set(body) != {"reason"} or type(body["reason"]) is not str:
                raise ValueError("A revocation reason is required")
            await revoke_binding_v2(app.db, binding_id=revoke[1], revoked_by=actor, now_ms=app.now_ms(),
                                    reason=body["reason"])
            return api_response({"status": "revoked", "bindingId": revoke[1]})
        publish = re.fullmatch(r"/api/admin/risk/v2/feeds/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/publish", path)
        if publish:
            if set(body) != {"weightSetHash", "weightSetVersion", "calibrationCohortId"}:
                raise ValueError("An admitted weight reference and calibration cohort are required")
            weight = await app.db.first("SELECT body FROM artifacts WHERE hash=? AND kind='risk-weight-set'",
                                        (body["weightSetHash"],))
            if (not weight or content_hash(json.loads(weight["body"])) != body["weightSetHash"]
                    or json.loads(weight["body"]).get("version") != body["weightSetVersion"]):
                raise ValueError("Weight reference is not admitted")
            envelope = await self.publish_risk_v2(app, publish[1], body["weightSetHash"], body["weightSetVersion"],
                                                  body["calibrationCohortId"])
            return api_response({"status": "published", "envelope": to_dict(envelope)}, status=201)
        operate = re.fullmatch(r"/api/admin/risk/v2/feeds/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/operate", path)
        if operate:
            if set(body) != {"weightSetHash", "weightSetVersion", "calibrationCohortId", "enabled"} \
                    or type(body["enabled"]) is not bool:
                raise ValueError("Operation needs an admitted weight reference, cohort and enabled flag")
            await configure_operation(app.db, feed_id=operate[1], weight_set_hash=body["weightSetHash"],
                                      weight_set_version=body["weightSetVersion"],
                                      calibration_cohort_id=body["calibrationCohortId"], enabled=body["enabled"],
                                      configured_by=actor, now_ms=app.now_ms())
            return api_response({"status": "configured", "feedId": operate[1], "enabled": body["enabled"]})
        if path == "/api/admin/risk/v2/series":
            if set(body) != {"series", "enabled"} or type(body["enabled"]) is not bool:
                raise ValueError("A canonical series template and enabled flag are required")
            series = from_dict(RiskFeedSeriesV2, body["series"])
            digest = await configure_series(app.db, series=series, enabled=body["enabled"], configured_by=actor,
                                            now_ms=app.now_ms())
            return api_response({"status": "configured", "seriesId": series.series_id, "seriesHash": digest,
                                 "enabled": body["enabled"]})
        if path == "/api/admin/risk/v2/operate":
            if body:
                raise ValueError("Operation ticks take no parameters")
            outcomes = await operate_feeds_v2(app.db, now_ms=app.now_ms(),
                refresh=lambda binding_id: refresh_bound_prediction_v2(app, binding_id),
                publish=lambda feed_id, digest, version, cohort: self.publish_risk_v2(app, feed_id, digest,
                                                                                      version, cohort),
                seed=lambda question: app.seed(question, uncertainty_band=None, canonical_risk=True))
            return api_response({"status": "ticked", "feeds": outcomes})
        raise AppError(404, "not_found", "Unknown risk v2 route.")

    async def publish_risk_v2(self, app: Any, feed_id: str, weight_set_hash: str, weight_set_version: str,
                              calibration_cohort_id: str) -> Any:
        public_key = self.relayer_public_key()
        if public_key is None:
            raise AppError(503, "risk_signer_unavailable", "Risk feed signing is not configured.")
        try:
            return await publish_feed_v2(app.db, feed_id=feed_id,
                genesis_hash="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
                key_id="forecast-relayer-" + hashlib.sha256(public_key).hexdigest()[:16],
                public_key_hex=public_key.hex(), signer=self.sign_registry_message, now_ms=app.now_ms(),
                weight_set_hash=weight_set_hash, weight_set_version=weight_set_version,
                calibration_cohort_id=calibration_cohort_id)
        except Exception as error:
            # D1 reports constraint/statement failures by class and column, never row data;
            # a bounded prefix is enough to distinguish CAS rejection from a broken statement.
            detail = str(error)
            print(json.dumps({"event": "risk_v2_publish_failed", "feedId": feed_id,
                              "errorType": type(error).__name__,
                              "detail": detail[:160] if detail.startswith(("D1_", "Error: D1_")) else None}))
            raise

    async def route_api(self, request: Any, parsed: Any, path: str) -> Response:
        method = str(request.method).upper()
        if method not in {"GET", "POST", "PATCH"}:
            raise AppError(405, "method_not_allowed", "This request method is not supported.")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        is_admin = path.startswith("/api/admin/")
        if is_admin:
            admin_token = getattr(self.env, "ADMIN_TOKEN", None)
            supplied = str(request.headers.get("authorization") or "")
            authorized = (isinstance(admin_token, str) and len(admin_token) >= 32
                          and hmac.compare_digest(supplied, "Bearer " + admin_token))
            if not authorized and path in ("/api/admin/risk/v2/operate", "/api/admin/sweep"):
                # A scheduler credential may trigger work and nothing else, so a borrowed
                # cron service never has to hold the operator secret. Unset means this
                # branch cannot authorize anyone.
                scheduler_token = getattr(self.env, "SCHEDULER_TOKEN", None)
                authorized = (isinstance(scheduler_token, str) and len(scheduler_token) >= 32
                              and hmac.compare_digest(supplied, "Bearer " + scheduler_token))
            if not authorized:
                raise AppError(403, "forbidden", "You do not have access to this action.")
        if method != "GET" and not is_admin:
            supplied = request.headers.get("origin")
            if supplied != origin or request.headers.get("X-Forecast-Client") != "web":
                raise AppError(403, "origin_denied", "Please submit this request from the Forecast website.")
        body: dict[str, Any] = {}
        if method != "GET":
            if "application/json" not in str(request.headers.get("content-type") or "").lower():
                raise AppError(415, "json_required", "A JSON request is required.")
            raw = await bounded_bytes(request, MAX_PROVIDER_BYTES if is_admin else MAX_BODY_BYTES)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Request must be an object")
        app = self.application()
        if path == "/api/health" and method == "GET":
            row = await app.db.first("SELECT 1 AS ok")
            return api_response({"ok": bool(row and row["ok"] == 1), "version": VERSION})
        if path.startswith("/api/risk/feeds/") and method == "GET":
            risk_feed = re.fullmatch(r"/api/risk/feeds/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})", path)
            if risk_feed is None:
                raise ValueError("Invalid risk feed identity")
            envelope = await latest_feed(app.db, feed_id=risk_feed[1])
            now = app.now_ms()
            status = "unavailable" if envelope is None else "current" if envelope.payload.expires_at_ms > now else "stale"
            return api_response({"status": status, "serverTime": now,
                                 "envelope": to_dict(envelope) if envelope else None})
        if path.startswith("/api/risk/v2/feeds/") and method == "GET":
            risk_feed = re.fullmatch(r"/api/risk/v2/feeds/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})", path)
            if risk_feed is None:
                raise ValueError("Invalid risk feed identity")
            envelope_v2 = await latest_feed_v2(app.db, feed_id=risk_feed[1])
            now = app.now_ms()
            status = ("unavailable" if envelope_v2 is None
                      else "current" if envelope_v2.payload.expires_at_ms > now else "stale")
            return api_response({"status": status, "serverTime": now, "protocol": "forecast-risk-feed-v2",
                                 "envelope": to_dict(envelope_v2) if envelope_v2 else None})
        if path == "/api/status" and method == "GET":
            return api_response({"serverTime": app.now_ms(), "version": VERSION,
                "providers": list(app.ai.configured_providers), "challengeHours": CHALLENGE_MS // 3600000,
                "chain": {"status": "configured" if app.registry else "unconnected",
                          "network": "devnet" if app.registry else None,
                          "programId": str(getattr(self.env, "SOLANA_PROGRAM_ID", "")) if app.registry else None,
                          "relayEnabled": str(getattr(self.env, "SOLANA_REGISTRY_RELAY_ENABLED", "false")).lower() == "true",
                          "transaction": None},
                "features": {"sourceWatch": app.automation.enabled, "liveMarkets": app.markets.live_enabled,
                             "billing": {"billable": False, "mode": "sandbox"}}})
        if is_admin:
            if path == "/api/admin/risk/v2/health" and method == "GET":
                return api_response(await operations_health(app.db, now_ms=app.now_ms()))
            export = re.fullmatch(r"/api/admin/risk/v2/feeds/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/training", path)
            if export and method == "GET":
                return api_response(await training_export(app.db, feed_id=export[1], now_ms=app.now_ms()))
            if path == "/api/admin/analytics" and method == "GET":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"start", "end", "cohortStart", "cohortEnd"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("Analytics accepts one UTC window per parameter")
                if any(not re.fullmatch(r"[0-9]{1,16}", v[0]) for v in query.values()):
                    raise ValueError("Analytics windows use integer UTC milliseconds")
                now = app.now_ms()
                end = int(query["end"][0]) if "end" in query else now // 86400000 * 86400000
                start = int(query["start"][0]) if "start" in query else max(0, end - 30 * 86400000)
                return api_response(await product_analytics(app.db, as_of_ms=now,
                    window_start_ms=start, window_end_ms=end,
                    cohort_start_ms=int(query["cohortStart"][0]) if "cohortStart" in query else None,
                    cohort_end_ms=int(query["cohortEnd"][0]) if "cohortEnd" in query else None))
            if path.startswith("/api/admin/risk/v2/") and method == "POST":
                return await self.route_risk_v2(app, path, body)
            if path == "/api/admin/risk/seed" and method == "POST":
                if set(body) != {"question"} or type(body["question"]) is not str:
                    raise ValueError("A canonical risk question is required")
                # Rare tail events are useful risk questions even below the
                # ordinary editorial uncertainty band. Compiler, validation,
                # publication and later explicit binding approval still apply.
                return api_response(await app.seed(body["question"], uncertainty_band=None, canonical_risk=True), status=201)
            refresh = re.fullmatch(r"/api/admin/risk/bindings/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/refresh", path)
            if refresh and method == "POST":
                if body:
                    raise ValueError("Refresh uses the approved immutable question")
                return api_response(await refresh_bound_prediction(app, refresh[1]))
            if path == "/api/admin/risk/weights" and method == "POST":
                if set(body) != {"document"} or not isinstance(body["document"], dict):
                    raise ValueError("A canonical weight document is required")
                document = body["document"]
                if document.get("version") != "source-calibration-v1":
                    raise ValueError("Unsupported weight document version")
                digest = content_hash(document)
                canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                if len(canonical.encode("utf-8")) > 65536:
                    raise ValueError("Weight document exceeds limit")
                # This is authenticated immutable artifact admission, not a claim
                # of calibrated performance. The risk consumer validates its record.
                await app.db.execute("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) "
                                     "VALUES(?,?,?,?,?)", (digest, "risk-weight-set", canonical,
                                                          "application/json", app.now_ms()))
                retained = await app.db.first("SELECT kind,body FROM artifacts WHERE hash=?", (digest,))
                if not retained or retained["kind"] != "risk-weight-set" or retained["body"] != canonical:
                    raise ValueError("Weight artifact identity mismatch")
                return api_response({"status": "stored", "hash": digest, "version": document["version"]}, status=201)
            if path == "/api/admin/risk/bindings" and method == "POST":
                if set(body) != {"feedId", "binding"}:
                    raise ValueError("Feed identity and canonical binding are required")
                binding = from_dict(RiskFeedBinding, body["binding"])
                await approve_binding(app.db, feed_id=body["feedId"], binding=binding,
                                      approved_by="authenticated-operator", now_ms=app.now_ms())
                return api_response({"status": "approved", "bindingId": binding.binding_id}, status=201)
            revoke = re.fullmatch(r"/api/admin/risk/bindings/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/revoke", path)
            if revoke and method == "POST":
                if set(body) != {"reason"} or type(body["reason"]) is not str:
                    raise ValueError("A revocation reason is required")
                await revoke_binding(app.db, binding_id=revoke[1], revoked_by="authenticated-operator",
                                     now_ms=app.now_ms(), reason=body["reason"])
                return api_response({"status": "revoked", "bindingId": revoke[1]})
            publish = re.fullmatch(r"/api/admin/risk/feeds/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/publish", path)
            if publish and method == "POST":
                if set(body) != {"weightSetHash", "weightSetVersion"}:
                    raise ValueError("An admitted weight reference is required")
                weight = await app.db.first("SELECT body FROM artifacts WHERE hash=? AND kind='risk-weight-set'",
                                            (body["weightSetHash"],))
                if (not weight or content_hash(json.loads(weight["body"])) != body["weightSetHash"]
                        or json.loads(weight["body"]).get("version") != body["weightSetVersion"]):
                    raise ValueError("Weight reference is not admitted")
                public_key = self.relayer_public_key()
                if public_key is None:
                    raise AppError(503, "risk_signer_unavailable", "Risk feed signing is not configured.")
                envelope = await publish_feed(app.db, feed_id=publish[1],
                    genesis_hash="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
                    key_id="forecast-relayer-" + hashlib.sha256(public_key).hexdigest()[:16],
                    public_key_hex=public_key.hex(), signer=self.sign_registry_message,
                    now_ms=app.now_ms(), weight_set_hash=body["weightSetHash"],
                    weight_set_version=body["weightSetVersion"], window_ms=21600000)
                return api_response({"status": "published", "envelope": to_dict(envelope)}, status=201)
            participation = re.fullmatch(r"/api/admin/forecasts/([A-Za-z0-9_.:-]{1,128})/participation", path)
            if participation and method == "GET":
                return api_response(await app.participation_holds.status(participation[1]))
            if participation and method == "POST":
                return api_response(await app.participation_holds.change(participation[1], body))
            if path == "/api/admin/seed" and method == "POST":
                return api_response(await app.seed(body.get("question", "")), status=201)
            if path == "/api/admin/sweep" and method == "POST":
                # Four source polls per five-minute sweep keeps every watched publisher
                # and article current; one per tick starved the market source gate.
                result = await app.run_automation(limit=4)
                if app.registry is not None:
                    if str(getattr(self.env, "SOLANA_REGISTRY_RELAY_ENABLED", "false")).lower() == "true":
                        try:
                            result["registry"] = await app.registry.sync(limit=3)
                        except Exception:
                            result["registry"] = {"status": "retry_pending"}
                    else:
                        result["registry"] = {"status": "relay_paused"}
                return api_response(result)
            if path == "/api/admin/registry/health" and method == "GET":
                if app.registry is None:
                    raise AppError(503, "registry_disabled", "Devnet registry is not enabled.")
                # Fixed non-transaction message: no user-controlled signing oracle.
                await self.sign_registry_message(b"forecast-network:registry-key-self-test:v1")
                result = {"signerVerified": True, "rpcAvailable": False}
                try:
                    await app.registry.transport.genesis_hash()
                    result["rpcAvailable"] = True
                except SolanaRpcError:
                    pass
                return api_response(result)
            if path == "/api/admin/registry/run" and method == "POST":
                if app.registry is None:
                    raise AppError(503, "registry_disabled", "Devnet registry is not enabled.")
                try:
                    forecast_id = body.get("forecastId")
                    if forecast_id is not None:
                        if type(forecast_id) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", forecast_id):
                            raise ValueError("Invalid forecast identity")
                        await app.registry.enable(forecast_id)
                    return api_response(await app.registry.sync(limit=3))
                except (RegistryError, SolanaRpcError) as error:
                    # These classes contain only fixed local gate descriptions, never provider bodies.
                    return api_response({"status": "retry_pending", "reason": str(error)}, status=503)
            if path == "/api/admin/ai/health" and method == "GET":
                probe = await self.request_json(
                    f"https://{GEMINI_HOST}/v1beta/models/{self.env.GEMINI_MODEL}:generateContent", "POST",
                    {"Content-Type": "application/json",
                     "x-goog-api-key": "relay" if self.gemini_relay() else str(getattr(self.env, "GEMINI_API_KEY", "") or "")},
                    {"contents": [{"parts": [{"text": "Reply with the single word OK."}]}],
                     "generationConfig": {"maxOutputTokens": 8}})
                return api_response({"provider": "gemini", "ok": bool(probe.get("candidates")),
                                     "proxied": self.gemini_relay() is not None})
            if path == "/api/admin/automation" and method == "GET":
                return api_response(await app.automation.status())
            if path == "/api/admin/automation/run" and method == "POST":
                return api_response(await app.run_automation(limit=1))
            if path == "/api/admin/markets/treasury" and method == "GET":
                return api_response(await app.markets.budget(parse_qs(parsed.query).get("mode", ["shadow"])[0]))
            if path == "/api/admin/markets/treasury" and method == "POST":
                return api_response(await app.markets.fund_treasury(body.get("amountPoints"), body.get("idempotencyKey"), body.get("mode", "shadow")))
            market_admin = re.fullmatch(r"/api/admin/forecasts/([A-Za-z0-9_.:-]{1,128})/market", path)
            if market_admin and method == "POST":
                return api_response(await app.markets.create(market_admin[1], mode=body.get("mode", "shadow"),
                    expected_specification_hash=body.get("specificationHash")))
            if path == "/api/admin/billing/sandbox" and method == "GET":
                return api_response(await app.billing.summary())
            if path == "/api/admin/billing/sandbox" and method == "POST":
                # This API deliberately has no path for real payment confirmation.
                action = body.get("action")
                if action == "fund":
                    return api_response(await app.billing.fund_capital(body.get("amountCents"), body.get("idempotencyKey")))
                if action == "quote":
                    return api_response(await app.billing.quote(body.get("invoiceId"), body.get("ownerHash"), body.get("scopeHash"),
                        price_cents=body.get("priceCents", 200), cost_cap_cents=body.get("costCapCents", 115),
                        max_attempts=body.get("maxAttempts", 3), expires_at=body.get("expiresAt"),
                        refund_until=body.get("refundUntil"), key=body.get("idempotencyKey")))
                if action == "sandbox_receipt":
                    return api_response(await app.billing.accept_sandbox_receipt(body.get("invoiceId"), body.get("ownerHash"),
                        reference=body.get("reference"), amount_cents=body.get("amountCents"), scope_hash=body.get("scopeHash"), key=body.get("idempotencyKey")))
                if action == "command" and isinstance(body.get("payload", {}), dict):
                    return api_response(await app.billing.command(body.get("invoiceId"), body.get("ownerHash"),
                        body.get("command"), body.get("idempotencyKey"), **body.get("payload", {})))
                raise AppError(400, "invalid_input", "Choose a sandbox accounting operation.")
            translation = re.fullmatch(
                r"/api/admin/forecasts/([A-Za-z0-9_.:-]{1,128})/translations/en", path)
            if translation and method == "POST":
                return api_response(await app.set_translation(translation[1], body))
            adjudication = re.fullmatch(r"/api/admin/forecasts/([A-Za-z0-9_.:-]{1,128})/adjudicate", path)
            if adjudication and method == "POST":
                if type(body.get("revision")) is not int or body["revision"] < 0:
                    raise ValueError("Expected revision is required")
                artifacts = tuple(Artifact(**record) for record in body.get("artifacts", []))
                return api_response(await app.adjudicate_forecast(
                    adjudication[1], from_dict(Resolution, body.get("resolution")),
                    from_dict(AIProvenance, body.get("adjudicator")), artifacts,
                    body.get("idempotencyKey", ""), expected_revision=body["revision"]))
            raise AppError(404, "not_found", "This page could not be found.")
        ip = str(request.headers.get("CF-Connecting-IP") or "local")
        fingerprint = hmac.new(str(self.env.SESSION_SECRET).encode(), ip.encode(), hashlib.sha256).hexdigest()
        if method != "GET":
            await app.rate_limit("http:" + fingerprint, 180, 3600000)
        session = cookie_token(request)
        auth_context = cookie_token(request, AUTH_CONTEXT_COOKIE)
        if path.startswith("/api/auth/wallet/") and method == "POST":
            wallet_login = WalletLogin(app.db, now_ms=app.now_ms, token_hash=app.auth.token_hash,
                random_token=app.random_token, verify_signature=self.verify_wallet_signature, origin=origin,
                on_create=lambda: app.rate_limit("wallet-register:" + fingerprint, 5, 86400000))
            if path == "/api/auth/wallet/context":
                await app.rate_limit("wallet-context:" + fingerprint, 60, 3600000)
                if body:
                    raise ValueError("Context bootstrap does not accept client identity")
                result = await wallet_login.context(auth_context)
                token = result.pop("contextToken")
                return api_response(result, context=token)
            if path == "/api/auth/wallet/challenge":
                await app.rate_limit("wallet-login-challenge:" + fingerprint, 30, 3600000)
                return api_response(await wallet_login.challenge(auth_context, body, session))
            if path == "/api/auth/wallet/verify":
                await app.rate_limit("wallet-login-verify:" + fingerprint, 40, 3600000)
                result = await wallet_login.verify(auth_context, body, session)
                token = result.pop("sessionToken")
                return api_response(result, session=token)
            if path == "/api/auth/wallet/cancel":
                if body:
                    raise ValueError("Cancellation does not accept client identity")
                return api_response(await wallet_login.cancel(auth_context))
        if path == "/api/auth/register" and method == "POST":
            if str(getattr(self.env, "WALLET_LOGIN_REQUIRED", "true")).lower() == "true":
                raise AppError(409, "wallet_login_required", "Connect and sign with your wallet to create a profile.")
            await app.rate_limit("register:" + fingerprint, 5, 86400000)
            result = await app.register(body.get("displayName", ""))
            token = result.pop("sessionToken")
            return api_response(result, status=201, session=token)
        if path == "/api/auth/login" and method == "POST":
            await app.rate_limit("login:" + fingerprint, 20, 3600000)
            if not auth_context:
                raise AppError(409, "wallet_context_required", "Start sign-in again before importing your old profile.")
            result = await app.login(body.get("recoveryCode", ""), auth_context)
            token = result.pop("sessionToken")
            return api_response(result, session=token)
        if path == "/api/auth/logout" and method == "POST":
            return api_response(await app.logout(session, auth_context), clear_session=True)
        user = await app.authenticate(session, auth_context)
        user_id = user["id"] if user else None
        query = parse_qs(parsed.query)
        if path == "/api/billing/estimate" and method == "GET":
            return api_response(app.billing.estimate())
        receipt_request = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})/market/receipt", path)
        if receipt_request and method == "GET":
            if user_id is None:
                raise AppError(401, "authentication_required", "Sign in to check your market receipt.")
            return api_response(await app.markets.receipt_status(user_id, receipt_request[1],
                query.get("quoteId", [""])[0], query.get("idempotencyKey", [""])[0]))
        market_request = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})/market(?:/(quote|fill))?", path)
        if market_request:
            identifier, action = market_request.groups()
            if method == "GET" and action is None:
                return api_response(await app.markets.get(identifier))
            if method == "POST" and action == "quote":
                await app.rate_limit("market-quote:" + fingerprint, 60, 60000)
                if user_id is None:
                    if "expectedUserId" in body:
                        raise AppError(409, "account_changed", "Your signed-in account changed. Reload before requesting a quote.")
                    return api_response(await app.markets.preview(identifier, body.get("side"), body.get("spendPoints")))
                require_expected_user(body, user_id)
                return api_response(await app.markets.quote(user_id, identifier, body.get("side"), body.get("spendPoints")))
            if method == "POST" and action == "fill":
                if user_id is None:
                    raise AppError(401, "authentication_required", "Sign in before confirming a market quote.")
                require_expected_user(body, user_id)
                minimum = body.get("minClaimsAtomic")
                if type(minimum) is not str or not re.fullmatch(r"[0-9]{1,16}", minimum):
                    raise AppError(400, "market_invalid_request", "A precise minimum payout is required.")
                return api_response(await app.markets.accept(user_id, identifier, body.get("quoteId"),
                    int(minimum), body.get("idempotencyKey")))
        if path == "/api/me/markets" and method == "GET":
            if user_id is None:
                return api_response({"positions": []})
            selected = query.get("forecastId", [None])[0]
            if selected:
                return api_response({"positions": [await app.markets.positions(user_id, selected)]})
            rows = await app.db.all("SELECT forecast_id FROM point_markets ORDER BY created_at DESC LIMIT 100")
            return api_response({"positions": [await app.markets.positions(user_id, row["forecast_id"]) for row in rows]})
        translation_request = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})/translation", path)
        if translation_request and method == "GET":
            return api_response(await app.display_translations.get(
                translation_request[1], query.get("language", [""])[0]))
        if translation_request and method == "POST":
            return api_response(await app.display_translations.generate(
                translation_request[1], body, fingerprint))
        if path == "/api/me" and method == "GET":
            return api_response(await app.me(user_id))
        if path == "/api/forecasts" and method == "GET":
            return api_response(await app.list_forecasts(user_id=user_id,
                q=query.get("q", [""])[0], category=query.get("category", [""])[0],
                sort=query.get("sort", ["trending"])[0], cursor=query.get("cursor", [None])[0]))
        profile_card = re.fullmatch(r"/api/profile-cards/([0-9a-f]{64})", path)
        if profile_card and method == "GET":
            return api_response(await app.get_profile_card(profile_card[1]))
        match = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})", path)
        if match and method == "GET":
            return api_response(await app.forecast_detail(match[1], user_id))
        integrity = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})/integrity", path)
        if integrity and method == "GET":
            return api_response(await app.integrity(integrity[1]))
        creator = re.fullmatch(r"/api/creators/([A-Za-z0-9_.:-]{1,128})", path)
        if creator and method == "GET":
            return api_response(await app.creator(creator[1], user_id))
        if user_id is None:
            raise AppError(401, "authentication_required", "Create a profile or sign in to continue.")
        if path == "/api/points" and method == "GET":
            return api_response(await PointsService(app.db).summary(user_id))
        if path == "/api/me/share-card" and method == "POST":
            if set(body) != {"expectedUserId"} or not isinstance(body["expectedUserId"], str):
                raise ValueError("Profile publication requires the displayed account precondition")
            if body["expectedUserId"] != user_id:
                raise AppError(409, "profile_owner_changed",
                               "Your signed-in profile changed. Reload your profile before sharing.")
            await app.rate_limit("profile-card:" + user_id, 10, 3600000)
            return api_response(await app.create_profile_card(user_id), status=201)
        if path == "/api/seeker/verify" and method == "POST":
            require_expected_user(body, user_id)
            return api_response(await app.seeker.verify(user_id))
        if path == "/api/wallet" or path.startswith("/api/wallet/"):
            if method != "GET":
                if str(getattr(self.env, "WALLET_LOGIN_REQUIRED", "true")).lower() == "true":
                    raise AppError(409, "wallet_migration_required", "Use wallet sign-in to migrate your existing profile.")
                require_expected_user(body, user_id)
            wallets = WalletService(
                app.db, now_ms=app.now_ms, random_token=lambda: secrets.token_urlsafe(32),
                verify_signature=self.verify_wallet_signature, origin=origin)
            if path == "/api/wallet" and method == "GET":
                return api_response(await wallets.get_wallet(user_id))
            if path == "/api/wallet/challenge" and method == "POST":
                await app.rate_limit("wallet-challenge:" + user_id, 20, 3600000)
                return api_response(await wallets.challenge(user_id, body.get("address", "")))
            if path == "/api/wallet/link" and method == "POST":
                await app.rate_limit("wallet-link:" + user_id, 40, 3600000)
                wallet_body = {key: value for key, value in body.items() if key != "expectedUserId"}
                return api_response(await wallets.link(user_id, wallet_body))
            if path == "/api/wallet/unlink" and method == "POST":
                await app.rate_limit("wallet-unlink:" + user_id, 10, 3600000)
                return api_response(await wallets.unlink(user_id))
        if path == "/api/me" and method == "PATCH":
            return api_response(await app.update_profile(user_id, body.get("displayName", "")))
        if path == "/api/forecasts/compile" and method == "POST":
            await app.rate_limit("compile-ip:" + fingerprint, 12, 86400000)
            return api_response(await app.compile_forecast(user_id, body.get("question", "")))
        if path == "/api/forecasts" and method == "POST":
            return api_response(await app.publish_forecast(user_id, body.get("draftId", ""),
                                                          body.get("idempotencyKey", "")), status=201)
        attest = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})/attest/(prepare|confirm)", path)
        if attest and method == "POST":
            if attest[2] == "prepare":
                return api_response(await app.attestations.prepare(user_id, attest[1], body), status=201)
            return api_response(await app.attestations.confirm(user_id, attest[1], body))
        match = re.fullmatch(r"/api/forecasts/([A-Za-z0-9_.:-]{1,128})/(forecast|disputes|comments|share|evidence)", path)
        if match and method == "POST":
            identifier, action = match.groups()
            if action == "forecast":
                if "stakePoints" in body:
                    require_expected_user(body, user_id)
                    if type(body["stakePoints"]) is not int:
                        raise ValueError("Stake points must be a whole number")
                result = await app.submit_forecast(user_id, identifier, body.get("outcome"),
                    body.get("confidence"), body.get("revision"), body.get("idempotencyKey", ""),
                    stake_points=body.get("stakePoints"))
            elif action == "disputes":
                result = await app.submit_dispute(user_id, identifier, body.get("claim", ""),
                    body.get("evidenceUrl", ""), body.get("ruleClauseId", ""),
                    body.get("explanation", ""), body.get("revision"), body.get("idempotencyKey", ""))
            elif action == "comments":
                result = await app.add_comment(user_id, identifier, body.get("text", ""),
                                               body.get("idempotencyKey", ""))
            elif action == "evidence":
                result = await app.report_evidence(user_id, identifier, body.get("url", ""))
            else:
                result = await app.record_share(identifier, user_id)
            return api_response(result)
        if path == "/api/activity" and method == "GET":
            return api_response(await app.activity(user_id))
        if path == "/api/activity/read" and method == "POST":
            return api_response(await app.read_activity(user_id))
        match = re.fullmatch(r"/api/creators/([A-Za-z0-9_.:-]{1,128})/follow", path)
        if match and method == "POST":
            return api_response(await app.follow(user_id, match[1], body.get("following")))
        raise AppError(404, "not_found", "This page could not be found.")

    async def scheduled(self, controller: Any, env: Any, ctx: Any) -> None:
        # Dispatch through the self service binding so background work runs in the
        # ordinary authenticated fetch path; Gemini itself goes through the placed relay.
        # WorkerEntrypoint initializes wrapped bindings on self.env. Positional
        # handler arguments are not the instance's authoritative configuration.
        bindings = self.env
        token = getattr(bindings, "ADMIN_TOKEN", None)
        if not isinstance(token, str) or len(token) < 32:
            raise RuntimeError("Scheduled dispatch requires the configured operator secret")
        # Each cron pattern owns one job. Only the five-minute sweep is configured:
        # a per-minute Worker cron collided with in-flight requests in the same
        # isolate ("Cannot enter a promising task"), so the signed risk publication
        # tick is driven by the supervised operator host (scripts/operate_risk_v2.py).
        pattern = str(getattr(controller, "cron", "") or "")
        path, event = ("/api/admin/risk/v2/operate", "scheduled_risk_v2") if pattern == "* * * * *" \
            else ("/api/admin/sweep", "scheduled_sweep")
        # The SDK service-binding wrapper uses Python keyword fetch options.
        response = await bindings.SCHEDULED_JOBS.fetch(str(bindings.APP_ORIGIN) + path,
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": "Bearer " + token}, body="{}")
        print(json.dumps({"event": event, "httpStatus": int(response.status)}))
        if not 200 <= response.status < 300:
            raise RuntimeError("Scheduled job dispatch did not complete successfully")
