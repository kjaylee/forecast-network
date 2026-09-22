#!/usr/bin/env python3
"""Deploy the Rust edge Worker (apps/web-rs) next to the Python Worker.

Without --take-domain the edge stays on workers.dev for parity checks; the Python Worker keeps
the custom domain. With --take-domain the custom domain moves to the edge Worker (the Python
Worker must have been deployed without the route first, see docs/plans/2026-09-16-rust-rewrite.md).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_web import ROOT, STAGE, build
from cloudflare_keychain import deployment_environment
from worker_secrets import secret_payload

SOURCE = ROOT / "apps/web-rs"
EDGE_STAGE = ROOT / "tmp/edge-build"


def run(command: list[str], *, cwd: Path, env: dict[str, str], input_text: str | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True, input=input_text, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--take-domain", action="store_true", help="Attach forecast.eastsea.xyz to the edge Worker")
    parser.add_argument("--preview", action="store_true",
                        help="Deploy as forecast-network-edge-preview (workers.dev only) for route parity checks")
    parser.add_argument("--skip-checks", action="store_true")
    parser.add_argument("--triggers-only", action="store_true",
                        help="Apply the config's crons and routes without rebuilding or redeploying the Worker")
    args = parser.parse_args()
    if args.triggers_only and not args.take_domain:
        # `wrangler triggers deploy` applies the config's *whole* trigger set, routes included.
        # The base config carries no `routes` — the custom domain is attached by --take-domain —
        # so applying it alone would detach forecast.eastsea.xyz from the Worker that serves it.
        # Requiring the flag makes the domain part of what is re-asserted rather than dropped.
        parser.error("--triggers-only re-asserts the whole trigger set; pass --take-domain so the "
                     "custom domain is part of it")
    env = deployment_environment()
    env["PATH"] = str(Path.home() / ".cargo/bin") + os.pathsep + env.get("PATH", os.environ.get("PATH", ""))
    wrangler = ROOT / "tmp/cloudflare-tools/node_modules/.bin/wrangler"
    if not wrangler.is_file():
        parser.error("Install the pinned deployment tools as documented in docs/deployment.md")
    if not args.skip_checks and not args.triggers_only:
        run(["cargo", "fmt", "--check"], cwd=SOURCE, env=env)
        run(["cargo", "clippy", "--target", "wasm32-unknown-unknown", "--", "-D", "warnings"], cwd=SOURCE, env=env)
        run([sys.executable, "scripts/check.py"], cwd=ROOT, env=env)
    if not args.triggers_only:
        build()  # the same rendered assets the Python Worker serves
        run(["worker-build", "--release"], cwd=SOURCE, env=env)
    if EDGE_STAGE.exists():
        shutil.rmtree(EDGE_STAGE)
    EDGE_STAGE.mkdir(parents=True)
    if args.triggers_only:
        # Nothing is uploaded, but wrangler validates the whole config before applying the
        # triggers and refuses an `assets.directory` that does not exist. An empty one is
        # honest here: this command must not be able to publish assets.
        (EDGE_STAGE / "public").mkdir()
    else:
        shutil.copytree(SOURCE / "build", EDGE_STAGE / "build")
        shutil.copytree(STAGE / "public", EDGE_STAGE / "public")
    config = json.loads((SOURCE / "wrangler.jsonc").read_text())
    # The edge is deployed with head sampling at 1.0 for the LEGACY retirement window: every
    # request the binding still answers is logged as `forwarded_to_legacy`, and that log is the
    # evidence for retiring it. At 0.1, nine in ten of those requests were dropped before they
    # were written, so a week of silence was a week at one-tenth sensitivity. Return the config
    # to 0.1 once the binding is gone — and not before, whatever the log volume costs.
    assert config["observability"]["head_sampling_rate"] == 1.0, "retirement window: log every request"
    config.pop("build", None)  # already built above; wrangler must not rebuild inside the stage
    if args.preview:
        config["name"] += "-preview"
        config.pop("routes", None)
    if args.take_domain:
        config["routes"] = [{"pattern": "forecast.eastsea.xyz", "custom_domain": True}]
    (EDGE_STAGE / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    link = EDGE_STAGE / "node_modules"
    link.symlink_to(ROOT / "tmp/cloudflare-tools/node_modules", target_is_directory=True)
    run([str(wrangler), "whoami"], cwd=EDGE_STAGE, env=env)
    if args.triggers_only:
        # Say what will be applied before applying it: this command can detach a domain.
        run([str(wrangler), "triggers", "deploy", "--dry-run"], cwd=EDGE_STAGE, env=env)
        run([str(wrangler), "triggers", "deploy"], cwd=EDGE_STAGE, env=env)
        print("Triggers applied: crons %s, routes %s" % (config["triggers"]["crons"], config.get("routes")))
        return
    run([str(wrangler), "deploy"], cwd=EDGE_STAGE, env=env)
    # The edge serves the whole surface now, so it is deployed with the whole secret set — the
    # same one the Python Worker has, held in `worker_secrets.py` where `config_parity.py` can
    # compare it with what the crate reads.
    run([str(wrangler), "secret", "bulk"], cwd=EDGE_STAGE, env=env, input_text=json.dumps(secret_payload(config)))
    print("Edge Worker deployed; compare /api/status, /api/health and /api/risk/v2/feeds/* against the Python Worker.")


if __name__ == "__main__":
    main()
