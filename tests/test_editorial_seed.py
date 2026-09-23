"""Whose failure a day without a publication is.

For six of nine days in September the daily seed produced candidates and the service refused
every one — 409 `ai_work_in_progress`, 500 `non_json`, 502 `artifact_invalid`, 503
`source_temporarily_unavailable`. Each of those days ended in a red run and a mail, for a
condition this job neither caused nor can fix, until the mail stopped being read. The exit
code now says who is answerable, and this is the test of that sentence.
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from scripts import editorial_seed

TOKEN = "t" * 64
QUESTION = "Will example.com publish a press release announcing a thing by 2026-12-31?"


def run(*, candidates, seeded, argv=("--count", "1")):
    """`main()` with the proposal and the service both replaced, returning (code, output)."""
    printed: list[str] = []
    with patch.object(editorial_seed, "existing_questions", lambda _token: []), \
            patch.object(editorial_seed, "propose", lambda *_args, **_kwargs: list(candidates)), \
            patch.object(editorial_seed, "seed", lambda *_args: seeded), \
            patch.object(editorial_seed.time, "sleep", lambda _seconds: None), \
            patch.object(sys, "argv", ["editorial_seed.py", *argv]), \
            patch("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args))), \
            patch.dict("os.environ", {"ADMIN_TOKEN": TOKEN, "GEMINI_API_KEY": "k"}):
        return editorial_seed.main(), "\n".join(printed)


class EditorialSeedExitTests(unittest.TestCase):
    def test_a_published_question_is_a_success(self):
        code, output = run(candidates=[QUESTION], seeded=("published", "forecast-1", 201))
        self.assertEqual(code, 0)
        self.assertIn("published 1/1", output)

    def test_a_service_that_refuses_every_candidate_is_the_service_s_alarm(self):
        """The run ends green and says so: the watchdog and the standing issue carry this."""
        for status, code_name in ((409, "ai_work_in_progress"), (500, "non_json"),
                                  (502, "artifact_invalid"), (503, "source_temporarily_unavailable")):
            with self.subTest(status=status):
                exit_code, output = run(candidates=[QUESTION], seeded=("skipped", f"{status} {code_name}", status))
                self.assertEqual(exit_code, 0)
                self.assertIn("the service refused every candidate", output)

    def test_a_candidate_the_service_will_never_accept_is_still_a_failure(self):
        """A 4xx that is not 409 is this job sending something wrong, and stays red."""
        code, _ = run(candidates=[QUESTION], seeded=("skipped", "400 invalid_request", 400))
        self.assertEqual(code, 1)

    def test_producing_nothing_to_offer_is_this_job_s_own_failure(self):
        code, _ = run(candidates=[], seeded=("published", "unused", 201))
        self.assertEqual(code, 1)

    def test_missing_credentials_are_reported_before_anything_is_proposed(self):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "", "GEMINI_API_KEY": ""}), \
                patch.object(sys, "argv", ["editorial_seed.py"]):
            self.assertEqual(editorial_seed.main(), 2)


if __name__ == "__main__":
    unittest.main()
