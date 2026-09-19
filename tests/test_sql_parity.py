"""The parity harness, tested against the things that made it wrong before.

Writing it was a sequence of false alarms: a normalization that stripped single quotes
destroyed the SQL string literals it was supposed to protect, a brace rule applied to whole
files ate the Python side, and blanking Rust char literals turned `('FINALIZED','ARCHIVED')`
into `('FINALIZED ARCHIVED')` so a correct statement read as drift. Each of those is a test
here, because a harness that reports drift that is not there is worse than none.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sql_parity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CanonicalTests(unittest.TestCase):
    def test_whitespace_between_tokens_is_not_part_of_the_statement(self):
        self.assertEqual(sql_parity.canonical("SELECT a, b FROM t WHERE c=?"),
                         sql_parity.canonical("SELECT a,b\n  FROM t\n WHERE c = ?"))

    def test_a_comma_or_space_inside_a_string_literal_is(self):
        # The line-wrap difference that made correct statements look like drift.
        self.assertEqual(sql_parity.canonical("IN ('A','B')"), sql_parity.canonical("IN ('A','B')"))
        self.assertNotEqual(sql_parity.canonical("IN ('A','B')"), sql_parity.canonical("IN ('A B')"))

    def test_a_runtime_value_is_a_hole_whichever_language_wrote_it(self):
        self.assertEqual(sql_parity.canonical("... WHERE id IN ({identifiers})"),
                         sql_parity.canonical("... WHERE id IN ({})"))


class RustExtractionTests(unittest.TestCase):
    def test_adjacent_literals_are_one_statement(self):
        source = 'let sql = "SELECT a FROM t "\n    "WHERE b=? ";\n'
        self.assertEqual([text for _, text in sql_parity.rust_statements(source)],
                         ["SELECT a FROM t  WHERE b=?"])

    def test_a_char_literal_cannot_splice_unrelated_code_into_a_statement(self):
        # `trim_matches('"')` put a quote in the middle of a run of literals and produced a
        # statement that was really Rust code. It is the one char literal that can.
        source = ("let a = value.trim_matches('\"').to_string();\n"
                  "let b = \"SELECT x FROM t WHERE y=?\";\n")
        self.assertEqual([text for _, text in sql_parity.rust_statements(source)],
                         ["SELECT x FROM t WHERE y=?"])

    def test_sql_quotes_inside_a_statement_survive_extraction(self):
        source = "let sql = \"SELECT 1 FROM f WHERE state IN ('FINALIZED','ARCHIVED')\";\n"
        statement = sql_parity.rust_statements(source)[0][1]
        self.assertIn("('FINALIZED','ARCHIVED')", sql_parity.canonical(statement))

    def test_the_line_number_points_at_the_statement(self):
        source = "let a = 1;\nlet b = 2;\nlet sql = \"SELECT x FROM t WHERE y=?\";\n"
        self.assertEqual(sql_parity.rust_statements(source)[0][0], 3)


class PythonExtractionTests(unittest.TestCase):
    def test_implicit_concatenation_is_read_as_one_statement(self):
        source = 'sql = ("SELECT a "\n       "FROM t "\n       "WHERE b=?")\n'
        self.assertEqual(sql_parity.python_statements(source), ["SELECT a FROM t WHERE b=?"])

    def test_an_fstring_hole_becomes_a_marker(self):
        # The marker is the same one Rust format holes become, so a value supplied at
        # runtime on either side compares equal rather than reading as a difference.
        source = 'sql = f"SELECT a FROM {table} WHERE b=?"\n'
        extracted = sql_parity.python_statements(source)
        self.assertEqual(sql_parity.canonical(extracted[0]),
                         sql_parity.canonical("SELECT a FROM {} WHERE b=?"))

    def test_non_sql_strings_are_not_collected(self):
        source = 'greeting = "hello there, this is not a query at all"\n'
        self.assertEqual(sql_parity.python_statements(source), [])


class ComparisonTests(unittest.TestCase):
    def corpus(self, *statements):
        return [sql_parity.canonical(item) for item in statements]

    def test_a_statement_python_no_longer_runs_is_not_accounted_for(self):
        self.assertFalse(sql_parity.accounted_for(
            "SELECT a FROM t WHERE b=?", self.corpus("SELECT a FROM t WHERE c=?")))

    def test_a_statement_python_still_runs_is_accounted_for(self):
        self.assertTrue(sql_parity.accounted_for(
            "SELECT a FROM t WHERE b=?", self.corpus("SELECT a FROM t WHERE b=? AND d=?")))

    def test_a_hole_statement_is_accounted_for_by_its_fixed_pieces(self):
        # Python builds this one with `+`, so no single literal carries it whole.
        assembled = self.corpus("INSERT INTO mutation_guards(token,valid) ",
                                ") THEN 1 ELSE 0 END")
        self.assertTrue(sql_parity.accounted_for(
            "INSERT INTO mutation_guards(token,valid) {guard}) THEN 1 ELSE 0 END", assembled))

    def test_a_hole_statement_whose_pieces_are_gone_is_not_accounted_for(self):
        self.assertFalse(sql_parity.accounted_for(
            "INSERT INTO mutation_guards(token,valid) {guard}) THEN 1 ELSE 0 END",
            self.corpus("UPDATE something_else SET a=1")))

    def test_the_repository_is_actually_in_parity(self):
        # The harness passing is the claim; this is the claim being true right now.
        entries = sql_parity.corpus()
        self.assertGreater(len(entries), 100, "the Python corpus must be read, not empty")
        canon_only = [entry[2] for entry in entries]
        drifted = [(path.name, line)
                   for path in sorted(sql_parity.RUST_SOURCES.glob("*.rs"))
                   for line, statement in sql_parity.rust_statements(path.read_text(encoding="utf-8"))
                   if not sql_parity.accounted_for(statement, canon_only)]
        self.assertEqual(drifted, [], "the edge Worker has drifted from the Python Worker")


if __name__ == "__main__":
    unittest.main()
