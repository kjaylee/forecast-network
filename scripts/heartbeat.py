#!/usr/bin/env python3
"""Report success to a dead-man's switch, so that stopping is louder than failing.

A poller asks "is it broken?". A dead-man's switch asks "did it report at all?".
The failure this pipeline actually had — a job that silently stopped running for
two days — is invisible to the first question and immediate in the second.

Unconfigured is a no-op that exits 0, so this can ship before any account
exists: the moment a check URL is written to the config, reporting starts.

Config: ~/.config/forecast-network/heartbeat.json

    {"operator": "https://hc-ping.com/<uuid>", "monitor": "https://hc-ping.com/<uuid>"}

Override the path with FORECAST_HEARTBEAT_CONFIG. A ping is a courtesy, never a
precondition: any failure to send one is reported on stderr and swallowed, so a
monitoring service being down can never take the pipeline down with it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("FORECAST_HEARTBEAT_CONFIG")
              or Path.home() / ".config/forecast-network/heartbeat.json")
USER_AGENT = "forecast-heartbeat/1"
TIMEOUT_SECONDS = 10


def checks(path: Path = CONFIG) -> dict[str, str]:
    """The configured check URLs. A missing, unreadable or malformed file means none."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {name: url for name, url in loaded.items()
            if isinstance(name, str) and isinstance(url, str) and url.startswith(("http://", "https://"))}


def target(name: str, *, failed: bool = False, path: Path = CONFIG) -> str | None:
    """The URL to call for one named check, or None when it is not configured."""
    url = checks(path).get(name)
    if url is None:
        return None
    return url.rstrip("/") + "/fail" if failed else url


def ping(name: str, *, failed: bool = False, note: str | None = None,
         path: Path = CONFIG, timeout: float = TIMEOUT_SECONDS) -> bool:
    """Best-effort. Returns whether a ping was actually sent, never raises."""
    url = target(name, failed=failed, path=path)
    if url is None:
        return False
    body = (note or "").encode("utf-8")[:400]
    request = urllib.request.Request(url, data=body or None, method="POST",
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        print(json.dumps({"event": "heartbeat_failed", "check": name,
                          "errorType": type(error).__name__}), file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, help="Which configured check to report to")
    parser.add_argument("--fail", action="store_true", help="Report failure rather than success")
    parser.add_argument("--note", default=None, help="Short text carried with the ping")
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    sent = ping(args.check, failed=args.fail, note=args.note, path=args.config)
    print(json.dumps({"event": "heartbeat", "check": args.check, "failed": args.fail,
                      "sent": sent, "configured": target(args.check, path=args.config) is not None},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
