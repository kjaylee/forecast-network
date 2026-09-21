#!/usr/bin/env python3
"""Deploy the reviewed Cloudflare app using credentials read from macOS Keychain."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from build_web import ROOT, STAGE, build
from cloudflare_keychain import deployment_environment, secret
from worker_secrets import secret_payload


def run(command: list[str], *, cwd: Path, env: dict[str, str], input_text: str | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, input=input_text, text=True, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Use workers.dev before the custom domain")
    parser.add_argument("--dry-run", action="store_true", help="Validate bundle without remote mutations")
    parser.add_argument("--skip-checks", action="store_true", help="Only after fresh checks from this exact source")
    args = parser.parse_args()
    env = deployment_environment()
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / "tmp/uv-python")
    uv = ROOT / "tmp/uv-tool/bin/uv"
    wrangler = ROOT / "tmp/cloudflare-tools/node_modules/.bin/wrangler"
    if not uv.is_file() or not wrangler.is_file():
        parser.error("Install the pinned deployment tools as documented in docs/deployment.md")
    if not args.skip_checks:
        run([sys.executable, "scripts/check.py", "--tools"], cwd=ROOT, env=env)
        tests = [str(path) for path in sorted((ROOT / "apps/web/tests").glob("*.mjs"))]
        run(["node", "--test", *tests], cwd=ROOT, env=env)
        run(["ruff", "check", "apps/web/src"], cwd=ROOT, env=env)
    build(preview=args.preview)
    link = STAGE / "node_modules"
    if not link.exists():
        link.symlink_to(ROOT / "tmp/cloudflare-tools/node_modules", target_is_directory=True)
    local_secrets = STAGE / ".dev.vars"
    if local_secrets.exists():
        local_secrets.unlink()
    python = sys.executable
    cli = [str(uv), "run", "--python", python, "pywrangler"]
    run([str(wrangler), "whoami"], cwd=STAGE, env=env)
    if args.dry_run:
        run([*cli, "deploy", "--dry-run", "--outdir", str(ROOT / "tmp/cloudflare/upload-check")],
            cwd=STAGE, env=env)
        return
    config = json.loads((STAGE / "wrangler.jsonc").read_text())
    # The bound database is the migration target; the name lives only in wrangler.jsonc.
    run([str(wrangler), "d1", "migrations", "apply", config["d1_databases"][0]["database_name"], "--remote"],
        cwd=STAGE, env=env)
    # The Gemini relay is a separate region-placed Worker; it alone holds the Gemini key.
    proxy = ROOT / "apps/ai-proxy"
    run([str(wrangler), "deploy"], cwd=proxy, env=env)
    run([str(wrangler), "secret", "bulk"], cwd=proxy, env=env,
        input_text=json.dumps({"PROXY_TOKEN": secret("AI_PROXY_TOKEN"), "GEMINI_API_KEY": secret("GEMINI_API_KEY")}))
    payload = secret_payload(config)
    run([str(wrangler), "secret", "bulk"], cwd=STAGE, env=env, input_text=json.dumps(payload))
    del payload
    run([*cli, "deploy"], cwd=STAGE, env=env)
    for name in ("uv.lock", "pylock.toml"):
        if (STAGE / name).is_file():
            shutil.copy2(STAGE / name, ROOT / "apps/web" / name)
    print("Cloudflare deployment complete; verify health, data, documents and domain before reporting success.")


if __name__ == "__main__":
    main()
