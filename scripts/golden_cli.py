#!/usr/bin/env python3
"""The command line every golden generator shares: `--write` the vector, `--check` it is current.

Thirty-five generators carried the same twenty-line `main`, copied per file, and the copies
had begun to differ in the one place they should not — how the document is serialised. A
vector rendered with a different `default=` or without `ensure_ascii=False` is a vector that
disagrees with itself between `--write` and `--check`. There is one rendering, and this is it.

    from golden_cli import golden_main
    if __name__ == "__main__":
        raise SystemExit(golden_main(build, GOLDEN, description=__doc__))

`build` returns the document or a coroutine that does; `default` is `json.dumps`'s, for a
document that carries records.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def render(document: Any, *, default: Callable[[Any], Any] | None = None) -> str:
    """The one serialisation: sorted keys, two-space indent, non-ASCII kept, a trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, default=default) + "\n"


def golden_main(build: Callable[[], Any], golden: Path, *, description: str | None = None,
                default: Callable[[Any], Any] | None = None, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--write", action="store_true", help="rewrite the golden file")
    parser.add_argument("--check", action="store_true", help="fail if the golden file is stale")
    arguments = parser.parse_args(argv)
    produced = build()
    if inspect.iscoroutine(produced):
        produced = asyncio.run(produced)
    document = render(produced, default=default)
    relative = golden.relative_to(ROOT) if golden.is_relative_to(ROOT) else golden
    if arguments.write:
        golden.write_text(document)
        print(f"wrote {relative}")
        return 0
    if arguments.check:
        current = golden.read_text() if golden.exists() else ""
        if current != document:
            print(f"{relative} is stale; regenerate with --write", file=sys.stderr)
            return 1
        print(f"{relative} is current")
        return 0
    print(document, end="")
    return 0
