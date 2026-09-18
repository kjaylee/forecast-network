#!/usr/bin/env python3
"""Split the off-device recovery key so no single place can lose it.

`scripts/backup_recovery.py` encrypts each archive with a fresh AES-256-GCM key
and wraps that key with an RSA-3072 public key whose private half is stored on
the NAS and nowhere else. The archive is therefore only as durable as that one
file: if the NAS dies, its disks are lost or it is stolen, the archives survive
and nobody can open them.

A second copy in the same house does not fix that — one fire, one theft or one
flood takes both. This splits the key into shares with a threshold, so any K of
N reconstruct it and any K-1 reveal nothing, and the shares can live in
different places, with different people, on different media.

Shamir over GF(256) is implemented here rather than imported: it is about fifty
lines, it has no dependencies, and it is worth being able to read the whole of
something that guards the only key.

    split   --key <pkcs8> --shares 5 --threshold 3 --out <dir>
    combine --share <file> --share <file> --share <file> --out <pkcs8>
    fingerprint --key <pkcs8>
    fingerprint --share <file>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path

FORMAT = "forecast-recovery-key-share-v1"

# GF(256) with the Rijndael polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D), built
# from the generator 0x02.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x = ((_x << 1) ^ 0x11D) & 0xFF if _x & 0x80 else (_x << 1) & 0xFF
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("cannot divide by zero in GF(256)")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


def split(secret: bytes, *, shares: int, threshold: int) -> list[tuple[int, bytes]]:
    """N shares, any K of which reconstruct. K-1 reveal nothing about the secret."""
    if not secret:
        raise ValueError("nothing to split")
    if not 2 <= threshold <= shares <= 255:
        raise ValueError("require 2 <= threshold <= shares <= 255")
    points: list[tuple[int, bytearray]] = [(index + 1, bytearray()) for index in range(shares)]
    for byte in secret:
        # A fresh random polynomial per byte, whose constant term is the secret byte.
        coefficients = [byte] + [secrets.randbelow(256) for _ in range(threshold - 1)]
        for index, values in points:
            accumulator = 0
            for coefficient in reversed(coefficients):
                accumulator = _mul(accumulator, index) ^ coefficient
            values.append(accumulator)
    return [(index, bytes(values)) for index, values in points]


def combine(shares: list[tuple[int, bytes]]) -> bytes:
    """Lagrange interpolation at x = 0 over the supplied points."""
    if len(shares) < 2:
        raise ValueError("at least two shares are required")
    indices = [index for index, _ in shares]
    if len(set(indices)) != len(indices):
        raise ValueError("the same share was supplied twice")
    if any(not 1 <= index <= 255 for index in indices):
        raise ValueError("share indices must be between 1 and 255")
    lengths = {len(values) for _, values in shares}
    if len(lengths) != 1:
        raise ValueError("shares have different lengths and do not belong to one secret")
    length = lengths.pop()
    recovered = bytearray()
    for position in range(length):
        accumulator = 0
        for index, values in shares:
            numerator, denominator = 1, 1
            for other, _ in shares:
                if other == index:
                    continue
                numerator = _mul(numerator, other)
                denominator = _mul(denominator, index ^ other)
            accumulator ^= _mul(values[position], _div(numerator, denominator))
        recovered.append(accumulator)
    return bytes(recovered)


def fingerprint(key: bytes) -> str:
    """Stable, safe to write down: identifies the key without being the key."""
    return hashlib.sha256(b"forecast-network:recovery-key-fingerprint:v1\n" + key).hexdigest()


def encode_share(index: int, values: bytes, *, shares: int, threshold: int, key_fingerprint: str) -> str:
    body = base64.b64encode(values).decode("ascii")
    return json.dumps({"format": FORMAT, "index": index, "shares": shares, "threshold": threshold,
                       "fingerprint": key_fingerprint, "value": body}, sort_keys=True) + "\n"


def decode_share(text: str) -> tuple[int, bytes, dict[str, object]]:
    loaded = json.loads(text)
    if not isinstance(loaded, dict) or loaded.get("format") != FORMAT:
        raise ValueError("not a recovery key share")
    index, value = loaded.get("index"), loaded.get("value")
    if type(index) is not int or not 1 <= index <= 255 or not isinstance(value, str):
        raise ValueError("share is missing its index or value")
    return index, base64.b64decode(value, validate=True), loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    make = commands.add_parser("split", help="Split a private key into shares")
    make.add_argument("--key", type=Path, required=True)
    make.add_argument("--shares", type=int, required=True)
    make.add_argument("--threshold", type=int, required=True)
    make.add_argument("--out", type=Path, required=True, help="Directory to write the shares into")

    merge = commands.add_parser("combine", help="Reconstruct a private key from shares")
    merge.add_argument("--share", type=Path, action="append", required=True)
    merge.add_argument("--out", type=Path, required=True)

    print_fingerprint = commands.add_parser("fingerprint", help="Identify a key or a share")
    print_fingerprint.add_argument("--key", type=Path)
    print_fingerprint.add_argument("--share", type=Path)

    args = parser.parse_args()

    if args.command == "split":
        key = args.key.read_bytes()
        if not key:
            raise SystemExit(f"not a readable key: {args.key}")
        digest = fingerprint(key)
        args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        produced = []
        for index, values in split(key, shares=args.shares, threshold=args.threshold):
            path = args.out / f"share-{index}-of-{args.shares}.json"
            path.write_text(encode_share(index, values, shares=args.shares,
                                         threshold=args.threshold, key_fingerprint=digest))
            path.chmod(0o600)
            produced.append(str(path))
        print(json.dumps({"event": "recovery_key_split", "shares": args.shares,
                          "threshold": args.threshold, "fingerprint": digest,
                          "paths": produced}, sort_keys=True))
        return 0

    if args.command == "combine":
        decoded = [decode_share(path.read_text()) for path in args.share]
        expected = {share[2].get("fingerprint") for share in decoded}
        if len(expected) != 1:
            raise SystemExit("these shares belong to different keys")
        key = combine([(index, values) for index, values, _ in decoded])
        if fingerprint(key) != expected.pop():
            raise SystemExit("the reconstructed key does not match the shares' fingerprint")
        args.out.write_bytes(key)
        args.out.chmod(0o600)
        print(json.dumps({"event": "recovery_key_combined", "shares": len(decoded),
                          "fingerprint": fingerprint(key), "path": str(args.out)}, sort_keys=True))
        return 0

    if args.key:
        key = args.key.read_bytes()
    elif args.share:
        _, key, _ = decode_share(args.share.read_text())
    else:
        raise SystemExit("give either --key or --share")
    print(json.dumps({"fingerprint": fingerprint(key)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
