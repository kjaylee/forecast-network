#!/usr/bin/env python3
"""Installable backup scheduling and non-destructive retention planning.

No service is installed, retention duration invented, or archive deleted here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import backup_recovery as backup  # noqa: E402

ROOT = backup.ROOT


def positive(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Explicit positive " + name + " is required")
    return value


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if (
        type(config.get("version")) is not int
        or config.get("version") != 1
        or not re.fullmatch("[a-z][a-z0-9-]{0,47}", config.get("taskId", ""))
    ):
        raise ValueError("Invalid backup task identity")
    for name in (
        "intervalSeconds",
        "exportTimeoutSeconds",
        "backupTimeoutSeconds",
        "archiveRetentionDays",
        "minimumVerifiedArchives",
    ):
        positive(config.get(name), name)
    delays = config.get("retryDelaysSeconds")
    if type(delays) is not list or not delays or len(delays) > 10:
        raise ValueError("Explicit bounded retry delays are required")
    for delay in delays:
        positive(delay, "retry delay")
    command = config.get("exportCommand")
    if (
        type(command) is not list
        or not command
        or not all(type(arg) is str and arg for arg in command)
        or command.count("{snapshot}") != 1
    ):
        raise ValueError("Export command must be an argument array with one snapshot placeholder")
    if not Path(command[0]).is_absolute() or any(
        re.search(r"(?i)(password|api.token|secret|authorization)=", arg) for arg in command
    ):
        raise ValueError("Use an absolute executable and no secret command arguments")
    if type(config.get("enabled")) is not bool:
        raise ValueError("Explicit enabled boolean is required")
    return dict(config)


def save_state(path: Path, value: dict[str, Any]) -> None:
    temporary = path.parent / (".state-" + os.urandom(6).hex() + ".json")
    backup.write_new(temporary, backup.canonical(value))
    os.replace(temporary, path)


def task_directory(task_id: str) -> Path:
    root = ROOT / "tmp" / "backup-schedule"
    backup.secure_directory(root)
    directory = root / task_id
    backup.secure_directory(directory)
    return directory


def export_snapshot(config: dict[str, Any], destination: Path) -> None:
    command = [str(destination) if arg == "{snapshot}" else arg for arg in config["exportCommand"]]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        timeout=config["exportTimeoutSeconds"],
        check=False,
        env={
            **os.environ,
            "TMPDIR": str(ROOT / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "WRANGLER_LOG_PATH": str(destination.parent / "wrangler.log"),
        },
    )
    if completed.returncode or not destination.is_file():
        raise RuntimeError("Read-only snapshot export failed")
    destination.chmod(0o600)
    if not 0 < destination.stat().st_size <= backup.MAX_SQL:
        raise ValueError("Export is empty or too large")


def execute_backup(
    config: dict[str, Any], snapshot: Path, identifier: str, resume: bool
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts/backup_recovery.py"),
        "resume" if resume else "run",
        "--backup-id",
        identifier,
    ]
    if not resume:
        command.extend(["--snapshot", str(snapshot)])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        timeout=config["backupTimeoutSeconds"],
        check=False,
        env={**os.environ, "TMPDIR": str(ROOT / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode:
        raise RuntimeError("Backup did not return a verified result")
    return dict(json.loads(completed.stdout))


def validate_proof(proof: dict[str, Any], job: dict[str, Any]) -> None:
    if proof["backupId"] != job["backupId"] or proof["sourceSha256"] != job["snapshotSha256"]:
        raise ValueError("Recovery proof differs from frozen scheduled snapshot")
    recovery = proof["recovery"]
    stage = ROOT / "tmp" / "backup-recovery" / job["backupId"]
    pin = json.loads((stage / "public-pin.json").read_text())
    if (
        pin["publicKeys"] != proof["publicKeys"]
        or backup.digest((stage / "archive.fnbak").read_bytes()) != recovery["archiveSha256"]
    ):
        raise ValueError("Recovery proof differs from pinned encrypted intent")
    backup.verify_recovery(
        recovery["signatures"],
        proof["publicKeys"],
        job["backupId"],
        backup.unb64(recovery["challenge"]),
    )
    if recovery["restored"]["integrity"] != "ok" or recovery["restored"]["foreignKeyErrors"] != 0:
        raise ValueError("Recovery integrity was not established")


def run_once(
    config: dict[str, Any], *, now_ms: int, exporter=export_snapshot, backuper=execute_backup
) -> dict[str, Any]:
    config = validate_config(config)
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("Invalid scheduler clock")
    if not config["enabled"]:
        return {"status": "disabled", "mutations": 0}
    directory = task_directory(config["taskId"])
    descriptor = os.open(
        directory / "run.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    os.fchmod(descriptor, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already-running"}
        state_path = directory / "state.json"
        state = (
            json.loads(state_path.read_text())
            if state_path.exists()
            else {"lastCompletedSlot": None, "job": None}
        )
        slot = now_ms // (config["intervalSeconds"] * 1000)
        job = state["job"]
        if job is None:
            if state["lastCompletedSlot"] is not None and slot <= state["lastCompletedSlot"]:
                return {"status": "already-complete", "slot": slot}
            identifier = (
                "forecast-"
                + datetime.fromtimestamp(slot * config["intervalSeconds"], timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ"
                )
                + "-"
                + hashlib.sha256(f"{config['taskId']}:{slot}".encode()).hexdigest()[:12]
            )
            job = {
                "slot": slot,
                "backupId": identifier,
                "phase": "export",
                "attempts": 0,
                "nextAttemptMs": now_ms,
                "configSha256": backup.digest(backup.canonical(config)),
            }
            state["job"] = job
            save_state(state_path, state)
        if job.get("configSha256", backup.digest(backup.canonical(config))) != backup.digest(
            backup.canonical(config)
        ):
            raise ValueError("Pending backup configuration changed; reconcile or use a new task ID")
        completed_receipt = directory / (str(job["slot"]) + "-completed.json")
        if completed_receipt.exists():
            prior = json.loads(completed_receipt.read_text())
            if prior["backupId"] != job["backupId"] or prior["snapshotSha256"] != job.get(
                "snapshotSha256"
            ):
                raise ValueError("Completed receipt does not match pending job")
            state.update(lastCompletedSlot=job["slot"], job=None)
            save_state(state_path, state)
            return {**prior, "reconciled": True}
        if job["phase"] == "reconciliation-required":
            return {"status": "reconciliation-required", "backupId": job["backupId"]}
        if now_ms < job["nextAttemptMs"]:
            return {"status": "retry-wait", "nextAttemptMs": job["nextAttemptMs"]}
        job["attempts"] += 1
        try:
            if job["phase"] == "export":
                attempt = directory / f"{job['slot']}-{job['attempts']}"
                backup.secure_directory(attempt, new=True)
                snapshot = attempt / "snapshot.sql"
                exporter(config, snapshot)
                if (
                    snapshot.is_symlink()
                    or not snapshot.is_file()
                    or snapshot.stat().st_mode & 0o077
                ):
                    raise ValueError("Export is not a private regular file")
                job.update(
                    snapshot=str(snapshot),
                    snapshotSha256=backup.digest(snapshot.read_bytes()),
                    phase="backup-ready",
                )
                save_state(state_path, state)
            snapshot = Path(job["snapshot"])
            if backup.digest(snapshot.read_bytes()) != job["snapshotSha256"]:
                job["phase"] = "reconciliation-required"
                save_state(state_path, state)
                raise ValueError("Frozen scheduled snapshot changed")
            resume = job["phase"] == "backup-running"
            job["phase"] = "backup-running"
            save_state(state_path, state)
            proof = backuper(config, snapshot, job["backupId"], resume)
            validate_proof(proof, job)
            report = {
                "status": "complete",
                "slot": job["slot"],
                "backupId": job["backupId"],
                "snapshotSha256": job["snapshotSha256"],
                "archiveSha256": proof["recovery"]["archiveSha256"],
                "proofSha256": backup.digest(backup.canonical(proof)),
                "activationAllowed": False,
            }
            backup.write_new(
                directory / (str(job["slot"]) + "-completed.json"), backup.canonical(report)
            )
            state.update(lastCompletedSlot=job["slot"], job=None)
            save_state(state_path, state)
            return report
        except Exception as error:
            attempt = job["attempts"]
            if job["phase"] == "backup-running":
                stage = ROOT / "tmp" / "backup-recovery" / job["backupId"]
                if (
                    not (stage / "archive.fnbak").is_file()
                    or not (stage / "public-pin.json").is_file()
                ):
                    job["phase"] = "reconciliation-required"
            if attempt > len(config["retryDelaysSeconds"]):
                job["phase"] = "reconciliation-required"
            if job["phase"] != "reconciliation-required":
                job["nextAttemptMs"] = now_ms + config["retryDelaysSeconds"][attempt - 1] * 1000
            job["lastErrorType"] = type(error).__name__
            save_state(state_path, state)
            return {
                "status": job["phase"]
                if job["phase"] == "reconciliation-required"
                else "retry-scheduled",
                "backupId": job["backupId"],
                "nextAttemptMs": job["nextAttemptMs"],
                "errorType": type(error).__name__,
            }
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def retention_plan(
    archives: list[dict[str, Any]],
    *,
    now_ms: int,
    retention_days: int,
    minimum_verified_archives: int,
) -> dict[str, Any]:
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("Invalid retention clock")
    positive(retention_days, "archive retention days")
    positive(minimum_verified_archives, "minimum verified archives")
    verified = sorted(
        (a for a in archives if a.get("verified") is True),
        key=lambda a: (a["createdAtMs"], a["backupId"]),
        reverse=True,
    )
    keep = {a["backupId"] for a in verified[:minimum_verified_archives]}
    candidates = []
    for item in archives:
        backup.backup_id(item["backupId"])
        if (
            type(item.get("createdAtMs")) is not int
            or item["createdAtMs"] > now_ms
            or (
                item.get("verified") is True
                and not re.fullmatch("[0-9a-f]{64}", item.get("archiveSha256") or "")
            )
        ):
            raise ValueError("Retention inventory lacks a valid timestamp/hash")
        if (
            item.get("verified") is True
            and not item.get("pinned")
            and item["backupId"] not in keep
            and now_ms - item["createdAtMs"] > retention_days * 86400000
        ):
            candidates.append(
                {
                    "backupId": item["backupId"],
                    "archiveSha256": item["archiveSha256"],
                    "reason": "explicit-retention-exceeded",
                }
            )
    plan = {
        "version": 1,
        "dryRun": True,
        "deletionPerformed": False,
        "asOf": now_ms,
        "retentionDays": retention_days,
        "minimumVerifiedArchives": minimum_verified_archives,
        "inventorySha256": backup.digest(
            backup.canonical(sorted(archives, key=lambda item: item["backupId"]))
        ),
        "candidates": sorted(candidates, key=lambda item: item["backupId"]),
        "excludedKinds": [
            "unverified-or-failed-archives",
            "wrapping-keys",
            "tombstone-registry",
            "source-database",
            "source-export",
        ],
    }
    return {**plan, "artifactSha256": backup.digest(backup.canonical(plan))}


def archive_inventory() -> list[dict[str, Any]]:
    """Read local ciphertext/pins and retained off-device receipts; never delete."""
    records = []
    for stage in sorted((ROOT / "tmp" / "backup-recovery").glob("forecast-*")):
        identifier = backup.backup_id(stage.name)
        created = int(
            datetime.strptime(identifier[9:25], "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
            * 1000
        )
        archive = stage / "archive.fnbak"
        row = {
            "backupId": identifier,
            "createdAtMs": created,
            "verified": False,
            "pinned": False,
            "archiveSha256": None,
            "verificationSource": "local-ciphertext-and-retained-off-device-proof",
        }
        if archive.is_file() and not archive.is_symlink():
            row["archiveSha256"] = backup.digest(archive.read_bytes())
            try:
                proof = json.loads((stage / "proof.json").read_text())
                validate_proof(
                    proof, {"backupId": identifier, "snapshotSha256": proof["sourceSha256"]}
                )
                row["verified"] = True
            except (OSError, ValueError, KeyError, TypeError):
                row["verified"] = False
        records.append(row)
    return records


def config_template() -> dict[str, Any]:
    return {
        "version": 1,
        "taskId": "forecast-backup",
        "enabled": False,
        "intervalSeconds": None,
        "exportTimeoutSeconds": None,
        "backupTimeoutSeconds": None,
        "retryDelaysSeconds": None,
        "archiveRetentionDays": None,
        "minimumVerifiedArchives": None,
        "exportCommand": [
            str(ROOT / "apps/web/node_modules/.bin/wrangler"),
            "d1",
            "export",
            "forecast-network-enam",
            "--remote",
            "--output",
            "{snapshot}",
            "--config",
            str(ROOT / "apps/web/wrangler.jsonc"),
        ],
    }


def supervisor_template(config: dict[str, Any], config_path: Path) -> bytes:
    config = validate_config(config)
    directory = task_directory(config["taskId"])
    for name in ("supervisor.log", "supervisor-error.log"):
        if not (directory / name).exists():
            backup.write_new(directory / name, b"")
    return plistlib.dumps(
        {
            "Label": "com.forecast.backup." + config["taskId"],
            "ProgramArguments": [
                sys.executable,
                str(ROOT / "scripts/backup_schedule.py"),
                "run-once",
                "--config",
                str(config_path.resolve()),
            ],
            "WorkingDirectory": str(ROOT),
            "StartInterval": config["intervalSeconds"],
            "RunAtLoad": False,
            "ProcessType": "Background",
            "Umask": 0o077,
            "StandardOutPath": str(directory / "supervisor.log"),
            "StandardErrorPath": str(directory / "supervisor-error.log"),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("template", "inventory", "run-once", "supervisor", "retention-plan")
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--inventory", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "inventory":
        print(json.dumps(archive_inventory(), sort_keys=True))
        return
    if args.action == "template":
        print(json.dumps(config_template(), indent=2))
        return
    if args.config is None or args.config.is_symlink() or args.config.stat().st_mode & 0o077:
        raise ValueError("An explicit owner-only task configuration is required")
    config = validate_config(json.loads(args.config.read_text()))
    if args.action == "supervisor":
        sys.stdout.buffer.write(supervisor_template(config, args.config))
        return
    now_ms = int(time.time() * 1000)
    if args.action == "retention-plan":
        if args.inventory is None:
            raise ValueError("A verified archive inventory is required")
        result = retention_plan(
            json.loads(args.inventory.read_text()),
            now_ms=now_ms,
            retention_days=config["archiveRetentionDays"],
            minimum_verified_archives=config["minimumVerifiedArchives"],
        )
    else:
        result = run_once(config, now_ms=now_ms)
    print(json.dumps(result, sort_keys=True))
    if result.get("status") in ("retry-scheduled", "reconciliation-required"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
