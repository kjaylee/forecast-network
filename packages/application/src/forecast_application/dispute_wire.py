"""Independent intake v1: exact native ABI, immutable evidence and chain-first seals.

Pure codecs only. Inclusion proof requires finalized accounts from the configured
program/Devnet transport; decoding bytes alone does not authenticate them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from . import solana_wire as wire

MAX = (1 << 53) - 1
GENESIS = wire.base58_decode("EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG", length=32)
GATE_SEED, RECEIPT_SEED, REVIEWER_SEED = b"intake-v1", b"dispute-v1", b"intake-review-v1"
GATE_SIZE, RECEIPT_HEADER, REVIEWER_SIZE = 392, 424, 48
MAX_BODY, CHUNK = 32768, 512
ZERO = bytes(32)
SYSTEM = ZERO
COMPUTE_BUDGET = wire.base58_decode("ComputeBudget111111111111111111111111111111", length=32)
_GATE = struct.Struct("<8s32s32s32s3Q32s32sqq3Q32sB7sQ32s32s32sqQ")
_RECEIPT = struct.Struct("<8s32s32s32s32sQQ32s32s32s32s32sIIB7sqQq32s32sq")


def _require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def _num(value: int, maximum: int = MAX) -> int:
    _require(type(value) is int and 0 <= value <= maximum, "invalid integer")
    return value


def _key(value: bytes, nonzero: bool = False) -> bytes:
    _require(type(value) is bytes and len(value) == 32 and (not nonzero or value != ZERO), "invalid hash/key")
    return value


def _bytes(value: bytes, size: int) -> bytes:
    _require(type(value) is bytes and len(value) == size, "invalid wire length")
    return value


def _hash(domain: bytes, data: bytes) -> bytes:
    return hashlib.sha256(domain + data).digest()


def evidence_hash(body: bytes) -> bytes:
    _require(type(body) is bytes and 0 < len(body) <= MAX_BODY, "invalid evidence length")
    return _hash(b"forecast-intake-v1:evidence:", body)


def gate_address(program: bytes, forecast: bytes) -> tuple[bytes, int]:
    return wire.find_program_address(_key(program, True), (GATE_SEED, _key(forecast, True)))


def receipt_address(program: bytes, forecast: bytes, epoch: int, user: bytes) -> tuple[bytes, int]:
    _require(_num(epoch) > 0, "invalid epoch")
    return wire.find_program_address(_key(program, True),
        (RECEIPT_SEED, _key(forecast, True), struct.pack("<Q", epoch), _key(user, True)))


def reviewer_address(program: bytes) -> tuple[bytes, int]:
    return wire.find_program_address(_key(program, True), (REVIEWER_SEED,))


@dataclass(frozen=True, slots=True)
class Reviewer:
    key: bytes
    revision: int

    def __post_init__(self) -> None:
        _key(self.key, True)
        _require(_num(self.revision) > 0, "invalid reviewer revision")

    def encode(self) -> bytes:
        return struct.pack("<8s32sQ", b"FNJUDG01", self.key, self.revision)

    @classmethod
    def decode(cls, raw: bytes) -> Reviewer:
        magic, key, revision = struct.unpack("<8s32sQ", _bytes(raw, REVIEWER_SIZE))
        _require(magic == b"FNJUDG01", "invalid reviewer discriminator")
        return cls(key, revision)

    def commitment(self) -> bytes:
        return _hash(b"forecast-intake-v1:reviewer:", self.encode())


@dataclass(frozen=True, slots=True)
class Accumulator:
    forecast: bytes
    specification: bytes
    epoch: int
    revision: int
    proposal_revision: int
    resolution: bytes
    proposal_event: bytes
    opened: int
    deadline: int
    pending: int
    material: int
    accepted: int
    head: bytes
    phase: int
    sealed_revision: int = 0
    sealed_event: bytes = ZERO
    sealed_snapshot: bytes = ZERO
    sealed_payload: bytes = ZERO
    sealed_at: int = 0
    sealed_slot: int = 0

    def __post_init__(self) -> None:
        for key in (self.forecast, self.specification, self.head):
            _key(key, True)
        for key in (self.resolution, self.proposal_event, self.sealed_event, self.sealed_snapshot, self.sealed_payload):
            _key(key)
        for n in (self.epoch, self.revision, self.proposal_revision, self.opened, self.deadline,
                  self.pending, self.material, self.accepted, self.sealed_revision, self.sealed_at, self.sealed_slot):
            _num(n)
        _num(self.phase, 3)
        _require(self.revision > 0 and self.pending + self.material <= self.accepted, "invalid accumulator counters")
        if self.phase == 0:
            _require((self.epoch, self.proposal_revision, self.opened, self.deadline, self.accepted) == (0, 0, 0, 0, 0)
                     and self.resolution == self.proposal_event == ZERO, "invalid dormant accumulator")
        else:
            _require(self.epoch > 0 and self.proposal_revision > 0 and self.resolution != ZERO
                     and self.proposal_event != ZERO and self.opened < self.deadline, "invalid epoch")
        if self.phase < 2:
            _require((self.sealed_revision, self.sealed_at, self.sealed_slot) == (0, 0, 0)
                     and self.sealed_event == self.sealed_snapshot == self.sealed_payload == ZERO, "premature seal")
        else:
            _require(self.pending == self.material == 0 and self.sealed_revision > self.proposal_revision
                     and self.sealed_event != ZERO and self.sealed_snapshot != ZERO and self.sealed_payload != ZERO
                     and self.sealed_at > self.deadline and self.sealed_slot > 0, "invalid seal")

    def encode(self) -> bytes:
        return _GATE.pack(b"FNINTK01", self.forecast, self.specification, GENESIS, self.epoch, self.revision,
            self.proposal_revision, self.resolution, self.proposal_event, self.opened, self.deadline,
            self.pending, self.material, self.accepted, self.head, self.phase, bytes(7), self.sealed_revision,
            self.sealed_event, self.sealed_snapshot, self.sealed_payload, self.sealed_at, self.sealed_slot)

    @classmethod
    def decode(cls, raw: bytes) -> Accumulator:
        v = _GATE.unpack(_bytes(raw, GATE_SIZE))
        _require(v[0] == b"FNINTK01" and v[3] == GENESIS and v[16] == bytes(7), "invalid gate scope/reserved")
        return cls(v[1], v[2], *v[4:16], *v[17:])

    def commitment(self) -> bytes:
        return _hash(b"forecast-intake-v1:accumulator:", self.encode())


@dataclass(frozen=True, slots=True)
class Receipt:
    forecast: bytes
    specification: bytes
    program: bytes
    epoch: int
    proposal_revision: int
    resolution: bytes
    proposal_event: bytes
    user: bytes
    nonce: bytes
    evidence: bytes
    body_length: int
    body: bytes
    status: int = 0
    accepted_at: int = 0
    accepted_slot: int = 0
    deadline: int = 0
    review: bytes = ZERO
    reviewer: bytes = ZERO
    reviewed_at: int = 0

    def __post_init__(self) -> None:
        for key in (self.forecast, self.specification, self.program, self.resolution, self.proposal_event,
                    self.user, self.nonce, self.evidence):
            _key(key, True)
        _key(self.review)
        _key(self.reviewer)
        for n in (self.epoch, self.proposal_revision, self.accepted_at, self.accepted_slot, self.deadline, self.reviewed_at):
            _num(n)
        _num(self.status, 3)
        _require(self.epoch > 0 and self.proposal_revision > 0 and 0 < _num(self.body_length, MAX_BODY)
                 and type(self.body) is bytes and len(self.body) <= self.body_length, "invalid receipt body/scope")
        if self.status == 0:
            _require(self.accepted_at == self.accepted_slot == self.deadline == 0, "unsubmitted receipt time")
        else:
            _require(len(self.body) == self.body_length and evidence_hash(self.body) == self.evidence
                     and self.accepted_slot > 0 and self.accepted_at <= self.deadline, "invalid accepted receipt")
        if self.status < 2:
            _require(self.review == self.reviewer == ZERO and self.reviewed_at == 0, "premature review")
        else:
            _require(self.review != ZERO and self.reviewer != ZERO and self.reviewed_at >= self.accepted_at,
                     "invalid review")

    def encode(self) -> bytes:
        return _RECEIPT.pack(b"FNDRCP01", self.forecast, self.specification, GENESIS, self.program,
            self.epoch, self.proposal_revision, self.resolution, self.proposal_event, self.user, self.nonce,
            self.evidence, self.body_length, len(self.body), self.status, bytes(7), self.accepted_at,
            self.accepted_slot, self.deadline, self.review, self.reviewer, self.reviewed_at) + self.body

    @classmethod
    def decode(cls, raw: bytes) -> Receipt:
        _require(type(raw) is bytes and RECEIPT_HEADER <= len(raw) <= RECEIPT_HEADER + MAX_BODY, "receipt length")
        v = _RECEIPT.unpack(raw[:RECEIPT_HEADER])
        _require(v[0] == b"FNDRCP01" and v[3] == GENESIS and v[15] == bytes(7)
                 and v[13] == len(raw) - RECEIPT_HEADER, "invalid receipt scope/length/reserved")
        return cls(v[1], v[2], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[11], v[12],
                   raw[RECEIPT_HEADER:], v[14], v[16], v[17], v[18], v[19], v[20], v[21])

    def commitment(self) -> bytes:
        return _hash(b"forecast-intake-v1:receipt:", self.encode())


def encode_reviewer(key: bytes, previous: Reviewer | None = None) -> bytes:
    return b"\x0e" + struct.pack("<Q", previous.revision if previous else 0) + (
        previous.commitment() if previous else ZERO) + _key(key, True)


def encode_activate(forecast: wire.ForecastAccount) -> bytes:
    return b"\x06" + struct.pack("<Q", forecast.revision) + forecast.event_hash + forecast.specification_hash


def _advance(data: bytes) -> bytes:
    _bytes(data, 255)
    _require(data[0] == 2, "expected original Advance encoding")
    return data[1:]


def encode_advance(data: bytes, *, mode: int = 0, artifact: bytes = ZERO, head: bytes = ZERO) -> bytes:
    _num(mode, 2)
    body = _advance(data)
    _require(body[112] != 10, "finalization requires a seal")
    suffix = b""
    if mode == 1:
        suffix = _key(artifact, True) + _key(head, True)
    elif mode == 2:
        suffix = _key(artifact, True)
    else:
        _require(artifact == head == ZERO, "unexpected advance extension")
    return bytes((7, mode)) + body + suffix


def encode_draft(gate: Accumulator, nonce: bytes, body: bytes) -> bytes:
    _require(gate.phase == 1, "epoch not open")
    return (b"\x08" + struct.pack("<QQ", gate.epoch, gate.proposal_revision) + gate.specification
            + gate.resolution + gate.proposal_event + _key(nonce, True) + evidence_hash(body)
            + struct.pack("<I", len(body)))


def encode_append(offset: int, chunk: bytes) -> bytes:
    _num(offset, MAX_BODY)
    _require(type(chunk) is bytes and 0 < len(chunk) <= CHUNK and offset + len(chunk) <= MAX_BODY, "invalid chunk")
    return b"\x09" + struct.pack("<IH", offset, len(chunk)) + chunk


def encode_submit(receipt: Receipt) -> bytes:
    _require(receipt.status == 0 and len(receipt.body) == receipt.body_length
             and evidence_hash(receipt.body) == receipt.evidence, "incomplete or already accepted receipt")
    return b"\x0a" + struct.pack("<Q", receipt.epoch) + receipt.nonce + receipt.evidence


def encode_review(receipt: Receipt, artifact: bytes, disposition: int) -> bytes:
    _require(receipt.status == 1 and type(disposition) is int and disposition in (2, 3), "invalid review")
    return b"\x0b" + struct.pack("<Q", receipt.epoch) + receipt.commitment() + _key(artifact, True) + bytes((disposition,))


def advance_hash(data: bytes) -> bytes:
    return _hash(b"forecast-intake-v1:advance:", _advance(data))


def encode_seal(gate: Accumulator, data: bytes) -> bytes:
    body = _advance(data)
    _require(gate.phase == 1 and gate.pending == gate.material == 0 and body[112] == 10, "cannot seal")
    return b"\x0c" + body + struct.pack("<Q", gate.revision) + gate.commitment()


def encode_finalize(gate: Accumulator, data: bytes) -> bytes:
    body = _advance(data)
    _require(gate.phase == 2 and gate.sealed_payload == advance_hash(data) and body[112] == 10
             and struct.unpack_from("<Q", body)[0] == gate.sealed_revision
             and body[48:80] == gate.sealed_event and body[80:112] == gate.sealed_snapshot, "candidate differs from seal")
    return b"\x0d" + body + gate.commitment()


def decode_instruction(data: bytes) -> dict[str, Any]:
    """Strict structural ABI decoder; state-dependent authorization is native."""
    _require(type(data) is bytes and len(data) > 0, "missing instruction")
    tag = data[0]
    fixed = {6: 73, 8: 181, 10: 73, 11: 74, 12: 295, 13: 287, 14: 73}
    if tag in fixed:
        _bytes(data, fixed[tag])
    elif tag == 7:
        _require(len(data) > 1 and data[1] in (0, 1, 2), "unknown advance mode")
        _bytes(data, {0: 256, 1: 320, 2: 288}[data[1]])
    elif tag == 9:
        _require(len(data) >= 8, "empty evidence chunk")
        offset, size = struct.unpack_from("<IH", data, 1)
        _require(encode_append(offset, data[7:]) == data and size == len(data)-7, "invalid append payload")
        return {"tag": tag, "offset": offset, "chunk": data[7:]}
    else:
        raise ValueError("unsupported intake opcode")
    if tag in (7, 12, 13):
        start = 2 if tag == 7 else 1
        body = data[start:start+254]
        revision, occurred = struct.unpack_from("<Qq", body)
        deadline, pending, material = struct.unpack_from("<qHH", body, 242)
        fields: dict[str, Any] = {"revision": revision, "occurred_at_ms": occurred,
                  "previous_event_hash": body[16:48], "event_hash": body[48:80],
                  "snapshot_hash": body[80:112], "state": body[112], "outcome": body[113],
                  "resolution_hash": body[114:146], "dispute_hash": body[146:178],
                  "reputation_hash": body[178:210], "trigger_hash": body[210:242],
                  "challenge_until_ms": deadline, "pending_disputes": pending, "material_disputes": material}
        _require(wire.encode_advance(**fields) == b"\x02"+body, "noncanonical advance")
        _require((fields["state"] == 10) == (tag in (12, 13)), "wrong finalization opcode")
        if tag == 12:
            _num(struct.unpack_from("<Q", data, 255)[0])
        return {"tag": tag, "mode": data[1] if tag == 7 else None, "advance": fields,
                "extension": data[start+254:]}
    _num(struct.unpack_from("<Q", data, 1)[0])
    if tag == 8:
        _num(struct.unpack_from("<Q", data, 9)[0])
        size = struct.unpack_from("<I", data, 177)[0]
        _require(0 < size <= MAX_BODY, "invalid declared evidence length")
    if tag == 11:
        _require(data[73] in (2, 3), "invalid review disposition")
    return {"tag": tag, "payload": data[1:]}


def instruction(program: bytes, data: bytes, *, actor: bytes, forecast: bytes = ZERO,
                receipt: bytes = ZERO, adjudicator: bytes = ZERO) -> wire.Instruction:
    """Exact metas; caller includes heap_frame() once per transaction."""
    _key(program, True)
    _key(actor, True)
    decode_instruction(data)
    tag = data[0]
    conf = wire.config_address(program)[0]
    judge = reviewer_address(program)[0]
    g = gate_address(program, forecast)[0] if forecast != ZERO else ZERO
    def meta(key: bytes, signer: bool = False, writable: bool = False) -> wire.AccountMeta:
        return wire.AccountMeta(_key(key), signer, writable)
    metas: tuple[wire.AccountMeta, ...]
    if tag == 14:
        metas = (meta(actor, True, True), meta(conf), meta(judge, writable=True), meta(SYSTEM))
    elif tag == 6:
        metas = (meta(actor, True, True), meta(conf), meta(forecast), meta(g, writable=True), meta(SYSTEM))
    elif tag == 7:
        _require(len(data) >= 2, "missing advance mode")
        metas = (meta(actor, True), meta(conf), meta(forecast, writable=True), meta(g, writable=True))
        if data[1] == 1:
            metas += (meta(_key(adjudicator, True), True), meta(judge))
        elif data[1] == 2:
            metas += (meta(_key(receipt, True)),)
    elif tag == 8:
        metas = (meta(actor, True, True), meta(forecast), meta(g), meta(_key(receipt, True), writable=True), meta(SYSTEM))
    elif tag == 9:
        metas = (meta(actor, True, True), meta(_key(receipt, True), writable=True), meta(SYSTEM))
    elif tag == 10:
        metas = (meta(actor, True), meta(forecast), meta(g, writable=True), meta(_key(receipt, True), writable=True))
    elif tag == 11:
        metas = (meta(actor, True), meta(conf), meta(judge), meta(forecast), meta(g, writable=True),
                 meta(_key(receipt, True), writable=True))
    elif tag in (12, 13):
        metas = (meta(actor, True), meta(conf), meta(forecast, writable=True), meta(g, writable=True))
    else:
        raise ValueError("unsupported intake opcode")
    _require(len({m.pubkey for m in metas}) == len(metas), "aliased accounts")
    return wire.Instruction(program, metas, data)


def heap_frame() -> wire.Instruction:
    return wire.Instruction(COMPUTE_BUDGET, (), b"\x01" + struct.pack("<I", 262144))


def decode_evidence(body: bytes) -> dict[str, Any]:
    """Full semantic format belongs off chain; native inclusion is not validity."""
    evidence_hash(body)
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            _require(key not in result, "duplicate evidence field")
            result[key] = value
        return result
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs)
        _require(type(value) is dict and set(value) == {"version", "claim", "rule_clause_id", "explanation", "sources"}
                 and value["version"] == "forecast-dispute-evidence-v1", "invalid evidence schema")
        for name, limit in (("claim", 1000), ("rule_clause_id", 128), ("explanation", 3000)):
            _require(type(value[name]) is str and 0 < len(value[name]) <= limit, "invalid evidence text")
        _require(type(value["sources"]) is list and 1 <= len(value["sources"]) <= 8, "invalid evidence sources")
        for source in value["sources"]:
            _require(type(source) is dict and set(source) == {"url", "body_base64", "sha256", "captured_at_ms"}, "invalid source")
            _require(type(source["url"]) is str and source["url"].startswith("https://") and len(source["url"]) <= 2000, "invalid source URL")
            parsed = urlsplit(source["url"])
            _require(parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None
                     and parsed.password is None and not parsed.fragment, "invalid source URL")
            _num(source["captured_at_ms"])
            _require(type(source["body_base64"]) is str, "invalid retained source")
            raw = base64.b64decode(source["body_base64"], validate=True)
            _require(len(raw) > 0 and base64.b64encode(raw).decode() == source["body_base64"]
                     and hashlib.sha256(raw).hexdigest() == source["sha256"], "source hash mismatch")
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        _require(canonical == body, "noncanonical evidence JSON")
        return cast(dict[str, Any], value)
    except (UnicodeError, TypeError, RecursionError) as exc:
        raise ValueError("invalid evidence encoding") from exc
