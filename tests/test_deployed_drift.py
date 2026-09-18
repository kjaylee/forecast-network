"""Deployment drift is reported, because a copied file that was never checked looks fine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.deployed_drift import compare


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "scripts"
        self.deployed = root / "deployed"
        self.source.mkdir()
        self.deployed.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, directory: Path, name: str, text: str) -> None:
        (directory / name).write_text(text)

    def test_an_identical_directory_reports_nothing(self):
        for name in ("a.py", "b.py"):
            self.write(self.source, name, "same\n")
            self.write(self.deployed, name, "same\n")
        self.assertEqual(compare(self.deployed, self.source), [])

    def test_a_module_the_deployed_copy_imports_but_never_got_is_reported(self):
        # This is the 2026-09-18 failure exactly: the script was copied, the module it
        # had just started importing was not, and the process died on every start.
        self.write(self.source, "operator.py", "from heartbeat import ping\n")
        self.write(self.deployed, "operator.py", "from heartbeat import ping\n")
        self.write(self.source, "heartbeat.py", "def ping(): ...\n")
        self.assertEqual(compare(self.deployed, self.source),
                         ["operator.py imports heartbeat, which is not deployed"])

    def test_the_module_is_not_reported_once_it_is_deployed(self):
        self.write(self.source, "operator.py", "import heartbeat\n")
        self.write(self.deployed, "operator.py", "import heartbeat\n")
        self.write(self.source, "heartbeat.py", "def ping(): ...\n")
        self.write(self.deployed, "heartbeat.py", "def ping(): ...\n")
        self.assertEqual(compare(self.deployed, self.source), [])

    def test_standard_library_imports_are_never_reported(self):
        self.write(self.source, "operator.py", "import json, sys\nfrom pathlib import Path\n")
        self.write(self.deployed, "operator.py", "import json, sys\nfrom pathlib import Path\n")
        self.assertEqual(compare(self.deployed, self.source), [])

    def test_a_repository_file_nobody_deployed_is_not_drift(self):
        # scripts/ ships far more than any one deployment directory needs.
        self.write(self.source, "operator.py", "x = 1\n")
        self.write(self.deployed, "operator.py", "x = 1\n")
        self.write(self.source, "unrelated_tool.py", "y = 2\n")
        self.assertEqual(compare(self.deployed, self.source), [])

    def test_a_stale_deployed_copy_is_reported(self):
        self.write(self.source, "operator.py", "new behaviour\n")
        self.write(self.deployed, "operator.py", "old behaviour\n")
        self.assertEqual(compare(self.deployed, self.source),
                         ["operator.py: deployed copy differs from the repository"])

    def test_a_deployed_file_with_no_source_is_reported(self):
        self.write(self.source, "a.py", "same\n")
        self.write(self.deployed, "a.py", "same\n")
        self.write(self.deployed, "leftover.py", "orphan\n")
        self.assertEqual(compare(self.deployed, self.source),
                         ["leftover.py: deployed but no longer in the repository"])

    def test_a_missing_directory_is_reported_rather_than_crashing(self):
        self.assertEqual(compare(Path(self.temp.name) / "absent", self.source),
                         [f"{Path(self.temp.name) / 'absent'} is not a directory"])

    def test_non_python_files_are_ignored(self):
        self.write(self.source, "a.py", "same\n")
        self.write(self.deployed, "a.py", "same\n")
        self.write(self.deployed, "notes.txt", "not a module\n")
        self.assertEqual(compare(self.deployed, self.source), [])


if __name__ == "__main__":
    unittest.main()
