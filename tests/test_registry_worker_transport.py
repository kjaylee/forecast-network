"""Exercise actual Worker RPC/stream methods against Workerd-compatible fakes."""

from __future__ import annotations

import ast
import asyncio
import json
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.test_web_transport import scheduled_method

ENDPOINT = "https://api.devnet.solana.com"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"


def actual_bounded_bytes():
    source = ast.parse((Path(__file__).resolve().parents[1] / "apps/web/src/entry.py").read_text())
    functions = [node for node in source.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"python_value", "bounded_bytes"}]
    scope = {"Any": Any, "Mapping": Mapping, "Sequence": Sequence}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "actual_worker_rpc_stream", "exec"),
         scope)
    return scope["bounded_bytes"]


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.canceled = False
        self.released = False
        self.read_count = 0

    async def read(self):
        self.read_count += 1
        chunk = next(self.chunks, None)
        return SimpleNamespace(done=chunk is None, value=chunk)

    async def cancel(self):
        self.canceled = True

    def releaseLock(self):
        self.released = True


class WorkerRpcFixture:
    def __init__(self, *, response=None, raw=None, chunks=None, status=200, headers=None):
        if raw is None:
            raw = json.dumps(response if response is not None else {
                "jsonrpc": "2.0", "id": 1, "result": GENESIS}).encode()
        self.stream = Stream(chunks if chunks is not None else [raw])
        self.response = SimpleNamespace(status=status, headers=headers or {},
                                        body=SimpleNamespace(getReader=lambda: self.stream))
        self.fetches = []
        self.logs = []
        self.timeouts = []
        self.stall = False
        self.fetch_canceled = False
        self.worker = SimpleNamespace(env=SimpleNamespace(SOLANA_RPC_URL=ENDPOINT))

    async def fetch(self, url, options):
        # Actual Workerd permits only follow/manual. In particular redirect:error
        # throws before there is an HTTP response, which previously hid this bug.
        if options.get("redirect") not in ("manual", "follow"):
            raise TypeError("Workerd does not support this redirect mode")
        self.fetches.append((url, options))
        if self.stall:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.fetch_canceled = True
                raise
        return self.response

    async def wait_for(self, awaitable, *, timeout):
        self.timeouts.append(timeout)
        # The production timeout must remain 20 seconds; accelerate only the
        # injected deadline for the deliberate stalled-I/O cancellation test.
        return await asyncio.wait_for(awaitable, timeout=0.01 if self.stall else timeout)

    async def call(self, method="getGenesisHash", params=None):
        actual = scheduled_method("registry_rpc", js_fetch=self.fetch,
            bounded_bytes=actual_bounded_bytes(), asyncio=SimpleNamespace(wait_for=self.wait_for),
            print=lambda value: self.logs.append(json.loads(value)))
        return await actual(self.worker, method, [] if params is None else params)


class RegistryWorkerTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_genesis_succeeds_with_workerd_supported_manual_redirect_mode(self):
        fixture = WorkerRpcFixture()
        self.assertEqual(await fixture.call(), GENESIS)
        url, options = fixture.fetches[0]
        self.assertEqual(url, ENDPOINT)
        self.assertEqual(options["method"], "POST")
        self.assertEqual(options["redirect"], "manual")
        self.assertEqual(options["headers"], {"Content-Type": "application/json",
            "User-Agent": "Forecast-Registry/0.9 (+https://forecast.eastsea.xyz)"})
        self.assertEqual(json.loads(options["body"]), {
            "jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []})
        self.assertEqual(fixture.timeouts, [20])
        self.assertTrue(fixture.stream.released)

    async def test_rpc_parameters_are_forwarded_without_changing_finality(self):
        fixture = WorkerRpcFixture(response={"jsonrpc": "2.0", "id": 1, "result": None})
        params = ["test-public-address", {"encoding": "base64", "commitment": "finalized"}]
        self.assertIsNone(await fixture.call("getAccountInfo", params))
        self.assertEqual(json.loads(fixture.fetches[0][1]["body"])["params"], params)

    async def test_redirects_and_http_failures_never_follow_or_parse_upstream_bodies(self):
        for status in (301, 302, 307, 308, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                fixture = WorkerRpcFixture(status=status, raw=b"upstream-private-marker",
                    headers={"location": "https://api.mainnet-beta.solana.com"})
                with self.assertRaises(RuntimeError) as caught:
                    await fixture.call()
                self.assertEqual(len(fixture.fetches), 1)
                self.assertEqual(fixture.stream.read_count, 0)
                self.assertEqual(fixture.logs, [{"event": "registry_rpc_http_error",
                    "method": "getGenesisHash", "httpStatus": status}])
                self.assertNotIn("upstream-private-marker", str(caught.exception))
                self.assertNotIn("upstream-private-marker", json.dumps(fixture.logs))

    async def test_wrong_endpoint_and_unapproved_methods_fail_before_fetch(self):
        for endpoint in ("https://api.mainnet-beta.solana.com", "http://api.devnet.solana.com",
                         ENDPOINT + "/", ENDPOINT + "?api-key=untrusted", ""):
            with self.subTest(endpoint=endpoint):
                fixture = WorkerRpcFixture()
                fixture.worker.env.SOLANA_RPC_URL = endpoint
                with self.assertRaises(ValueError):
                    await fixture.call()
                self.assertEqual(fixture.fetches, [])
                self.assertEqual(fixture.timeouts, [])
        for method in ("requestAirdrop", "getIdentity", "getGenesisHash ", ""):
            fixture = WorkerRpcFixture()
            with self.assertRaises(ValueError):
                await fixture.call(method)
            self.assertEqual(fixture.fetches, [])

    async def test_malformed_rpc_envelopes_and_provider_errors_fail_closed(self):
        responses = [[], "string", {}, {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 2, "result": GENESIS},
            {"jsonrpc": "2.0", "id": True, "result": GENESIS},
            {"jsonrpc": "2.0", "id": "1", "result": GENESIS},
            {"jsonrpc": "1.0", "id": 1, "result": GENESIS},
            {"id": 1, "result": GENESIS},
            {"jsonrpc": "2.0", "id": 1, "error": {"message": "private-upstream-error"}},
            {"jsonrpc": "2.0", "id": 1, "result": GENESIS, "error": None}]
        for response in responses:
            with self.subTest(response=response):
                fixture = WorkerRpcFixture(response=response)
                with self.assertRaises(RuntimeError) as caught:
                    await fixture.call()
                self.assertNotIn("private-upstream-error", str(caught.exception))
                self.assertTrue(fixture.stream.released)
        for raw in (b"not json", b"\xff", b""):
            fixture = WorkerRpcFixture(raw=raw)
            with self.assertRaises(ValueError):
                await fixture.call()
            self.assertTrue(fixture.stream.released)

    async def test_response_limit_applies_to_header_and_streamed_bytes(self):
        fixture = WorkerRpcFixture(headers={"content-length": "262145"})
        with self.assertRaisesRegex(ValueError, "size limit"):
            await fixture.call()
        self.assertEqual(fixture.stream.read_count, 0)
        fixture = WorkerRpcFixture(chunks=[b" " * 131072, b" " * 131072, b" "])
        with self.assertRaisesRegex(ValueError, "size limit"):
            await fixture.call()
        self.assertTrue(fixture.stream.canceled)
        self.assertTrue(fixture.stream.released)

    async def test_stream_exactly_at_limit_is_accepted(self):
        envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": GENESIS}).encode()
        fixture = WorkerRpcFixture(chunks=[envelope, b" " * (262144 - len(envelope))])
        self.assertEqual(await fixture.call(), GENESIS)
        self.assertFalse(fixture.stream.canceled)
        self.assertTrue(fixture.stream.released)

    async def test_twenty_second_network_deadline_cancels_stalled_request(self):
        fixture = WorkerRpcFixture()
        fixture.stall = True
        with self.assertRaises(TimeoutError):
            await fixture.call()
        self.assertEqual(fixture.timeouts, [20])
        self.assertTrue(fixture.fetch_canceled)
        self.assertEqual(fixture.stream.read_count, 0)
