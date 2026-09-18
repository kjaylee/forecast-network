#!/usr/bin/env python3
"""Compare what is deployed against what the repository says should be.

On 2026-09-18 the operator's script was copied to its runtime directory without
the module it had just started importing. launchd kept starting a process that
died on `ModuleNotFoundError`, the per-minute feed trigger stopped, and nobody
noticed for ten minutes — the feed only stayed alive because a separate fallback
had been built the day before.

The failure was not the missing file. It was that deploying meant copying files
and reloading, with nothing that looked at the result. This looks at the result.

    python3 scripts/deployed_drift.py                     # the operator directory
    python3 scripts/deployed_drift.py --dir <path> ...    # anywhere else

Exits 1 listing what is missing, what differs, and what is deployed but no
longer exists in the repository, so a copy-and-reload can be checked the same
way a test run is.
"""

from __future__ import annotations

import argparse
import ast
import filecmp
import sys
from pathlib import Path

REPO_SCRIPTS = Path(__file__).resolve().parent
DEFAULT_DEPLOYED = Path.home() / ".local/share/forecast-network/risk-v2-operator"


def sibling_modules(path: Path, source: Path) -> set[str]:
    """Modules this file imports that the repository also ships as a module.

    A deployment directory holds a subset of scripts/, not a mirror, so "not
    deployed" is only meaningful for something a deployed file actually imports.
    """
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return set()
    wanted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            wanted.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            wanted.add(node.module.split(".")[0])
    return {name for name in wanted if (source / f"{name}.py").is_file()}


def compare(deployed: Path, source: Path) -> list[str]:
    """Everything that would make the deployed copy behave differently."""
    problems: list[str] = []
    if not deployed.is_dir():
        return [f"{deployed} is not a directory"]

    deployed_files = {path.name for path in deployed.glob("*.py")}

    for name in sorted(deployed_files):
        origin = source / name
        if not origin.is_file():
            problems.append(f"{name}: deployed but no longer in the repository")
        elif not filecmp.cmp(deployed / name, origin, shallow=False):
            problems.append(f"{name}: deployed copy differs from the repository")

    # The 2026-09-18 failure: a script was copied, the module it had just started
    # importing was not, and every start died on ModuleNotFoundError.
    for name in sorted(deployed_files):
        for module in sorted(sibling_modules(deployed / name, source)):
            if f"{module}.py" not in deployed_files:
                problems.append(f"{name} imports {module}, which is not deployed")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, action="append", default=None,
                        help="A deployed directory to compare against scripts/")
    parser.add_argument("--source", type=Path, default=REPO_SCRIPTS)
    args = parser.parse_args()
    directories = args.dir or [DEFAULT_DEPLOYED]

    drifted = False
    for directory in directories:
        problems = compare(directory, args.source)
        if problems:
            drifted = True
            print(f"{directory}:")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"{directory}: in step with {args.source}")
    if drifted:
        print("\nDeployed copies are not what the repository says they are. A change that was "
              "copied and never checked looks exactly like this.", file=sys.stderr)
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
