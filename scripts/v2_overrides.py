#!/usr/bin/env python3
"""Every method an upgraded record *overrides* must be dispatched on the variant.

The reference is Python, so its v2 records are subclasses and their differences from v1 are
**virtual method overrides**: `EarlyResolution(Resolution)` replaces `_validate_evidence_time`, and
`ForecastV2(Forecast)` replaces `_transition_result`, `_resolution_not_before_ms` and `validate`. A
port that models the two as an enum — which is what this one does — has to dispatch on the variant
at every call site. Where it does not, the v1 rule silently applies to a v2 record.

That is not hypothetical. `resolution_timing::check` called `Resolution::validate_for` on an
`AnyResolution`, so the ordinary rule — evidence collected after the forecast expired — applied to
early proposals, which are judged *before* expiry by construction. Every one of them was refused,
and the golden vectors did not reach the arm. The audit that found the *other* v2 defect (enumerate
`isinstance(..., ForecastV2)` against the port's `.base()` uses) would not have found this one: an
override is not an `isinstance`.

So this enumerates the overrides and the Rust dispatch each one needs. The table is a reviewed
decision, not a proof: a new override in Python, or a removed one, fails the check until somebody
says where the port dispatches it.

Run `--check`.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOMAIN = ROOT / "packages/domain/src/forecast_domain"

# Every override of a v1 record, and where the port dispatches it. `None` means the override is not
# reachable through the base type in this crate — the method only exists on the v2 type — which is
# itself a decision, and one that has to be re-made if the v2 type starts being used through a base.
DISPATCHED: dict[str, dict[str, str | None]] = {
    "EarlyResolution": {
        "_validate_evidence_time": "AnyResolution::validate_for -> EarlyResolution::validate_for",
        "validate": "AnyResolution::validate -> EarlyResolution::validate",
        "decision_input_hash": None,
    },
    "ForecastV2": {
        "_transition_result": "lifecycle::apply -> Snapshot::V2 branch",
        "_resolution_not_before_ms": "Snapshot::resolution_not_before_ms",
        "validate": "Snapshot::validate -> ForecastV2::validate",
    },
}


def classes() -> dict[str, tuple[list[str], set[str]]]:
    found: dict[str, tuple[list[str], set[str]]] = {}
    for path in sorted(DOMAIN.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                methods = {
                    m.name
                    for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                found[node.name] = (bases, methods)
    return found


def overrides() -> dict[str, set[str]]:
    """The methods each record redefines of a *concrete* base record.

    A class under `Record` that defines `validate` is implementing an abstract method, not
    overriding behaviour, and there are dozens of those. What matters is a class whose base is
    itself a record with behaviour: `ForecastV2(Forecast)`, `EarlyResolution(Resolution)`. Those
    are the subclasses whose differences from v1 are virtual, and those are what this tables.
    """
    found = classes()
    out: dict[str, set[str]] = {}
    for name, (bases, methods) in found.items():
        inherited: set[str] = set()
        for base in bases:
            if base == "Record" or base not in found:
                continue
            inherited |= found[base][1]
        shared = methods & inherited
        if shared:
            out[name] = shared
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when an override is unreviewed")
    arguments = parser.parse_args()
    if not arguments.check:
        parser.error("this script only reports drift; pass --check")

    found = overrides()
    problems = []
    for name, methods in sorted(found.items()):
        for method in sorted(methods):
            if name not in DISPATCHED:
                problems.append(f"{name}.{method} overrides a v1 method and is not in the table")
            elif method not in DISPATCHED[name]:
                problems.append(f"{name}.{method} overrides a v1 method and has no dispatch recorded")
    for name, entries in sorted(DISPATCHED.items()):
        for method in sorted(entries):
            if method not in found.get(name, set()):
                problems.append(f"{name}.{method} is in the table and no longer overrides anything")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"v2 overrides: {len(problems)} unreviewed", file=sys.stderr)
        return 1
    reviewed = sum(len(methods) for methods in found.values())
    print(f"v2 overrides verified: {reviewed} overrides, each with a reviewed dispatch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
