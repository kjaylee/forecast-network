#!/usr/bin/env python3
"""Dependency-free root verification; use --tools for installed Ruff and mypy."""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]
sys.dont_write_bytecode = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", action="store_true", help="Also require installed Ruff and mypy")
    args = parser.parse_args()
    task_tmp = ROOT / "tmp"
    task_tmp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(task_tmp)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONPATH"] = os.pathsep.join([
        str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT),
    ])
    # Parse every source file without emitting bytecode outside tmp/.
    files = [
        *ROOT.glob("packages/**/*.py"), *ROOT.glob("tests/**/*.py"),
        *ROOT.glob("scripts/*.py"), *ROOT.glob("apps/web/src/**/*.py"),
    ]
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"Syntax verified: {len(files)} Python files", flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    commands = [[sys.executable, "scripts/generate_schemas.py", "--check"],
                # The edge Worker mirrors the Python queries by hand, so the two have to be
                # compared by something other than a person remembering to.
                [sys.executable, "scripts/sql_parity.py", "--check"],
                # The article parser decides whether evidence can be placed relative to
                # participation, which decides whether a reward may be credited.
                [sys.executable, "scripts/generate_article_golden.py", "--check"]]
    if args.tools:
        for tool in ("ruff", "mypy"):
            if shutil.which(tool) is None:
                parser.error(f"{tool} is unavailable; run dependency-free checks without --tools")
        commands.extend([
            ["ruff", "check", "packages", "tests", "scripts", "apps/web/src"],
            ["mypy", "--strict", "packages/domain/src", "packages/application/src"],
        ])
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
