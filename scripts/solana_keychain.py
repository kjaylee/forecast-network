#!/usr/bin/env python3
"""Keep distinct Devnet signing seeds in macOS Keychain; display public keys only.

Security.framework receives secrets in memory, never as command arguments.
Deployment tooling may use temporary owner-only CLI keypair files via the context
manager; durable private keys remain in Keychain. Requires the local cryptography
tooling package, never imported by the application runtime.
"""

from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = b"forecast-network"
SERVICES = {
    role: f"forecast-network-devnet-{role}-seed-v1"
    for role in ("relayer", "upgrade", "program", "buffer")
}
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    result = ""
    while number:
        number, digit = divmod(number, 58)
        result = ALPHABET[digit] + result
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + result


def public_bytes(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )


class Keychain:
    def __init__(self) -> None:
        self.security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self.security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p
        ]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32

    def read(self, role: str) -> bytes | None:
        service = SERVICES[role].encode()
        size = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None, len(service), service, len(ACCOUNT), ACCOUNT,
            ctypes.byref(size), ctypes.byref(data), None,
        )
        if status == -25300:
            return None
        if status:
            raise RuntimeError(f"Keychain read failed for {role}: OSStatus {status}")
        try:
            value = ctypes.string_at(data, size.value)
            if len(value) != 32:
                raise RuntimeError(f"Invalid stored seed length for {role}")
            return value
        finally:
            self.security.SecKeychainItemFreeContent(None, data)

    def ensure(self, role: str) -> bytes:
        existing = self.read(role)
        if existing is not None:
            return existing
        service = SERVICES[role].encode()
        value = os.urandom(32)
        status = self.security.SecKeychainAddGenericPassword(
            None, len(service), service, len(ACCOUNT), ACCOUNT,
            len(value), value, None,
        )
        if status == -25299:  # Another process won creation; never overwrite it.
            winner = self.read(role)
            if winner is None:
                raise RuntimeError(f"Concurrent Keychain creation failed for {role}")
            return winner
        if status:
            raise RuntimeError(f"Keychain creation failed for {role}: OSStatus {status}")
        persisted = self.read(role)
        if persisted is None or not hmac.compare_digest(value, persisted):
            raise RuntimeError(f"Keychain verification failed for {role}")
        return persisted


@contextmanager
def temporary_keypairs(roles: tuple[str, ...]):
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    keychain = Keychain()
    with tempfile.TemporaryDirectory(prefix="devnet-keys-", dir=scratch) as directory:
        os.chmod(directory, 0o700)
        paths = {}
        for role in roles:
            seed = keychain.read(role)
            if seed is None:
                raise RuntimeError(f"Missing Keychain role: {role}")
            path = Path(directory) / f"{role}.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(list(seed + public_bytes(seed)), output)
            paths[role] = path
        yield paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "inventory"))
    args = parser.parse_args()
    keychain = Keychain()
    result = {}
    for role, service in SERVICES.items():
        seed = keychain.ensure(role) if args.action == "ensure" else keychain.read(role)
        result[role] = {
            "service": service,
            "publicKey": base58(public_bytes(seed)) if seed is not None else None,
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
