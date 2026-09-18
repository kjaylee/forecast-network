"""A share becomes a page someone can print, understand, and store elsewhere."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.recovery_key_shares import encode_share, fingerprint, split
from scripts.render_share_sheet import render

SECRET = bytes(range(48)) + b"recovery key material for the sheet test"


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "share-2-of-5.json"
        self.digest = fingerprint(SECRET)
        index, values = split(SECRET, shares=5, threshold=3)[1]
        self.path.write_text(encode_share(index, values, shares=5, threshold=3,
                                          key_fingerprint=self.digest))

    def tearDown(self):
        self.temp.cleanup()

    def test_the_sheet_states_which_share_it_is_and_how_many_are_needed(self):
        page = render(self.path)
        self.assertIn("share 2 of 5", page)
        self.assertIn("3 of them", page)
        self.assertIn("<td>3 shares</td>", page)

    def test_the_fingerprint_is_on_the_page_so_a_rebuild_can_be_checked_against_it(self):
        self.assertIn(self.digest, render(self.path))

    def test_the_value_carries_the_whole_share_so_it_can_be_typed_back_in(self):
        page = render(self.path)
        stored = json.loads(self.path.read_text())
        self.assertIn(stored["value"], page)
        self.assertIn(stored["format"], page)

    def test_it_warns_against_storing_this_beside_another_share(self):
        page = render(self.path)
        self.assertIn("Do not keep this with another share", page)

    def test_it_says_what_the_share_opens(self):
        self.assertIn("forecast-network/backups", render(self.path))

    def test_a_file_that_is_not_a_share_is_refused_rather_than_rendered_blank(self):
        bad = Path(self.temp.name) / "bad.json"
        for content in ("{}", "not json", '{"format": "something-else"}'):
            with self.subTest(content=content[:20]):
                bad.write_text(content)
                with self.assertRaises((ValueError, TypeError)):
                    render(bad)


if __name__ == "__main__":
    unittest.main()
