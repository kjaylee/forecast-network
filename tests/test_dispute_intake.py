"""Real SQLite migration and finalized-RPC fixtures matching the tested SBF ABI."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import struct
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from forecast_application import dispute_intake as module
from forecast_application import dispute_wire as w
from forecast_application import solana_wire as legacy
from forecast_application.database import SQLiteDatabase
from forecast_application.solana_registry import DEVNET_GENESIS, identity_hash

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = bytes([31])*32
USER = bytes([32])*32
FORECAST_ID = "intake-fixture"
CREATOR_ID = "creator-fixture"
SPEC = bytes([33])*32
EVENT = bytes([34])*32
RESOLUTION = bytes([35])*32
# Actual local SBF finalized RPC account capture, coherent slot79, artifact
# df84554ba3cb3a0bf2cb77036c40f70683e95004a7623cd9f14c5a79f31236a2.
# The source genesis is LOCAL. Only the positive replay test substitutes the
# expected Devnet genesis; this is fixture-based verification, not a Devnet claim.
NATIVE_CAPTURE = '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'
ADDRESS = legacy.forecast_address(PROGRAM, identity_hash("forecast", FORECAST_ID))[0]
GATE_ADDRESS = w.gate_address(PROGRAM, ADDRESS)[0]


def evidence() -> bytes:
    raw = b"retained public evidence"
    value = {"version": "forecast-dispute-evidence-v1", "claim": "A material fact", "rule_clause_id": "rule-1",
             "explanation": "The source disagrees.", "sources": [{"url": "https://example.org/source",
             "body_base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest(), "captured_at_ms": 50}]}
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def forecast_bytes(*, revision: int = 4, resolution: bytes = RESOLUTION, deadline: int = 100) -> bytes:
    return (b"FNFORE01" + identity_hash("forecast", FORECAST_ID) + identity_hash("creator", CREATOR_ID) + SPEC
            + struct.pack("<qqQq", 10, 20, revision, 30) + bytes((6, 1, 0, 0))
            + EVENT + bytes([36])*32 + resolution + bytes(96) + struct.pack("<qqqHH", deadline, deadline, 0, 0, 0))


def gate() -> w.Accumulator:
    # Migration anchors to current revision4/event, not an invented old proposal3.
    return w.Accumulator(ADDRESS, SPEC, 1, 1, 4, RESOLUTION, EVENT, 30, 100, 0, 0, 0, bytes([37])*32, 1)


def receipt(body: bytes | None = None) -> w.Receipt:
    body = evidence() if body is None else body
    return w.Receipt(ADDRESS, SPEC, PROGRAM, 1, 4, RESOLUTION, EVENT, USER, bytes([38])*32,
                     w.evidence_hash(body), len(body), body, 1, 100, 10, 100)


def candidate() -> bytes:
    return legacy.encode_advance(revision=5, occurred_at_ms=150, previous_event_hash=EVENT,
        event_hash=bytes([39])*32, snapshot_hash=bytes([40])*32, state=10, outcome=1,
        resolution_hash=RESOLUTION, reputation_hash=bytes([41])*32, challenge_until_ms=100)


class Rpc:
    def __init__(self) -> None:
        self.genesis = DEVNET_GENESIS
        self.accounts: dict[bytes, bytes] = {ADDRESS: forecast_bytes(), GATE_ADDRESS: gate().encode()}
        self.owners: dict[bytes, bytes] = {}
        self.calls: list[tuple[str, list[Any]]] = []
        self.slot = 100
        self.hook: Any = None
        self.force_slot: int | None = None

    async def __call__(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if self.hook is not None:
            await self.hook(method, params)
        if method == "getGenesisHash":
            return self.genesis
        if method != "getAccountInfo":
            raise AssertionError("importer attempted a network mutation")
        if params[1]["commitment"] != "finalized":
            raise AssertionError("importer did not request finalized evidence")
        self.slot += 1
        address = legacy.base58_decode(params[0], length=32)
        if address == PROGRAM:
            value = {"owner": "BPFLoaderUpgradeab1e11111111111111111111111", "executable": True, "lamports": 1,
                     "data": ["", "base64"]}
        elif address not in self.accounts:
            value = None
        else:
            value = {"owner": legacy.base58_encode(self.owners.get(address, PROGRAM)), "executable": False,
                     "lamports": 1000000, "data": [base64.b64encode(self.accounts[address]).decode(), "base64"]}
        return {"context": {"slot": self.slot if self.force_slot is None else self.force_slot}, "value": value}


class DisputeIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript((ROOT / "apps/web/migrations/0001_initial.sql").read_text())
        migration = ROOT / "apps/web/migrations/0025_independent_intake.sql"
        self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        await self.db.execute("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                              (CREATOR_ID, "Creator", "creator", "recovery", 0))
        await self.db.execute("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
                              "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (FORECAST_ID, CREATOR_ID, "draft", "{}", 4,
                              "CHALLENGE", "science", "Fixture", "Fixture?", "fixture", SPEC.hex(), 10, 20, 0, 30, "fixture"))
        await self.db.execute("INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                              (FORECAST_ID, 4, EVENT.hex(), "{}", 30))
        self.rpc = Rpc()
        self.now = 1000
        self.service = module.DisputeIntake(self.db, self.rpc, program_id=PROGRAM, now_ms=lambda: self.now)
        self.receipt_address = w.receipt_address(PROGRAM, ADDRESS, 1, USER)[0]

    async def asyncTearDown(self) -> None:
        self.connection.close()

    def set_receipt(self, r: w.Receipt, g: w.Accumulator | None = None) -> None:
        self.rpc.accounts[self.receipt_address] = r.encode()
        self.rpc.accounts[GATE_ADDRESS] = (g or replace(gate(), pending=1, accepted=1)).encode()

    def set_seal(self, data: bytes | None = None, *, revision: int = 2, accepted: int = 0) -> w.Accumulator:
        raw = candidate() if data is None else data
        fields = w.decode_instruction(b"\x0c"+raw[1:]+bytes(40))["advance"]
        g = replace(gate(), revision=revision, phase=2, accepted=accepted,
                    sealed_revision=fields["revision"], sealed_event=fields["event_hash"],
                    sealed_snapshot=fields["snapshot_hash"], sealed_payload=w.advance_hash(raw), sealed_at=200, sealed_slot=50)
        self.rpc.accounts[GATE_ADDRESS] = g.encode()
        return g

    async def test_migration_does_not_install_global_finalization_trigger(self) -> None:
        triggers = await self.db.all("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='forecasts'")
        self.assertEqual(triggers, [])
        await self.db.execute("UPDATE forecasts SET state='FINALIZED' WHERE id=?", (FORECAST_ID,))
        self.assertEqual((await self.db.first("SELECT state FROM forecasts"))["state"], "FINALIZED")

    async def test_timely_late_import_retains_exact_bytes_and_is_idempotent(self) -> None:
        r = receipt()
        self.set_receipt(r)
        self.assertEqual(await self.service.activate(FORECAST_ID), 1)
        result = await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.assertTrue(result["lateImport"])
        self.assertEqual(result["acceptedAt"], 100)
        self.assertTrue(result["canonicalEvidence"])
        self.now += 1000
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        rows = await self.db.all("SELECT * FROM intake_receipts")
        self.assertEqual(len(rows), 1)
        self.assertEqual(base64.b64decode(rows[0]["body_base64"]), r.body)
        self.assertEqual(rows[0]["imported_at"], 1000)
        with self.assertRaisesRegex(module.IntakeError, "unresolved"):
            await self.service.seal_admission(FORECAST_ID, 1)
        self.assertEqual(await self.db.all("SELECT * FROM mutation_guards"), [])

    async def test_wrong_genesis_owner_pda_and_native_scope_reject(self) -> None:
        await self.service.activate(FORECAST_ID)
        self.set_receipt(receipt())
        self.rpc.genesis = "mainnet"
        with self.assertRaisesRegex(module.IntakeError, "wrong_genesis"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.rpc.genesis = DEVNET_GENESIS
        self.rpc.owners[self.receipt_address] = bytes([99])*32
        with self.assertRaisesRegex(module.IntakeError, "account_owner"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.rpc.owners.clear()
        for field, value in (("program", bytes([99])*32), ("user", bytes([98])*32),
                             ("proposal_revision", 3), ("specification", bytes([97])*32)):
            self.rpc.accounts[self.receipt_address] = replace(receipt(), **{field: value}).encode()
            with self.subTest(field=field), self.assertRaises(module.IntakeError):
                await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.assertEqual(await self.db.all("SELECT * FROM intake_receipts"), [])

    async def test_evidence_tampering_rejected_but_native_invalid_json_retained(self) -> None:
        self.set_receipt(receipt())
        await self.service.activate(FORECAST_ID)
        raw = bytearray(receipt().encode())
        raw[-1] ^= 1
        self.rpc.accounts[self.receipt_address] = bytes(raw)
        with self.assertRaisesRegex(module.IntakeError, "receipt_invalid"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.set_receipt(receipt(b"*"*w.MAX_BODY))
        result = await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.assertFalse(result["canonicalEvidence"])
        self.assertEqual((await self.db.first("SELECT body_length FROM intake_receipts"))["body_length"], w.MAX_BODY)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute("UPDATE intake_receipts SET body_base64='eA=='")
        with self.assertRaisesRegex(module.IntakeError, "unresolved"):
            await self.service.seal_admission(FORECAST_ID, 1)

    async def test_incomplete_receipt_and_inconsistent_counter_reject(self) -> None:
        await self.service.activate(FORECAST_ID)
        full = receipt()
        draft = replace(full, status=0, accepted_at=0, accepted_slot=0, deadline=0, body=full.body[:1])
        self.set_receipt(draft)
        with self.assertRaisesRegex(module.IntakeError, "not_submitted"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.set_receipt(full, gate())
        with self.assertRaisesRegex(module.IntakeError, "counter_binding"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)

    async def test_generation_changed_during_reads_rolls_back_entire_import(self) -> None:
        self.set_receipt(receipt())
        await self.service.activate(FORECAST_ID)
        changed = False
        async def bump(method: str, params: list[Any]) -> None:
            nonlocal changed
            if method == "getAccountInfo" and not changed:
                changed = True
                await self.service.advance_generation(FORECAST_ID, 1)
        self.rpc.hook = bump
        with self.assertRaisesRegex(module.IntakeError, "generation_changed"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.assertEqual(await self.db.all("SELECT * FROM intake_receipts"), [])
        self.rpc.hook = None
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 2)

    async def test_read_coherence_retry_and_stale_finalized_context(self) -> None:
        self.set_receipt(receipt())
        await self.service.activate(FORECAST_ID)
        reads = 0
        async def change(method: str, params: list[Any]) -> None:
            nonlocal reads
            if method == "getAccountInfo" and params[0] == legacy.base58_encode(GATE_ADDRESS):
                reads += 1
                if reads == 2:
                    self.rpc.accounts[GATE_ADDRESS] = replace(gate(), revision=2, pending=1, accepted=1, head=bytes([50])*32).encode()
        self.rpc.hook = change
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.assertEqual(reads, 4)
        self.rpc.hook = None
        self.rpc.force_slot = 1
        with self.assertRaisesRegex(module.IntakeError, "stale_context"):
            await self.service.refresh(FORECAST_ID, 1)

    async def test_same_revision_fork_and_receipt_status_regression_fail_closed(self) -> None:
        self.set_receipt(receipt())
        await self.service.activate(FORECAST_ID)
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        original = self.rpc.accounts[GATE_ADDRESS]
        self.rpc.accounts[GATE_ADDRESS] = replace(w.Accumulator.decode(original), head=bytes([80])*32).encode()
        with self.assertRaisesRegex(module.IntakeError, "mirror_conflict"):
            await self.service.refresh(FORECAST_ID, 1)
        reviewed = replace(receipt(), status=2, review=bytes([81])*32, reviewer=bytes([82])*32, reviewed_at=150)
        self.set_receipt(reviewed, replace(gate(), revision=2, accepted=1, head=bytes([83])*32))
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.set_receipt(receipt(), replace(gate(), revision=3, pending=1, accepted=1, head=bytes([84])*32))
        with self.assertRaisesRegex(module.IntakeError, "mirror_conflict"):
            await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)

    async def test_epoch_phase_deadline_and_accepted_count_cannot_regress(self) -> None:
        self.set_receipt(receipt())
        await self.service.activate(FORECAST_ID)
        original = self.rpc.accounts[GATE_ADDRESS]
        variants = [replace(gate(), revision=2, deadline=99, pending=1, accepted=1),
                    replace(gate(), revision=2),
                    replace(gate(), revision=2, epoch=0, proposal_revision=0, resolution=bytes(32),
                            proposal_event=bytes(32), opened=0, deadline=0, phase=0)]
        for changed in variants:
            self.rpc.accounts[GATE_ADDRESS] = changed.encode()
            with self.assertRaisesRegex(module.IntakeError, "mirror_conflict"):
                await self.service.refresh(FORECAST_ID, 1)
        self.rpc.accounts[GATE_ADDRESS] = original
        self.set_seal(revision=3, accepted=1)
        await self.service.refresh(FORECAST_ID, 1)
        self.rpc.accounts[GATE_ADDRESS] = replace(gate(), revision=4, accepted=1).encode()
        with self.assertRaisesRegex(module.IntakeError, "mirror_conflict"):
            await self.service.refresh(FORECAST_ID, 1)

    async def test_material_current_epoch_holds_but_reviewed_old_epoch_is_historical(self) -> None:
        material = replace(receipt(), status=3, review=bytes([81])*32, reviewer=bytes([82])*32, reviewed_at=150)
        self.set_receipt(material, replace(gate(), revision=2, material=1, accepted=1))
        await self.service.activate(FORECAST_ID)
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        with self.assertRaisesRegex(module.IntakeError, "unresolved"):
            await self.service.seal_admission(FORECAST_ID, 1)
        new_resolution = bytes([85])*32
        self.rpc.accounts[ADDRESS] = forecast_bytes(revision=8, resolution=new_resolution, deadline=200)
        self.rpc.accounts[GATE_ADDRESS] = replace(gate(), epoch=2, revision=3, proposal_revision=7,
            proposal_event=bytes([86])*32, resolution=new_resolution, deadline=200).encode()
        result = await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        self.assertTrue(result["historical"])
        await self.service.seal_admission(FORECAST_ID, 1)

    async def test_empty_counter_preflight_cannot_authorize_local_finalization(self) -> None:
        await self.service.activate(FORECAST_ID)
        await self.service.seal_admission(FORECAST_ID, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.batch([module.admission_guard_sql(FORECAST_ID, 1, candidate(), token="test-final"),
                                 ("UPDATE forecasts SET state='FINALIZED' WHERE id=?", (FORECAST_ID,))])
        self.assertEqual((await self.db.first("SELECT state FROM forecasts"))["state"], "CHALLENGE")

    async def test_exact_seal_admission_is_fenced_and_candidate_immutable(self) -> None:
        await self.service.activate(FORECAST_ID)
        self.set_seal()
        proof = await self.service.verify_and_store_seal(FORECAST_ID, 1, candidate())
        self.assertEqual(proof["candidateEventHash"], (bytes([39])*32).hex())
        await self.service.advance_generation(FORECAST_ID, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.batch([module.admission_guard_sql(FORECAST_ID, 1, candidate(), token="old-owner")])
        await self.service.verify_and_store_seal(FORECAST_ID, 2, candidate())
        altered = bytearray(candidate())
        altered[60] ^= 1
        self.set_seal(bytes(altered), revision=3)
        with self.assertRaisesRegex(module.IntakeError, "mirror_conflict"):
            await self.service.verify_and_store_seal(FORECAST_ID, 2, bytes(altered))
        rows = await self.db.all("SELECT * FROM intake_seals")
        self.assertEqual(len(rows), 1)
        self.assertEqual(base64.b64decode(rows[0]["advance_base64"]), candidate())
        self.set_seal()
        await self.db.batch([module.admission_guard_sql(FORECAST_ID, 2, candidate(), token="new-owner"),
            ("UPDATE forecasts SET state='FINALIZED',revision=5 WHERE id=? AND revision=4", (FORECAST_ID,)),
            ("DELETE FROM mutation_guards WHERE token=?", ("new-owner",))])
        self.assertEqual((await self.db.first("SELECT state FROM forecasts"))["state"], "FINALIZED")

    async def test_stale_pending_mirror_blocks_seal_until_review_imported(self) -> None:
        self.set_receipt(receipt())
        await self.service.activate(FORECAST_ID)
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        reviewed = replace(receipt(), status=2, review=bytes([81])*32, reviewer=bytes([82])*32, reviewed_at=150)
        self.rpc.accounts[self.receipt_address] = reviewed.encode()
        self.set_seal(revision=3, accepted=1)
        with self.assertRaisesRegex(module.IntakeError, "mirror_conflict"):
            await self.service.verify_and_store_seal(FORECAST_ID, 1, candidate())
        self.assertEqual(await self.db.all("SELECT * FROM intake_seals"), [])
        await self.service.import_receipt(FORECAST_ID, self.receipt_address, 1)
        await self.service.verify_and_store_seal(FORECAST_ID, 1, candidate())

    async def test_actual_sbf_account_capture_preserves_maximum_body(self) -> None:
        captured = json.loads(zlib.decompress(base64.b64decode(NATIVE_CAPTURE)))
        self.assertEqual(captured["source"], "actual-local-SBF-finalized-RPC")
        self.assertNotEqual(captured["source_genesis"], DEVNET_GENESIS)
        program = legacy.base58_decode(captured["program"], length=32)
        forecast_id = captured["forecast_id"]
        forecast_address = legacy.forecast_address(program, identity_hash("forecast", forecast_id))[0]
        f = legacy.decode_forecast(base64.b64decode(captured["accounts"][legacy.base58_encode(forecast_address)]["value"]["data"][0]))
        await self.db.execute("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
                              "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (forecast_id, CREATOR_ID, "native-draft", "{}", f.revision,
                              "CHALLENGE", "science", "Native fixture", "Native?", "native-fixture", f.specification_hash.hex(),
                              f.open_at_ms, f.close_at_ms, 0, f.occurred_at_ms, "native-fixture"))
        await self.db.execute("INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                              (forecast_id, f.revision, f.event_hash.hex(), "{}", f.occurred_at_ms))
        genesis = captured["source_genesis"]
        async def replay(method: str, params: list[Any]) -> Any:
            if method == "getGenesisHash":
                return genesis
            self.assertEqual(method, "getAccountInfo")
            self.assertEqual(params[1]["commitment"], "finalized")
            return captured["accounts"][params[0]]
        raddr = legacy.base58_decode(captured["receipt"], length=32)
        raw = base64.b64decode(captured["accounts"][captured["receipt"]]["value"]["data"][0])
        r = w.Receipt.decode(raw)
        service = module.DisputeIntake(self.db, replay, program_id=program, now_ms=lambda: r.deadline+1000)
        with self.assertRaisesRegex(module.IntakeError, "wrong_genesis"):
            await service.activate(forecast_id)
        # Positive fixture replay substitutes only network response for a unit
        # test; original local genesis remains retained above and is rejected.
        genesis = DEVNET_GENESIS
        await service.activate(forecast_id)
        result = await service.import_receipt(forecast_id, raddr, 1)
        self.assertTrue(result["lateImport"])
        self.assertEqual(r.body_length, 32768)
        row = await self.db.first("SELECT body_base64 FROM intake_receipts WHERE forecast_id=?", (forecast_id,))
        self.assertEqual(base64.b64decode(row["body_base64"]), r.body)
        with self.assertRaisesRegex(module.IntakeError, "unresolved"):
            await service.seal_admission(forecast_id, 1)


if __name__ == "__main__":
    unittest.main()
