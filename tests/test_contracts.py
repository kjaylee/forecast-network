"""Wire-boundary failures and portable commitment vectors independent of providers."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Literal

from forecast_domain.errors import ValidationError
from forecast_domain.records import MAX_SAFE_INTEGER, Record
from forecast_domain.schema import schema_for
from forecast_domain.serialization import (
    canonical_bytes,
    content_hash,
    dumps,
    from_dict,
    loads,
    to_dict,
)


class Choice(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True, slots=True, kw_only=True)
class ContractFixture(Record):
    name: str = field(metadata={"maxLength": 40})
    count: int = field(metadata={"maximum": 100})
    choice: Choice
    tags: tuple[str, ...] = field(metadata={"minItems": 1, "uniqueItems": True})
    note: str | None = None
    kind: Literal["fixture"] = "fixture"


@dataclass(frozen=True, slots=True, kw_only=True)
class Envelope(Record):
    payload: ContractFixture
    trail: tuple[int, str]


def fixture() -> ContractFixture:
    return ContractFixture(name="Forecast 한", count=4, choice=Choice.YES, tags=("evidence",))


class StrictRecordTests(unittest.TestCase):
    def test_roundtrip_includes_version_defaults_enums_and_nested_records(self):
        value = Envelope(payload=fixture(), trail=(7, "accepted"))
        self.assertEqual(loads(Envelope, dumps(value)), value)
        self.assertEqual(loads(Envelope, dumps(value).encode()), value)
        self.assertEqual(to_dict(value)["payload"]["choice"], "YES")
        self.assertEqual(to_dict(value)["schema_version"], 1)

    def test_invalid_primitive_types_and_bounds(self):
        for name, invalid in (
            ("count", True), ("count", 4.0), ("count", -1), ("count", 101),
            ("count", MAX_SAFE_INTEGER + 1), ("name", " \n"), ("name", "\ud800"),
            ("name", "x" * 41), ("name", 5), ("choice", "YES"), ("choice", "INVALID"),
            ("tags", ["evidence"]), ("tags", ()), ("tags", ("same", "same")),
            ("tags", (1,)), ("kind", "other"), ("schema_version", 2), ("schema_version", True),
        ):
            with self.subTest(name=name, invalid=repr(invalid)), self.assertRaises(ValidationError):
                replace(fixture(), **{name: invalid})

    def test_frozen_records_and_recursive_immutable_containers(self):
        record = fixture()
        with self.assertRaises(FrozenInstanceError):
            record.count = 10
        with self.assertRaises(ValidationError):
            Envelope(payload=record, trail=[7, "accepted"])

    def test_strict_external_field_and_type_handling(self):
        valid = to_dict(fixture())
        variants = [
            {**valid, "unknown": "x"}, {key: value for key, value in valid.items() if key != "note"},
            {**valid, "schema_version": 0}, {**valid, "count": "4"},
            {**valid, "count": True}, {**valid, "choice": "INVALID"},
            {**valid, "tags": "evidence"}, {**valid, "tags": [None]},
            {**valid, "note": False}, {**valid, "kind": "else"},
        ]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                from_dict(ContractFixture, value)
        nested = to_dict(Envelope(payload=fixture(), trail=(7, "accepted")))
        nested["payload"]["currency"] = "USDC"
        with self.assertRaises(ValidationError):
            from_dict(Envelope, nested)

    def test_json_parser_rejects_duplicates_numbers_encoding_and_nesting(self):
        raw = dumps(fixture())
        invalid = [
            raw.replace('"count":4', '"count":4,"count":5'),
            raw.replace('"count":4', '"count":4.0'),
            raw.replace('"count":4', '"count":4e0'),
            raw.replace('"count":4', '"count":NaN'),
            raw.replace('"count":4', '"count":Infinity'),
            raw.replace('"count":4', '"count":' + "9" * 5000),
            b"\xff", "{", "[]", "null", "[" * 1500 + "]" * 1500,
            b" " * (8 * 1024 * 1024 + 1),
        ]
        for value in invalid:
            with self.subTest(raw=repr(value)[:100]), self.assertRaises(ValidationError):
                loads(ContractFixture, value)

    def test_schema_constraints_come_from_record_fields(self):
        schema = schema_for(Envelope)
        definition = schema["$defs"]["ContractFixture"]
        self.assertEqual(set(definition["properties"]), {f.name for f in fields(ContractFixture)})
        self.assertEqual(set(definition["required"]), set(definition["properties"]))
        self.assertFalse(definition["additionalProperties"])
        self.assertEqual(definition["properties"]["schema_version"]["const"], 1)
        self.assertEqual(definition["properties"]["count"]["maximum"], 100)
        self.assertEqual(definition["properties"]["choice"]["enum"], ["YES", "NO"])


class CommitmentTests(unittest.TestCase):
    def test_portable_golden_vectors(self):
        path = Path(__file__).parent / "fixtures/commitments-v1.json"
        vectors = json.loads(path.read_text())["vectors"]
        for vector in vectors:
            with self.subTest(value=vector["input"]):
                self.assertEqual(canonical_bytes(vector["input"]), vector["canonical_json"].encode())
                self.assertEqual(content_hash(vector["input"]), vector["sha256"])

    def test_key_order_unicode_normalization_and_record_field_commitments(self):
        self.assertEqual(content_hash({"b": 1, "a": 2}), content_hash({"a": 2, "b": 1}))
        self.assertNotEqual(content_hash("é"), content_hash("e\u0301"))
        self.assertEqual(canonical_bytes({"😀": 1, "\ue000": 2}), '{"\ue000":2,"😀":1}'.encode())
        self.assertNotEqual(content_hash(fixture()), content_hash(replace(fixture(), count=5)))

    def test_unsupported_hash_inputs_fail(self):
        for value in (1.0, float("nan"), float("inf"), MAX_SAFE_INTEGER + 1,
                      {1: "not a string key"}, {"x"}, b"bytes", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                canonical_bytes(value)


class RepositoryContractsTests(unittest.TestCase):
    def test_committed_schemas_have_no_drift(self):
        from scripts.generate_schemas import ROOT, render_schemas

        expected = render_schemas()
        present = {path.name for path in (ROOT / "schemas/v1").glob("*.schema.json")}
        self.assertEqual(present, set(expected))
        for name, text in expected.items():
            self.assertEqual((ROOT / "schemas/v1" / name).read_text(), text, name)
            schema = json.loads(text)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_domain_imports_only_standard_library_and_its_own_package(self):
        import ast
        import sys

        root = Path(__file__).resolve().parents[1] / "packages/domain/src/forecast_domain"
        allowed = sys.stdlib_module_names | {"forecast_domain", "__future__"}
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], allowed, str(path))
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertIn(node.module.split(".")[0], allowed, str(path))


if __name__ == "__main__":
    unittest.main()
