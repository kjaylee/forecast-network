#!/usr/bin/env python3
"""Export the Solana wire codecs, so a Rust port can be held to them byte for byte.

`solana_wire` and `dispute_wire` build the bytes that go on chain and read the bytes that
come back. A port that is one byte out does not produce a slightly different transaction:
it produces a transaction that fails signature verification, or worse, a *different valid
instruction* — an activation of the wrong forecast, a seal of the wrong event.

Three things here are not ordinary serialisation and are the reason this vector exists:

  * **The PDA bump search.** `find_program_address` walks the bump down from 255 and takes
    the first digest that is *not* a decompressible Edwards point. Python's check matches
    `curve25519-dalek` decompression rather than strict Ed25519 verification, so small-order
    points and noncanonical encodings count as on-curve — treating them otherwise finds a
    different address than Solana does.
  * **The reserved bytes and the genesis binding.** Every account carries `GENESIS` and a
    reserved run of seven NULs — the reference's `bytes(7)`, which is a count of zero bytes
    rather than a fill value. Reading it as seven 0x07 bytes produces an account that looks
    plausible and that no validator will accept.
  * **The seal chain.** A finalize is only accepted if the candidate advance equals the
    sealed payload hash, revision, event and snapshot, so the seal is a commitment to one
    specific candidate rather than to the act of sealing.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/application/src"), str(ROOT / "packages/domain/src"), str(ROOT)]

from forecast_application import dispute_wire as d  # noqa: E402
from forecast_application import solana_wire as w  # noqa: E402

GOLDEN = ROOT / "tests/golden/solana-wire-golden.json"

# The same fixture `tests/test_dispute_wire.py` uses, so the four commitments that repository
# already publishes are reproduced here rather than merely paralleled by a different fixture.
PROGRAM = bytes([10]) * 32
FORECAST = bytes([7]) * 32
SPECIFICATION = bytes([3]) * 32
RESOLUTION = bytes([6]) * 32
EVENT = bytes([8]) * 32
USER = bytes([11]) * 32
NONCE = bytes([12]) * 32
ARTIFACT = bytes([12]) * 32
HEAD = bytes([9]) * 32

# Published by `tests/test_dispute_wire.py::test_rust_python_golden_commitments`. They are the
# on-chain contract, so a port that disagrees with them is wrong regardless of what this
# vector says about the rest.
PINNED = {
    "gate": "27194d29a1041f5d1a7e4dec8b6001560b4f269fe5ee413dfc87e9964555ebed",
    "receipt": "bb91319fb3146d0653cf42b84726fa5c51c0b9aa1d30276b01e9c7e85e76db6d",
    "reviewer": "b633e1b6358a9eb49d9758b46e3da9fe2f92cfe1d4db0a400125d3745bd29e01",
    "advance": "cbc73db439e88aa792bee35cf094ec142913bbe37efbbba6ca120bb75a46e972",
}


def advance(*, state: int = 10, revision: int = 5, outcome: int = 1,
            occurred: int = 101, challenge: int = 100, pending: int = 0, material: int = 0) -> bytes:
    return w.encode_advance(revision=revision, occurred_at_ms=occurred, previous_event_hash=bytes([4]) * 32,
                            event_hash=bytes([13]) * 32, snapshot_hash=bytes([14]) * 32, state=state,
                            outcome=outcome, resolution_hash=RESOLUTION, reputation_hash=bytes([15]) * 32,
                            dispute_hash=bytes([16]) * 32 if (pending or material) else w._ZERO,
                            challenge_until_ms=challenge, pending_disputes=pending, material_disputes=material)


def draft_gate() -> d.Accumulator:
    """Phase 1: an epoch is open and accepting drafts."""
    return d.Accumulator(FORECAST, SPECIFICATION, 1, 1, 3, RESOLUTION, EVENT, 30, 100, 0, 0, 0, HEAD, 1)


def sealed_gate() -> d.Accumulator:
    """Phase 2: the epoch is sealed against one candidate."""
    body = advance(state=10)
    payload = d.advance_hash(body)
    return replace(draft_gate(), phase=2, sealed_revision=5, sealed_event=bytes([13]) * 32,
                   sealed_snapshot=bytes([14]) * 32, sealed_payload=payload, sealed_at=101, sealed_slot=42)


def receipt(body: bytes = b"*", *, status: int = 0) -> d.Receipt:
    base = d.Receipt(FORECAST, SPECIFICATION, PROGRAM, 1, 3, RESOLUTION, EVENT, USER, NONCE,
                     d.evidence_hash(body), len(body), body)
    if status == 0:
        return base
    accepted = replace(base, status=1, accepted_at=50, accepted_slot=9, deadline=100)
    if status == 1:
        return accepted
    return replace(accepted, status=2, review=bytes([17]) * 32, reviewer=bytes([11]) * 32, reviewed_at=60)


def hexed(value: bytes) -> str:
    return value.hex()


def case(name: str, call) -> dict:
    try:
        return {"name": name, "result": call()}
    except ValueError as error:
        return {"name": name, "error": str(error)}


def build() -> dict:
    body = b"forecast evidence body" * 4
    gates = {"dormant": d.Accumulator(FORECAST, SPECIFICATION, 0, 1, 0, w._ZERO, w._ZERO, 0, 0, 0, 0, 0, HEAD, 0),
             "open": draft_gate(), "sealed": sealed_gate()}
    receipts = {"draft": receipt(body), "accepted": receipt(body, status=1), "reviewed": receipt(body, status=2)}
    reviewer = d.Reviewer(bytes([11]) * 32, 1)

    encoded = {
        "gate": {name: {"bytes": hexed(gate.encode()), "commitment": hexed(gate.commitment()),
                        "length": len(gate.encode())} for name, gate in gates.items()},
        "receipt": {name: {"bytes": hexed(item.encode()), "commitment": hexed(item.commitment()),
                           "length": len(item.encode())} for name, item in receipts.items()},
        "reviewer": {"bytes": hexed(reviewer.encode()), "commitment": hexed(reviewer.commitment()),
                     "length": len(reviewer.encode())},
    }

    # Every decode has to give back what was encoded, including the trailing evidence body.
    roundtrips = {}
    for name, gate in gates.items():
        decoded = d.Accumulator.decode(gate.encode())
        roundtrips[f"gate:{name}"] = hexed(decoded.encode()) == hexed(gate.encode())
    for name, item in receipts.items():
        decoded = d.Receipt.decode(item.encode())
        roundtrips[f"receipt:{name}"] = hexed(decoded.encode()) == hexed(item.encode())
    roundtrips["reviewer"] = hexed(d.Reviewer.decode(reviewer.encode()).encode()) == hexed(reviewer.encode())

    def address(pair: tuple[bytes, int]) -> dict:
        return {"address": hexed(pair[0]), "bump": pair[1]}

    addresses = {
        "gate": address(d.gate_address(PROGRAM, FORECAST)),
        "receipt": address(d.receipt_address(PROGRAM, FORECAST, 1, USER)),
        "reviewer": address(d.reviewer_address(PROGRAM)),
        "config": address(w.config_address(PROGRAM)),
        "forecast": address(w.forecast_address(PROGRAM, FORECAST)),
    }

    instructions = {
        "reviewer": hexed(d.encode_reviewer(bytes([11]) * 32, reviewer)),
        "reviewer_first": hexed(d.encode_reviewer(bytes([11]) * 32)),
        # `encode_activate` reads three fields off the account, so the fixture is the account
        # rather than a register instruction: an activation binds to what was published.
        "activate": hexed(d.encode_activate(w.ForecastAccount(
            forecast_id_hash=FORECAST, creator_hash=bytes([5]) * 32, specification_hash=SPECIFICATION,
            open_at_ms=1, close_at_ms=100, revision=5, occurred_at_ms=50, state=4, outcome=0,
            paused_from=0, event_hash=bytes([13]) * 32, snapshot_hash=bytes([14]) * 32,
            resolution_hash=RESOLUTION, dispute_hash=w._ZERO, reputation_hash=w._ZERO, trigger_hash=w._ZERO,
            challenge_until_ms=0, chain_finalize_not_before_ms=0, paused_at_ms=0,
            pending_disputes=0, material_disputes=0))),
        # A final state (10) requires a seal, so the plain advance encodings carry a live state.
        "advance": hexed(d.encode_advance(advance(state=9))),
        "advance_mode1": hexed(d.encode_advance(advance(state=9), mode=1, artifact=ARTIFACT, head=HEAD)),
        "advance_mode2": hexed(d.encode_advance(advance(state=9), mode=2, artifact=ARTIFACT)),
        "draft": hexed(d.encode_draft(draft_gate(), NONCE, body)),
        "append": hexed(d.encode_append(0, b"chunk")),
        "submit": hexed(d.encode_submit(receipt(body))),
        "review": hexed(d.encode_review(receipt(body, status=1), ARTIFACT, 2)),
        "seal": hexed(d.encode_seal(draft_gate(), advance(state=10))),
        "finalize": hexed(d.encode_finalize(sealed_gate(), advance(state=10))),
    }

    # The seal chain: a candidate that differs from the seal in any of four ways is refused.
    sealed = sealed_gate()
    candidates = {
        "exact": advance(state=10),
        "different_revision": advance(state=10, revision=7),
        "different_event": w.encode_advance(revision=5, occurred_at_ms=101, previous_event_hash=bytes([4]) * 32,
                                            event_hash=bytes([99]) * 32, snapshot_hash=bytes([14]) * 32,
                                            state=10, outcome=1, resolution_hash=RESOLUTION,
                                            reputation_hash=bytes([15]) * 32, challenge_until_ms=100),
        "not_final": advance(state=9),
    }
    seal_chain = [case(f"finalize:{name}", lambda data=data: hexed(d.encode_finalize(sealed, data)))
                  for name, data in candidates.items()]

    # One refusal per invariant that can be broken cheaply, so a port that validates less is
    # visible rather than merely permissive.
    refusals = [
        case("gate:dormant_with_resolution", lambda: replace(gates["open"], phase=0).encode()),
        case("gate:premature_seal", lambda: replace(gates["open"], sealed_revision=6).encode()),
        case("gate:sealed_without_payload", lambda: replace(sealed_gate(), sealed_payload=w._ZERO).encode()),
        case("receipt:unsubmitted_time", lambda: replace(receipt(body), accepted_at=5).encode()),
        case("receipt:evidence_mismatch", lambda: replace(receipt(body, status=1), evidence=bytes([1]) * 32).encode()),
        case("receipt:premature_review", lambda: replace(receipt(body, status=1), review=bytes([1]) * 32).encode()),
        case("evidence:empty", lambda: hexed(d.evidence_hash(b""))),
        case("evidence:oversized", lambda: hexed(d.evidence_hash(b"x" * (d.MAX_BODY + 1)))),
        case("append:oversized_chunk", lambda: hexed(d.encode_append(0, b"x" * (d.CHUNK + 1)))),
        case("append:past_the_end", lambda: hexed(d.encode_append(d.MAX_BODY - 1, b"xx"))),
        case("review:disposition", lambda: hexed(d.encode_review(receipt(body, status=1), ARTIFACT, 4))),
        case("submit:already_accepted", lambda: hexed(d.encode_submit(receipt(body, status=1)))),
        case("advance:final_without_seal", lambda: hexed(d.encode_advance(advance(state=10)))),
        case("advance:unexpected_extension", lambda: hexed(d.encode_advance(advance(state=9), mode=0, artifact=ARTIFACT))),
        case("draft:epoch_not_open", lambda: hexed(d.encode_draft(sealed_gate(), NONCE, body))),
        case("seal:cannot_seal", lambda: hexed(d.encode_seal(sealed_gate(), advance(state=10)))),
        case("pda:too_many_seeds", lambda: hexed(w.find_program_address(PROGRAM, tuple(b"s" for _ in range(16)))[0])),
        case("pda:seed_too_long", lambda: hexed(w.find_program_address(PROGRAM, (b"s" * 33,))[0])),
        case("base58:invalid_length", lambda: hexed(w.base58_decode("1", length=0))),
        case("base58:bad_character", lambda: hexed(w.base58_decode("0OIl", length=32))),
    ]

    # The wire's own primitives, which the codecs above are built from.
    base58 = []
    for raw in [bytes(32), bytes([1]) + bytes(31), bytes(range(32)), b"\xff" * 32, bytes([7]) * 32]:
        text = w.base58_encode(raw)
        base58.append({"bytes": hexed(raw), "text": text,
                       "roundtrip": hexed(w.base58_decode(text, length=len(raw)))})

    shortvecs = [{"value": value, "bytes": hexed(w.shortvec(value))} for value in (0, 1, 127, 128, 255, 256, 16383, 65535)]
    shortvec_refusals = [case(f"shortvec:{value}", lambda value=value: hexed(w.shortvec(value)))
                         for value in (-1, 65536)]

    points = [{"bytes": hexed(raw), "on_curve": w.is_edwards_point(raw)} for raw in [bytes(32)]]

    instructions_for_message = (
        w.Instruction(PROGRAM, (w.AccountMeta(USER, True, True), w.AccountMeta(FORECAST, False, True)),
                      d.encode_submit(receipt(body))),
        w.Instruction(d.SYSTEM, (w.AccountMeta(USER, True, True), w.AccountMeta(FORECAST, False, True)), b"\x02"),
    )
    compiled = w.compile_message(USER, HEAD, instructions_for_message)
    message = {"data": hexed(compiled.data), "signer_keys": [hexed(key) for key in compiled.signer_keys],
               "account_keys": [hexed(key) for key in compiled.account_keys]}

    return {
        # The four values the repository already publishes, recomputed from the same fixture.
        # A port that disagrees with these is wrong regardless of anything else below.
        "pinned": {
            "expected": PINNED,
            "computed": {
                "gate": hexed(draft_gate().commitment()),
                "receipt": hexed(receipt(b"*").commitment()),
                "reviewer": hexed(d.Reviewer(bytes([11]) * 32, 1).commitment()),
                "advance": hexed(d.advance_hash(advance(state=10))),
            },
        },
        "description": "The Solana wire codecs: account layouts, PDAs, instruction encodings and the "
                       "seal chain, exported byte for byte.",
        "genesis": hexed(d.GENESIS),
        "program": hexed(PROGRAM),
        "seeds": {"gate": d.GATE_SEED.decode(), "receipt": d.RECEIPT_SEED.decode(),
                  "reviewer": d.REVIEWER_SEED.decode()},
        "sizes": {"gate": d.GATE_SIZE, "receipt_header": d.RECEIPT_HEADER, "reviewer": d.REVIEWER_SIZE,
                  "max_body": d.MAX_BODY, "chunk": d.CHUNK},
        "encoded": encoded,
        "roundtrips": roundtrips,
        "addresses": addresses,
        "instructions": instructions,
        "sealChain": seal_chain,
        "refusals": refusals,
        "base58": base58,
        "shortvecs": shortvecs,
        "shortvecRefusals": shortvec_refusals,
        "points": points,
        "message": message,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args()
    document = json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
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
