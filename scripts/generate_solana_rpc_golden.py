#!/usr/bin/env python3
"""Export the fail-closed Solana RPC transport, so a Rust port can be held to it.

This is the only code in the port that spends money and the only code that writes to a chain, so
every method is a sequence of refusals and the *order* of the sequence is the safety property.
Nothing is signed until the program is known to be executable, the relayer's authority has been
read out of the program's own configuration, the fee and rent are inside their caps, the balance
covers them plus its floor, and the blockhash is still valid — and then the blockhash is checked
*again* after simulation, because a simulated transaction is not a landed one.

Every case records the RPC calls it made, with the reply each one got, and the port replays from
that log. A vector that recorded only the outcome would let a port reach the same refusal through a
different sequence of checks, which is exactly the property being ported.

The cases walk the order rather than sampling it: a publication that succeeds, and then one
refusal at each gate in turn — the pinned genesis, the program's executability, the configuration's
existence, the relayer's authority, the target's shape, the advance's predecessor, the fee cap,
the rent cap, the balance floor, the blockhash, the simulation, the signature the node returns, and
the spend authorizer. Plus the account and status readers, where leniency is the failure mode.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.solana_rpc import SolanaRpcError, SolanaRpcTransport  # noqa: E402
from forecast_application.solana_wire import (  # noqa: E402
    base58_encode,
    encode_advance,
    encode_register,
    forecast_address,
)

from tests.test_solana_rpc import (  # noqa: E402
    ADMIN,
    CLOCK_ADDRESS,
    CLOCK_DATA,
    CLOCK_OWNER,
    CONFIG,
    CONFIG_DATA,
    CREATOR,
    EVENT,
    FORECAST_DATA,
    GENESIS,
    PROGRAM,
    PUBLICATION,
    RELAYER,
    SIGNATURE,
    SNAPSHOT,
    SPEC,
    TARGET,
    RpcFixture,
    account,
    context,
)

GOLDEN = ROOT / "tests/golden/solana-rpc-golden.json"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

ADVANCE = encode_advance(
    revision=4, occurred_at_ms=2000, previous_event_hash=EVENT, event_hash=bytes([7]) * 32,
    snapshot_hash=bytes([8]) * 32, state=2, outcome=0, resolution_hash=bytes(32),
    dispute_hash=bytes(32), reputation_hash=bytes(32), trigger_hash=bytes(32),
    challenge_until_ms=0, pending_disputes=0, material_disputes=0)


class Spy:
    """The transport's four entry points, with their arguments written down.

    A spy rather than per-case bookkeeping: every case would otherwise state its own inputs, and a
    case that stated them wrongly would disagree with the call it just made.
    """

    def __init__(self, transport, entry: dict) -> None:
        self.transport = transport
        self.entry = entry

    async def send(self, instruction, target, *, register):
        self.entry["input"] = {"kind": "send", "instruction": b64(instruction),
                               "target": base58_encode(target), "register": register}
        return await self.transport.send(instruction, target, register=register)

    async def account(self, address):
        self.entry["input"] = {"kind": "account", "address": base58_encode(address)}
        return await self.transport.account(address)

    async def finalized_time_ms(self):
        self.entry["input"] = {"kind": "finalized_time_ms"}
        return await self.transport.finalized_time_ms()

    async def signature_finalized(self, signature):
        self.entry["input"] = {"kind": "signature_finalized", "signature": str(signature)}
        return await self.transport.signature_finalized(signature)


class Recording(RpcFixture):
    """The reference's own fixture, with the replies written down."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[dict] = []

    async def rpc(self, method, params):
        try:
            value = await super().rpc(method, params)
        except Exception as error:
            self.log.append({"method": method, "params": params, "errorType": type(error).__name__})
            raise
        self.log.append({"method": method, "params": params, "response": value})
        return value


def transport(fixture: Recording) -> SolanaRpcTransport:
    return SolanaRpcTransport(fixture.rpc, fixture.sign, program_id=PROGRAM, relayer=RELAYER,
                              expected_genesis_hash=GENESIS, authorize_spend=fixture.reserve)


async def record(cases: list[dict], name: str, setup, call, **inputs) -> None:
    """One case, with the arguments it was called with.

    The inputs travel with the outcome: a replay that had to *reconstruct* an instruction from the
    case's name would be testing its own reconstruction.
    """
    fixture = Recording()
    setup(fixture)
    entry: dict = {"call": name, "input": inputs}
    try:
        entry["result"] = await call(Spy(transport(fixture), entry), fixture)
    except SolanaRpcError as error:
        entry["error"] = str(error)
    except ValueError as error:
        entry["error"] = str(error)
    entry["rpc"] = fixture.log
    entry["signed"] = [base64.b64encode(message).decode() for message in fixture.signed]
    # What the *signer returns* is part of the contract too: a zero signature is a slot nobody
    # signed, and the reference refuses it after the spend is authorized rather than before.
    entry["signature"] = base64.b64encode(fixture.signature).decode()
    entry["spend"] = fixture.reservations
    cases.append(entry)


def overridden(**values):
    def setup(fixture: Recording) -> None:
        fixture.overrides.update(values)
    return setup


def accounts(**values):
    def setup(fixture: Recording) -> None:
        fixture.account_overrides.update(values)
    return setup


def nothing(fixture: Recording) -> None:
    return None


async def build() -> dict:
    cases: list[dict] = []
    target_key, program_key, config_key, clock_key = (
        base58_encode(TARGET), base58_encode(PROGRAM), base58_encode(CONFIG), CLOCK_ADDRESS)

    # --- send: the path that succeeds, and then one refusal at each gate.
    await record(cases, "send:register", nothing, lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    # The advance needs a target whose revision and event hash the new instruction extends: the
    # predecessor check is what makes an advance an extension rather than a fork.
    advance_target = account(FORECAST_DATA)
    await record(cases, "send:advance", accounts(**{target_key: advance_target}),
                 lambda t, f: t.send(ADVANCE, TARGET, register=False))
    await record(cases, "send:wrong-pda", nothing,
                 lambda t, f: t.send(encode_register(
                     forecast_id_hash=bytes([99]) * 32, creator_hash=CREATOR, specification_hash=SPEC,
                     open_at_ms=1000, close_at_ms=5000, revision=3, occurred_at_ms=1000,
                     event_hash=EVENT, snapshot_hash=SNAPSHOT), TARGET, register=True))
    # Noncanonicality has to be *constructed*: the register layout is fixed, so a flipped byte
    # inside it is a different valid instruction rather than a noncanonical encoding of this one.
    await record(cases, "send:truncated", nothing,
                 lambda t, f: t.send(PUBLICATION[:-1], TARGET, register=True))
    await record(cases, "send:genesis-mismatch", overridden(getGenesisHash=base58_encode(bytes([1]) * 32)),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:program-not-executable",
                 accounts(**{program_key: {"executable": False, "owner": "BPFLoaderUpgradeab1e11111111111111111111111"}}),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:config-missing", accounts(**{config_key: None}),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:relayer-not-authorized",
                 accounts(**{config_key: account(b"FNCONF01" + ADMIN + bytes([99]) * 32 + bytes(32))}),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:target-exists-on-register", accounts(**{target_key: account(FORECAST_DATA)}),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:advance-does-not-extend", accounts(**{target_key: account(FORECAST_DATA)}),
                 lambda t, f: t.send(encode_advance(
                     revision=3, occurred_at_ms=2000, previous_event_hash=bytes([9]) * 32,
                     event_hash=bytes([7]) * 32, snapshot_hash=bytes([8]) * 32, state=2, outcome=0,
                     resolution_hash=bytes(32), dispute_hash=bytes(32), reputation_hash=bytes(32),
                     trigger_hash=bytes(32), challenge_until_ms=0, pending_disputes=0,
                     material_disputes=0), TARGET, register=False))
    await record(cases, "send:fee-over-cap", overridden(getFeeForMessage=context(20_001)),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:rent-over-cap", overridden(getMinimumBalanceForRentExemption=10_000_001),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:balance-below-floor", overridden(getBalance=context(10_000_000)),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:blockhash-invalid", overridden(isBlockhashValid=context(False)),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:block-height-expired", overridden(getBlockHeight=901),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:simulation-failed",
                 overridden(simulateTransaction=context({"err": {"InstructionError": [0, "Custom"]}})),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:signature-mismatch", overridden(sendTransaction=base58_encode(bytes([7]) * 64)),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:zero-signature", accounts(), lambda t, f: _zero_signature(t, f))
    await record(cases, "send:spend-rejected", lambda f: setattr(f, "reject_spend", True),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))
    await record(cases, "send:rpc-raises", overridden(getGenesisHash=RuntimeError("provider token leaked")),
                 lambda t, f: t.send(PUBLICATION, TARGET, register=True))

    # --- account: `None`, the prefunded System target, and the ways a reply can be wrong.
    await record(cases, "account:none", nothing, lambda t, f: _account(t, TARGET))
    await record(cases, "account:system-empty",
                 accounts(**{config_key: {"owner": base58_encode(bytes(32)), "lamports": 1,
                                          "executable": False, "data": ["", "base64"]}}),
                 lambda t, f: _account(t, CONFIG))
    await record(cases, "account:system-with-data",
                 accounts(**{config_key: {"owner": base58_encode(bytes(32)), "lamports": 1,
                                          "executable": False,
                                          "data": [base64.b64encode(b"x").decode(), "base64"]}}),
                 lambda t, f: _account(t, CONFIG))
    await record(cases, "account:wrong-owner",
                 accounts(**{config_key: {"owner": base58_encode(bytes([77]) * 32), "lamports": 1,
                                          "executable": False,
                                          "data": [base64.b64encode(CONFIG_DATA).decode(), "base64"]}}),
                 lambda t, f: _account(t, CONFIG))
    await record(cases, "account:executable",
                 accounts(**{config_key: {"owner": base58_encode(PROGRAM), "lamports": 1,
                                          "executable": True,
                                          "data": [base64.b64encode(CONFIG_DATA).decode(), "base64"]}}),
                 lambda t, f: _account(t, CONFIG))
    # A noncanonical encoding decodes to the right bytes by a different spelling: the reference
    # re-encodes and compares, so the *length* is not the check.
    noncanonical = base64.b64encode(CONFIG_DATA).decode().rstrip("=")
    noncanonical = noncanonical[:-1] + ("A" if noncanonical[-1] != "A" else "B")
    await record(cases, "account:noncanonical",
                 accounts(**{config_key: {"owner": base58_encode(PROGRAM), "lamports": 1,
                                          "executable": False, "data": [noncanonical, "base64"]}}),
                 lambda t, f: _account(t, CONFIG))
    # A forecast account whose own `forecast_id_hash` does not derive to the address it was read
    # from: the reply is well-formed and the *identity* is wrong.
    other = forecast_address(PROGRAM, bytes([42]) * 32)[0]
    await record(cases, "account:pda-mismatch",
                 accounts(**{base58_encode(other): account(FORECAST_DATA)}),
                 lambda t, f: _account(t, other))

    # --- finalized_time_ms: the sysvar the program itself reads.
    await record(cases, "clock:ok", nothing, lambda t, f: t.finalized_time_ms())
    await record(cases, "clock:wrong-owner",
                 accounts(**{clock_key: {**account(CLOCK_DATA), "owner": base58_encode(PROGRAM)}}),
                 lambda t, f: t.finalized_time_ms())
    await record(cases, "clock:unallocated",
                 accounts(**{clock_key: {**account(CLOCK_DATA), "owner": CLOCK_OWNER, "lamports": 0}}),
                 lambda t, f: t.finalized_time_ms())
    await record(cases, "clock:not-base64",
                 accounts(**{clock_key: {**account(CLOCK_DATA), "owner": CLOCK_OWNER,
                                         "data": ["x", "base64"]}}),
                 lambda t, f: t.finalized_time_ms())
    await record(cases, "clock:wrong-length",
                 accounts(**{clock_key: {**account(CLOCK_DATA), "owner": CLOCK_OWNER,
                                         "data": [base64.b64encode(bytes(39)).decode(), "base64"]}}),
                 lambda t, f: t.finalized_time_ms())
    await record(cases, "clock:slot-mismatch",
                 accounts(**{clock_key: {**account(CLOCK_DATA), "owner": CLOCK_OWNER,
                                         "data": [base64.b64encode(struct_pack_clock(999)).decode(), "base64"]}}),
                 lambda t, f: t.finalized_time_ms())
    await record(cases, "clock:missing-context",
                 overridden(getAccountInfo={"value": None}),
                 lambda t, f: t.finalized_time_ms())

    # --- signature_finalized: what counts as final, and what only looks like it.
    await record(cases, "status:absent", nothing, lambda t, f: t.signature_finalized(base58_encode(SIGNATURE)))
    await record(cases, "status:processed",
                 overridden(getSignatureStatuses=context([{"err": None, "slot": 123,
                                                           "confirmationStatus": "processed"}])),
                 lambda t, f: t.signature_finalized(base58_encode(SIGNATURE)))
    await record(cases, "status:finalized",
                 overridden(getSignatureStatuses=context([{"err": None, "slot": 123,
                                                           "confirmationStatus": "finalized",
                                                           "confirmations": None}])),
                 lambda t, f: t.signature_finalized(base58_encode(SIGNATURE)))
    await record(cases, "status:failed",
                 overridden(getSignatureStatuses=context([{"err": {"InstructionError": [0, "Custom"]},
                                                           "slot": 123, "confirmationStatus": "finalized"}])),
                 lambda t, f: t.signature_finalized(base58_encode(SIGNATURE)))
    await record(cases, "status:unknown-state",
                 overridden(getSignatureStatuses=context([{"err": None, "slot": 123,
                                                           "confirmationStatus": "pending"}])),
                 lambda t, f: t.signature_finalized(base58_encode(SIGNATURE)))
    await record(cases, "status:inconsistent",
                 overridden(getSignatureStatuses=context([{"err": None, "slot": 123,
                                                           "confirmationStatus": "finalized",
                                                           "confirmations": 4}])),
                 lambda t, f: t.signature_finalized(base58_encode(SIGNATURE)))
    await record(cases, "status:not-a-signature", nothing, lambda t, f: t.signature_finalized("too-short"))

    return {
        "description": "The fail-closed Solana RPC transport: the pinned genesis, the program's "
                       "executability, the relayer's authority, the caps, the balance floor, the "
                       "blockhash checked twice, and the two account readers.",
        "genesis": GENESIS,
        "publication": base64.b64encode(PUBLICATION).decode(),
        "advance": base64.b64encode(ADVANCE).decode(),
        "cases": cases,
    }


def struct_pack_clock(slot: int) -> bytes:
    import struct
    return struct.pack("<QqQQq", slot, 1_799_000_000, 4, 5, 1_800_000_000)


async def _zero_signature(transport_object, fixture: Recording):
    fixture.signature = bytes(64)
    return await transport_object.send(PUBLICATION, TARGET, register=True)


async def _account(transport_object, address: bytes):
    value = await transport_object.account(address)
    if value is None:
        return None
    return {"address": base58_encode(value.address), "owner": base58_encode(value.owner),
            "data": base64.b64encode(value.data).decode(), "slot": value.slot,
            "finalized": value.finalized}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(asyncio.run(build()), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if arguments.write:
        GOLDEN.write_text(document)
        print(f"wrote {GOLDEN.relative_to(ROOT)}")
        return 0
    if arguments.check:
        current = GOLDEN.read_text() if GOLDEN.exists() else ""
        if current != document:
            print(f"{GOLDEN.relative_to(ROOT)} is stale; regenerate with --write", file=sys.stderr)
            return 1
        print(f"{GOLDEN.relative_to(ROOT)} is current")
        return 0
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
