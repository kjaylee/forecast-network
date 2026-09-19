#!/usr/bin/env python3
"""Drive the official-source sweep from the operator host, every five minutes.

The Worker's own cron is supposed to do this and fails on most calls — measured
at 4 of 6, then 1 of 5 — and the phases show a cold start among the causes. When
it fails, no source is polled, and sources have sat ten to thirteen hours past
their `next_poll` with `failure_count` still zero, which is the signature of
polls that were never attempted rather than polls that failed.

This adds attempts from a host that is not Pyodide. It does not fix the Worker;
it gives the same request more chances, which is what a sweep that succeeds one
time in five needs most.

Deliberately a separate process from `operate_risk_v2.py`. A sweep can take
seventy seconds and the feed tick runs every sixty against a two-minute expiry,
so sharing a loop would let the sweep push the feed past its own deadline. Two
jobs with two cadences, and neither can delay the other.
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


def sweep(origin: str, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(
        origin + "/api/admin/sweep", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "forecast-sweep-trigger/1",
                 "Authorization": "Bearer " + secret("ADMIN_TOKEN")})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
            data = body.get("data", {})
            return {"httpStatus": response.status, "seconds": round(time.time() - started, 2),
                    "polled": data.get("sources", {}).get("polled"),
                    "phases": data.get("phaseMs")}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        return {"httpStatus": error.code, "seconds": round(time.time() - started, 2), "error": detail}
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        return {"httpStatus": None, "seconds": round(time.time() - started, 2),
                "error": type(error).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=ORIGIN)
    parser.add_argument("--timeout", type=float, default=240.0,
                        help="A sweep has been measured at seventy seconds; leave room")
    args = parser.parse_args()
    result = sweep(args.origin, args.timeout)
    print(json.dumps({"event": "sweep_trigger", "at": int(time.time() * 1000), **result}, sort_keys=True))
    ok = result["httpStatus"] == 200
    # The dead-man's switch watches this job's absence as well as the operator's, so a
    # sweep trigger that stops running is distinguishable from a Worker that stopped
    # answering. No-op until a check URL is configured; see scripts/heartbeat.py.
    heartbeat_ping("sweep", failed=not ok, note=f"httpStatus={result['httpStatus']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
