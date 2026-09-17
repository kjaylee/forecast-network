"""Real loopback HTTP tests with an injected upstream; no live-chain claims."""

from __future__ import annotations

import ast
import base64
import contextlib
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import devnet_rpc_proxy as proxy

ROOT = Path(__file__).resolve().parents[1]
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def payload(method: str = "getSlot", params: list[Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or []}


def success(request: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request["id"],
            "result": proxy.GENESIS if request["method"] == "getGenesisHash" else 123}


class Response:
    def __init__(self, body: bytes, status: int = 200, url: str = proxy.UPSTREAM) -> None:
        self.body, self.status, self.url = body, status, url
        self.headers: dict[str, str] = {}
        self.read_sizes: list[int] = []

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body[:size]

    def geturl(self) -> str:
        return self.url


class DevnetProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[dict[str, Any]] = []

        def forward(request: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(request)
            return success(request)

        self.state = proxy.ProxyState(TOKEN, forward)
        self.server = proxy.ProxyServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, body: Any = None, *, path: str = "/rpc", method: str = "POST",
                token: str | None = TOKEN, raw: bytes | None = None) -> tuple[int, Any, dict[str, str]]:
        data = raw if raw is not None else json.dumps(body if body is not None else payload()).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[proxy.AUTH_HEADER] = token
        if method == "GET":
            data = b""
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw_result = response.read()
            return response.status, json.loads(raw_result) if raw_result else None, dict(response.getheaders())
        finally:
            connection.close()

    def raw_request(self, headers: bytes, body: bytes = b"") -> bytes:
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(headers + b"\r\n\r\n" + body)
            connection.shutdown(socket.SHUT_WR)
            result = bytearray()
            while chunk := connection.recv(65536):
                result.extend(chunk)
            return bytes(result)

    def test_allowlist_matches_worker(self) -> None:
        tree = ast.parse((ROOT / "apps/web/src/entry.py").read_text())
        method = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "registry_rpc")
        allowed = next(node for node in ast.walk(method)
                       if isinstance(node, ast.Set) and any(isinstance(item, ast.Constant)
                           and item.value == "getGenesisHash" for item in node.elts))
        self.assertEqual(proxy.METHODS, set(ast.literal_eval(allowed)))
        self.assertEqual(len(proxy.METHODS), 13)

    def test_real_http_positive_and_authenticated_health(self) -> None:
        status, body, headers = self.request()
        self.assertEqual((status, body), (200, {"jsonrpc": "2.0", "id": 7, "result": 123}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.calls, [payload()])
        status, body, _ = self.request(path="/health", method="GET")
        self.assertEqual(status, 200)
        self.assertFalse(body["genesis_verified"])
        self.assertIsNone(body["last_genesis_probe_ms"])
        self.assertEqual(body["pid"], os.getpid())
        self.assertEqual(body["counters"]["forwarded"], 1)
        self.assertNotIn(TOKEN, json.dumps(body))
        self.state.verify_genesis()
        self.assertTrue(self.request(path="/health", method="GET")[1]["genesis_verified"])

    def test_missing_bad_auth_and_wrong_routes_never_forward(self) -> None:
        for token in (None, "wrong", TOKEN + "x"):
            self.assertEqual(self.request(token=token)[0], 401)
            self.assertEqual(self.request(path="/health", method="GET", token=token)[0], 401)
        for path in ("/rpc?token=" + TOKEN, "/", "https://evil.example/rpc", "/health"):
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.calls, [])

    def test_strict_json_rpc_envelope(self) -> None:
        bad = [[], [payload()], payload("requestAirdrop"), payload("getTransaction"),
               payload() | {"url": "https://evil.example"}, payload() | {"jsonrpc": "1.0"},
               payload() | {"params": {}}, payload() | {"id": True},
               {key: value for key, value in payload().items() if key != "id"}]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        for raw in (b'{"jsonrpc":"2.0","id":7,"id":7,"method":"getSlot","params":[]}',
                    b'{"jsonrpc":"2.0","id":7,"method":"getSlot","params":[NaN]}',
                    b'{"jsonrpc":"2.0","id":7,"method":"getSlot","params":[Infinity]}',
                    b"[" * 1000 + b"]" * 1000, b"\xff", b"{}{}"):
            self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(self.calls, [])

    def test_body_limit_duplicate_and_ambiguous_headers(self) -> None:
        prefix = (b"POST /rpc HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                  + proxy.AUTH_HEADER.encode() + b": " + TOKEN.encode() + b"\r\n")
        bad = [b"Content-Length: 2\r\nContent-Length: 2", b"Content-Length: 2\r\ncontent-length: 2",
               b"Content-Length: 2\r\nTransfer-Encoding: chunked", b"Content-Length: +2",
               b"Content-Length: 2\r\nExpect: 100-continue", b"Content-Length: 2\r\nHost: other",
               b"Content-Length: 2\r\nX-A: x\r\n folded"]
        for tail in bad:
            with self.subTest(tail=tail):
                response = self.raw_request(prefix + tail, b"{}")
                self.assertIn(b" 400 ", response.split(b"\r\n")[0])
        response = self.raw_request(prefix + f"Content-Length: {proxy.MAX_BODY + 1}".encode())
        self.assertIn(b" 413 ", response.split(b"\r\n")[0])
        self.assertEqual(self.calls, [])

    def test_every_write_probes_genesis_and_wrong_cluster_blocks_send(self) -> None:
        self.state.verify_genesis()
        self.calls.clear()
        self.assertEqual(self.request(payload("sendTransaction", ["test-base64"]))[0], 200)
        self.assertEqual([item["method"] for item in self.calls], ["getGenesisHash", "sendTransaction"])
        self.calls.clear()

        def wrong(request: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(request)
            return success(request) | {"result": "mainnet-or-unknown"}

        self.state.forward = wrong
        self.assertEqual(self.request(payload("sendTransaction", ["test-base64"]))[0], 503)
        self.assertEqual([item["method"] for item in self.calls], ["getGenesisHash"])
        self.assertFalse(self.state.health()["genesis_verified"])
        self.state.forward = success
        self.assertEqual(self.request(payload("sendTransaction", ["test-base64"]))[0], 200)
        self.assertTrue(self.state.health()["genesis_verified"])

    def test_startup_probe_failure_is_not_verified(self) -> None:
        self.state.forward = lambda request: success(request) | {"result": "wrong"}
        with self.assertRaises(proxy.ProxyError):
            self.state.verify_genesis()
        self.assertFalse(self.state.health()["genesis_verified"])

    def test_error_code_preserved_private_message_and_traceback_redacted(self) -> None:
        secret = TOKEN + " upstream-private-host/path"
        self.state.forward = lambda request: {
            "jsonrpc": "2.0", "id": request["id"],
            "error": {"code": -32002, "message": secret, "data": {"logs": [secret]}}}
        status, body, _ = self.request()
        self.assertEqual((status, body["error"]["code"]), (200, -32002))
        self.assertNotIn(secret, json.dumps(body))

        def fail(request: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(secret)

        self.state.forward = fail
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            status, body, _ = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(TOKEN, json.dumps(body) + output.getvalue())
        self.state.forward = success
        self.assertEqual(self.request()[0], 200)

    def test_bad_upstream_envelope_size_and_rate_limit_recover(self) -> None:
        for body in ({"jsonrpc": "2.0", "id": True, "result": 1},
                     {"jsonrpc": "2.0", "id": 7, "result": 1, "error": {}},
                     {"jsonrpc": "2.0", "id": 7, "result": "x" * proxy.MAX_RESPONSE}):
            self.state.forward = lambda request, body=body: body
            self.assertEqual(self.request()[0], 502)

        def limited(request: dict[str, Any]) -> dict[str, Any]:
            raise proxy.ProxyError(429, "Upstream rate limited")

        self.state.forward = limited
        status, _, headers = self.request()
        self.assertEqual((status, headers["Retry-After"]), (429, "1"))
        self.state.forward = success
        self.assertEqual(self.request()[0], 200)

    def test_bounded_concurrency_rejects_excess_then_recovers(self) -> None:
        gate = threading.Event()
        condition = threading.Condition()
        active = 0
        maximum = 0

        def blocked(request: dict[str, Any]) -> dict[str, Any]:
            nonlocal active, maximum
            with condition:
                active += 1
                maximum = max(active, maximum)
                condition.notify_all()
            try:
                if not gate.wait(3):
                    raise RuntimeError("test gate expired")
                return success(request)
            finally:
                with condition:
                    active -= 1

        self.state.forward = blocked
        with ThreadPoolExecutor(max_workers=proxy.CONCURRENCY) as executor:
            futures = [executor.submit(self.request) for _ in range(proxy.CONCURRENCY)]
            try:
                with condition:
                    self.assertTrue(condition.wait_for(lambda: active == proxy.CONCURRENCY, timeout=2))
                self.assertEqual(self.request()[0], 503)
            finally:
                gate.set()
            self.assertTrue(all(future.result()[0] == 200 for future in futures))
        self.assertEqual(maximum, proxy.CONCURRENCY)
        self.assertEqual(self.state.health()["counters"]["busy"], 1)
        self.assertEqual(self.request()[0], 200)

    def test_only_loopback_binding(self) -> None:
        with self.assertRaises(ValueError):
            proxy.ProxyServer(("0.0.0.0", 0), self.state)


class UpstreamAndTokenTests(unittest.TestCase):
    def test_token_file_permissions_symlinks_and_environment(self) -> None:
        temporary_root = ROOT / "tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root, prefix="rpc-proxy-test-") as directory:
            path = Path(directory) / "token"
            path.write_text(TOKEN + "\n")
            path.chmod(0o600)
            self.assertEqual(proxy.load_token(path), TOKEN)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                proxy.load_token(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                proxy.load_token(link)
        with patch.dict(os.environ, {proxy.TOKEN_ENV: TOKEN}):
            self.assertEqual(proxy.load_token(None), TOKEN)
        for token in ("", "a" * 64, "secret", "x" * 1024, TOKEN + "\n"):
            with self.assertRaises(ValueError):
                proxy.validate_token(token)

    def test_fixed_upstream_timeout_no_auth_or_ambient_proxy(self) -> None:
        response = Response(json.dumps(success(payload())).encode())
        with patch.object(proxy.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(proxy.forward_rpc(payload())["result"], 123)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, proxy.UPSTREAM)
            self.assertNotIn(proxy.AUTH_HEADER.lower(), {key.lower() for key, _ in request.header_items()})
            self.assertNotIn(TOKEN, str(request.header_items()) + str(request.data))
            self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": proxy.UPSTREAM_TIMEOUT})
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], proxy.NoRedirect)
            self.assertEqual(response.read_sizes, [proxy.MAX_RESPONSE + 1])

    def test_redirect_and_oversized_response_rejected_without_following(self) -> None:
        self.assertIsNone(proxy.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example"))
        responses = [Response(b"{}", 302), Response(b"{}", url="https://evil.example"),
                     Response(b"x" * (proxy.MAX_RESPONSE + 1)), Response(b'{"result": NaN}')]
        for response in responses:
            with patch.object(proxy.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = response
                with self.assertRaises(proxy.ProxyError) as caught:
                    proxy.forward_rpc(payload())
                self.assertEqual(caught.exception.status, 502)
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_transport_rate_limit_and_timeout_redaction(self) -> None:
        for error, status in ((urllib.error.HTTPError(proxy.UPSTREAM, 429, TOKEN, {}, None), 429),
                              (urllib.error.HTTPError(proxy.UPSTREAM, 302, TOKEN, {}, None), 502),
                              (urllib.error.URLError(TOKEN), 502), (TimeoutError(TOKEN), 502)):
            with patch.object(proxy.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(proxy.ProxyError) as caught:
                    proxy.forward_rpc(payload())
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
