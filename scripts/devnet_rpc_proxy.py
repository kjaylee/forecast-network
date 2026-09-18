#!/usr/bin/env python3
"""Authenticated, loopback-only relay to the pinned Solana Devnet RPC.

No dependencies, signing keys, arbitrary upstreams or retries. Expose through a
separately configured TLS ingress; this process binds only 127.0.0.1. Generate the
shared token with secrets.token_urlsafe(32), store it in an owned 0600 file, and
send it in X-Forecast-RPC-Token. Never put the token in a URL or command argument.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

UPSTREAM = "https://api.devnet.solana.com"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
AUTH_HEADER = "X-Forecast-RPC-Token"
TOKEN_ENV = "FORECAST_DEVNET_RPC_TOKEN"
METHODS = frozenset({
    "getGenesisHash", "getAccountInfo", "getSignatureStatuses", "getSlot",
    "getBlockTime", "getLatestBlockhash", "getBlockHeight", "getBalance",
    "getMinimumBalanceForRentExemption", "getFeeForMessage", "simulateTransaction",
    "sendTransaction", "isBlockhashValid",
})
MAX_BODY = 16_384
MAX_RESPONSE = 262_144
MAX_HEADERS = 8_192
UPSTREAM_TIMEOUT = 8
CLIENT_TIMEOUT = 5
CONCURRENCY = 6
Forward = Callable[[dict[str, Any]], dict[str, Any]]


class ProxyError(Exception):
    """Only constant public messages belong here; upstream exceptions never escape."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def validate_token(token: str) -> str:
    if type(token) is not str or not 43 <= len(token) <= 512:
        raise ValueError("RPC token must encode at least 32 random bytes")
    try:
        if re.fullmatch(r"[0-9a-fA-F]{64,512}", token) and len(token) % 2 == 0:
            raw = bytes.fromhex(token)
        elif re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", token):
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        else:
            raise ValueError
    except (ValueError, binascii.Error):
        raise ValueError("RPC token must encode at least 32 random bytes") from None
    if len(raw) < 32 or len(set(raw)) < 12:
        raise ValueError("RPC token must encode at least 32 random bytes")
    return token


def load_token(path: Path | None) -> str:
    if path is None:
        return validate_token(os.environ.get(TOKEN_ENV, ""))
    # O_NOFOLLOW plus fstat avoid following a replacement symlink or racing chmod.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > 1024):
            raise ValueError("RPC token file must be an owned 0600 regular file")
        raw = os.read(fd, 1025)
        return validate_token(raw.decode("ascii").removesuffix("\n"))
    finally:
        os.close(fd)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _tree(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [32768]
    budget[0] -= 1
    if depth > 24 or budget[0] < 0:
        raise ValueError("JSON structure exceeds bounds")
    if type(value) is dict:
        for item in value.values():
            _tree(item, depth + 1, budget)
    elif type(value) is list:
        for item in value:
            _tree(item, depth + 1, budget)
    elif type(value) not in (str, int, bool, type(None)):
        # None of the permitted registry calls needs floating-point parameters.
        raise ValueError("unsupported JSON value")


def parse_json(raw: bytes) -> Any:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        _tree(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ProxyError(400, "Invalid JSON") from None


def validate_request(value: Any) -> dict[str, Any]:
    if (type(value) is not dict or set(value) != {"jsonrpc", "id", "method", "params"}
            or value["jsonrpc"] != "2.0" or type(value["method"]) is not str
            or value["method"] not in METHODS or type(value["params"]) is not list
            or len(value["params"]) > 64):
        raise ProxyError(400, "Unsupported RPC request")
    ident = value["id"]
    if not ((type(ident) is int and abs(ident) <= (1 << 53) - 1)
            or (type(ident) is str and len(ident) <= 64)):
        raise ProxyError(400, "Unsupported RPC request")
    return value


def _encode(value: Any) -> bytes:
    try:
        _tree(value)
        data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError):
        raise ProxyError(502, "Invalid upstream response") from None
    if len(data) > MAX_RESPONSE:
        raise ProxyError(502, "Upstream response exceeds limit")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def forward_rpc(payload: dict[str, Any]) -> dict[str, Any]:
    """One bounded request, fixed URL, no ambient HTTP proxy or redirected target."""
    request = urllib.request.Request(UPSTREAM, data=_encode(payload), headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Accept-Encoding": "identity", "User-Agent": "Forecast-Devnet-Relay/1",
    }, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=UPSTREAM_TIMEOUT) as response:
            if response.status != 200 or response.geturl() != UPSTREAM:
                raise ProxyError(502, "Upstream request failed")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ProxyError(502, "Unsupported upstream encoding")
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        status = 429 if exc.code == 429 else 502
        exc.close()
        raise ProxyError(status, "Upstream rate limited" if status == 429 else "Upstream request failed") from None
    except (OSError, urllib.error.URLError, TimeoutError):
        raise ProxyError(502, "Upstream unavailable") from None
    if len(raw) > MAX_RESPONSE:
        raise ProxyError(502, "Upstream response exceeds limit")
    try:
        result = parse_json(raw)
    except ProxyError:
        raise ProxyError(502, "Invalid upstream response") from None
    if type(result) is not dict:
        raise ProxyError(502, "Invalid upstream response")
    return result


def sanitize_response(result: dict[str, Any], ident: int | str) -> dict[str, Any]:
    _encode(result)
    if (type(result) is not dict or result.get("jsonrpc") != "2.0"
            or type(result.get("id")) is not type(ident) or result.get("id") != ident):
        raise ProxyError(502, "Invalid upstream response")
    if set(result) == {"jsonrpc", "id", "result"}:
        return result
    error = result.get("error")
    if (set(result) != {"jsonrpc", "id", "error"} or type(error) is not dict
            or type(error.get("code")) is not int or abs(error["code"]) > (1 << 31)):
        raise ProxyError(502, "Invalid upstream response")
    # Numeric error codes remain available for reconciliation; messages/data may
    # contain upstream internals and are deliberately not reflected to callers.
    return {"jsonrpc": "2.0", "id": ident,
            "error": {"code": error["code"], "message": "Devnet RPC error"}}


class ProxyState:
    def __init__(self, token: str, forward: Forward = forward_rpc) -> None:
        self._token_hash = hashlib.sha256(validate_token(token).encode("ascii")).digest()
        self.forward = forward
        self.lock = threading.Lock()
        self.started_at_ms = int(time.time() * 1000)
        self.started_monotonic = time.monotonic()
        self.pid = os.getpid()
        self.genesis_verified = False
        self.last_probe_ms: int | None = None
        self.last_success_ms: int | None = None
        self.counters = {name: 0 for name in ("requests", "rejected", "forwarded", "errors", "busy", "probes")}

    def count(self, name: str) -> None:
        with self.lock:
            self.counters[name] += 1

    def authenticated(self, token: str) -> bool:
        # Hash first so compare_digest receives fixed-length inputs even for a bad token.
        supplied = hashlib.sha256(token.encode("utf-8")).digest()
        return hmac.compare_digest(supplied, self._token_hash)

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = sanitize_response(self.forward(payload), payload["id"])
        except ProxyError:
            self.count("errors")
            raise
        except Exception:
            self.count("errors")
            raise ProxyError(502, "Upstream unavailable") from None
        self.count("forwarded")
        with self.lock:
            self.last_success_ms = int(time.time() * 1000)
        return response

    def verify_genesis(self) -> None:
        self.count("probes")
        verified = False
        try:
            response = self._call({"jsonrpc": "2.0", "id": "devnet-genesis-probe",
                                   "method": "getGenesisHash", "params": []})
            verified = response.get("result") == GENESIS
            if not verified:
                raise ProxyError(503, "Devnet genesis verification failed")
        finally:
            with self.lock:
                self.genesis_verified = verified
                self.last_probe_ms = int(time.time() * 1000)

    def rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        validate_request(payload)
        # Probe every write. Neither a cached health flag nor a caller's own
        # getGenesisHash response can authorize a transaction on another cluster.
        if payload["method"] == "sendTransaction":
            self.verify_genesis()
        result = self._call(payload)
        if payload["method"] == "getGenesisHash" and result.get("result") != GENESIS:
            with self.lock:
                self.genesis_verified = False
            raise ProxyError(503, "Devnet genesis verification failed")
        return result

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {"service": "forecast-devnet-rpc", "pid": self.pid,
                    "started_at_ms": self.started_at_ms,
                    "uptime_seconds": int(time.monotonic() - self.started_monotonic),
                    "genesis_verified": self.genesis_verified,
                    "last_genesis_probe_ms": self.last_probe_ms,
                    "last_success_ms": self.last_success_ms, "counters": dict(self.counters),
                    "max_concurrency": CONCURRENCY}


class ProxyServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = CONCURRENCY

    def __init__(self, address: tuple[str, int], state: ProxyState) -> None:
        if address[0] != "127.0.0.1":
            raise ValueError("RPC proxy must bind to 127.0.0.1")
        self.state = state
        self.slots = threading.BoundedSemaphore(CONCURRENCY)
        super().__init__(address, ProxyHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, address = super().get_request()
        request.settimeout(CLIENT_TIMEOUT)
        return request, address

    def process_request(self, request: socket.socket | tuple[bytes, socket.socket],
                        client_address: Any) -> None:
        if not isinstance(request, socket.socket):
            raise ValueError("RPC proxy accepts TCP connections only")
        if not self.slots.acquire(blocking=False):
            self.state.count("busy")
            # One bounded write, no new thread/queue for an overloaded connection.
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\nRetry-After: 1\r\n\r\n")
                # Finish the write side, then read what the caller had already sent.
                # Closing a socket that still holds unread data sends a reset rather
                # than a finish, and a reset discards a response the caller has not
                # read yet — so an overloaded proxy looked like a broken connection
                # instead of a rate limit. The read is bounded by the same limit the
                # request body is.
                request.shutdown(socket.SHUT_WR)
                request.recv(MAX_BODY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket.socket | tuple[bytes, socket.socket],
                               client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints tracebacks, which can contain private request data.
        self.state.count("errors")


class ProxyHandler(BaseHTTPRequestHandler):
    server: ProxyServer
    protocol_version = "HTTP/1.0"
    server_version = "ForecastDevnetRPC"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._reply(code, {"error": "Invalid HTTP request"})

    def _reply(self, status: int, value: Any) -> None:
        raw = _encode(value)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _headers(self) -> None:
        headers = list(self.headers.raw_items())
        names = [name.lower() for name, _ in headers]
        if (len(names) != len(set(names)) or len(names) > 32
                or sum(len(name) + len(value) + 4 for name, value in headers) > MAX_HEADERS
                or any("\r" in value or "\n" in value for _, value in headers)
                or "transfer-encoding" in names or "expect" in names):
            raise ProxyError(400, "Invalid request headers")
        if not self.server.state.authenticated(self.headers.get(AUTH_HEADER, "")):
            raise ProxyError(401, "Unauthorized")

    def _handle(self, health: bool) -> None:
        self.server.state.count("requests")
        try:
            self._headers()
            if self.path != ("/health" if health else "/rpc"):
                raise ProxyError(404, "Not found")
            length = self.headers.get("Content-Length", "0" if health else "")
            if not re.fullmatch(r"0|[1-9][0-9]{0,8}", length):
                raise ProxyError(400, "Invalid Content-Length")
            size = int(length)
            if size > MAX_BODY:
                raise ProxyError(413, "Request exceeds limit")
            if health:
                if size:
                    raise ProxyError(400, "Health request must be empty")
                self._reply(200, self.server.state.health())
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ProxyError(415, "JSON content type required")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ProxyError(400, "Incomplete request body")
            payload = validate_request(parse_json(raw))
            self._reply(200, self.server.state.rpc(payload))
        except ProxyError as exc:
            self.server.state.count("rejected")
            self._reply(exc.status, {"error": exc.message})
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.server.state.count("errors")
            self._reply(500, {"error": "RPC proxy unavailable"})

    def do_POST(self) -> None:
        self._handle(False)

    def do_GET(self) -> None:
        self._handle(True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        state = ProxyState(load_token(args.token_file))
        state.verify_genesis()
        with ProxyServer((args.host, args.port), state) as server:
            print(json.dumps({"event": "devnet_rpc_proxy_started", "host": args.host,
                              "port": args.port, **state.health()}), flush=True)
            server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Never print token paths, environment values or raw upstream exceptions.
        print(json.dumps({"event": "devnet_rpc_proxy_startup_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
