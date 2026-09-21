#!/usr/bin/env python3
"""Export the wallet codecs, so a Rust port can be held to them.

Two of these decide whether a sign-in is genuine, and both are places where being *slightly*
more permissive than the reference is a security bug rather than a compatibility one:

  * `_valid_ed25519_public_key` is RFC 8032 §§5.1.3–5.1.4 decoding **plus** rejection of the
    full 8-torsion. That second part is the whole reason it exists: several WebCrypto
    implementations accept vacuous signatures for small-order keys, so verifying the
    signature alone does not establish ownership. A port that decompressed and stopped would
    accept an identity anybody can forge.
  * `decode_address` requires the input to be *canonical*: 32 bytes, re-encoding to exactly
    what was given, and a valid non-small-order point. A noncanonical encoding has more than
    one byte string for the same key, and two accounts for one key is a confusion the
    reference refuses.

Note that this is deliberately *stricter* than `is_edwards_point` in the Solana wire codecs,
which matches `curve25519-dalek` decompression alone because a PDA search must find the
same address the chain would. Two different questions, two different checks, and the vector
pins both sides of the difference.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/application/src"), str(ROOT / "packages/domain/src"), str(ROOT)]

from forecast_application.errors import AppError  # noqa: E402
from forecast_application.wallets import (  # noqa: E402
    _valid_ed25519_public_key,
    decode_address,
    decode_signature,
    encode_address,
)
from golden_cli import golden_main  # noqa: E402

GOLDEN = ROOT / "tests/golden/wallet-golden.json"

# The points of order 1, 2 and 8, which are the ones a signature check cannot distinguish.
SMALL_ORDER = [
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
]

# Real Ed25519 public points: the base point, its double, and a few arbitrary encodings.
ORDINARY = [
    "5866666666666666666666666666666666666666666666666666666666666666",
    "c9a3f86aae465f0e56513864510f3997561fa2c9e85ea21dc2292309f3cd6022",
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
]


def case(name: str, call, **inputs) -> dict:
    """The outcome, with the input recorded beside it.

    A vector whose inputs are only implicit in the caller is a vector a port cannot replay;
    the address and the signature go into the file with the answer.
    """
    entry = {"name": name, **inputs}
    try:
        entry["result"] = call()
    except AppError as error:
        entry["error"] = {"code": error.code, "message": error.message}
    return entry


def build() -> dict:
    small_order = [{"hex": value, "valid": _valid_ed25519_public_key(bytes.fromhex(value))} for value in SMALL_ORDER]
    ordinary = [{"hex": value, "valid": _valid_ed25519_public_key(bytes.fromhex(value))} for value in ORDINARY]

    # A handful of encodings that are wrong for reasons other than torsion.
    malformed = []
    for name, raw in [
        ("all-ff", bytes([0xFF] * 32)),
        ("y-equals-p", bytes.fromhex("edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f")),
        ("y-greater-than-p", bytes.fromhex("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f")),
        ("short", bytes(31)),
        ("long", bytes(33)),
    ]:
        malformed.append({"name": name, "hex": raw.hex(), "valid": _valid_ed25519_public_key(raw)})

    rounds = [{"raw": raw.hex(), "address": encode_address(raw)} for raw in (bytes(32), bytes([1]) + bytes(31),
                                                                              bytes(range(32)), bytes([255]) * 32)]

    # Every valid address has to survive a round trip, and every invalid one has a name for why.
    decode_cases = []
    for entry in ordinary:
        raw = bytes.fromhex(entry["hex"])
        address = encode_address(raw)
        decode_cases.append(case(f"decode:ordinary-{entry['hex'][:8]}", lambda a=address: decode_address(a).hex(),
                                 value=address, kind="address"))
    for name, value in [
        ("short", "1"),
        ("bad-character", "0OIl" + "1" * 28),
        ("too-long", "1" * 45),
        ("noncanonical", encode_address(bytes.fromhex("edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"))),
        ("small-order", encode_address(bytes(32))),
        ("not-a-string", 7),
    ]:
        decode_cases.append(case(f"decode:{name}", lambda v=value: decode_address(v).hex(),
                                 value=value if isinstance(value, str) else None, kind="address"))

    signatures = []
    for name, raw in [
        ("short", base64.b64encode(bytes(63)).decode()),
        ("bad-characters", "!" * 88),
        ("not-canonical", base64.b64encode(bytes(64)).decode().rstrip("=") + "A"),
        ("not-a-string", 7),
        ("too-long", base64.b64encode(bytes(65)).decode()),
    ]:
        signatures.append(case(f"signature:{name}", lambda v=raw: decode_signature(v).hex(),
                               value=raw if isinstance(raw, str) else None, kind="signature"))
    good = bytes(range(64))
    signatures.append(case("signature:valid", lambda: decode_signature(base64.b64encode(good).decode()).hex(),
                           value=base64.b64encode(good).decode(), kind="signature"))
    signatures.append(case("signature:standard-padding", lambda: decode_signature(
        base64.b64encode(b"\x00" * 64).decode()).hex(),
        value=base64.b64encode(b"\x00" * 64).decode(), kind="signature"))

    return {
        "description": "The wallet codecs: RFC 8032 decoding with the 8-torsion rejected, canonical base58 "
                       "addresses, and strict signature decoding.",
        "smallOrder": small_order,
        "ordinary": ordinary,
        "malformed": malformed,
        "rounds": rounds,
        "decodes": decode_cases,
        "signatures": signatures,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
