"""Scheduling races, durable replay and immutable dry-run retention proposals."""

from __future__ import annotations

import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backup_recovery as backup
from scripts import backup_schedule as schedule

ROOT = Path(__file__).resolve().parents[1]


class BackupScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="schedule-test-")
        self.root = Path(self.temp.name)
        (self.root / "tmp").mkdir(mode=0o700)
        self.patches = [
            patch.object(schedule, "ROOT", self.root),
            patch.object(backup, "ROOT", self.root),
        ]
        for item in self.patches:
            item.start()
        self.config = {
            "version": 1,
            "taskId": "test-backup",
            "enabled": True,
            "intervalSeconds": 300,
            "exportTimeoutSeconds": 30,
            "backupTimeoutSeconds": 60,
            "retryDelaysSeconds": [1, 2],
            "archiveRetentionDays": 7,
            "minimumVerifiedArchives": 1,
            "exportCommand": ["/bin/cat", "{snapshot}"],
        }
        self.exports = 0
        self.backups = []
        self.seeds = {role: os.urandom(32) for role in backup.ROLES}
        self.keys = {role: backup.base58(backup.public(seed)) for role, seed in self.seeds.items()}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def export(self, config, path):
        self.exports += 1
        backup.write_new(path, b"CREATE TABLE forecasts(id TEXT);")

    def prepare(self, identifier):
        root = self.root / "tmp" / "backup-recovery"
        root.mkdir(exist_ok=True, mode=0o700)
        stage = root / identifier
        stage.mkdir(exist_ok=True, mode=0o700)
        if not (stage / "archive.fnbak").exists():
            backup.write_new(stage / "archive.fnbak", b"encrypted-test-intent")
            backup.write_new(stage / "public-pin.json", backup.canonical({"publicKeys": self.keys}))
        return stage

    def complete(self, config, snapshot, identifier, resume):
        self.backups.append(resume)
        stage = self.prepare(identifier)
        nonce = b"x" * 32
        seed_data = backup.canonical(
            {
                "cluster": "devnet",
                "genesisHash": backup.GENESIS,
                "seeds": {role: backup.b64(seed) for role, seed in self.seeds.items()},
            }
        )
        signatures = backup.sign_recovery(
            seed_data, {"backupId": identifier, "publicKeys": self.keys}, nonce
        )
        return {
            "backupId": identifier,
            "sourceSha256": backup.digest(snapshot.read_bytes()),
            "publicKeys": self.keys,
            "recovery": {
                "signatures": signatures,
                "challenge": backup.b64(nonce),
                "archiveSha256": backup.digest((stage / "archive.fnbak").read_bytes()),
                "restored": {"integrity": "ok", "foreignKeyErrors": 0},
            },
        }

    def run_task(self, now=2_000_000, exporter=None, backuper=None):
        return schedule.run_once(
            self.config,
            now_ms=now,
            exporter=exporter or self.export,
            backuper=backuper or self.complete,
        )

    def test_duplicate_interval_does_not_export_or_back_up_twice(self):
        self.assertEqual(self.run_task()["status"], "complete")
        self.assertEqual(self.run_task()["status"], "already-complete")
        self.assertEqual(self.exports, 1)
        self.assertEqual(self.backups, [False])
        self.assertEqual(self.run_task(now=2_400_000)["status"], "complete")
        self.assertEqual(self.exports, 2)

    def test_real_process_lock_blocks_overlapping_launch(self):
        nested = []

        def exporting(config, path):
            nested.append(self.run_task()["status"])
            self.export(config, path)

        self.assertEqual(self.run_task(exporter=exporting)["status"], "complete")
        self.assertEqual(nested, ["already-running"])

    def test_partial_export_retries_without_overwriting_failed_snapshot(self):
        def failing(config, path):
            backup.write_new(path, b"partial private export")
            raise RuntimeError("do not log secrets")

        self.assertEqual(self.run_task(exporter=failing)["status"], "retry-scheduled")
        self.assertEqual(self.run_task(now=2_000_500)["status"], "retry-wait")
        self.assertEqual(self.run_task(now=2_001_000)["status"], "complete")
        snapshots = list((self.root / "tmp" / "backup-schedule").rglob("snapshot.sql"))
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(any(p.read_bytes() == b"partial private export" for p in snapshots))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in snapshots))

    def test_partial_backup_resumes_same_prepared_identity_and_ciphertext(self):
        seen = []

        def failing(config, path, identifier, resume):
            seen.append(identifier)
            self.prepare(identifier)
            raise RuntimeError("transport lost after encrypted transfer")

        first = self.run_task(backuper=failing)
        self.assertEqual(first["status"], "retry-scheduled")
        second = self.run_task(now=2_001_000)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(first["backupId"], second["backupId"])
        self.assertEqual(self.backups, [True])
        self.assertEqual(self.exports, 1)

    def test_unprepared_or_changed_intent_requires_reconciliation(self):
        def failing(config, path, identifier, resume):
            raise RuntimeError("unknown initialization state")

        self.assertEqual(self.run_task(backuper=failing)["status"], "reconciliation-required")
        self.assertEqual(self.run_task(now=3_000_000)["status"], "reconciliation-required")
        self.assertEqual(self.exports, 1)

    def test_completed_receipt_reconciles_crash_before_state_update(self):
        complete = self.run_task()
        directory = schedule.task_directory(self.config["taskId"])
        state = {
            "lastCompletedSlot": None,
            "job": {
                "slot": complete["slot"],
                "backupId": complete["backupId"],
                "snapshotSha256": complete["snapshotSha256"],
                "phase": "backup-running",
                "attempts": 1,
                "nextAttemptMs": 0,
            },
        }
        schedule.save_state(directory / "state.json", state)
        replay = self.run_task()
        self.assertTrue(replay["reconciled"])
        self.assertEqual(self.exports, 1)

    def test_retention_requires_policy_keeps_verified_floor_and_never_deletes(self):
        records = [
            {
                "backupId": f"forecast-20260915T00000{i}Z-0123456789ab",
                "archiveSha256": str(i) * 64,
                "createdAtMs": i * 86400000,
                "verified": i != 0,
                "pinned": i == 1,
            }
            for i in range(4)
        ]
        plan = schedule.retention_plan(
            records, now_ms=20 * 86400000, retention_days=7, minimum_verified_archives=1
        )
        self.assertEqual([r["backupId"] for r in plan["candidates"]], [records[2]["backupId"]])
        self.assertTrue(plan["dryRun"])
        self.assertFalse(plan["deletionPerformed"])
        body = {k: v for k, v in plan.items() if k != "artifactSha256"}
        self.assertEqual(plan["artifactSha256"], backup.digest(backup.canonical(body)))
        with self.assertRaises(ValueError):
            schedule.retention_plan(
                records, now_ms=1, retention_days=None, minimum_verified_archives=1
            )

    def test_supervisor_template_is_installable_but_no_default_policy_exists(self):
        template = schedule.config_template()
        self.assertFalse(template["enabled"])
        self.assertIsNone(template["archiveRetentionDays"])
        with self.assertRaises(ValueError):
            schedule.validate_config(template)
        plist = plistlib.loads(schedule.supervisor_template(self.config, self.root / "task.json"))
        self.assertEqual(plist["StartInterval"], 300)
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(plist["Umask"], 0o077)
        self.assertIn("run-once", plist["ProgramArguments"])
