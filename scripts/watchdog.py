#!/usr/bin/env python3
"""Observe the public service from outside every one of its own failure domains.

The pipeline monitor runs on the operator host, so it cannot report that host
being down, and nothing polls the Worker's health at all. A monitor that shares
a fate with what it watches is not a monitor.

This runs on a GitHub runner instead. The operator host, Cloudflare and GitHub
are three separate ways to fail, and this observes the first two from the third.
It reads only public endpoints, so it needs no credential and can live in a
public workflow.

Exits 0 when everything answers and the risk feed is current, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

ORIGIN = "https://forecast.eastsea.xyz"
FEED_ID = "devnet-stable-risk-v2"
USER_AGENT = "forecast-watchdog/1 (+https://github.com/kjaylee/forecast-network)"

# The operator publishes every minute and the feed expires after two. Five
# minutes is five missed publications in a row, which is not a slow tick, it is
# an outage. It is deliberately tighter than the ten minutes this started at:
# the check only runs every five minutes, so the threshold plus the cadence is
# what a dead operator host actually costs in detection time.
FEED_STALE_MS = 300_000
HTTP_TIMEOUT_SECONDS = 30


def fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def observe(origin: str, timeout: float) -> dict[str, Any]:
    """Read the three public surfaces, tolerating any single one being down."""
    observed: dict[str, Any] = {"health": None, "status": None, "feed": None}
    for key, path in (("health", "/api/health"), ("status", "/api/status"),
                      ("feed", "/api/risk/v2/feeds/" + quote(FEED_ID, safe=""))):
        try:
            body = fetch_json(origin + path, timeout)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            continue
        observed[key] = body.get("data") if isinstance(body, dict) else None
    return observed


def evaluate(observed: dict[str, Any], *, now_ms: int, feed_stale_ms: int = FEED_STALE_MS) -> list[str]:
    """Everything that is wrong right now, in the terms a reader would use."""
    problems: list[str] = []

    health = observed.get("health")
    if health is None:
        problems.append("the health endpoint did not answer")
    elif not health.get("ok"):
        problems.append("the health endpoint answered that it is not ok")

    if observed.get("status") is None:
        problems.append("the status endpoint did not answer")

    feed = observed.get("feed")
    if feed is None:
        problems.append("the risk feed did not answer")
        return problems

    status = feed.get("status")
    if status != "current":
        problems.append(f"the risk feed reports status {status!r} rather than 'current'")

    payload = ((feed.get("envelope") or {}).get("payload") or {})
    issued = payload.get("issued_at_ms")
    server = feed.get("serverTime")
    if type(issued) is not int:
        problems.append("the risk feed carries no issuance time")
    else:
        reference = server if type(server) is int else now_ms
        age = reference - issued
        if age > feed_stale_ms:
            problems.append(f"the risk feed was last issued {age / 1000:.0f}s ago")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=ORIGIN)
    parser.add_argument("--timeout", type=float, default=HTTP_TIMEOUT_SECONDS)
    args = parser.parse_args()
    observed = observe(args.origin, args.timeout)
    problems = evaluate(observed, now_ms=int(time.time() * 1000))
    print(json.dumps({"event": "watchdog", "origin": args.origin, "healthy": not problems,
                      "problems": problems}, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
