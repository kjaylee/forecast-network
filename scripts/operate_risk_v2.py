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


def loop(origin: str, timeout: float, cadence: float) -> int:
    """Tick from this process instead of from a scheduler.

    launchd's StartInterval fired irregularly enough that the feed's
    inter-publication gap exceeded its 120-second expiry for 16% of publications —
    measured across 400 publications in D1 and cross-checked against this log. The
    long gaps did not follow long ticks (correlation 0.23), so the scheduler was
    skipping starts, not the work overrunning. A process that sleeps until its own
    next tick takes launchd out of the per-tick path.

    It gives up rather than hanging. A cycle that overruns its cadence several times
    over means something is stuck, and exiting lets the supervisor start clean.
    """
    while True:
        started = time.monotonic()
        result = tick(origin, timeout)
        elapsed = time.monotonic() - started
        ok = result["httpStatus"] == 200
        print(json.dumps({"event": "risk_v2_operate", "at": int(time.time() * 1000),
                          "loopSeconds": round(elapsed, 2), **result}, sort_keys=True), flush=True)
        heartbeat_ping("operator", failed=not ok, note=f"httpStatus={result['httpStatus']}")
        if elapsed > cadence * 3:
            print(json.dumps({"event": "risk_v2_operator_cycle_too_long",
                              "loopSeconds": round(elapsed, 2)}), flush=True)
            return 1
        time.sleep(max(1.0, cadence - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=ORIGIN)
    parser.add_argument("--timeout", type=float, default=50.0, help="Below the one-minute cadence")
    parser.add_argument("--loop", action="store_true",
                        help="Keep ticking in this process for a supervisor, instead of one tick per launchd start")
    parser.add_argument("--cadence", type=float, default=60.0, help="Seconds between ticks in --loop")
    args = parser.parse_args()
    if args.loop:
        return loop(args.origin, args.timeout, args.cadence)
    result = tick(args.origin, args.timeout)
    print(json.dumps({"event": "risk_v2_operate", "at": int(time.time() * 1000), **result}, sort_keys=True))
    ok = result["httpStatus"] == 200
    # Report to the dead-man's switch, so that this job stopping is louder than it
    # failing. A no-op until a check URL is configured; see scripts/heartbeat.py.
    heartbeat_ping("operator", failed=not ok, note=f"httpStatus={result['httpStatus']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
