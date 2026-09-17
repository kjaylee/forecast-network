#!/usr/bin/env python3
"""Authenticated off-device D1/Devnet backup and isolated recovery proof.

Operational tooling only; existing cryptography is not an application dependency.
No private signing seed is written outside Keychain or exposed in process arguments.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = Path("/home/spritz/AI/forecast-network")
ROLES = ("relayer", "upgrade", "program", "buffer")
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
FORMAT = "forecast-off-device-backup-v1"
LABEL = b"forecast-network:backup-wrap:v1"
CHALLENGE_PREFIX = b"forecast-network:off-device-recovery-proof:v1\n"
MAX_SQL = 64 * 1024 * 1024
PATHS = ("d1.sql", "devnet-roles.json")
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def base58(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    result = ""
    while number:
        number, remainder = divmod(number, 58)
        result = ALPHABET[remainder] + result
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + result


def public(seed: bytes) -> bytes:
    return (
        ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def fingerprint(key: rsa.RSAPublicKey) -> str:
    return digest(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def backup_id(value: str) -> str:
    if not re.fullmatch(r"forecast-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", value):
        raise ValueError("Invalid backup ID")
    return value


def write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    if os.fstat(fd).st_mode & 0o077:
        os.close(fd)
        raise ValueError("Unable to enforce private file permissions")
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def secure_directory(path: Path, *, new: bool = False) -> None:
    if path.exists() and not new:
        info = path.lstat()
        if (
            path.is_symlink()
            or not path.is_dir()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Existing recovery directory is not private and owned")
        return
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("Unable to enforce private directory permissions")


def validate_seeds(seeds: dict[str, bytes], deployment: dict[str, Any]) -> dict[str, str]:
    if (
        set(seeds) != set(ROLES)
        or deployment.get("cluster") != "devnet"
        or deployment.get("genesisHash") != GENESIS
    ):
        raise ValueError("Only the four existing public Forecast Devnet roles are supported")
    if any(type(seed) is not bytes or len(seed) != 32 for seed in seeds.values()):
        raise ValueError("Invalid signing seed")
    keys = {role: base58(public(seeds[role])) for role in ROLES}
    for role, field in (
        ("relayer", "relayer"),
        ("upgrade", "upgradeAuthority"),
        ("program", "programId"),
    ):
        if keys[role] != deployment.get(field):
            raise ValueError("Devnet role does not match pinned deployment")
    if deployment.get("buffer") is not None and keys["buffer"] != deployment["buffer"]:
        raise ValueError("Buffer role does not match pinned deployment")
    return keys


def encrypt_archive(
    sql: bytes,
    seeds: dict[str, bytes],
    deployment: dict[str, Any],
    recipient: rsa.RSAPublicKey,
    identifier: str,
) -> tuple[bytes, dict[str, Any]]:
    backup_id(identifier)
    if not 0 < len(sql) <= MAX_SQL or recipient.key_size < 3072:
        raise ValueError("Unsupported backup size or wrapping key")
    keys = validate_seeds(seeds, deployment)
    seed_data = canonical(
        {
            "cluster": "devnet",
            "genesisHash": GENESIS,
            "seeds": {role: b64(seeds[role]) for role in ROLES},
        }
    )
    files = {"d1.sql": sql, "devnet-roles.json": seed_data}
    manifest = {
        "backupId": identifier,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "cluster": "devnet",
        "genesisHash": GENESIS,
        "publicKeys": keys,
        "pathCount": len(files),
        "files": [
            {"path": name, "bytes": len(value), "sha256": digest(value)}
            for name, value in files.items()
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    key, nonce = os.urandom(32), os.urandom(12)
    wrapped = recipient.encrypt(
        key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=LABEL)
    )
    header = {
        "format": FORMAT,
        "keyFingerprint": fingerprint(recipient),
        "manifest": manifest,
        "nonce": b64(nonce),
        "wrappedKey": b64(wrapped),
    }
    ciphertext = AESGCM(key).encrypt(nonce, buffer.getvalue(), canonical(header))
    encoded = canonical({**header, "ciphertext": b64(ciphertext)})
    return encoded, manifest


def decrypt_archive(
    encoded: bytes, recipient: rsa.RSAPrivateKey, expected_fingerprint: str
) -> tuple[dict[str, bytes], dict[str, Any]]:
    if len(encoded) > 2 * MAX_SQL:
        raise ValueError("Encrypted archive exceeds bound")
    value = json.loads(encoded)
    if set(value) != {"format", "keyFingerprint", "manifest", "nonce", "wrappedKey", "ciphertext"}:
        raise ValueError("Invalid encrypted archive shape")
    if (
        value["format"] != FORMAT
        or value["keyFingerprint"] != expected_fingerprint
        or fingerprint(recipient.public_key()) != expected_fingerprint
    ):
        raise ValueError("Wrapping key fingerprint mismatch")
    ciphertext = unb64(value.pop("ciphertext"))
    nonce = unb64(value["nonce"])
    if len(nonce) != 12:
        raise ValueError("Invalid encryption nonce")
    key = recipient.decrypt(
        unb64(value["wrappedKey"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=LABEL),
    )
    plain = AESGCM(key).decrypt(nonce, ciphertext, canonical(value))
    manifest = value["manifest"]
    backup_id(manifest["backupId"])
    if (
        manifest["cluster"] != "devnet"
        or manifest["genesisHash"] != GENESIS
        or manifest["pathCount"] != 2
    ):
        raise ValueError("Backup scope mismatch")
    entries = manifest["files"]
    if [item["path"] for item in entries] != list(PATHS):
        raise ValueError("Unexpected archive paths")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        if archive.namelist() != list(PATHS):
            raise ValueError("Unexpected or duplicate archive members")
        for item in entries:
            info = archive.getinfo(item["path"])
            limit = MAX_SQL if item["path"] == "d1.sql" else 4096
            if not 0 < info.file_size <= limit or info.file_size != item["bytes"]:
                raise ValueError("Unpacked file exceeds bound or manifest")
            data = archive.read(info)
            if digest(data) != item["sha256"]:
                raise ValueError("Recovered file hash mismatch")
            files[item["path"]] = data
    return files, manifest


def challenge_message(identifier: str, nonce: bytes) -> bytes:
    backup_id(identifier)
    if len(nonce) != 32:
        raise ValueError("Recovery challenge must contain 32 random bytes")
    return CHALLENGE_PREFIX + identifier.encode() + b"\n" + nonce


def sign_recovery(seed_data: bytes, manifest: dict[str, Any], nonce: bytes) -> list[dict[str, str]]:
    decoded = json.loads(seed_data)
    if (
        decoded.get("cluster") != "devnet"
        or decoded.get("genesisHash") != GENESIS
        or set(decoded["seeds"]) != set(ROLES)
    ):
        raise ValueError("Recovered seed scope mismatch")
    message = challenge_message(manifest["backupId"], nonce)
    proof = []
    for role in ROLES:
        seed = unb64(decoded["seeds"][role])
        pub = public(seed)
        if base58(pub) != manifest["publicKeys"][role]:
            raise ValueError("Recovered role public key mismatch")
        signature = ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(message)
        proof.append(
            {
                "role": role,
                "publicKey": base58(pub),
                "publicKeyBase64": b64(pub),
                "signature": b64(signature),
            }
        )
    return proof


def verify_recovery(
    proof: list[dict[str, str]], keys: dict[str, str], identifier: str, nonce: bytes
) -> None:
    if len(proof) != 4 or {item["role"] for item in proof} != set(ROLES):
        raise ValueError("Recovery proof is missing or duplicates a role")
    message = challenge_message(identifier, nonce)
    for item in proof:
        raw = unb64(item["publicKeyBase64"])
        if base58(raw) != keys[item["role"]] or item["publicKey"] != keys[item["role"]]:
            raise ValueError("Recovery proof does not match local public pin")
        ed25519.Ed25519PublicKey.from_public_bytes(raw).verify(unb64(item["signature"]), message)


def restore_sql(sql: bytes, directory: Path) -> dict[str, Any]:
    secure_directory(directory, new=True)
    write_new(
        directory / "RESTORE_QUARANTINE.json",
        canonical(
            {
                "status": "quarantined",
                "activationAllowed": False,
                "reason": "A latest independently pinned tombstone registry has not been applied.",
                "applicationErasureAvailable": False,
            }
        ),
    )
    path = directory / "restore.sqlite"
    write_new(path, b"")
    connection = sqlite3.connect(path)

    def authorize(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in (
            "load_extension",
            "writefile",
            "readfile",
        ):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (first or "").lower() not in (
            "foreign_keys",
            "defer_foreign_keys",
            "integrity_check",
            "foreign_key_check",
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        connection.executescript(sql.decode("utf-8"))
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise ValueError("Restored database integrity failed")
        foreign_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if foreign_errors:
            raise ValueError("Restored database foreign keys failed")
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            name: connection.execute(
                'SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"'
            ).fetchone()[0]
            for name in names
        }
        return {
            "integrity": "ok",
            "privacyStatus": "quarantined",
            "activationAllowed": False,
            "foreignKeyErrors": foreign_errors,
            "tableCount": len(names),
            "tableCounts": counts,
            "forecastCount": counts.get("forecasts", 0),
            "sqlitePath": str(path),
            "sqliteMode": oct(path.stat().st_mode & 0o777),
        }
    finally:
        connection.close()


def remote_initialize(identifier: str) -> dict[str, Any]:
    backup_id(identifier)
    if shutil.disk_usage("/home/spritz/AI").free < 1024**3 or os.getloadavg()[0] > 4:
        raise ValueError("NAS free-space/load gate failed")
    for directory in (REMOTE_ROOT, REMOTE_ROOT / "backups", REMOTE_ROOT / "recovery-keys"):
        secure_directory(directory)
    archive_dir, key_dir = (
        REMOTE_ROOT / "backups" / identifier,
        REMOTE_ROOT / "recovery-keys" / identifier,
    )
    secure_directory(archive_dir, new=True)
    secure_directory(key_dir, new=True)
    secure_directory(archive_dir / "tmp", new=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key_path = key_dir / "wrapping-key.pem"
    write_new(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    write_new(key_dir / "wrapping-public.pem", pem)
    return {
        "archiveDirectory": str(archive_dir),
        "keyDirectory": str(key_dir),
        "publicKeyPem": pem.decode(),
        "keyFingerprint": fingerprint(key.public_key()),
        "freeBytes": shutil.disk_usage("/home/spritz/AI").free,
        "loadAverage": list(os.getloadavg()),
        "host": os.uname().nodename,
        "keyFileMode": oct(key_path.stat().st_mode & 0o777),
        "keyDirectoryMode": oct(key_dir.stat().st_mode & 0o777),
        "archiveDirectoryMode": oct(archive_dir.stat().st_mode & 0o777),
    }


def remote_restore(
    identifier: str, archive_sha: str, key_fingerprint: str, nonce: bytes
) -> dict[str, Any]:
    backup_id(identifier)
    archive_dir = REMOTE_ROOT / "backups" / identifier
    path = archive_dir / "archive.fnbak"
    encoded = path.read_bytes()
    if digest(encoded) != archive_sha:
        raise ValueError("Transferred encrypted archive hash mismatch")
    key_path = REMOTE_ROOT / "recovery-keys" / identifier / "wrapping-key.pem"
    secure_directory(key_path.parent)
    if (
        key_path.is_symlink()
        or key_path.stat().st_uid != os.getuid()
        or key_path.stat().st_mode & 0o077
    ):
        raise ValueError("Wrapping key permissions changed")
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("Unexpected wrapping key type")
    files, manifest = decrypt_archive(encoded, key, key_fingerprint)
    if manifest["backupId"] != identifier:
        raise ValueError("Archive identifier mismatch")
    cache = archive_dir / "tmp" / ("proof-" + digest(nonce)[:16] + ".json")
    if cache.exists():
        previous = json.loads(cache.read_text())
        if previous["archiveSha256"] != archive_sha or previous["challenge"] != b64(nonce):
            raise ValueError("Cached recovery does not match immutable intent")
        return previous
    proof = sign_recovery(files["devnet-roles.json"], manifest, nonce)
    target = archive_dir / "tmp" / ("restore-" + digest(nonce)[:16])
    if target.exists():
        target = (
            archive_dir
            / "tmp"
            / ("restore-" + digest(nonce)[:16] + "-retry-" + os.urandom(6).hex())
        )
    restored = restore_sql(files["d1.sql"], target)
    result = {
        "backupId": identifier,
        "archiveSha256": digest(encoded),
        "archiveBytes": len(encoded),
        "archiveMode": oct(path.stat().st_mode & 0o777),
        "keyFingerprint": key_fingerprint,
        "pathCount": len(files),
        "files": manifest["files"],
        "restored": restored,
        "challenge": b64(nonce),
        "signatures": proof,
        "host": os.uname().nodename,
    }
    write_new(cache, canonical(result))
    return result


def ssh_script(arguments: list[str]) -> dict[str, Any]:
    command = "python3 - " + shlex.join(arguments)
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=8",
            "poc-nas",
            command,
        ],
        input=Path(__file__).read_bytes(),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # Remote exceptions must never cause plaintext archive/seed dumps in logs.
        raise RuntimeError("Remote backup action failed; retained archive remains untouched")
    return dict(json.loads(result.stdout))


def transfer_encrypted(archive: Path, remote_directory: str) -> dict[str, Any]:
    """System rsync first; Synology's restricted path wrapper can require SSH streaming."""
    result = subprocess.run(
        [
            "/usr/bin/rsync",
            "-a",
            "-e",
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8",
            "--",
            str(archive),
            "poc-nas:" + remote_directory + "/archive.fnbak",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return {"transport": "/usr/bin/rsync", "returnCode": 0}
    encoded = archive.read_bytes()
    receiver = """import os,sys,hashlib,json
from pathlib import Path
p=Path(sys.argv[1]); expected=sys.argv[2]; size=int(sys.argv[3])
if not p.parent.is_dir() or p.parent.stat().st_uid!=os.getuid() or p.parent.stat().st_mode&0o077: raise ValueError('Destination directory is not private and owned')
part=p.parent/('.archive.'+os.urandom(6).hex()+'.partial')
fd=os.open(part,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
os.fchmod(fd,0o600)
if os.fstat(fd).st_mode&0o077: raise ValueError('Encrypted partial file permissions are unsafe')
h=hashlib.sha256(); count=0
with os.fdopen(fd,'wb') as out:
 while True:
  block=sys.stdin.buffer.read(1048576)
  if not block: break
  count+=len(block)
  if count>size: raise ValueError('Encrypted transfer exceeds expected size')
  h.update(block); out.write(block)
 out.flush(); os.fsync(out.fileno())
if count!=size or h.hexdigest()!=expected: raise ValueError('Encrypted transfer integrity mismatch')
os.link(part,p)
part.unlink()
print(json.dumps({'transport':'encrypted-stdin-over-SSH','bytes':count,'sha256':h.hexdigest(),'mode':oct(p.stat().st_mode&0o777)}))
"""
    command = shlex.join(
        [
            "python3",
            "-c",
            receiver,
            remote_directory + "/archive.fnbak",
            digest(encoded),
            str(len(encoded)),
        ]
    )
    streamed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=8",
            "poc-nas",
            command,
        ],
        input=encoded,
        capture_output=True,
        check=False,
    )
    if streamed.returncode:
        raise RuntimeError("Encrypted transfer failed; all original and partial archives retained")
    return {**json.loads(streamed.stdout), "systemRsyncReturnCode": result.returncode}


def run_local(
    snapshot: Path, reference_proof: Path | None = None, identifier: str | None = None
) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from scripts.solana_keychain import Keychain

    snapshot = snapshot.resolve()
    if not snapshot.is_relative_to(ROOT / "tmp") or not snapshot.is_file():
        raise ValueError("Use the existing repository tmp D1 export")
    identifier = (
        backup_id(identifier)
        if identifier is not None
        else "forecast-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + os.urandom(6).hex()
    )
    local_root = ROOT / "tmp" / "backup-recovery"
    secure_directory(local_root)
    stage = local_root / identifier
    secure_directory(stage, new=True)
    deployment = json.loads((ROOT / "infra/solana/devnet.json").read_text())
    store = Keychain()
    seeds = {role: store.read(role) for role in ROLES}
    if any(value is None for value in seeds.values()):
        raise ValueError("An existing Keychain role is missing; no keys were created")
    keys = validate_seeds(seeds, deployment)
    initialized = ssh_script(["remote-init", "--backup-id", identifier])
    recipient = serialization.load_pem_public_key(initialized["publicKeyPem"].encode())
    if (
        not isinstance(recipient, rsa.RSAPublicKey)
        or fingerprint(recipient) != initialized["keyFingerprint"]
    ):
        raise ValueError("Unexpected recipient public key")
    write_new(stage / "wrapping-public.pem", initialized["publicKeyPem"].encode())
    write_new(
        stage / "public-pin.json",
        canonical({"backupId": identifier, **initialized, "publicKeys": keys}),
    )
    encoded, manifest = encrypt_archive(
        snapshot.read_bytes(), seeds, deployment, recipient, identifier
    )
    archive = stage / "archive.fnbak"
    write_new(archive, encoded)
    write_new(stage / "manifest.json", canonical(manifest))
    transfer = transfer_encrypted(archive, initialized["archiveDirectory"])
    nonce = os.urandom(32)
    write_new(
        stage / "challenge.json",
        canonical({"backupId": identifier, "nonce": b64(nonce), "archiveSha256": digest(encoded)}),
    )
    recovered = ssh_script(
        [
            "remote-restore",
            "--backup-id",
            identifier,
            "--archive-sha",
            digest(encoded),
            "--key-fingerprint",
            initialized["keyFingerprint"],
            "--challenge",
            b64(nonce),
        ]
    )
    verify_recovery(recovered["signatures"], keys, identifier, nonce)
    if (
        recovered["files"] != manifest["files"]
        or recovered["archiveBytes"] != len(encoded)
        or recovered["pathCount"] != 2
    ):
        raise ValueError("Restored archive manifest differs from source")
    reference_matched = False
    if reference_proof is not None:
        reference = json.loads(reference_proof.read_text())
        if (
            reference["backupSha256"] != digest(snapshot.read_bytes())
            or reference["tableCounts"] != recovered["restored"]["tableCounts"]
        ):
            raise ValueError("Off-device restore differs from pinned local baseline")
        reference_matched = True
    report = {
        "format": FORMAT,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "backupId": identifier,
        "sourceSha256": digest(snapshot.read_bytes()),
        "sourceBytes": snapshot.stat().st_size,
        "sourcePathCount": 2,
        "remote": initialized,
        "transfer": transfer,
        "referenceRestoreMatched": reference_matched,
        "recovery": recovered,
        "signaturesVerifiedLocally": True,
        "publicKeys": keys,
        "bufferPinSource": "existing forecast-network Devnet buffer Keychain inventory; deployment manifest has no buffer field",
        "privateWrappingKeyOnLocalDevice": False,
        "plaintextSeedFilesCreated": 0,
        "liveDatabaseRestored": False,
        "realAssetTransactions": 0,
        "retentionPolicyDrillComplete": False,
        "keyRotationDrillComplete": False,
    }
    write_new(stage / "proof.json", canonical(report))
    return report


def resume_prepared_backup(identifier: str) -> dict[str, Any]:
    """Retry only an already encrypted/pinned intent; never create or replace keys."""
    backup_id(identifier)
    stage = ROOT / "tmp" / "backup-recovery" / identifier
    secure_directory(stage)
    pin = json.loads((stage / "public-pin.json").read_text())
    if any(
        pin.get(name) != mode
        for name, mode in (
            ("keyFileMode", "0o600"),
            ("keyDirectoryMode", "0o700"),
            ("archiveDirectoryMode", "0o700"),
        )
    ):
        raise ValueError("Earlier key enrollment did not meet private-permission requirements")
    archive = stage / "archive.fnbak"
    encoded = archive.read_bytes()
    manifest = json.loads(encoded)["manifest"]
    if manifest["backupId"] != identifier or manifest["publicKeys"] != pin["publicKeys"]:
        raise ValueError("Prepared archive does not match pinned backup intent")
    code = (
        "import json,hashlib;from pathlib import Path;p=Path("
        + repr(pin["archiveDirectory"] + "/archive.fnbak")
        + ");print(json.dumps({'exists':p.exists(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None}))"
    )
    state = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "poc-nas",
            shlex.join(["python3", "-c", code]),
        ],
        capture_output=True,
        check=False,
    )
    if state.returncode:
        raise RuntimeError("Cannot reconcile remote archive status")
    remote = json.loads(state.stdout)
    if remote["exists"]:
        if remote["sha256"] != digest(encoded):
            raise ValueError("Existing remote archive differs; never overwrite it")
    else:
        transfer_encrypted(archive, pin["archiveDirectory"])
    challenge = stage / "challenge.json"
    if not challenge.exists():
        write_new(
            challenge,
            canonical(
                {
                    "backupId": identifier,
                    "nonce": b64(os.urandom(32)),
                    "archiveSha256": digest(encoded),
                }
            ),
        )
    saved = json.loads(challenge.read_text())
    if saved["backupId"] != identifier or saved["archiveSha256"] != digest(encoded):
        raise ValueError("Saved challenge refers to a different archive")
    nonce = unb64(saved["nonce"])
    recovered = ssh_script(
        [
            "remote-restore",
            "--backup-id",
            identifier,
            "--archive-sha",
            digest(encoded),
            "--key-fingerprint",
            pin["keyFingerprint"],
            "--challenge",
            saved["nonce"],
        ]
    )
    verify_recovery(recovered["signatures"], pin["publicKeys"], identifier, nonce)
    if recovered["files"] != manifest["files"]:
        raise ValueError("Recovered files differ from prepared manifest")
    result = {
        "format": FORMAT,
        "backupId": identifier,
        "sourceSha256": manifest["files"][0]["sha256"],
        "sourceBytes": manifest["files"][0]["bytes"],
        "publicKeys": pin["publicKeys"],
        "remote": pin,
        "recovery": recovered,
        "signaturesVerifiedLocally": True,
        "resumedPreparedIntent": True,
        "liveDatabaseRestored": False,
        "activationAllowed": False,
        "privacyStatus": "quarantined",
    }
    if not (stage / "proof.json").exists():
        write_new(stage / "proof.json", canonical(result))
    return result


TOMBSTONE_PREFIX = b"forecast-network:restore-tombstone-registry:v1\n"


def validate_tombstones(
    registry: dict[str, Any], anchor: dict[str, Any], public_key: bytes, now_ms: int
) -> tuple[dict[str, Any], str]:
    """The anchor must come from a current independent registry, never this backup."""
    if type(now_ms) is not int or now_ms < 0 or len(public_key) != 32:
        raise ValueError("Invalid tombstone verification context")
    if set(registry) != {"body", "signature"}:
        raise ValueError("Invalid tombstone envelope")
    body = registry["body"]
    fields = {"version", "purpose", "sequence", "issuedAtMs", "validUntilMs", "entries"}
    if (
        set(body) != fields
        or type(body["version"]) is not int
        or body["version"] != 1
        or body["purpose"] != "restore-privacy-tombstones"
    ):
        raise ValueError("Tombstone purpose/version mismatch")
    for name in ("sequence", "issuedAtMs", "validUntilMs"):
        if type(body[name]) is not int or body[name] < 0:
            raise ValueError("Invalid tombstone clock or sequence")
    if not body["issuedAtMs"] <= now_ms <= body["validUntilMs"]:
        raise ValueError("Tombstone registry is expired or from the future")
    for name in ("sequence", "observedAtMs", "maxAgeMs"):
        if type(anchor.get(name)) is not int or anchor[name] < 0:
            raise ValueError("An explicit fresh registry anchor is required")
    if not anchor["maxAgeMs"] or not 0 <= now_ms - anchor["observedAtMs"] <= anchor["maxAgeMs"]:
        raise ValueError("Independent registry head is stale")
    head = digest(canonical(registry))
    if anchor.get("headSha256") != head or anchor["sequence"] != body["sequence"]:
        raise ValueError("Registry is not the independently pinned latest head")
    ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(
        unb64(registry["signature"]), TOMBSTONE_PREFIX + canonical(body)
    )
    if type(body["entries"]) is not list or len(body["entries"]) > 100000:
        raise ValueError("Invalid tombstone entry count")
    seen = set()
    for entry in body["entries"]:
        if set(entry) != {"kind", "subject", "recordedAtMs"} or entry["kind"] not in (
            "account-erasure-requested",
            "wallet-retired",
            "session-revoked",
        ):
            raise ValueError("Unsupported tombstone action")
        if type(entry["subject"]) is not str or not 1 <= len(entry["subject"]) <= 128:
            raise ValueError("Invalid tombstone subject")
        if (
            type(entry["recordedAtMs"]) is not int
            or not 0 <= entry["recordedAtMs"] <= body["issuedAtMs"]
        ):
            raise ValueError("Invalid tombstone time")
        if entry["kind"] == "session-revoked" and not re.fullmatch(
            "[0-9a-f]{64}", entry["subject"]
        ):
            raise ValueError("Invalid revoked session hash")
        identity = digest(canonical(entry))
        if identity in seen:
            raise ValueError("Duplicate tombstone")
        seen.add(identity)
    return body, head


def apply_restore_tombstones(
    directory: Path,
    *,
    registry: dict[str, Any] | None,
    anchor: dict[str, Any] | None,
    public_key: bytes | None,
    now_ms: int,
) -> dict[str, Any]:
    """Quarantine only: no existing application erasure flow can authorize promotion.

    Enforce revocations in the isolated SQLite copy without altering immutable
    wallet/audit records. Actual private-data erasure remains an application gap.
    """
    secure_directory(directory)
    if not (directory / "RESTORE_QUARANTINE.json").is_file():
        raise ValueError("Not an explicitly quarantined restore workspace")
    if registry is None or anchor is None or public_key is None:
        return {
            "status": "quarantined",
            "activationAllowed": False,
            "reason": "latest-tombstone-registry-unavailable",
        }
    body, head = validate_tombstones(registry, anchor, public_key, now_ms)
    path = directory / "restore.sqlite"
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Unsafe isolated restore database")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS _recovery_tombstones(id TEXT PRIMARY KEY,kind TEXT NOT NULL,subject TEXT NOT NULL,body TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS _recovery_subjects(user_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS _recovery_privacy_head(id INTEGER PRIMARY KEY CHECK(id=1),sequence INTEGER NOT NULL,hash TEXT NOT NULL)"
        )
        prior = connection.execute(
            "SELECT sequence,hash FROM _recovery_privacy_head WHERE id=1"
        ).fetchone()
        if prior and (
            body["sequence"] < prior[0] or (body["sequence"] == prior[0] and head != prior[1])
        ):
            raise ValueError("Tombstone registry rollback or same-sequence fork")
        expected = {digest(canonical(entry)) for entry in body["entries"]}
        previous = {row[0] for row in connection.execute("SELECT id FROM _recovery_tombstones")}
        if not previous <= expected:
            raise ValueError("Latest registry omitted previously applied tombstones")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "sessions" not in tables or "users" not in tables:
            raise ValueError("Restore does not have the supported account/session schema")
        before = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        users = set()
        for entry in body["entries"]:
            connection.execute(
                "INSERT OR IGNORE INTO _recovery_tombstones VALUES(?,?,?,?)",
                (
                    digest(canonical(entry)),
                    entry["kind"],
                    entry["subject"],
                    canonical(entry).decode(),
                ),
            )
            if entry["kind"] == "account-erasure-requested":
                users.add(entry["subject"])
            elif entry["kind"] == "wallet-retired":
                for table, field in (("wallet_identities", "address"), ("wallet_links", "address")):
                    if table in tables:
                        users.update(
                            row[0]
                            for row in connection.execute(
                                f"SELECT user_id FROM {table} WHERE {field}=?", (entry["subject"],)
                            )
                        )
            else:
                if "wallet_login_contexts" in tables:
                    connection.execute(
                        "UPDATE wallet_login_contexts SET revoked_at=COALESCE(revoked_at,?),active_session_hash=NULL,epoch=epoch+1 WHERE active_session_hash=?",
                        (now_ms, entry["subject"]),
                    )
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (entry["subject"],))
        for user in users:
            connection.execute("INSERT OR IGNORE INTO _recovery_subjects VALUES(?)", (user,))
            if "wallet_login_contexts" in tables and "wallet_login_challenges" in tables:
                connection.execute(
                    "UPDATE wallet_login_contexts SET revoked_at=COALESCE(revoked_at,?),active_session_hash=NULL,epoch=epoch+1 WHERE revoked_at IS NULL AND token_hash IN "
                    "(SELECT context_hash FROM sessions WHERE user_id=? UNION SELECT context_hash FROM wallet_login_challenges WHERE target_user_id=?)",
                    (now_ms, user, user),
                )
                connection.execute(
                    "UPDATE wallet_login_challenges SET revoked_at=? WHERE target_user_id=? AND revoked_at IS NULL",
                    (now_ms, user),
                )
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user,))
        for operation in ("INSERT", "UPDATE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS _recovery_block_session_{operation.lower()} BEFORE {operation} ON sessions "
                "WHEN EXISTS(SELECT 1 FROM _recovery_subjects WHERE user_id=NEW.user_id) OR EXISTS(SELECT 1 FROM _recovery_tombstones WHERE kind='session-revoked' AND subject=NEW.token_hash) "
                "BEGIN SELECT RAISE(ABORT,'restore_subject_quarantined'); END"
            )
        if "wallet_login_challenges" in tables:
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS _recovery_block_login BEFORE INSERT ON wallet_login_challenges "
                "WHEN EXISTS(SELECT 1 FROM _recovery_subjects WHERE user_id=NEW.target_user_id) OR EXISTS(SELECT 1 FROM _recovery_tombstones WHERE kind='wallet-retired' AND subject=NEW.address) "
                "BEGIN SELECT RAISE(ABORT,'restore_subject_quarantined'); END"
            )
        if "wallet_identities" in tables:
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS _recovery_block_wallet BEFORE INSERT ON wallet_identities "
                "WHEN NEW.status='active' AND EXISTS(SELECT 1 FROM _recovery_tombstones WHERE kind='wallet-retired' AND subject=NEW.address) "
                "BEGIN SELECT RAISE(ABORT,'restore_wallet_retired'); END"
            )
        connection.execute(
            "INSERT INTO _recovery_privacy_head VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence,hash=excluded.hash",
            (body["sequence"], head),
        )
        after = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Privacy replay violated foreign keys")
        connection.commit()
        return {
            "status": "quarantined",
            "activationAllowed": False,
            "registryHead": head,
            "registrySequence": body["sequence"],
            "tombstonesApplied": len(expected),
            "blockedSubjects": len(users),
            "sessionsRevoked": before - after,
            "privatePayloadErasureComplete": False,
            "applicationErasureAvailable": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("run", "resume", "apply-tombstones", "remote-init", "remote-restore")
    )
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--reference-proof", type=Path)
    parser.add_argument("--backup-id")
    parser.add_argument("--archive-sha")
    parser.add_argument("--key-fingerprint")
    parser.add_argument("--challenge")
    parser.add_argument("--restore-directory", type=Path)
    parser.add_argument("--tombstone-registry", type=Path)
    parser.add_argument("--registry-anchor", type=Path)
    parser.add_argument("--registry-public-key", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "run":
        if args.snapshot is None:
            parser.error("--snapshot is required")
        result = run_local(args.snapshot, args.reference_proof, args.backup_id)
    elif args.action == "apply-tombstones":
        if args.restore_directory is None:
            parser.error("--restore-directory is required")
        if any(
            value is None
            for value in (args.tombstone_registry, args.registry_anchor, args.registry_public_key)
        ):
            result = apply_restore_tombstones(
                args.restore_directory,
                registry=None,
                anchor=None,
                public_key=None,
                now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            )
        else:
            for source in (args.tombstone_registry, args.registry_anchor):
                if any(
                    source.resolve().is_relative_to(base)
                    for base in (ROOT / "tmp" / "backup-recovery", REMOTE_ROOT / "backups")
                ):
                    raise ValueError(
                        "Latest tombstone registry and anchor must be stored independently of backups"
                    )
            result = apply_restore_tombstones(
                args.restore_directory,
                registry=json.loads(args.tombstone_registry.read_text()),
                anchor=json.loads(args.registry_anchor.read_text()),
                public_key=unb64(args.registry_public_key.read_text().strip()),
                now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            )
    elif args.action == "resume":
        result = resume_prepared_backup(args.backup_id)
    elif args.action == "remote-init":
        result = remote_initialize(args.backup_id)
    else:
        result = remote_restore(
            args.backup_id, args.archive_sha, args.key_fingerprint, unb64(args.challenge)
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
