#!/usr/bin/env python3
"""Initialize and verify the exact registry binary on localhost or pinned Devnet."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import time
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from solana_keychain import ROOT, Keychain, public_bytes

sys.path[:0] = [str(ROOT / "packages/application/src"), str(ROOT / "packages/domain/src")]
from forecast_application import solana_wire as wire  # noqa: E402

PROGRAM = wire.base58_decode("BvZLYrSmzDGTP5jYb14cjreRfHqfMo2sRBfUPpAi5Sgp", length=32)
DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
SYSTEM = bytes(32)


class Operator:
    def __init__(self, local: bool) -> None:
        self.local = local
        self.url = "http://127.0.0.1:61500" if local else "https://api.devnet.solana.com"
        genesis = self.rpc("getGenesisHash", [])
        if not local and genesis != DEVNET_GENESIS:
            raise RuntimeError("Wrong cluster; refusing to sign")
        keychain = Keychain()
        self.seeds = {role: keychain.read(role) for role in ("relayer", "upgrade")}
        if any(seed is None for seed in self.seeds.values()):
            raise RuntimeError("Required Keychain entry missing")
        self.keys = {role: public_bytes(seed) for role, seed in self.seeds.items()}
        self.config = wire.config_address(PROGRAM)[0]

    def rpc(self, method: str, params: list) -> object:
        request = urllib.request.Request(self.url, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise RuntimeError("RPC response too large")
        result = json.loads(raw)
        if result.get("error") is not None:
            print(json.dumps({"rpcMethod": method, "error": result["error"]}))
            raise RuntimeError(f"RPC failed: {method}")
        return result["result"]

    def account(self, address: bytes) -> bytes | None:
        value = self.rpc("getAccountInfo", [wire.base58_encode(address), {
            "encoding": "base64", "commitment": "finalized",
        }])["value"]
        if value is None:
            return None
        if (value["owner"] == wire.base58_encode(SYSTEM) and value["executable"] is False
                and value["data"] == ["", "base64"]):
            return None  # A funded but unallocated PDA is still eligible for initialization.
        if value["owner"] != wire.base58_encode(PROGRAM) or value["executable"]:
            raise RuntimeError("Registry account has unexpected owner or executable flag")
        return base64.b64decode(value["data"][0], validate=True)

    def signed(self, instructions: tuple) -> str:
        recent = self.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
        message = wire.compile_message(self.keys["relayer"],
            wire.base58_decode(recent["blockhash"], length=32), instructions)
        signatures = {}
        for public_key in message.signer_keys:
            role = next(role for role in self.keys if self.keys[role] == public_key)
            signatures[public_key] = Ed25519PrivateKey.from_private_bytes(self.seeds[role]).sign(message.data)
        return base64.b64encode(wire.assemble_transaction(message, signatures)).decode()

    def send(self, instructions: tuple, *, reject: bool = False) -> str | None:
        signed = self.signed(instructions)
        simulated = self.rpc("simulateTransaction", [signed, {
            "encoding": "base64", "sigVerify": True, "commitment": "confirmed",
        }])["value"]
        if reject:
            if simulated["err"] is None:
                raise RuntimeError("Security test unexpectedly accepted transaction")
            return None
        if simulated["err"] is not None:
            print(json.dumps({"simulationError": simulated["err"], "logs": simulated.get("logs", [])}))
            raise RuntimeError("Registry runtime simulation failed")
        signature = self.rpc("sendTransaction", [signed, {"encoding": "base64", "skipPreflight": False,
                                                         "preflightCommitment": "confirmed"}])
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            status = self.rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])["value"][0]
            if status and status["err"] is not None:
                raise RuntimeError("Registry transaction failed")
            if status and status["confirmationStatus"] == "finalized":
                return signature
            time.sleep(1)
        raise RuntimeError("Finality not observed yet; reconcile account before retrying")

    def initialize(self) -> str | None:
        existing = self.account(self.config)
        if existing is not None:
            config = wire.decode_config(existing)
            if config.administrator != self.keys["upgrade"] or config.relayer != self.keys["relayer"]:
                raise RuntimeError("Registry authorities do not match Keychain")
            return None
        funding = wire.Instruction(SYSTEM, (
            wire.AccountMeta(self.keys["relayer"], True, True),
            wire.AccountMeta(self.keys["upgrade"], False, True),
        ), struct.pack("<IQ", 2, 10_000_000))
        initialize = wire.Instruction(PROGRAM, (
            wire.AccountMeta(self.keys["upgrade"], True, True),
            wire.AccountMeta(self.config, False, True), wire.AccountMeta(SYSTEM),
        ), wire.encode_initialize(self.keys["relayer"]))
        signature = self.send((funding, initialize))
        config = wire.decode_config(self.account(self.config))
        if config.administrator != self.keys["upgrade"] or config.relayer != self.keys["relayer"]:
            raise RuntimeError("Initialized registry mismatch")
        return signature

    def runtime_test(self) -> dict:
        # Clearly identified synthetic registry test, never an application user or points receipt.
        identity = hashlib.sha256(f"forecast-registry-runtime-test:{time.time_ns()}".encode()).digest()
        address = wire.forecast_address(PROGRAM, identity)[0]
        now = int(time.time() - 5) * 1000
        event = hashlib.sha256(identity + b"published-event").digest()
        snapshot = hashlib.sha256(identity + b"published-snapshot").digest()
        register = wire.encode_register(forecast_id_hash=identity,
            creator_hash=hashlib.sha256(b"synthetic-runtime-verifier").digest(),
            specification_hash=hashlib.sha256(identity + b"specification").digest(),
            open_at_ms=now - 60_000, close_at_ms=now + 86_400_000,
            revision=2, occurred_at_ms=now, event_hash=event, snapshot_hash=snapshot)
        accounts = (wire.AccountMeta(self.keys["relayer"], True, True),
                    wire.AccountMeta(self.config), wire.AccountMeta(address, False, True))
        prefund = wire.Instruction(SYSTEM, (
            wire.AccountMeta(self.keys["relayer"], True, True), wire.AccountMeta(address, False, True),
        ), struct.pack("<IQ", 2, 1_000_000))
        self.send((prefund,))
        if self.account(address) is not None:
            raise RuntimeError("Prefunded address was mistaken for a registry record")
        registration = self.send((wire.Instruction(PROGRAM, accounts + (wire.AccountMeta(SYSTEM),), register),))
        account = wire.decode_forecast(self.account(address))
        if account.revision != 2 or account.event_hash != event or account.snapshot_hash != snapshot:
            raise RuntimeError("Runtime registration differs from submitted commitment")
        before = self.account(address)
        replay = wire.Instruction(PROGRAM, accounts + (wire.AccountMeta(SYSTEM),), register)
        self.send((replay,), reject=True)
        # The cold administrator cannot impersonate the hot relayer.
        wrong = (wire.AccountMeta(self.keys["upgrade"], True, True), *accounts[1:], wire.AccountMeta(SYSTEM))
        self.send((wire.Instruction(PROGRAM, wrong, register),), reject=True)
        zero = bytes(32)
        advance = dict(revision=3, occurred_at_ms=now, previous_event_hash=event,
            event_hash=hashlib.sha256(identity + b"next-event").digest(),
            snapshot_hash=hashlib.sha256(identity + b"next-snapshot").digest(),
            state=3, outcome=0, resolution_hash=zero, dispute_hash=zero,
            reputation_hash=zero, trigger_hash=zero, challenge_until_ms=0,
            pending_disputes=0, material_disputes=0)
        self.send((wire.Instruction(PROGRAM, accounts, wire.encode_advance(**advance)),), reject=True)
        advance["trigger_hash"] = hashlib.sha256(identity + b"reviewed-positive-trigger").digest()
        locking = self.send((wire.Instruction(PROGRAM, accounts, wire.encode_advance(**advance)),))
        locked = wire.decode_forecast(self.account(address))
        if locked.state != 3 or locked.revision != 3 or locked.trigger_hash != advance["trigger_hash"]:
            raise RuntimeError("Runtime early lock commitment mismatch")
        frozen = self.account(address)
        self.send((wire.Instruction(PROGRAM, accounts, wire.encode_advance(**advance)),), reject=True)
        if self.account(address) != frozen or before == frozen:
            raise RuntimeError("Failed transaction altered canonical state")
        transitions = []
        for state in (4, 5, 6):
            advance["previous_event_hash"] = advance["event_hash"]
            advance["revision"] += 1
            advance["state"] = state
            advance["event_hash"] = hashlib.sha256(identity + bytes([state]) + b"event").digest()
            advance["snapshot_hash"] = hashlib.sha256(identity + bytes([state]) + b"snapshot").digest()
            if state == 5:
                advance["outcome"] = 1
                advance["resolution_hash"] = hashlib.sha256(identity + b"synthetic-resolution").digest()
            if state == 6:
                advance["challenge_until_ms"] = now + 172_800_000
            transitions.append(self.send((wire.Instruction(PROGRAM, accounts, wire.encode_advance(**advance)),)))
        challenged_bytes = self.account(address)
        challenged = wire.decode_forecast(challenged_bytes)
        if challenged.state != 6 or challenged.chain_finalize_not_before_ms < now + 172_800_000:
            raise RuntimeError("Registry did not establish independent chain challenge delay")
        advance["previous_event_hash"] = advance["event_hash"]
        advance["revision"] += 1
        advance["state"] = 10
        advance["event_hash"] = hashlib.sha256(identity + b"premature-final-event").digest()
        advance["snapshot_hash"] = hashlib.sha256(identity + b"premature-final-snapshot").digest()
        advance["reputation_hash"] = hashlib.sha256(b"synthetic-empty-reputation").digest()
        self.send((wire.Instruction(PROGRAM, accounts, wire.encode_advance(**advance)),), reject=True)
        if self.account(address) != challenged_bytes:
            raise RuntimeError("Premature finalization changed canonical state")
        return {"account": wire.base58_encode(address), "registration": registration,
                "earlyLock": locking, "challengeTransactions": transitions,
                "chainFinalizeNotBeforeMs": challenged.chain_finalize_not_before_ms,
                "verified": ["prefunded PDA registered successfully", "exact storage", "duplicate registration rejected",
                "wrong authority rejected", "unreviewed early lock rejected", "stale revision rejected",
                "failed operation atomicity", "real chain challenge delay", "premature finalization rejected"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("action", choices=("initialize", "runtime-test"))
    args = parser.parse_args()
    operator = Operator(args.local)
    result = {"cluster": "localhost" if args.local else "devnet",
              "programId": wire.base58_encode(PROGRAM), "config": wire.base58_encode(operator.config),
              "initialization": operator.initialize()}
    if args.action == "runtime-test":
        result["runtimeTest"] = operator.runtime_test()
    path = ROOT / f"tmp/registry-deploy/{result['cluster']}-{args.action}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
