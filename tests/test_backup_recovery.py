"""Authenticated archives, path guards and off-device recovery proof semantics."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts import backup_recovery as backup

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "forecast-20260915T000000Z-0123456789ab"


class BackupRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.seeds = {role: os.urandom(32) for role in backup.ROLES}
        cls.deployment = {
            "cluster": "devnet",
            "genesisHash": backup.GENESIS,
            "relayer": backup.base58(backup.public(cls.seeds["relayer"])),
            "upgradeAuthority": backup.base58(backup.public(cls.seeds["upgrade"])),
            "programId": backup.base58(backup.public(cls.seeds["program"])),
        }
        cls.sql = (
            b'CREATE TABLE forecasts(id TEXT PRIMARY KEY); INSERT INTO forecasts VALUES("one");'
        )

    def archive(self):
        return backup.encrypt_archive(
            self.sql, self.seeds, self.deployment, self.key.public_key(), IDENTIFIER
        )

    def test_encrypted_archive_recovers_exact_paths_bytes_and_hashes(self):
        encoded, manifest = self.archive()
        for seed in self.seeds.values():
            self.assertNotIn(backup.b64(seed).encode(), encoded)
            self.assertNotIn(seed, encoded)
        self.assertNotIn(self.sql, encoded)
        files, recovered = backup.decrypt_archive(
            encoded, self.key, backup.fingerprint(self.key.public_key())
        )
        self.assertEqual(manifest, recovered)
        self.assertEqual(list(files), ["d1.sql", "devnet-roles.json"])
        self.assertEqual(files["d1.sql"], self.sql)
        self.assertEqual(manifest["pathCount"], 2)
        self.assertEqual(manifest["files"][0]["sha256"], backup.digest(self.sql))

    def test_manifest_and_ciphertext_are_authenticated_before_restore(self):
        encoded, _ = self.archive()
        for part in ("manifest", "ciphertext", "nonce", "wrappedKey"):
            value = json.loads(encoded)
            if part == "manifest":
                value[part]["files"][0]["path"] = "../../outside.sql"
            else:
                raw = bytearray(backup.unb64(value[part]))
                raw[0] ^= 1
                value[part] = backup.b64(bytes(raw))
            with self.subTest(part=part), self.assertRaises((InvalidTag, ValueError)):
                backup.decrypt_archive(
                    backup.canonical(value), self.key, backup.fingerprint(self.key.public_key())
                )

    def test_wrong_recipient_pin_and_unsupported_scope_fail(self):
        encoded, _ = self.archive()
        with self.assertRaises(ValueError):
            backup.decrypt_archive(encoded, self.key, "0" * 64)
        for change in ({"cluster": "mainnet-beta"}, {"genesisHash": "wrong"}, {"relayer": "wrong"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                backup.encrypt_archive(
                    self.sql,
                    self.seeds,
                    {**self.deployment, **change},
                    self.key.public_key(),
                    IDENTIFIER,
                )
        with self.assertRaises(ValueError):
            backup.encrypt_archive(
                self.sql,
                {**self.seeds, "risk": os.urandom(32)},
                self.deployment,
                self.key.public_key(),
                IDENTIFIER,
            )

    def test_recovered_signatures_bind_local_pins_role_set_and_random_challenge(self):
        encoded, manifest = self.archive()
        files, _ = backup.decrypt_archive(
            encoded, self.key, backup.fingerprint(self.key.public_key())
        )
        nonce = os.urandom(32)
        proof = backup.sign_recovery(files["devnet-roles.json"], manifest, nonce)
        backup.verify_recovery(proof, manifest["publicKeys"], IDENTIFIER, nonce)
        self.assertEqual(len(proof), 4)
        with self.assertRaises(InvalidSignature):
            backup.verify_recovery(proof, manifest["publicKeys"], IDENTIFIER, os.urandom(32))
        with self.assertRaises(InvalidSignature):
            backup.verify_recovery(
                proof, manifest["publicKeys"], "forecast-20260915T000001Z-0123456789ab", nonce
            )
        with self.assertRaises(ValueError):
            backup.verify_recovery([proof[0]] * 4, manifest["publicKeys"], IDENTIFIER, nonce)
        with self.assertRaises(ValueError):
            backup.verify_recovery(
                proof, {**manifest["publicKeys"], "relayer": "wrong"}, IDENTIFIER, nonce
            )

    def test_restore_is_isolated_rejects_external_sql_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="backup-test-") as temp:
            root = Path(temp)
            result = backup.restore_sql(self.sql, root / "restore")
            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(result["tableCount"], 1)
            self.assertEqual(result["forecastCount"], 1)
            self.assertEqual(result["sqliteMode"], "0o600")
            with self.assertRaises(FileExistsError):
                backup.restore_sql(self.sql, root / "restore")
            for index, sql in enumerate(
                (b"ATTACH DATABASE ':memory:' AS external;", b"PRAGMA writable_schema=ON;")
            ):
                with self.subTest(sql=index), self.assertRaises(Exception):
                    backup.restore_sql(sql, root / f"bad-{index}")

    def test_files_and_paths_are_owner_only_and_exclusive(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="backup-mode-test-") as temp:
            root = Path(temp)
            backup.secure_directory(root / "private", new=True)
            self.assertEqual((root / "private").stat().st_mode & 0o777, 0o700)
            backup.write_new(root / "private" / "cipher", b"encrypted")
            self.assertEqual((root / "private" / "cipher").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                backup.write_new(root / "private" / "cipher", b"replacement")
            (root / "private").chmod(0o755)
            with self.assertRaises(ValueError):
                backup.secure_directory(root / "private")
        for value in ("../../keys", IDENTIFIER + "/child", "forecast-wrong"):
            with self.assertRaises(ValueError):
                backup.backup_id(value)

    def test_inherited_acl_modes_are_fixed_before_private_file_write(self):
        real_open, real_fdopen = os.open, os.fdopen
        real_mkdir = Path.mkdir
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="backup-acl-test-") as temp:
            root = Path(temp)

            def inherited_open(path, flags, mode=0o777):
                descriptor = real_open(path, flags, mode)
                os.fchmod(descriptor, 0o777)
                return descriptor

            def private_fdopen(descriptor, mode):
                self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
                return real_fdopen(descriptor, mode)

            with (
                patch.object(backup.os, "open", side_effect=inherited_open),
                patch.object(backup.os, "fdopen", side_effect=private_fdopen),
            ):
                backup.write_new(root / "cipher", b"private-test-bytes")

            def inherited_mkdir(path, mode=0o777):
                real_mkdir(path, mode=mode)
                path.chmod(0o777)

            with patch.object(Path, "mkdir", inherited_mkdir):
                backup.secure_directory(root / "private", new=True)
            self.assertEqual((root / "private").stat().st_mode & 0o777, 0o700)


class RestorePrivacyTests(unittest.TestCase):
    def setUp(self):
        import sqlite3

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="privacy-restore-test-")
        self.directory = Path(self.temp.name) / "restore"
        db = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            db.executescript(migration.read_text())
        db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u-a','Alice','alice','old-recovery-hash',1)"
        )
        db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u-b','Bob','bob','other-recovery-hash',1)"
        )
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,1,100000)",
            ("a" * 64, "u-a"),
        )
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,1,100000)",
            ("b" * 64, "u-b"),
        )
        db.execute(
            "INSERT INTO wallet_identities(address,user_id,status,created_at) VALUES('retired-address','u-a','active',1)"
        )
        sql = "\n".join(db.iterdump()).encode()
        db.close()
        backup.restore_sql(sql, self.directory)
        self.key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        self.public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, entries, sequence=1):
        body = {
            "version": 1,
            "purpose": "restore-privacy-tombstones",
            "sequence": sequence,
            "issuedAtMs": 1000,
            "validUntilMs": 5000,
            "entries": entries,
        }
        envelope = {
            "body": body,
            "signature": backup.b64(
                self.key.sign(backup.TOMBSTONE_PREFIX + backup.canonical(body))
            ),
        }
        anchor = {
            "sequence": sequence,
            "headSha256": backup.digest(backup.canonical(envelope)),
            "observedAtMs": 2000,
            "maxAgeMs": 1000,
        }
        return envelope, anchor

    def apply(self, entries, sequence=1):
        registry, anchor = self.registry(entries, sequence)
        return backup.apply_restore_tombstones(
            self.directory, registry=registry, anchor=anchor, public_key=self.public, now_ms=2000
        )

    def test_missing_latest_registry_never_allows_activation(self):
        result = backup.apply_restore_tombstones(
            self.directory, registry=None, anchor=None, public_key=None, now_ms=2000
        )
        self.assertFalse(result["activationAllowed"])
        self.assertEqual(result["status"], "quarantined")
        self.assertFalse(
            json.loads((self.directory / "RESTORE_QUARANTINE.json").read_text())[
                "activationAllowed"
            ]
        )

    def test_actual_sqlite_revocation_and_guard_do_not_bypass_immutable_wallet(self):
        import sqlite3

        entries = [{"kind": "account-erasure-requested", "subject": "u-a", "recordedAtMs": 1000}]
        result = self.apply(entries)
        self.assertEqual(result["sessionsRevoked"], 1)
        self.assertFalse(result["privatePayloadErasureComplete"])
        self.assertFalse(result["activationAllowed"])
        with sqlite3.connect(self.directory / "restore.sqlite") as db:
            self.assertEqual(db.execute("SELECT user_id FROM sessions").fetchall(), [("u-b",)])
            self.assertEqual(
                db.execute(
                    "SELECT status FROM wallet_identities WHERE address='retired-address'"
                ).fetchone()[0],
                "active",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,1,10000)",
                    ("c" * 64, "u-a"),
                )
        replay = self.apply(entries)
        self.assertEqual(replay["sessionsRevoked"], 0)

    def test_retired_wallet_and_specific_session_are_reapplied(self):
        import sqlite3

        entries = [
            {"kind": "wallet-retired", "subject": "retired-address", "recordedAtMs": 1000},
            {"kind": "session-revoked", "subject": "b" * 64, "recordedAtMs": 1000},
        ]
        result = self.apply(entries)
        self.assertEqual(result["sessionsRevoked"], 2)
        with sqlite3.connect(self.directory / "restore.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,1,10000)",
                    ("b" * 64, "u-b"),
                )

    def test_registry_cannot_roll_back_omit_or_forge_tombstones(self):
        entries = [{"kind": "account-erasure-requested", "subject": "u-a", "recordedAtMs": 1000}]
        self.apply(entries, sequence=2)
        with self.assertRaises(ValueError):
            self.apply(entries, sequence=1)
        with self.assertRaises(ValueError):
            self.apply([], sequence=3)
        registry, anchor = self.registry(entries, 3)
        registry["signature"] = backup.b64(b"0" * 64)
        anchor["headSha256"] = backup.digest(backup.canonical(registry))
        with self.assertRaises(InvalidSignature):
            backup.apply_restore_tombstones(
                self.directory,
                registry=registry,
                anchor=anchor,
                public_key=self.public,
                now_ms=2000,
            )
        registry, anchor = self.registry(entries, 3)
        with self.assertRaises(ValueError):
            backup.apply_restore_tombstones(
                self.directory,
                registry=registry,
                anchor=anchor,
                public_key=self.public,
                now_ms=6000,
            )
