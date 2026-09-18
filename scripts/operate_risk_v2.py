#!/usr/bin/env python3
"""Drive one v2 risk feed operation tick against the live Worker; run every minute under launchd.

The Worker's own cron cannot run each minute without colliding with in-flight
requests in its Python isolate, so the supervised operator host calls the
authenticated tick instead. The token comes from Keychain and is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cloudflare_keychain import secret  # noqa: E402
from heartbeat import ping as heartbeat_ping  # noqa: E402

ORIGIN = "https://forecast.eastsea.xyz"


def tick(origin: str, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(
        origin + "/api/admin/risk/v2/operate", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "forecast-risk-v2-operator/1",
                 "Authorization": "Bearer " + secret("ADMIN_TOKEN")})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
            return {"httpStatus": response.status, "seconds": round(time.time() - started, 2),
                    "feeds": body.get("data", {}).get("feeds")}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        return {"httpStatus": error.code, "seconds": round(time.time() - started, 2), "error": detail}
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        return {"httpStatus": None, "seconds": round(time.time() - started, 2), "error": type(error).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=ORIGIN)
    parser.add_argument("--timeout", type=float, default=50.0, help="Below the one-minute cadence")
    args = parser.parse_args()
    result = tick(args.origin, args.timeout)
    print(json.dumps({"event": "risk_v2_operate", "at": int(time.time() * 1000), **result}, sort_keys=True))
    ok = result["httpStatus"] == 200
    # Report to the dead-man's switch, so that this job stopping is louder than it
    # failing. A no-op until a check URL is configured; see scripts/heartbeat.py.
    heartbeat_ping("operator", failed=not ok, note=f"httpStatus={result['httpStatus']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
