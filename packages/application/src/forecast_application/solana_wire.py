"""Dependency-free registry ABI and Solana legacy transaction wire format.

This module performs no I/O or signing. Account decoding checks data semantics;
callers must separately verify RPC cluster, program ownership, PDA and finality.
Assembly checks signature framing, not cryptographic signature validity.
"""

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ZERO = bytes(32)
MAX_INTEGER = 9_007_199_254_740_991
MAX_TRANSACTION_BYTES = 1232
_P = 2**255 - 19
_D = (-121665 * pow(121666, -1, _P)) % _P


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _raw(value: bytes, size: int, name: str, *, nonzero: bool = False) -> bytes:
    _require(type(value) is bytes and len(value) == size, f"{name} must be {size} bytes")
    _require(not nonzero or any(value), f"{name} must not be zero")
    return value


def _integer(value: int, low: int = 0, high: int = MAX_INTEGER) -> int:
    _require(type(value) is int and low <= value <= high, "integer outside allowed range")
    return value


def base58_encode(value: bytes) -> str:
    _require(type(value) is bytes, "base58 input must be bytes")
    number = int.from_bytes(value, "big")
    result = ""
    while number:
        number, digit = divmod(number, 58)
        result = _ALPHABET[digit] + result
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + result


def base58_decode(value: str, *, length: int) -> bytes:
    _integer(length, 1, 1232)
    _require(type(value) is str and 0 < len(value) <= length * 2, "invalid base58 length")
    number = 0
    for character in value:
        digit = _ALPHABET.find(character)
        _require(digit >= 0, "invalid base58 character")
        number = number * 58 + digit
    raw = b"\0" * (len(value) - len(value.lstrip("1")))
    raw += number.to_bytes((number.bit_length() + 7) // 8, "big")
    return _raw(raw, length, "decoded base58")


def is_edwards_point(encoded: bytes) -> bool:
    """Match curve25519-dalek decompression, not strict Ed25519 verification.

    Small-order points and noncanonical field encodings are decompressible too.
    Treating them as off-curve would produce a different PDA than Solana.
    """
    _raw(encoded, 32, "compressed point")
    y = (int.from_bytes(encoded, "little") & ((1 << 255) - 1)) % _P
    yy = y * y % _P
    numerator, denominator = (yy - 1) % _P, (_D * yy + 1) % _P
    if denominator == 0:
        return False
    square = numerator * pow(denominator, -1, _P) % _P
    return square == 0 or pow(square, (_P - 1) // 2, _P) == 1


def find_program_address(program_id: bytes, seeds: tuple[bytes, ...]) -> tuple[bytes, int]:
    _raw(program_id, 32, "program ID")
    _require(type(seeds) is tuple and len(seeds) <= 15, "PDA permits at most 15 seeds plus bump")
    for seed in seeds:
        _require(type(seed) is bytes and len(seed) <= 32, "PDA seed exceeds 32 bytes")
    for bump in range(255, -1, -1):
        digest = sha256(b"".join(seeds) + bytes([bump]) + program_id
                        + b"ProgramDerivedAddress").digest()
        if not is_edwards_point(digest):
            return digest, bump
    raise ValueError("no viable PDA bump")


def config_address(program_id: bytes) -> tuple[bytes, int]:
    return find_program_address(program_id, (b"config",))


def forecast_address(program_id: bytes, forecast_id_hash: bytes) -> tuple[bytes, int]:
    return find_program_address(program_id, (b"forecast", _raw(
        forecast_id_hash, 32, "forecast identity", nonzero=True)))


def encode_initialize(relayer: bytes) -> bytes:
    return b"\0" + _raw(relayer, 32, "relayer", nonzero=True)


def encode_set_relayer(relayer: bytes) -> bytes:
    return b"\3" + _raw(relayer, 32, "relayer", nonzero=True)


def encode_propose_admin(administrator: bytes) -> bytes:
    return b"\4" + _raw(administrator, 32, "administrator", nonzero=True)


def encode_accept_admin() -> bytes:
    return b"\5"


def encode_register(*, forecast_id_hash: bytes, creator_hash: bytes, specification_hash: bytes,
                    open_at_ms: int, close_at_ms: int, revision: int, occurred_at_ms: int,
                    event_hash: bytes, snapshot_hash: bytes) -> bytes:
    hashes = [forecast_id_hash, creator_hash, specification_hash, event_hash, snapshot_hash]
    for value in hashes:
        _raw(value, 32, "publication commitment", nonzero=True)
    for timestamp in (open_at_ms, close_at_ms, occurred_at_ms):
        _integer(timestamp)
    _integer(revision, 1)
    _require(open_at_ms < close_at_ms and occurred_at_ms < close_at_ms,
             "publication must precede close")
    return b"\1" + b"".join(hashes[:3]) + struct.pack(
        "<qqQq", open_at_ms, close_at_ms, revision, occurred_at_ms) + b"".join(hashes[3:])


def encode_advance(*, revision: int, occurred_at_ms: int, previous_event_hash: bytes,
                   event_hash: bytes, snapshot_hash: bytes, state: int, outcome: int,
                   resolution_hash: bytes = _ZERO, dispute_hash: bytes = _ZERO,
                   reputation_hash: bytes = _ZERO, trigger_hash: bytes = _ZERO,
                   challenge_until_ms: int = 0, pending_disputes: int = 0,
                   material_disputes: int = 0) -> bytes:
    _integer(revision, 1)
    _integer(occurred_at_ms)
    _integer(challenge_until_ms)
    _integer(state, 2, 11)
    _integer(outcome, 0, 3)
    _integer(pending_disputes, 0, 256)
    _integer(material_disputes, 0, 256)
    _require(pending_disputes + material_disputes <= 256, "too many disputes")
    for value in (previous_event_hash, event_hash, snapshot_hash):
        _raw(value, 32, "event commitment", nonzero=True)
    _require(previous_event_hash != event_hash, "new event must differ from predecessor")
    for value in (resolution_hash, dispute_hash, reputation_hash, trigger_hash):
        _raw(value, 32, "optional commitment")
    _require(not (pending_disputes or material_disputes) or dispute_hash != _ZERO,
             "dispute commitment required")
    return (b"\2" + struct.pack("<Qq", revision, occurred_at_ms)
            + previous_event_hash + event_hash + snapshot_hash + bytes([state, outcome])
            + resolution_hash + dispute_hash + reputation_hash + trigger_hash
            + struct.pack("<qHH", challenge_until_ms, pending_disputes, material_disputes))


@dataclass(frozen=True, slots=True)
class ConfigAccount:
    administrator: bytes
    relayer: bytes
    pending_administrator: bytes


def decode_config(data: bytes) -> ConfigAccount:
    _raw(data, 104, "config account")
    _require(data[:8] == b"FNCONF01", "invalid config discriminator")
    admin, relayer, pending = data[8:40], data[40:72], data[72:104]
    _require(admin != _ZERO and relayer != _ZERO and admin != relayer,
             "invalid authority separation")
    _require(pending == _ZERO or pending not in (admin, relayer), "invalid pending administrator")
    return ConfigAccount(admin, relayer, pending)


@dataclass(frozen=True, slots=True)
class ForecastAccount:
    forecast_id_hash: bytes
    creator_hash: bytes
    specification_hash: bytes
    open_at_ms: int
    close_at_ms: int
    revision: int
    occurred_at_ms: int
    state: int
    outcome: int
    paused_from: int
    event_hash: bytes
    snapshot_hash: bytes
    resolution_hash: bytes
    dispute_hash: bytes
    reputation_hash: bytes
    trigger_hash: bytes
    challenge_until_ms: int
    chain_finalize_not_before_ms: int
    paused_at_ms: int
    pending_disputes: int
    material_disputes: int


def decode_forecast(data: bytes) -> ForecastAccount:
    _raw(data, 360, "forecast account")
    _require(data[:8] == b"FNFORE01" and data[139] == 0, "invalid forecast discriminator/reserved")
    open_at, close_at, revision, occurred_at = struct.unpack_from("<qqQq", data, 104)
    challenge, chain_delay, paused_at, pending, material = struct.unpack_from("<qqqHH", data, 332)
    f = ForecastAccount(
        data[8:40], data[40:72], data[72:104], open_at, close_at, revision, occurred_at,
        data[136], data[137], data[138], data[140:172], data[172:204], data[204:236],
        data[236:268], data[268:300], data[300:332], challenge, chain_delay, paused_at,
        pending, material)
    for value in (f.forecast_id_hash, f.creator_hash, f.specification_hash,
                  f.event_hash, f.snapshot_hash):
        _raw(value, 32, "required account commitment", nonzero=True)
    for time in (f.open_at_ms, f.close_at_ms, f.occurred_at_ms, f.challenge_until_ms,
                 f.chain_finalize_not_before_ms, f.paused_at_ms):
        _integer(time)
    _integer(f.revision, 1)
    _integer(f.state, 2, 11)
    _require(f.open_at_ms < f.close_at_ms, "invalid publication window")
    effective = f.paused_from if f.state == 9 else f.state
    _require((f.state == 9 and 4 <= f.paused_from <= 8 and f.paused_at_ms > 0)
             or (f.state != 9 and f.paused_from == 0 and f.paused_at_ms == 0),
             "invalid pause state")
    resolved = effective >= 5
    _require((f.resolution_hash != _ZERO and 1 <= f.outcome <= 3
              and f.chain_finalize_not_before_ms > 0) if resolved else (
                  f.resolution_hash == _ZERO and f.outcome == 0
                  and f.chain_finalize_not_before_ms == 0), "invalid resolution state")
    _require(f.challenge_until_ms > 0 if effective >= 6 else (
        f.challenge_until_ms == 0 and f.pending_disputes == 0 and f.material_disputes == 0),
        "invalid challenge state")
    _require(f.pending_disputes + f.material_disputes <= 256, "too many disputes")
    _require(f.chain_finalize_not_before_ms >= f.challenge_until_ms
             and (effective not in (10, 11) or f.occurred_at_ms >= f.challenge_until_ms),
             "invalid finalization time")
    _require(effective not in (6, 10, 11) or (
        f.pending_disputes == 0 and f.material_disputes == 0), "unresolved disputes")
    _require(effective != 8 or (f.pending_disputes == 0 and f.material_disputes > 0),
             "invalid escalation")
    _require(not (f.pending_disputes or f.material_disputes) or f.dispute_hash != _ZERO,
             "missing dispute commitment")
    _require(effective >= 10 or f.reputation_hash == _ZERO, "premature reputation commitment")
    _require(f.state != 2 or (f.trigger_hash == _ZERO and f.occurred_at_ms < f.close_at_ms),
             "invalid open state")
    _require(f.state == 2 or f.occurred_at_ms >= f.close_at_ms or f.trigger_hash != _ZERO,
             "missing early trigger")
    _require(f.occurred_at_ms >= f.close_at_ms or not resolved or f.outcome == 1,
             "early outcome must be YES")
    return f


@dataclass(frozen=True, slots=True)
class AccountMeta:
    pubkey: bytes
    is_signer: bool = False
    is_writable: bool = False

    def __post_init__(self) -> None:
        _raw(self.pubkey, 32, "account public key")
        _require(type(self.is_signer) is bool and type(self.is_writable) is bool,
                 "account privileges must be boolean")


@dataclass(frozen=True, slots=True)
class Instruction:
    program_id: bytes
    accounts: tuple[AccountMeta, ...]
    data: bytes

    def __post_init__(self) -> None:
        _raw(self.program_id, 32, "instruction program")
        _require(type(self.accounts) is tuple and len(self.accounts) <= 256
                 and all(type(a) is AccountMeta for a in self.accounts), "invalid account metas")
        _require(type(self.data) is bytes and len(self.data) <= MAX_TRANSACTION_BYTES,
                 "invalid instruction data")


@dataclass(frozen=True, slots=True)
class CompiledMessage:
    data: bytes
    signer_keys: tuple[bytes, ...]
    account_keys: tuple[bytes, ...]


def shortvec(value: int) -> bytes:
    """Encode canonical Solana compact-u16; reject truncation and bool integers."""
    _integer(value, 0, 65535)
    encoded = bytearray()
    while True:
        digit = value & 127
        value >>= 7
        encoded.append(digit | (128 if value else 0))
        if not value:
            return bytes(encoded)


def compile_message(payer: bytes, blockhash: bytes,
                    instructions: tuple[Instruction, ...]) -> CompiledMessage:
    _raw(payer, 32, "fee payer", nonzero=True)
    _raw(blockhash, 32, "blockhash", nonzero=True)
    _require(type(instructions) is tuple and 0 < len(instructions) <= 256
             and all(type(i) is Instruction for i in instructions), "invalid instructions")
    privileges = {payer: (True, True)}
    for instruction in instructions:
        for account in instruction.accounts:
            signer, writable = privileges.get(account.pubkey, (False, False))
            privileges[account.pubkey] = (signer or account.is_signer,
                                         writable or account.is_writable)
        privileges.setdefault(instruction.program_id, (False, False))
    _require(len(privileges) <= 256, "legacy transaction supports at most 256 accounts")
    # BTreeMap order matches the official Rust SDK: raw public-key lexical order
    # within each privilege class, with the writable fee payer first.
    ordered = sorted((key for key in privileges if key != payer),
                     key=lambda key: (not privileges[key][0], not privileges[key][1], key))
    keys = (payer, *ordered)
    signers = tuple(key for key in keys if privileges[key][0])
    readonly_signed = sum(s and not w for s, w in privileges.values())
    readonly_unsigned = sum(not s and not w for s, w in privileges.values())
    _require(len(signers) < 128 and readonly_unsigned <= 255, "legacy header exceeds bounds")
    data = bytes([len(signers), readonly_signed, readonly_unsigned])
    data += shortvec(len(keys)) + b"".join(keys) + blockhash + shortvec(len(instructions))
    indexes = {key: index for index, key in enumerate(keys)}
    for instruction in instructions:
        data += bytes([indexes[instruction.program_id]]) + shortvec(len(instruction.accounts))
        data += bytes(indexes[account.pubkey] for account in instruction.accounts)
        data += shortvec(len(instruction.data)) + instruction.data
    _require(len(shortvec(len(signers))) + 64 * len(signers) + len(data)
             <= MAX_TRANSACTION_BYTES, "transaction exceeds Solana packet size")
    return CompiledMessage(data, signers, keys)


def assemble_transaction(message: CompiledMessage, signatures: Mapping[bytes, bytes]) -> bytes:
    _require(type(message) is CompiledMessage and isinstance(signatures, Mapping),
             "invalid transaction assembly arguments")
    _require(type(message.data) is bytes and len(message.data) >= 3
             and type(message.signer_keys) is tuple and type(message.account_keys) is tuple,
             "invalid compiled message")
    _require(0 < len(message.signer_keys) < 128
             and message.data[0] == len(message.signer_keys)
             and len(set(message.signer_keys)) == len(message.signer_keys)
             and message.account_keys[:len(message.signer_keys)] == message.signer_keys,
             "invalid signer ordering")
    for key in message.account_keys:
        _raw(key, 32, "message account")
    encoded_keys = shortvec(len(message.account_keys)) + b"".join(message.account_keys)
    _require(message.data[3:3 + len(encoded_keys)] == encoded_keys,
             "compiled message account metadata mismatch")
    _require(set(signatures) == set(message.signer_keys), "missing or extraneous signature")
    wire = shortvec(len(message.signer_keys)) + b"".join(
        _raw(signatures[key], 64, "signature", nonzero=True) for key in message.signer_keys)
    wire += message.data
    _require(len(wire) <= MAX_TRANSACTION_BYTES, "transaction exceeds Solana packet size")
    return wire
