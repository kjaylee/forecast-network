#!/usr/bin/env python3
"""Deploy only to pinned Solana Devnet using distinct Keychain-backed roles."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from solana_keychain import ROOT, Keychain, base58, public_bytes, temporary_keypairs

SOLANA = os.environ.get("SOLANA_CLI", str(Path.home() / ".local/share/solana/install/active_release/bin/solana"))
RPC = "https://api.devnet.solana.com"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"


def rpc(method: str, params: list) -> object:
    request = urllib.request.Request(RPC, data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise RuntimeError("Devnet RPC response exceeded limit")
    body = json.loads(raw)
    if body.get("error") is not None or "result" not in body:
        raise RuntimeError(f"Devnet RPC failed: {method}")
    return body["result"]


def verified_keys() -> dict[str, str]:
    manifest = json.loads((ROOT / "infra/solana/devnet.json").read_text())
    keychain = Keychain()
    keys = {}
    for role, field in (("program", "programId"), ("upgrade", "upgradeAuthority"),
                        ("relayer", "relayer")):
        seed = keychain.read(role)
        if seed is None:
            raise RuntimeError(f"Missing Keychain seed: {role}")
        keys[role] = base58(public_bytes(seed))
        if keys[role] != manifest[field]:
            raise RuntimeError(f"Keychain public identity mismatch: {role}")
    if manifest["genesisHash"] != GENESIS or rpc("getGenesisHash", []) != GENESIS:
        raise RuntimeError("Cluster mismatch; refusing all transactions")
    return keys


def verify_program(keys: dict[str, str], binary: Path, report: dict) -> None:
    deadline = time.monotonic() + 55
    while True:
        info = rpc("getAccountInfo", [keys["program"], {"encoding": "base64", "commitment": "finalized"}])["value"]
        if info is not None or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if not info or info["owner"] != LOADER or not info["executable"]:
        raise RuntimeError("Program deployment not yet proven at finalized commitment")
    data = base64.b64decode(info["data"][0], validate=True)
    if len(data) != 36 or int.from_bytes(data[:4], "little") != 2:
        raise RuntimeError("Unexpected upgradeable program account")
    program_data = base58(data[4:])
    loaded = rpc("getAccountInfo", [program_data, {"encoding": "base64", "commitment": "finalized"}])["value"]
    raw = base64.b64decode(loaded["data"][0], validate=True)
    if (loaded["owner"] != LOADER or len(raw) != binary.stat().st_size + 45
            or int.from_bytes(raw[:4], "little") != 3 or raw[12] != 1
            or base58(raw[13:45]) != keys["upgrade"] or raw[45:] != binary.read_bytes()):
        raise RuntimeError("Deployed program bytes or authority mismatch")
    report.update({"programData": program_data, "verifiedFinalized": True})
    (ROOT / "tmp/registry-deploy/deployment-evidence.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "deploy", "deploy-buffer", "verify"))
    args = parser.parse_args()
    keys = verified_keys()
    binary = ROOT / "tmp/registry-deploy/forecast_registry.so"
    evidence = json.loads((binary.parent / "build-evidence.json").read_text())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    # Compare to the reviewed build record without silently accepting a new binary.
    if (digest != evidence.get("sha256") or evidence.get("program_id") != keys["program"]
            or evidence.get("initial_admin") != keys["upgrade"]):
        raise RuntimeError("SBF binary does not match reviewed build evidence")
    size = binary.stat().st_size
    balance = rpc("getBalance", [keys["relayer"], {"commitment": "finalized"}])["value"]
    rent = rpc("getMinimumBalanceForRentExemption", [size + 45])
    report = {"cluster": "devnet", "programId": keys["program"],
              "binarySha256": digest, "binaryBytes": size,
              "programDataRentLamports": rent, "relayerBalanceLamports": balance}
    print(json.dumps(report))
    if args.action == "check":
        return
    if args.action == "verify":
        verify_program(keys, binary, report)
        return
    # Buffer and program data coexist during first deployment; retain a fee margin.
    if balance < rent * 2 + 50_000_000:
        raise RuntimeError("Insufficient Devnet test SOL for buffer, program rent and fees")
    account = rpc("getAccountInfo", [keys["program"], {"encoding": "base64", "commitment": "finalized"}])["value"]
    if account is not None:
        # Program upgrade is a separate reviewed release action, never an implicit retry.
        raise RuntimeError("Program already exists; inspect its deployment before an upgrade")
    with temporary_keypairs(("relayer", "upgrade", "program", "buffer")) as paths:
        if args.action == "deploy-buffer":
            buffer_seed = Keychain().read("buffer")
            if buffer_seed is None:
                raise RuntimeError("Missing retained deployment buffer")
            buffered = rpc("getAccountInfo", [base58(public_bytes(buffer_seed)), {
                "encoding": "base64", "commitment": "finalized"}])["value"]
            data = base64.b64decode(buffered["data"][0], validate=True) if buffered else b""
            if (not buffered or buffered["owner"] != LOADER or buffered["executable"]
                    or data[:5] != b"\1\0\0\0\1" or base58(data[5:37]) != keys["upgrade"]
                    or data[37:] != binary.read_bytes()):
                raise RuntimeError("Retained buffer does not exactly match reviewed binary and authority")
        command = [SOLANA, "program", "deploy"]
        if args.action == "deploy":
            command.append(str(binary))
        command.extend(["--url", RPC,
                   "--keypair", str(paths["relayer"]), "--fee-payer", str(paths["relayer"]),
                   "--upgrade-authority", str(paths["upgrade"]), "--program-id", str(paths["program"]),
                   "--buffer", str(paths["buffer"]), "--max-len", str(size),
                   "--max-sign-attempts", "2", "--use-rpc", "--output", "json"])
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                env={**os.environ, "TMPDIR": str(ROOT / "tmp")})
        if result.returncode:
            # Do not echo CLI output: recovery diagnostics from signing tools can contain secrets.
            for line in result.stderr.splitlines():
                if line.startswith("Error: ") and not re.search(
                    r"seed|secret|private|mnemonic|recovery|keypair", line, re.IGNORECASE
                ):
                    print(line[:300], flush=True)
            raise RuntimeError("Devnet deployment failed; retained buffer identity is in Keychain")
    verify_program(keys, binary, report)


if __name__ == "__main__":
    main()
