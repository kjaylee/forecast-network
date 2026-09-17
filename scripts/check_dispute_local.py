#!/usr/bin/env python3
"""Actual local SBF intake tests with ephemeral keys and explicitly seeded accounts.

Never contacts Devnet, reads Keychain or deploys/upgrades an external program.
Fixture challenge windows exercise ordering; they do not prove elapsed 48 hours.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/application/src"), str(ROOT / "packages/domain/src")]
from forecast_application import dispute_wire as d  # noqa: E402
from forecast_application import solana_wire as w  # noqa: E402


def key() -> tuple[bytes, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw), private


def digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def fixture(program: bytes, name: str, deadline: int) -> tuple[bytes, bytes, d.Accumulator]:
    fid, spec = digest(name), digest(name + "spec")
    address = w.forecast_address(program, fid)[0]
    history = min(deadline, int(time.time())*1000)
    raw = (b"FNFORE01" + fid + digest("creator") + spec
           + struct.pack("<qqQq", history-400000, history-300000, 4, history-200000)
           + bytes((6, 1, 0, 0)) + digest(name+"event") + digest(name+"snapshot")
           + digest(name+"resolution") + bytes(96) + struct.pack("<qqqHH", deadline, deadline, 0, 0, 0))
    w.decode_forecast(raw)
    gate = d.Accumulator(address, spec, 1, 1, 3, digest(name+"resolution"), digest(name+"proposal"),
                         history-200000, deadline, 0, 0, 0, digest(name+"head"), 1)
    return address, raw, gate


class Local:
    def __init__(self, port: int, keys: dict[bytes, Ed25519PrivateKey]) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.keys = keys
        self.events: list[dict[str, Any]] = []

    def rpc(self, method: str, params: list[Any]) -> Any:
        request = urllib.request.Request(self.url, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise RuntimeError("local response exceeds bound")
        value = json.loads(raw)
        if "error" in value:
            raise RuntimeError(f"local RPC {method}: {value['error']}")
        return value["result"]

    def account(self, address: bytes) -> bytes:
        value = self.rpc("getAccountInfo", [w.base58_encode(address), {"encoding": "base64", "commitment": "confirmed"}])["value"]
        if value is None:
            raise RuntimeError("local fixture account absent")
        return base64.b64decode(value["data"][0], validate=True)

    def clock(self) -> int:
        slot = self.rpc("getSlot", [{"commitment": "confirmed"}])
        stamp = self.rpc("getBlockTime", [slot])
        if type(stamp) is not int:
            raise RuntimeError("local clock unavailable")
        return stamp * 1000

    def send(self, name: str, payer: bytes, instructions: tuple[w.Instruction, ...], *,
             reject: bool = False, heap: bool = True) -> dict[str, Any]:
        if heap:
            instructions = (d.heap_frame(),) + instructions
        # Explicit budget avoids relying on the number of instructions for CU allocation.
        instructions = (w.Instruction(d.COMPUTE_BUDGET, (), b"\x02"+struct.pack("<I", 1_000_000)),) + instructions
        block = self.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
        message = w.compile_message(payer, w.base58_decode(block, length=32), instructions)
        raw = w.assemble_transaction(message, {public: self.keys[public].sign(message.data) for public in message.signer_keys})
        encoded = base64.b64encode(raw).decode()
        simulated = self.rpc("simulateTransaction", [encoded, {"encoding": "base64", "sigVerify": True,
                                                                "commitment": "confirmed"}])["value"]
        if reject:
            if simulated["err"] is None:
                raise RuntimeError(f"negative local test accepted: {name}")
            entry = {"name": name, "simulation_error": simulated["err"], "logs": simulated.get("logs", [])}
            self.events.append(entry)
            return entry
        if simulated["err"] is not None:
            raise RuntimeError(f"local SBF {name}: {simulated}")
        signature = self.rpc("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": False,
                                                            "preflightCommitment": "confirmed"}])
        until = time.monotonic()+25
        while time.monotonic() < until:
            state = self.rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])["value"][0]
            if state and state["err"] is not None:
                raise RuntimeError(f"local transaction failed: {name}: {state['err']}")
            if state and state["confirmationStatus"] in ("confirmed", "finalized"):
                entry = {"name": name, "signature": signature, "slot": state["slot"],
                         "confirmation": state["confirmationStatus"], "units": simulated.get("unitsConsumed")}
                self.events.append(entry)
                return entry
            time.sleep(0.1)
        raise RuntimeError(f"local confirmation timeout: {name}")


def run(artifact: Path, port: int, output: Path) -> None:
    validator = shutil.which("solana-test-validator")
    if validator is None or not artifact.is_file():
        raise RuntimeError("validator or SBF artifact unavailable")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    temporary_root = ROOT / "tmp"
    temporary_root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="dispute-local-", dir=temporary_root))
    pairs = [key() for _ in range(5)]
    (admin, _), (relay, _), (user, _), (judge, _), (other, _) = pairs
    program = digest("forecast-intake-v1-local-program")
    local = Local(port, dict(pairs))
    stamp = int(time.time())*1000
    fixtures = {name: fixture(program, name, deadline) for name, deadline in (
        ("timely", stamp+45_000), ("sealed-first", stamp-1000), ("maximum", stamp+3_600_000),
        ("adjudicated", stamp-1000), ("adjudication-pending", stamp-1000))}
    for name, pending in (("adjudicated", 0), ("adjudication-pending", 1)):
        address, raw, g = fixtures[name]
        raw = bytearray(raw)
        raw[136] = 8
        raw[236:268] = digest(name+"legacy-reviewed-material")
        raw[358:360] = struct.pack("<H", 1)
        w.decode_forecast(bytes(raw))
        fixtures[name] = (address, bytes(raw), replace(g, material=1, pending=pending, accepted=1+pending))
    config = b"FNCONF01" + admin + relay + bytes(32)
    account_args: list[str] = []

    def seed(address: bytes, raw: bytes) -> None:
        path = directory / (w.base58_encode(address)+".json")
        path.write_text(json.dumps({"pubkey": w.base58_encode(address), "account": {
            "lamports": 1_000_000_000, "data": [base64.b64encode(raw).decode(), "base64"],
            "owner": w.base58_encode(program), "executable": False, "rentEpoch": 0}}))
        account_args.extend(("--account", w.base58_encode(address), str(path)))

    seed(w.config_address(program)[0], config)
    for address, raw, gate in fixtures.values():
        seed(address, raw)
        seed(d.gate_address(program, address)[0], gate.encode())
    command = [validator, "--quiet", "--ledger", str(directory / "ledger"), "--rpc-port", str(port),
               "--faucet-port", str(port+2), "--bind-address", "127.0.0.1", "--bpf-program",
               w.base58_encode(program), str(artifact), *account_args]
    with (directory / "validator.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   env=os.environ | {"TMPDIR": str(temporary_root), "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            until = time.monotonic()+45
            while time.monotonic() < until:
                if process.poll() is not None:
                    raise RuntimeError(f"local validator exited; inspect {directory / 'validator.log'}")
                try:
                    local.rpc("getHealth", [])
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("local validator startup timeout")
            for public, _ in pairs:
                local.rpc("requestAirdrop", [w.base58_encode(public), 2_000_000_000])
            until = time.monotonic()+10
            while local.rpc("getBalance", [w.base58_encode(user), {"commitment": "confirmed"}])["value"] == 0:
                if time.monotonic() >= until:
                    raise RuntimeError("local faucet timeout")
                time.sleep(0.1)
            conf = w.config_address(program)[0]
            legacy = w.Instruction(program, (w.AccountMeta(admin, True), w.AccountMeta(conf, False, True)),
                                   w.encode_set_relayer(relay))
            local.send("small_legacy_without_heap_rejected", admin, (legacy,), reject=True, heap=False)
            local.send("small_legacy_with_heap", admin, (legacy,))
            local.send("reviewer_init_wrong_actor", relay, (d.instruction(program, d.encode_reviewer(judge), actor=relay),), reject=True)
            local.send("reviewer_init", admin, (d.instruction(program, d.encode_reviewer(judge), actor=admin),))
            role = d.Reviewer.decode(local.account(d.reviewer_address(program)[0]))
            if role != d.Reviewer(judge, 1):
                raise RuntimeError("reviewer ABI mismatch")

            def ix(data: bytes, actor: bytes, address: bytes, receipt: bytes = d.ZERO) -> w.Instruction:
                return d.instruction(program, data, actor=actor, forecast=address, receipt=receipt)

            def upload(name: str, body: bytes, actor: bytes = user) -> tuple[bytes, d.Receipt]:
                address, _, gate = fixtures[name]
                account = d.receipt_address(program, address, gate.epoch, actor)[0]
                # Prefunding must not squat a canonical PDA.
                transfer = w.Instruction(d.SYSTEM, (w.AccountMeta(actor, True, True), w.AccountMeta(account, False, True)),
                                         struct.pack("<IQ", 2, 1_000_000))
                local.send(name+"_prefund", actor, (transfer,))
                local.send(name+"_draft", actor, (ix(d.encode_draft(gate, digest(name+"nonce"), body), actor, address, account),))
                partial = d.Receipt.decode(local.account(account))
                if len(partial.encode()) != d.RECEIPT_HEADER:
                    raise RuntimeError("draft allocated declared body prematurely")
                forged_submit = b"\x0a" + struct.pack("<Q", gate.epoch) + partial.nonce + partial.evidence
                local.send(name+"_incomplete_rejected", actor, (ix(forged_submit, actor, address, account),), reject=True)
                for offset in range(0, len(body), d.CHUNK):
                    local.send(name+f"_append_{offset}", actor, (ix(d.encode_append(offset, body[offset:offset+d.CHUNK]), actor, address, account),))
                    got = local.account(account)
                    expected = d.RECEIPT_HEADER + min(offset+d.CHUNK, len(body))
                    if len(got) != expected:
                        raise RuntimeError("staged growth length mismatch")
                return account, d.Receipt.decode(local.account(account))

            address, raw, gate = fixtures["timely"]
            account, ready = upload("timely", b'{"fixture":"timely"}')
            local.send("timely_wrong_signer", other, (ix(d.encode_submit(ready), other, address, account),), reject=True)
            local.send("timely_submit", user, (ix(d.encode_submit(ready), user, address, account),))
            accepted = d.Receipt.decode(local.account(account))
            if accepted.status != 1 or accepted.accepted_at > accepted.deadline:
                raise RuntimeError("timeliness/signer ABI mismatch")
            local.send("timely_replay", user, (ix(d.encode_submit(ready), user, address, account),), reject=True)
            g = d.Accumulator.decode(local.account(d.gate_address(program, address)[0]))
            if (g.pending, g.accepted) != (1, 1):
                raise RuntimeError("native accumulator did not count direct intake")
            alias = w.Instruction(program, (w.AccountMeta(user, True), w.AccountMeta(address),
                                  w.AccountMeta(address, False, True), w.AccountMeta(account, False, True)),
                                  d.encode_submit(ready))
            local.send("aliased_accumulator_rejected", user, (alias,), reject=True)

            publication_time = local.clock()
            new_id = digest("registered-with-global-heap")
            published = w.forecast_address(program, new_id)[0]
            registration = w.encode_register(forecast_id_hash=new_id, creator_hash=digest("creator"),
                specification_hash=digest("fresh-spec"), open_at_ms=publication_time-1000,
                close_at_ms=publication_time+60000, revision=2, occurred_at_ms=publication_time,
                event_hash=digest("fresh-event"), snapshot_hash=digest("fresh-snapshot"))
            local.send("small_register_global_heap", relay, (w.Instruction(program, (
                w.AccountMeta(relay, True, True), w.AccountMeta(conf), w.AccountMeta(published, False, True),
                w.AccountMeta(d.SYSTEM)), registration),))
            fresh = w.decode_forecast(local.account(published))
            fresh_gate = d.gate_address(program, published)[0]
            prefund = w.Instruction(d.SYSTEM, (w.AccountMeta(relay, True, True), w.AccountMeta(fresh_gate, False, True)),
                                   struct.pack("<IQ", 2, 1_000_000))
            local.send("activate_prefunded_sidecar", relay,
                       (prefund, ix(d.encode_activate(fresh), relay, published)))
            if d.Accumulator.decode(local.account(fresh_gate)).phase != 0:
                raise RuntimeError("new publication did not create dormant sidecar")
            local.send("activation_replay_rejected", relay, (ix(d.encode_activate(fresh), relay, published),), reject=True)

            def candidate(address: bytes) -> bytes:
                f = w.decode_forecast(local.account(address))
                return w.encode_advance(revision=f.revision+1, occurred_at_ms=max(local.clock(), f.challenge_until_ms),
                    previous_event_hash=f.event_hash, event_hash=digest(w.base58_encode(address)+"final"),
                    snapshot_hash=digest("final-snapshot"), state=10, outcome=f.outcome,
                    resolution_hash=f.resolution_hash, dispute_hash=f.dispute_hash, reputation_hash=digest("reputation"),
                    trigger_hash=f.trigger_hash, challenge_until_ms=f.challenge_until_ms)

            second, _, sg = fixtures["sealed-first"]
            final = candidate(second)
            local.send("seal_first", relay, (ix(d.encode_seal(sg, final), relay, second),))
            sealed = d.Accumulator.decode(local.account(d.gate_address(program, second)[0]))
            # Draft creation itself fails after seal, so manufacture the structurally valid instruction from the old gate.
            receipt_key = d.receipt_address(program, second, 1, user)[0]
            local.send("sealed_epoch_rejects_new_draft", user,
                       (ix(d.encode_draft(sg, digest("late"), b"x"), user, second, receipt_key),), reject=True)
            local.send("legacy_finalize_disabled", relay,
                       (w.Instruction(program, (w.AccountMeta(relay, True), w.AccountMeta(conf), w.AccountMeta(second, False, True)), final),), reject=True)
            local.send("finalize_sealed", relay, (ix(d.encode_finalize(sealed, final), relay, second),))
            if w.decode_forecast(local.account(second)).state != 10:
                raise RuntimeError("sealed finalization did not commit")
            for name, blocked in (("adjudicated", False), ("adjudication-pending", True)):
                target, _, previous_gate = fixtures[name]
                previous = w.decode_forecast(local.account(target))
                change = w.encode_advance(revision=previous.revision+1, occurred_at_ms=local.clock(),
                    previous_event_hash=previous.event_hash, event_hash=digest(name+"next-event"),
                    snapshot_hash=digest(name+"next-snapshot"), state=5, outcome=2,
                    resolution_hash=digest(name+"replacement"))
                local.send(name+"_ordinary_bypass_rejected", relay,
                           (ix(d.encode_advance(change), relay, target),), reject=True)
                guarded = d.encode_advance(change, mode=1, artifact=digest(name+"adjudication"), head=previous_gate.head)
                local.send(name+"_wrong_reviewer_rejected", relay,
                           (d.instruction(program, guarded, actor=relay, forecast=target, adjudicator=other),), reject=True)
                local.send(name+"_reviewed_replacement", relay,
                           (d.instruction(program, guarded, actor=relay, forecast=target, adjudicator=judge),), reject=blocked)
                after = d.Accumulator.decode(local.account(d.gate_address(program, target)[0]))
                if blocked and after != previous_gate:
                    raise RuntimeError("pending intake was discarded by replacement")
                if not blocked and (after.epoch != 2 or after.pending or after.material
                                    or after.resolution != digest(name+"replacement")):
                    raise RuntimeError("adjudicated replacement epoch mismatch")

            print(json.dumps({"phase": "maximum_evidence_upload", "bytes": d.MAX_BODY}), flush=True)
            maximum_address, _, _ = fixtures["maximum"]
            maximum_key, maximum = upload("maximum", b"*"*d.MAX_BODY)
            local.send("maximum_missing_heap_rejected", user,
                       (ix(d.encode_submit(maximum), user, maximum_address, maximum_key),), reject=True, heap=False)
            local.send("maximum_submit", user, (ix(d.encode_submit(maximum), user, maximum_address, maximum_key),))
            maximum = d.Receipt.decode(local.account(maximum_key))
            if len(maximum.body) != d.MAX_BODY or maximum.status != 1:
                raise RuntimeError("maximum native evidence incomplete")
            until = time.monotonic()+60
            while local.clock() <= gate.deadline:
                if time.monotonic() > until:
                    raise RuntimeError("local challenge clock did not advance")
                time.sleep(0.2)
            pending = d.Accumulator.decode(local.account(d.gate_address(program, address)[0]))
            finish = candidate(address)
            # encode_seal correctly refuses pending; raw bytes exercise the native guard independently.
            pending_seal = b"\x0c" + finish[1:] + struct.pack("<Q", pending.revision) + pending.commitment()
            local.send("intake_first_blocks_seal_after_deadline", relay, (ix(pending_seal, relay, address),), reject=True)
            review = d.encode_review(accepted, digest("retained-independent-review"), 2)
            local.send("relayer_cannot_clear", relay, (ix(review, relay, address, account),), reject=True)
            local.send("reviewer_role_alias_rejected", admin,
                       (d.instruction(program, d.encode_reviewer(relay, role), actor=admin),), reject=True)
            local.send("reviewer_rotation_requires_admin", relay,
                       (d.instruction(program, d.encode_reviewer(other, role), actor=relay),), reject=True)
            local.send("reviewer_rotation", admin, (d.instruction(program, d.encode_reviewer(other, role), actor=admin),))
            local.send("old_reviewer_cannot_clear", judge, (ix(review, judge, address, account),), reject=True)
            rotated = d.Reviewer.decode(local.account(d.reviewer_address(program)[0]))
            local.send("reviewer_restore_scoped_key", admin,
                       (d.instruction(program, d.encode_reviewer(judge, rotated), actor=admin),))
            local.send("review_rejects_nonmaterial", judge, (ix(review, judge, address, account),))
            local.send("review_cannot_replay", judge, (ix(review, judge, address, account),), reject=True)
            cleared = d.Accumulator.decode(local.account(d.gate_address(program, address)[0]))
            finish = candidate(address)
            local.send("seal_after_authenticated_review", relay, (ix(d.encode_seal(cleared, finish), relay, address),))
            sealed = d.Accumulator.decode(local.account(d.gate_address(program, address)[0]))
            local.send("finish_after_authenticated_review", relay, (ix(d.encode_finalize(sealed, finish), relay, address),))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"network": "local-validator", "program": w.base58_encode(program),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "global_heap_bytes": 262144,
                "max_evidence_bytes": len(maximum.body), "fixture_windows_not_elapsed_48h": True,
                "events": local.events, "validator_log": str(directory / "validator.log")}, indent=2)+"\n")
            print(json.dumps({"passed": True, "events": len(local.events), "report": str(output)}), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--port", type=int, default=61545)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/dispute-local-report.json")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65532 or not args.output.resolve().is_relative_to(ROOT / "tmp"):
        parser.error("port must be unprivileged; output must remain under repository tmp/")
    run(args.artifact.resolve(), args.port, args.output.resolve())


if __name__ == "__main__":
    main()
