#!/usr/bin/env python3
"""Take over the feed tick when the operator host has already stopped.

The per-minute operator runs on the operator's own Mac, and nothing else drives
the risk feed. If that host stops, the feed goes stale inside its 120-second
expiry and stays stale until a person notices — which is what happened for two
days in September.

This runs from GitHub's runners, a failure domain the operator does not own, and
it acts *only* when the primary has already failed. On a healthy feed it reads
one public endpoint and stops, so the normal path carries no extra traffic.

It triggers through the scoped scheduler credential, which can start an
operation tick and cannot administer anything else. Alerting is not this
script's job: `scripts/watchdog.py` owns that, and duplicating it here would
make a Cloudflare blip look like two problems.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ORIGIN = "https://forecast.eastsea.xyz"
FEED_PATH = "/api/risk/v2/feeds/devnet-stable-risk-v2"
OPERATE_PATH = "/api/admin/risk/v2/operate"
USER_AGENT = "forecast-feed-fallback/1 (+https://github.com/kjaylee/forecast-network)"

# The feed expires after 120 seconds and is reissued every 60. Three minutes is
# three missed publications: the operator has stopped, not stumbled.
STALE_AFTER_SECONDS = 180
HTTP_TIMEOUT_SECONDS = 30


def fetch(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
          timeout: float = HTTP_TIMEOUT_SECONDS) -> Any:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def feed_age_seconds(payload: Any, now_ms: int) -> float | None:
    """Age from the feed's own clock, or None when the payload does not carry one."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    issued = ((data.get("envelope") or {}).get("payload") or {}).get("issued_at_ms")
    if type(issued) is not int:
        return None
    reference = data.get("serverTime")
    return ((reference if type(reference) is int else now_ms) - issued) / 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=ORIGIN)
    parser.add_argument("--stale-after", type=float, default=STALE_AFTER_SECONDS)
    args = parser.parse_args()
    now_ms = int(time.time() * 1000)

    try:
        age = feed_age_seconds(fetch(args.origin + FEED_PATH), now_ms)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        # Observation failed, which is not this script's problem to report.
        print(json.dumps({"event": "feed_fallback", "observed": False,
                          "errorType": type(error).__name__}))
        return 0

    if age is None:
        print(json.dumps({"event": "feed_fallback", "observed": False, "reason": "no issuance time"}))
        return 0
    if age <= args.stale_after:
        print(json.dumps({"event": "feed_fallback", "observed": True, "acted": False,
                          "ageSeconds": round(age, 1)}))
        return 0

    token = os.environ.get("SCHEDULER_TOKEN", "")
    if len(token) < 32:
        print(json.dumps({"event": "feed_fallback", "observed": True, "acted": False,
                          "ageSeconds": round(age, 1), "reason": "scheduler credential unavailable"}))
        return 1
    try:
        result = fetch(args.origin + OPERATE_PATH, data=b"{}",
                       headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        print(json.dumps({"event": "feed_fallback", "observed": True, "acted": True,
                          "ageSeconds": round(age, 1), "errorType": type(error).__name__}))
        return 1
    print(json.dumps({"event": "feed_fallback", "observed": True, "acted": True,
                      "ageSeconds": round(age, 1), "result": (result or {}).get("data")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
