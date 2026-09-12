#!/usr/bin/env python3
"""Resume the retained Devnet buffer with paced, exact-difference writes.

Encoding follows installed official solana-loader-v3-interface 2.2.0 Write.
Only the reviewed binary and the Keychain-owned retained buffer are accepted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time

from registry_operator import Operator, wire
from solana_keychain import ROOT, Keychain, public_bytes


def main() -> None:
    operator = Operator(False)
    seed = Keychain().read("buffer")
    if seed is None:
        raise RuntimeError("Missing retained buffer identity")
    address = public_bytes(seed)
    loader = wire.base58_decode("BPFLoaderUpgradeab1e11111111111111111111111", length=32)
    binary = (ROOT / "tmp/registry-deploy/forecast_registry.so").read_bytes()
    evidence = json.loads((ROOT / "tmp/registry-deploy/build-evidence.json").read_text())
    if evidence["sha256"] != hashlib.sha256(binary).hexdigest():
        raise RuntimeError("Reviewed binary mismatch")

    def retained() -> bytes:
        value = operator.rpc("getAccountInfo", [wire.base58_encode(address), {
            "encoding": "base64", "commitment": "finalized"}])["value"]
        if not value or value["executable"] or value["owner"] != wire.base58_encode(loader):
            raise RuntimeError("Unexpected deployment buffer owner")
        data = base64.b64decode(value["data"][0], validate=True)
        if (len(data) != 37 + len(binary) or data[:5] != b"\1\0\0\0\1"
                or data[5:37] != operator.keys["upgrade"]):
            raise RuntimeError("Deployment buffer authority or layout mismatch")
        return data[37:]

    previous = retained()
    signatures = []
    missing = [(offset, binary[offset:offset+700]) for offset in range(0, len(binary), 700)
               if previous[offset:offset+700] != binary[offset:offset+700]]
    print(json.dumps({"pendingChunks": len(missing), "cluster": "devnet"}), flush=True)
    for index, (offset, chunk) in enumerate(missing):
        instruction = wire.Instruction(loader, (
            wire.AccountMeta(address, False, True), wire.AccountMeta(operator.keys["upgrade"], True),
        ), struct.pack("<IIQ", 1, offset, len(chunk)) + chunk)
        signed = operator.signed((instruction,))
        signature = operator.rpc("sendTransaction", [signed, {
            "encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed",
            "maxRetries": 3}])
        signatures.append(signature)
        (ROOT / "tmp/registry-deploy/buffer-write-signatures.json").write_text(json.dumps(signatures))
        if index % 10 == 0:
            print(json.dumps({"submittedChunks": index+1, "totalChunks": len(missing)}), flush=True)
        time.sleep(1)
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        if retained() == binary:
            print(json.dumps({"bufferBytesVerifiedFinalized": len(binary), "sha256": evidence["sha256"]}))
            return
        time.sleep(3)
    raise RuntimeError("Buffer not fully finalized; rerun reconciles exact missing chunks")


if __name__ == "__main__":
    main()
