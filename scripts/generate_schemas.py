#!/usr/bin/env python3
"""Regenerate portable contracts, or check that committed schemas have no drift."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/domain/src"))

from forecast_domain.schema import all_record_types, schema_for, schema_version  # noqa: E402


def render_schemas(version: int = 1) -> dict[str, str]:
    return _render(cls for cls in all_record_types() if schema_version(cls) == version)


def _render(types: Iterable[type[Any]]) -> dict[str, str]:
    return {f"{cls.__name__}.schema.json": json.dumps(schema_for(cls), indent=2, sort_keys=True,
                                                     ensure_ascii=False) + "\n" for cls in types}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on missing, extra or changed schemas")
    args = parser.parse_args()
    total = 0
    targets = [(f"v{version}", render_schemas(version))
               for version in sorted({schema_version(cls) for cls in all_record_types()})]
    for version, expected in targets:
        target = ROOT / f"schemas/{version}"
        existing = {path.name for path in target.glob("*.schema.json")}
        extras = existing - expected.keys()
        if args.check:
            changed = [name for name, text in expected.items()
                       if not (target / name).is_file() or (target / name).read_text() != text]
            if changed or extras:
                print(f"Schema {version} drift: changed/missing={sorted(changed)}, extra={sorted(extras)}")
                return 1
        else:
            if extras:
                parser.error(f"Obsolete schema files require an explicit version decision: {sorted(extras)}")
            target.mkdir(parents=True, exist_ok=True)
            for name, text in expected.items():
                (target / name).write_text(text, encoding="utf-8")
        total += len(expected)
    print(f"Schemas {'verified' if args.check else 'generated'}: {total}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
