#!/usr/bin/env python3
"""Run deployment tooling with task-scoped Keychain credentials, never print values."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Operator-local mapping of secret names to Keychain services plus the Cloudflare
# account id. It lives outside the repository; see docs/deployment.md for the shape.
CONFIG_PATH = Path(os.environ.get("FORECAST_KEYCHAIN_CONFIG",
                                  Path.home() / ".config/forecast-network/keychain.json"))
REQUIRED = ("cloudflare", "cloudflare_email", "GEMINI_API_KEY", "SESSION_SECRET", "ADMIN_TOKEN",
            "AI_PROXY_TOKEN", "account_id")


def _config() -> dict[str, str]:
    try:
        loaded = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Operator config unavailable: {CONFIG_PATH}") from exc
    if not isinstance(loaded, dict) or any(not isinstance(v, str) or not v for v in loaded.values()):
        raise RuntimeError("Operator config must map names to non-empty strings")
    missing = [name for name in REQUIRED if name not in loaded]
    if missing:
        raise RuntimeError(f"Operator config is missing {missing}")
    return loaded


KEYCHAIN_SERVICES = {name: value for name, value in _config().items() if name != "account_id"}
ACCOUNT_ID = _config()["account_id"]


def secret(name: str) -> str:
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICES[name], "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"Required Keychain entry unavailable: {KEYCHAIN_SERVICES[name]}")
    return result.stdout.strip()


def deployment_environment() -> dict[str, str]:
    scratch = ROOT / "tmp/cloudflare"
    scratch.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ, "CLOUDFLARE_API_KEY": secret("cloudflare"),
        "CLOUDFLARE_EMAIL": secret("cloudflare_email"), "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
        "TMPDIR": str(ROOT / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
        "WRANGLER_LOG_PATH": str(scratch / "wrangler.log"),
        "WRANGLER_SEND_METRICS": "false", "CI": "true",
        "UV_CACHE_DIR": str(ROOT / "tmp/uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(ROOT / "tmp/uv-python"),
        "UV_PROJECT_ENVIRONMENT": str(ROOT / "tmp/worker-cli-venv"),
        "PATH": os.pathsep.join([str(ROOT / "tmp/cloudflare-tools/node_modules/.bin"), str(ROOT / "tmp/uv-tool/bin"), os.environ.get("PATH", "")]),
        "npm_config_cache": str(ROOT / "tmp/npm-cache"),
    }


def api(path: str, *, method: str = "GET", body: dict[str, object] | None = None) -> dict[str, object]:
    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/" + path.lstrip("/"),
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-Auth-Key": secret("cloudflare"), "X-Auth-Email": secret("cloudflare_email"),
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret", choices=[name for name in KEYCHAIN_SERVICES if name != "cloudflare"])
    parser.add_argument("--cwd", type=Path, default=ROOT)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A deployment command is required")
    result = subprocess.run(
        command, cwd=args.cwd, env=deployment_environment(),
        input=(secret(args.secret) + "\n") if args.secret else None,
        text=True, check=False,
    )
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
