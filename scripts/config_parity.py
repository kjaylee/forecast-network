#!/usr/bin/env python3
"""Every configuration name a Worker reads must be one it is deployed with, and the same one
the other Worker reads.

`sql_parity.py` holds the statements to the reference and `route_parity.py` holds the surface.
Neither can see an environment read: `env.var("SOLANA_DEVNET_RPC")` is not a statement and not
a route, and it was read by the edge for a week under a name that exists in neither Worker's
`wrangler.jsonc`. In the same week the edge's deploy script pushed two secrets while the crate
read seven. This looks at exactly that.

Three sets, read from the source rather than transcribed:

  * The **edge** reads: every `var("…")`, `secret("…")`, `flag(env, "…")`, `switch(env, "…", …)`,
    `env.d1("…")`, `env.service("…")`, `env.assets("…")` and `get_binding::<T>("…")` in
    `apps/web-rs/src`, outside `#[cfg(test)]`.
  * The **reference** reads: every `getattr(env, "…")` / `getattr(self.env, "…")` /
    `getattr(bindings, "…")` and every `self.env.NAME` / `bindings.NAME` attribute in
    `apps/web/src/entry.py` and `packages/application/src`.
  * What each is **deployed with**: its `wrangler.jsonc` (`vars`, and the binding names of
    `d1_databases`, `services`, `assets`, `ai`), plus the secret list in `worker_secrets.py`.

Two failures. A name one Worker reads and the other does not, unless it is in `REVIEWED` with a
reason. A name a Worker reads that its deployment does not provide, unless it is in `OPTIONAL`
— a name whose absence the code handles by design, such as a direct API key when a relay holds
the real one.

This is a check, not a vector: run `--check`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_secrets import EDGE_WORKER_SECRETS, PYTHON_WORKER_SECRETS  # noqa: E402

EDGE_SRC = ROOT / "apps/web-rs/src"
EDGE_CONFIG = ROOT / "apps/web-rs/wrangler.jsonc"
PYTHON_SRC = [ROOT / "apps/web/src/entry.py", *sorted((ROOT / "packages/application/src").rglob("*.py"))]
PYTHON_CONFIG = ROOT / "apps/web/wrangler.jsonc"

NAME = r'"([A-Z][A-Z0-9_]+)"'
# `var(env, "X")`, `var(context.env, "X")`, `var("X")` (a local closure), `secret(env, "X")`,
# `env.secret("X")`, `flag(env, "X")`, `switch(env, "X", true)`, `env.d1("X")`, `env.service("X")`,
# `env.assets("X")`.
EDGE_READ = re.compile(r"\b(?:var|secret|flag|switch|d1|service|assets)\((?:[^()\"]*?,\s*)?" + NAME)
EDGE_BINDING = re.compile(r"get_binding::<[^>]+>\(" + NAME + r"\)")
PYTHON_GETATTR = re.compile(r"getattr\((?:self\.)?(?:_?env|bindings)[^,]*,\s*" + NAME)
PYTHON_ATTRIBUTE = re.compile(r"\b(?:self\.env|bindings)\.([A-Z][A-Z0-9_]+)\b")

# A name one Worker reads and the other does not, with the reason it is not a drift. A row
# added here without reading both sides is a hole in this check rather than a fix to it.
REVIEWED: dict[str, str] = {
    "SCHEDULED_JOBS": "The Python Worker's cron dispatched through a self-binding; the edge's scheduled "
                      "handler calls its application in-process and needs no binding and no bearer.",
    "APP_ORIGIN": "The URL the Python Worker's cron dispatches to through its self-binding; the "
                  "edge's own origin is the request's.",
    "LEGACY": "The strangler's service binding to the Python Worker: the edge's rollback path, "
              "which the reference has no counterpart for.",
}

# A name a Worker reads whose absence the code handles by design.
OPTIONAL: dict[str, str] = {
    "GEMINI_API_KEY": "Honoured only without a relay; the relay Worker holds the real key.",
    "SOLANA_MAINNET_RPC_KEYED": "Optional keyed mainnet RPC; public endpoints are the fallback.",
}


TEST_MODULE = re.compile(r"#\[cfg\(test\)\]\s*mod\s+\w+\s*\{")


def production(source: str) -> str:
    """Rust source up to its first inline `#[cfg(test)] mod … {`.

    The cut is at a test *module body*, not at the attribute: `lib.rs` declares `#[cfg(test)]
    mod golden;` near its top, and a cut there would hide every binding the entry reads.
    """
    match = TEST_MODULE.search(source)
    return source if match is None else source[:match.start()]


def edge_names(source: str) -> set[str]:
    text = production(source)
    return {match.group(1) for pattern in (EDGE_READ, EDGE_BINDING) for match in pattern.finditer(text)}


def python_names(source: str) -> set[str]:
    return {match.group(1) for pattern in (PYTHON_GETATTR, PYTHON_ATTRIBUTE) for match in pattern.finditer(source)}


def collect(paths: list[pathlib.Path], names) -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for path in paths:
        for name in names(path.read_text()):
            reads.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return reads


def edge_reads() -> dict[str, set[str]]:
    return collect(sorted(EDGE_SRC.rglob("*.rs")), edge_names)


def python_reads() -> dict[str, set[str]]:
    return collect(PYTHON_SRC, python_names)


def provided(config: dict, secrets: tuple[str, ...]) -> set[str]:
    names = set(config.get("vars", {})) | set(secrets)
    for key in ("d1_databases", "services"):
        names |= {entry["binding"] for entry in config.get(key, [])}
    for key in ("assets", "ai"):
        if isinstance(config.get(key), dict) and config[key].get("binding"):
            names.add(config[key]["binding"])
    return names


def problems(edge: dict[str, set[str]], python: dict[str, set[str]],
             edge_config: dict, python_config: dict) -> list[str]:
    """Every reason the two Workers' configuration is not in parity, in one pass."""
    edge_provided = provided(edge_config, EDGE_WORKER_SECRETS)
    python_provided = provided(python_config, PYTHON_WORKER_SECRETS)
    found: list[str] = []
    for name in sorted(set(edge) ^ set(python)):
        if name in REVIEWED:
            continue
        side, where = ("edge", edge[name]) if name in edge else ("reference", python[name])
        found.append(f"{name}: read only by the {side} ({', '.join(sorted(where))})")
    for label, reads, names in (("edge", edge, edge_provided), ("reference", python, python_provided)):
        for name in sorted(set(reads) - names - set(OPTIONAL)):
            found.append(f"{name}: read by the {label} ({', '.join(sorted(reads[name]))}) "
                         f"but not declared in its wrangler.jsonc or worker_secrets.py")
    # The two Workers serve one surface, so a var they both declare must carry one value: an edge
    # that pinned a different RPC URL or a different relayer would be a second deployment, not a
    # second implementation of the first.
    edge_vars, python_vars = edge_config.get("vars", {}), python_config.get("vars", {})
    for name in sorted(set(edge_vars) & set(python_vars)):
        if edge_vars[name] != python_vars[name]:
            found.append(f"{name}: declared with different values in the two wrangler.jsonc files")
    for name in sorted(REVIEWED):
        if name in edge and name in python:
            found.append(f"{name}: listed as a reviewed difference but both Workers read it")
    for name in sorted(OPTIONAL):
        if name not in edge and name not in python:
            found.append(f"{name}: listed as optional but neither Worker reads it")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 on a drift or a missing declaration")
    arguments = parser.parse_args()
    edge, python = edge_reads(), python_reads()
    found = problems(edge, python, json.loads(EDGE_CONFIG.read_text()), json.loads(PYTHON_CONFIG.read_text()))
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1 if arguments.check else 0
    print(f"config parity verified: {len(edge)} edge names, {len(python)} reference names, "
          f"{len(REVIEWED)} reviewed differences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
