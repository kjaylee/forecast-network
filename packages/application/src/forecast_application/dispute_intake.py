"""Finalized RPC intake mirror and opt-in exact-candidate seal admission.

RPC observations are trusted finalized-provider evidence, not light-client proofs.
No signing, chain writes, DomainV3 serialization or global lifecycle hooks live here.
All RPC awaits are sequential. Root owns closing-action persistence and the final
aggregate CAS; admission_guard_sql must be included in that same atomic batch.
"""
from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import dispute_wire as wire
from . import solana_wire as legacy
from .database import Database, Statement
from .solana_registry import DEVNET_GENESIS, identity_hash

Rpc = Callable[[str, list[Any]], Awaitable[Any]]
ZERO = bytes(32)
_LOADERS = frozenset({"BPFLoaderUpgradeab1e11111111111111111111111",
                      "BPFLoader2111111111111111111111111111111111",
                      "BPFLoader1111111111111111111111111111111111"})


class IntakeError(ValueError):
    """Fixed operational code; never raw provider/header/body data."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise IntakeError(code)


def _integer(value: Any) -> int:
    _require(type(value) is int and 0 <= value <= wire.MAX, "intake_invalid_integer")
    return int(value)


def _key(value: bytes) -> bytes:
    _require(type(value) is bytes and len(value) == 32 and value != ZERO, "intake_invalid_key")
    return value


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _guard(token: str, query: str, params: tuple[Any, ...]) -> Statement:
    return ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN " + query + " THEN 1 ELSE 0 END",
            (token, *params))


def _immutable(table: str, fields: tuple[str, ...], values: tuple[Any, ...], token: str) -> list[Statement]:
    # Table/field names are only module literals; every external value is bound.
    return [(f"INSERT OR IGNORE INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values),
            _guard(token, f"EXISTS(SELECT 1 FROM {table} WHERE " + " AND ".join(f"{name}=?" for name in fields) + ")", values),
            ("DELETE FROM mutation_guards WHERE token=?", (token,))]


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    address: bytes
    data: bytes
    slot: int


@dataclass(frozen=True, slots=True)
class EpochEvidence:
    accumulator: wire.Accumulator
    forecast: legacy.ForecastAccount
    accumulator_account: AccountEvidence
    forecast_account: AccountEvidence
    receipt_account: AccountEvidence | None = None


def _candidate(data: bytes) -> dict[str, Any]:
    # Leading tag2 is solely the existing canonical preimage format, never sent.
    _require(type(data) is bytes and len(data) == 255 and data[0] == 2, "intake_candidate_format")
    try:
        decoded = wire.decode_instruction(b"\x0c" + data[1:] + bytes(40))
    except ValueError:
        raise IntakeError("intake_candidate_format") from None
    return dict(decoded["advance"])


def admission_guard_sql(forecast_id: str, generation: int, native_advance: bytes, *, token: str) -> Statement:
    """Root inserts this before its forecast UPDATE, inside the SAME CAS batch.

    Requires explicit activation. Root must delete this guard token in that batch.
    This does not acquire a closing-action lease or serialize a DomainV3 candidate.
    """
    _require(type(forecast_id) is str and bool(forecast_id), "intake_forecast_id")
    _require(_integer(generation) > 0, "intake_generation")
    _require(type(token) is str and 0 < len(token) <= 128, "intake_guard_token")
    value = _candidate(native_advance)
    query = """EXISTS(SELECT 1 FROM intake_bindings b
 JOIN intake_heads h ON h.forecast_id=b.forecast_id AND h.generation=b.generation
 JOIN intake_seals s ON s.forecast_id=b.forecast_id AND s.epoch=h.epoch
 JOIN intake_seal_admissions a ON a.forecast_id=s.forecast_id AND a.epoch=s.epoch AND a.generation=b.generation
 JOIN forecasts f ON f.id=b.forecast_id
 JOIN events e ON e.forecast_id=f.id AND e.revision=f.revision
 WHERE b.forecast_id=? AND b.generation=? AND f.state='CHALLENGE' AND h.phase=2 AND h.pending_count=0 AND h.material_count=0
 AND h.commitment=s.seal_commitment AND a.context_slot<=h.context_slot
 AND s.advance_base64=? AND s.advance_hash=?
 AND s.predecessor_revision=f.revision AND s.predecessor_event_hash=e.hash
 AND s.candidate_revision=? AND s.candidate_event_hash=? AND s.candidate_snapshot_hash=?
 AND NOT EXISTS(SELECT 1 FROM intake_receipts r JOIN intake_receipt_heads rh ON rh.address=r.address
   WHERE r.forecast_id=b.forecast_id AND (rh.native_status=1 OR (r.epoch=h.epoch AND rh.native_status=3))))"""
    return _guard(token, query, (forecast_id, generation, _b64(native_advance), wire.advance_hash(native_advance).hex(),
                                value["revision"], value["event_hash"].hex(), value["snapshot_hash"].hex()))


class DisputeIntake:
    def __init__(self, db: Database, rpc: Rpc, *, program_id: bytes, now_ms: Callable[[], int]):
        self.db, self.rpc, self.program = db, rpc, _key(program_id)
        self.now_ms = now_ms

    async def _call(self, method: str, params: list[Any]) -> Any:
        _require(method in {"getGenesisHash", "getAccountInfo"}, "intake_read_only_transport")
        try:
            return await self.rpc(method, params)
        except Exception:
            raise IntakeError("intake_rpc_unavailable") from None

    async def _network(self) -> None:
        _require(await self._call("getGenesisHash", []) == DEVNET_GENESIS, "intake_wrong_genesis")
        response = await self._call("getAccountInfo", [legacy.base58_encode(self.program),
                                   {"encoding": "base64", "commitment": "finalized"}])
        _, value = self._context(response)
        _require(type(value) is dict and value.get("executable") is True
                 and type(value.get("owner")) is str and value.get("owner") in _LOADERS
                 and _integer(value.get("lamports")) > 0,
                 "intake_program_unverified")

    @staticmethod
    def _context(response: Any) -> tuple[int, Any]:
        _require(type(response) is dict and type(response.get("context")) is dict
                 and "value" in response, "intake_rpc_context")
        slot = _integer(response["context"].get("slot"))
        _require(slot > 0, "intake_rpc_context")
        return slot, response["value"]

    async def _account(self, address: bytes, *, maximum: int, minimum_slot: int) -> AccountEvidence:
        result = await self._call("getAccountInfo", [legacy.base58_encode(address), {
            "encoding": "base64", "commitment": "finalized", "minContextSlot": minimum_slot}])
        slot, value = self._context(result)
        _require(slot >= minimum_slot, "intake_stale_context")
        _require(type(value) is dict and value.get("owner") == legacy.base58_encode(self.program)
                 and value.get("executable") is False and _integer(value.get("lamports")) > 0,
                 "intake_account_owner")
        encoded = value.get("data")
        _require(type(encoded) is list and len(encoded) == 2 and encoded[1] == "base64"
                 and type(encoded[0]) is str and len(encoded[0]) <= 4*((maximum+2)//3), "intake_account_encoding")
        try:
            raw = base64.b64decode(encoded[0], validate=True)
        except ValueError:
            raise IntakeError("intake_account_encoding") from None
        _require(len(raw) <= maximum and _b64(raw) == encoded[0], "intake_account_encoding")
        return AccountEvidence(address, raw, slot)

    async def _local(self, forecast_id: str) -> dict[str, Any]:
        _require(type(forecast_id) is str and 0 < len(forecast_id) <= 128, "intake_forecast_id")
        row = await self.db.first("SELECT id,specification_hash,creator_id FROM forecasts WHERE id=?", (forecast_id,))
        _require(row is not None, "intake_forecast_missing")
        return dict(row or {})

    async def _binding(self, forecast_id: str, generation: int) -> dict[str, Any]:
        _require(type(forecast_id) is str and 0 < len(forecast_id) <= 128, "intake_forecast_id")
        _require(_integer(generation) > 0, "intake_generation")
        row = await self.db.first("SELECT * FROM intake_bindings WHERE forecast_id=?", (forecast_id,))
        _require(row is not None and row["generation"] == generation, "intake_generation_changed")
        _require(row is not None and row["program_id"] == legacy.base58_encode(self.program)
                 and row["genesis_hash"] == DEVNET_GENESIS, "intake_binding_mismatch")
        expected = legacy.forecast_address(self.program, identity_hash("forecast", forecast_id))[0]
        _require(row is not None and row["forecast_address"] == legacy.base58_encode(expected)
                 and row["accumulator_address"] == legacy.base58_encode(wire.gate_address(self.program, expected)[0]),
                 "intake_binding_mismatch")
        return dict(row or {})

    async def _observe(self, forecast_id: str, *, receipt_address: bytes | None = None) -> EpochEvidence:
        local = await self._local(forecast_id)
        await self._network()
        fid = identity_hash("forecast", forecast_id)
        forecast_address = legacy.forecast_address(self.program, fid)[0]
        accumulator_address = wire.gate_address(self.program, forecast_address)[0]
        head = await self.db.first("SELECT context_slot FROM intake_heads WHERE forecast_id=?", (forecast_id,))
        minimum = int(head["context_slot"]) if head else 1
        for _ in range(2):
            first = await self._account(accumulator_address, maximum=wire.GATE_SIZE, minimum_slot=minimum)
            receipt = None
            slot = first.slot
            if receipt_address is not None:
                receipt = await self._account(receipt_address, maximum=wire.RECEIPT_HEADER+wire.MAX_BODY, minimum_slot=slot)
                slot = receipt.slot
            forecast = await self._account(forecast_address, maximum=360, minimum_slot=slot)
            last = await self._account(accumulator_address, maximum=wire.GATE_SIZE, minimum_slot=forecast.slot)
            minimum = last.slot
            if first.data != last.data:
                continue
            try:
                g = wire.Accumulator.decode(last.data)
                f = legacy.decode_forecast(forecast.data)
            except ValueError:
                raise IntakeError("intake_wire_invalid") from None
            _require(g.forecast == forecast_address and g.specification == f.specification_hash
                     and f.forecast_id_hash == fid and f.creator_hash == identity_hash("creator", local["creator_id"])
                     and f.specification_hash.hex() == local["specification_hash"], "intake_forecast_binding")
            _require(g.phase == 0 or (g.resolution == f.resolution_hash and g.proposal_revision <= f.revision),
                     "intake_proposal_binding")
            return EpochEvidence(g, f, last, forecast, receipt)
        raise IntakeError("intake_observation_raced")

    @staticmethod
    def _generation_guard(forecast_id: str, generation: int, token: str) -> Statement:
        return _guard(token, "EXISTS(SELECT 1 FROM intake_bindings WHERE forecast_id=? AND generation=?)",
                      (forecast_id, generation))

    def _epoch_sql(self, forecast_id: str, generation: int, observation: EpochEvidence, token: str) -> list[Statement]:
        g, a, f = observation.accumulator, observation.accumulator_account, observation.forecast_account
        now = _integer(self.now_ms())
        # observed_at is excluded from equality so exact retries at a later wall
        # clock do not mutate the original archived observation.
        fields = ("forecast_id", "epoch", "revision", "context_slot", "commitment", "account_base64", "forecast_base64")
        values = (forecast_id, g.epoch, g.revision, a.slot, g.commitment().hex(), _b64(a.data), _b64(f.data))
        statements: list[Statement] = [
            ("INSERT OR IGNORE INTO intake_epoch_observations("+",".join(fields)+",observed_at) VALUES(?,?,?,?,?,?,?,?)", (*values, now)),
            _guard(token+"o", "EXISTS(SELECT 1 FROM intake_epoch_observations WHERE "+" AND ".join(name+"=?" for name in fields)+")", values),
            ("DELETE FROM mutation_guards WHERE token=?", (token+"o",)),
            ("INSERT INTO intake_heads(forecast_id,generation,epoch,revision,context_slot,commitment,phase,pending_count,material_count,accepted_count,deadline) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(forecast_id) DO UPDATE SET generation=excluded.generation,epoch=excluded.epoch,"
             "revision=excluded.revision,context_slot=excluded.context_slot,commitment=excluded.commitment,phase=excluded.phase,"
             "pending_count=excluded.pending_count,material_count=excluded.material_count,accepted_count=excluded.accepted_count,deadline=excluded.deadline "
             "WHERE excluded.generation>=intake_heads.generation AND excluded.revision>=intake_heads.revision "
             "AND excluded.context_slot>=intake_heads.context_slot AND excluded.epoch>=intake_heads.epoch "
             "AND ((excluded.epoch=intake_heads.epoch AND excluded.phase>=intake_heads.phase "
             "AND excluded.accepted_count>=intake_heads.accepted_count AND excluded.deadline>=intake_heads.deadline) "
             "OR (excluded.epoch>intake_heads.epoch AND intake_heads.phase<2 AND excluded.phase>=1)) "
             "AND (excluded.revision!=intake_heads.revision OR excluded.commitment=intake_heads.commitment)",
             (forecast_id, generation, g.epoch, g.revision, a.slot, g.commitment().hex(), g.phase, g.pending, g.material, g.accepted, g.deadline)),
            _guard(token+"h", "EXISTS(SELECT 1 FROM intake_heads WHERE forecast_id=? AND generation=? AND revision=? AND context_slot=? AND commitment=?)",
                   (forecast_id, generation, g.revision, a.slot, g.commitment().hex())),
            ("DELETE FROM mutation_guards WHERE token=?", (token+"h",)),
        ]
        return statements

    async def _batch(self, forecast_id: str, generation: int, statements: list[Statement], token: str) -> None:
        try:
            await self.db.batch([self._generation_guard(forecast_id, generation, token), *statements,
                                 ("DELETE FROM mutation_guards WHERE token=?", (token,))])
        except Exception:
            row = await self.db.first("SELECT generation FROM intake_bindings WHERE forecast_id=?", (forecast_id,))
            if row is None or row["generation"] != generation:
                raise IntakeError("intake_generation_changed") from None
            raise IntakeError("intake_mirror_conflict") from None

    @staticmethod
    def _token(forecast_id: str, generation: int, observation: EpochEvidence) -> str:
        raw = f"{forecast_id}:{generation}:{observation.accumulator_account.slot}:".encode()+observation.accumulator_account.data
        return "intake:"+hashlib.sha256(raw).hexdigest()

    async def activate(self, forecast_id: str) -> int:
        """Explicitly select an already-native-activated forecast; no native writes."""
        previous = await self.db.first("SELECT * FROM intake_bindings WHERE forecast_id=?", (forecast_id,))
        if previous:
            await self.refresh(forecast_id, int(previous["generation"]))
            return int(previous["generation"])
        observation = await self._observe(forecast_id)
        g = observation.accumulator
        token = self._token(forecast_id, 1, observation)
        fields = ("forecast_id", "program_id", "genesis_hash", "forecast_address", "accumulator_address", "specification_hash", "generation")
        values = (forecast_id, legacy.base58_encode(self.program), DEVNET_GENESIS, legacy.base58_encode(g.forecast),
                  legacy.base58_encode(observation.accumulator_account.address), g.specification.hex(), 1)
        sql: list[Statement] = [("INSERT OR IGNORE INTO intake_bindings("+",".join(fields)+",activated_at) VALUES(?,?,?,?,?,?,?,?)",
                                (*values, _integer(self.now_ms()))),
                               self._generation_guard(forecast_id, 1, token)]
        sql += [_guard(token+"b", "EXISTS(SELECT 1 FROM intake_bindings WHERE "+" AND ".join(name+"=?" for name in fields)+")", values),
                ("DELETE FROM mutation_guards WHERE token=?", (token+"b",))]
        sql += self._epoch_sql(forecast_id, 1, observation, token)
        sql.append(("DELETE FROM mutation_guards WHERE token=?", (token,)))
        try:
            await self.db.batch(sql)
        except Exception:
            raise IntakeError("intake_activation_raced") from None
        return 1

    async def advance_generation(self, forecast_id: str, expected_generation: int) -> int:
        await self._binding(forecast_id, expected_generation)
        next_generation = _integer(expected_generation+1)
        result = await self.db.execute("UPDATE intake_bindings SET generation=? WHERE forecast_id=? AND generation=?",
                                       (next_generation, forecast_id, expected_generation))
        _require(result["meta"]["changes"] == 1, "intake_generation_changed")
        return next_generation

    async def refresh(self, forecast_id: str, generation: int) -> EpochEvidence:
        await self._binding(forecast_id, generation)
        observation = await self._observe(forecast_id)
        token = self._token(forecast_id, generation, observation)
        await self._batch(forecast_id, generation, self._epoch_sql(forecast_id, generation, observation, token), token)
        return observation

    async def import_receipt(self, forecast_id: str, receipt_address: bytes, generation: int) -> dict[str, Any]:
        await self._binding(forecast_id, generation)
        observation = await self._observe(forecast_id, receipt_address=_key(receipt_address))
        a, g = observation.receipt_account, observation.accumulator
        _require(a is not None, "intake_receipt_missing")
        if a is None:
            raise IntakeError("intake_receipt_missing")
        try:
            r = wire.Receipt.decode(a.data)
        except ValueError:
            raise IntakeError("intake_receipt_invalid") from None
        _require(r.program == self.program and r.forecast == g.forecast and r.specification == g.specification
                 and wire.receipt_address(self.program, r.forecast, r.epoch, r.user)[0] == receipt_address,
                 "intake_receipt_binding")
        _require(r.status != 0 and 0 < r.accepted_slot <= a.slot, "intake_not_submitted")
        _require(r.epoch <= g.epoch, "intake_future_epoch")
        if r.epoch == g.epoch:
            _require(r.proposal_revision == g.proposal_revision and r.proposal_event == g.proposal_event
                     and r.resolution == g.resolution and r.accepted_at >= g.opened and g.accepted > 0
                     and (observation.forecast.state == 9 or r.deadline <= g.deadline)
                     and (r.status != 1 or g.pending > 0) and (r.status != 3 or g.material > 0),
                     "intake_receipt_counter_binding")
        else:
            # Under this native version replacement requires all pending receipts
            # reviewed; the immutable receipt retains the old anchor/proposal.
            _require(r.status in (2, 3), "intake_historical_pending")
        canonical = True
        try:
            envelope = wire.decode_evidence(r.body)
            canonical = all(source["captured_at_ms"] <= r.accepted_at for source in envelope["sources"])
        except ValueError:
            canonical = False
        token = self._token(forecast_id, generation, observation)
        address_text = legacy.base58_encode(receipt_address)
        fields = ("address", "forecast_id", "epoch", "proposal_revision", "proposal_event_hash", "resolution_hash", "user_signer",
                  "nonce", "evidence_hash", "body_base64", "body_length", "accepted_at", "accepted_slot", "accepted_deadline", "evidence_valid")
        values = (address_text, forecast_id, r.epoch, r.proposal_revision, r.proposal_event.hex(), r.resolution.hex(),
                  legacy.base58_encode(r.user), r.nonce.hex(), r.evidence.hex(), _b64(r.body), r.body_length,
                  r.accepted_at, r.accepted_slot, r.deadline, int(canonical))
        statements = self._epoch_sql(forecast_id, generation, observation, token)
        statements += [("INSERT OR IGNORE INTO intake_receipts("+",".join(fields)+",imported_at) VALUES("+",".join("?" for _ in range(len(values)+1))+")",
                        (*values, _integer(self.now_ms()))),
                       _guard(token+"r", "EXISTS(SELECT 1 FROM intake_receipts WHERE "+" AND ".join(name+"=?" for name in fields)+")", values),
                       ("DELETE FROM mutation_guards WHERE token=?", (token+"r",))]
        obs_fields = ("address", "commitment", "context_slot", "account_base64", "native_status", "review_hash", "reviewer", "reviewed_at")
        obs_values = (address_text, r.commitment().hex(), a.slot, _b64(a.data), r.status, r.review.hex(),
                      legacy.base58_encode(r.reviewer), r.reviewed_at)
        statements += _immutable("intake_receipt_observations", obs_fields, obs_values, token+"v")
        statements += [("INSERT INTO intake_receipt_heads(address,commitment,context_slot,native_status) VALUES(?,?,?,?) "
                        "ON CONFLICT(address) DO UPDATE SET commitment=excluded.commitment,context_slot=excluded.context_slot,native_status=excluded.native_status "
                        "WHERE excluded.context_slot>=intake_receipt_heads.context_slot AND "
                        "((excluded.native_status=intake_receipt_heads.native_status AND excluded.commitment=intake_receipt_heads.commitment) "
                        "OR (intake_receipt_heads.native_status=1 AND excluded.native_status IN (2,3)))", (address_text, r.commitment().hex(), a.slot, r.status)),
                       _guard(token+"p", "EXISTS(SELECT 1 FROM intake_receipt_heads WHERE address=? AND commitment=? AND context_slot=? AND native_status=?)",
                              (address_text, r.commitment().hex(), a.slot, r.status)),
                       ("DELETE FROM mutation_guards WHERE token=?", (token+"p",))]
        await self._batch(forecast_id, generation, statements, token)
        return {"address": address_text, "epoch": r.epoch, "nativeStatus": r.status,
                "acceptedAt": r.accepted_at, "acceptedSlot": r.accepted_slot,
                "importedAt": _integer(self.now_ms()), "lateImport": self.now_ms() > r.deadline,
                "canonicalEvidence": canonical, "historical": r.epoch < g.epoch}

    async def seal_admission(self, forecast_id: str, generation: int) -> EpochEvidence:
        """Read-only preflight, NOT authority for local finalization; native seal must follow."""
        observation = await self.refresh(forecast_id, generation)
        g = observation.accumulator
        _require(g.phase == 1 and g.pending == g.material == 0, "intake_unresolved")
        pending = await self.db.first("SELECT r.address FROM intake_receipts r JOIN intake_receipt_heads h ON h.address=r.address "
                                      "WHERE r.forecast_id=? AND (h.native_status=1 OR (r.epoch=? AND h.native_status=3)) LIMIT 1",
                                      (forecast_id, g.epoch))
        _require(pending is None, "intake_mirror_unresolved")
        return observation

    async def verify_and_store_seal(self, forecast_id: str, generation: int, native_advance: bytes) -> dict[str, Any]:
        await self._binding(forecast_id, generation)
        candidate = _candidate(native_advance)
        observation = await self._observe(forecast_id)
        g, f = observation.accumulator, observation.forecast
        try:
            wire.encode_finalize(g, native_advance)
        except ValueError:
            raise IntakeError("intake_seal_candidate_mismatch") from None
        _require(g.phase == 2 and g.pending == g.material == 0 and g.sealed_slot <= observation.accumulator_account.slot
                 and f.state == 6 and f.pending_disputes == f.material_disputes == 0
                 and candidate["revision"] == f.revision+1 and candidate["previous_event_hash"] == f.event_hash
                 and candidate["resolution_hash"] == f.resolution_hash and candidate["outcome"] == f.outcome
                 and candidate["dispute_hash"] == f.dispute_hash and candidate["trigger_hash"] == f.trigger_hash
                 and candidate["challenge_until_ms"] == f.challenge_until_ms
                 and f.challenge_until_ms <= candidate["occurred_at_ms"] <= g.sealed_at,
                 "intake_seal_predecessor_mismatch")
        token = self._token(forecast_id, generation, observation)
        fields = ("forecast_id", "epoch", "advance_base64", "advance_hash", "predecessor_revision", "predecessor_event_hash",
                  "candidate_revision", "candidate_event_hash", "candidate_snapshot_hash", "seal_commitment", "account_base64", "sealed_at", "sealed_slot")
        values = (forecast_id, g.epoch, _b64(native_advance), wire.advance_hash(native_advance).hex(), f.revision, f.event_hash.hex(),
                  candidate["revision"], candidate["event_hash"].hex(), candidate["snapshot_hash"].hex(), g.commitment().hex(),
                  _b64(observation.accumulator_account.data), g.sealed_at, g.sealed_slot)
        statements = self._epoch_sql(forecast_id, generation, observation, token)
        statements += _immutable("intake_seals", fields, values, token+"s")
        statements += [("INSERT INTO intake_seal_admissions(forecast_id,epoch,generation,context_slot) VALUES(?,?,?,?) "
                        "ON CONFLICT(forecast_id,epoch,generation) DO UPDATE SET context_slot=excluded.context_slot "
                        "WHERE excluded.context_slot>=intake_seal_admissions.context_slot",
                        (forecast_id, g.epoch, generation, observation.accumulator_account.slot))]
        # Guard the mirrored intake status atomically with admission storage. A
        # previous pending mirror must be refreshed, never silently discounted.
        statements += [_guard(token+"u", "NOT EXISTS(SELECT 1 FROM intake_receipts r JOIN intake_receipt_heads h ON h.address=r.address "
                              "WHERE r.forecast_id=? AND (h.native_status=1 OR (r.epoch=? AND h.native_status=3)))", (forecast_id, g.epoch)),
                       ("DELETE FROM mutation_guards WHERE token=?", (token+"u",))]
        await self._batch(forecast_id, generation, statements, token)
        return {"epoch": g.epoch, "generation": generation, "candidateEventHash": candidate["event_hash"].hex(),
                "candidateSnapshotHash": candidate["snapshot_hash"].hex(), "advanceHash": wire.advance_hash(native_advance).hex(),
                "sealCommitment": g.commitment().hex(), "finalizedContextSlot": observation.accumulator_account.slot}
