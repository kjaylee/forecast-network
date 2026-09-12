#!/usr/bin/env python3
"""One-time migration/reconciliation with the same durable adapter as the Worker.

This is an operator command, not a replacement for the deployed scheduler. D1
REST is used only for individually atomic prepared statements; multi-statement
transactions are deliberately unsupported here. Historical backfill must already
have been performed by the Worker's D1 batch implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time

from cloudflare_keychain import ACCOUNT_ID, api
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from deploy_devnet import rpc as devnet_rpc
from solana_keychain import ROOT, Keychain, public_bytes

sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src")]
from forecast_application.solana_registry import SolanaRegistry, reserve_daily_spend  # noqa: E402
from forecast_application.solana_rpc import SolanaRpcTransport  # noqa: E402
from forecast_application.solana_wire import base58_decode  # noqa: E402

FORECASTS = ("f_IklrwuvCXkq8PVKWnH1oaXjC", "f_NB_bVj_Y7gFrFB7vy61RZFU2", "f_uZX0iFRUQspTLFrqf2CnOJcV")


def bound_database_id() -> str:
    """The production D1 id lives only in wrangler.jsonc; never duplicate it here."""
    config = json.loads((ROOT / "apps/web/wrangler.jsonc").read_text())
    return str(config["d1_databases"][0]["database_id"])


class CloudflareStatements:
    def query(self, sql, params):
        result = api(f"accounts/{ACCOUNT_ID}/d1/database/{bound_database_id()}/query",
                     method="POST", body={"sql": sql, "params": list(params)})
        if not result.get("success") or not result.get("result"):
            raise RuntimeError("D1 operator request unsuccessful")
        item = result["result"][0]
        if not item.get("success"):
            raise RuntimeError("D1 operator statement unsuccessful")
        return item

    async def all(self, sql, params=()):
        return self.query(sql, params)["results"]

    async def first(self, sql, params=()):
        rows = await self.all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql, params=()):
        return self.query(sql, params)

    async def batch(self, statements):
        raise RuntimeError("Use the Worker for atomic D1 batches; REST does not emulate them")


async def main(sync: bool) -> None:
    db = CloudflareStatements()
    keychain = Keychain()
    seed = keychain.read("relayer")
    if seed is None:
        raise RuntimeError("Missing relayer Keychain entry")
    manifest = json.loads((ROOT / "infra/solana/devnet.json").read_text())
    relayer = base58_decode(manifest["relayer"], length=32)
    if public_bytes(seed) != relayer:
        raise RuntimeError("Relayer identity mismatch")
    async def rpc(method, params):
        return devnet_rpc(method, params)
    async def sign(message):
        return Ed25519PrivateKey.from_private_bytes(seed).sign(message)
    async def authorize_spend(amount):
        await reserve_daily_spend(db, amount, int(time.time()*1000), 50_000_000)
    program = base58_decode(manifest["programId"], length=32)
    transport = SolanaRpcTransport(rpc, sign, program_id=program, relayer=relayer,
        expected_genesis_hash=manifest["genesisHash"], authorize_spend=authorize_spend)
    registry = SolanaRegistry(db, transport, program_id=program, relayer=relayer,
        now_ms=lambda: int(time.time()*1000), random_token=lambda: secrets.token_urlsafe(24))
    enabled = await db.all("SELECT forecast_id FROM registry_forecasts WHERE enabled=1")
    if any(row["forecast_id"] not in FORECASTS for row in enabled):
        raise RuntimeError("Initial migration is limited to the three reviewed public forecasts")
    for iteration in range(50 if sync else 1):
        if sync:
            print(json.dumps({"iteration": iteration, "delivery": await registry.sync(limit=3)}), flush=True)
        statuses = {identifier: await registry.status(identifier) for identifier in FORECASTS}
        print(json.dumps({identifier: {key: status[key] for key in
            ("status", "localRevision", "confirmedRevision", "pendingReason")}
            for identifier, status in statuses.items()}), flush=True)
        (ROOT / "tmp/registry-deploy/public-forecast-status.json").write_text(json.dumps(statuses, indent=2)+"\n")
        if all(status["status"] == "confirmed" for status in statuses.values()):
            print("All three current public forecast revisions verified from finalized Devnet accounts.")
            return
        if not sync:
            return
        if any(status["status"] == "blocked" for status in statuses.values()):
            raise RuntimeError("Registry delivery blocked; inspect fixed reason codes before retrying")
        await asyncio.sleep(5)
    raise RuntimeError("Initial migration remains pending; retain the durable queue for reconciliation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true")
    asyncio.run(main(parser.parse_args().sync))
