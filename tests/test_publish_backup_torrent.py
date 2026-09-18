"""A backup archive becomes a trackerless, web-seeded torrent, or nothing happens."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.publish_backup_torrent import build

try:
    import torf  # noqa: F401
except ImportError:  # pragma: no cover - the dependency-free CI job collects this module
    raise unittest.SkipTest("torf is required: python3 -m pip install torf")

WEB_SEED = "https://forecast.eastsea.xyz/backups"


class PublishBackupTorrentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.archive = Path(self.directory.name) / "forecast-backup-2026-09-18.zip"
        self.archive.write_bytes(b"forecast-off-device-backup-v1" + bytes(range(256)) * 512)

    def tearDown(self):
        self.directory.cleanup()

    def test_an_archive_becomes_a_magnet_that_carries_its_web_seed(self):
        destination, infohash, magnet = build(self.archive, web_seed_base=WEB_SEED)
        self.assertTrue(destination.is_file())
        self.assertEqual(len(infohash), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in infohash))
        self.assertIn("xt=urn:btih:" + infohash, magnet)
        self.assertIn("ws=", magnet, "the web seed must travel in the magnet")
        self.assertIn(self.archive.name, magnet)

    def test_the_torrent_is_trackerless_but_never_private(self):
        destination, _, _ = build(self.archive, web_seed_base=WEB_SEED)
        from torf import Torrent
        written = Torrent.read(str(destination))
        self.assertEqual(list(written.trackers), [], "a tracker would reintroduce a central dependency")
        self.assertFalse(written.private, "private mode disables DHT and PEX, the only trackerless discovery")

    def test_a_trailing_slash_on_the_seed_base_never_doubles(self):
        _, _, magnet = build(self.archive, web_seed_base=WEB_SEED + "/")
        self.assertNotIn("%2F%2F" + self.archive.name, magnet)
        self.assertIn(self.archive.name, magnet)

    def test_an_absent_or_empty_archive_is_refused(self):
        for candidate in (Path(self.directory.name) / "missing.zip",):
            with self.assertRaises(SystemExit):
                build(candidate, web_seed_base=WEB_SEED)
        empty = Path(self.directory.name) / "empty.zip"
        empty.write_bytes(b"")
        with self.assertRaises(SystemExit):
            build(empty, web_seed_base=WEB_SEED)

    def test_two_runs_over_the_same_archive_agree_on_the_infohash(self):
        _, first, _ = build(self.archive, web_seed_base=WEB_SEED)
        _, second, _ = build(self.archive, web_seed_base=WEB_SEED)
        self.assertEqual(first, second, "the infohash identifies content and must be stable")


if __name__ == "__main__":
    unittest.main()
