#!/usr/bin/env python3
"""Supervised health monitor for the live risk pipeline; alerts on the operator host.

Checks the Worker's v2 operations health (feed age, tick failures, series schedule,
source-watch staleness) and the local keeper journal (last finalized policy action).
Findings go to stdout as JSON; a macOS notification fires when the state degrades
or recovers, so silent failures of the per-minute operator or keeper are noticed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cloudflare_keychain import secret  # noqa: E402
from deployed_drift import compare as deployment_drift  # noqa: E402
from heartbeat import ping as heartbeat_ping  # noqa: E402

ORIGIN = "https://forecast.eastsea.xyz"
KEEPER_JOURNAL = Path.home() / ".local/share/forecast-network/keeper-runtime/forecast-risk/tmp/keeper-stress/journal.sqlite3"
STATE = Path.home() / ".local/share/forecast-network/risk-v2-operator/tmp/monitor-state.json"
# Where this monitor runs from, and the repository it should match. Without
# FORECAST_REPO the comparison is skipped rather than resolved against the current
# directory, which under launchd is somewhere else entirely.
DEPLOYED = Path(__file__).resolve().parent
_REPOSITORY = os.environ.get("FORECAST_REPO", "")
REPO_SCRIPTS = Path(_REPOSITORY) / "scripts" if _REPOSITORY else None


RETRY_PAUSE_SECONDS = 5.0
HEALTH_ATTEMPTS = 3


def fetch_health(origin: str, timeout: float, attempts: int = HEALTH_ATTEMPTS) -> dict[str, object]:
    """Read the operator health view, giving a cold Worker more than one chance to answer.

    This endpoint runs the heaviest read in the system and returns 1101 on the first request
    after the Worker has been idle — reproduced directly: two 500s, then 200 in 0.7 seconds.
    Treating that as unreachable produces an alert that clears itself on the next run, which
    is the flapping this monitor was just corrected for on the series count. A retry is not
    leniency: a Worker that is genuinely down fails all of them, and the timeout still bounds
    the whole thing.
    """
    last: Exception = RuntimeError("no attempt was made")
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(RETRY_PAUSE_SECONDS)
        try:
            request = urllib.request.Request(origin + "/api/admin/risk/v2/health", headers={
                "User-Agent": "forecast-risk-monitor/1", "Authorization": "Bearer " + secret("ADMIN_TOKEN")})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)["data"]
            assert isinstance(data, dict)
            return data
        except (urllib.error.URLError, TimeoutError, ValueError, AssertionError, KeyError) as error:
            last = error
    raise last


def keeper_status(journal: Path, now_ms: int) -> dict[str, object]:
    if not journal.exists():
        return {"available": False}
    connection = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status, frame_json, created_ms FROM keeper_actions ORDER BY rowid DESC LIMIT 1").fetchone()
        finalized = connection.execute(
            "SELECT MAX(created_ms) FROM keeper_actions WHERE status='finalized'").fetchone()
    finally:
        connection.close()
    if row is None:
        return {"available": True, "actions": 0}
    frame = json.loads(row[1])
    evidence = frame.get("policy_evidence", {})
    return {
        "available": True, "lastStatus": row[0], "lastAgeMs": now_ms - row[2],
        "lastFinalizedAgeMs": now_ms - finalized[0] if finalized and finalized[0] else None,
        "feedStatus": evidence.get("feed_status"), "feedFailure": evidence.get("feed_failure"),
        "decision": frame.get("decision", {}).get("next_state"),
        "reason": frame.get("decision", {}).get("reason_code"),
    }


def evaluate(health: dict[str, object] | None, keeper: dict[str, object], now_ms: int) -> list[str]:
    """Problems that degrade the pipeline; external source failures are reported by warnings()."""
    problems: list[str] = []
    if health is None:
        problems.append("worker health endpoint unreachable")
    else:
        for feed in health.get("feeds", []):  # type: ignore[union-attr]
            if not feed["enabled"]:
                continue
            if feed["latestAgeMs"] is None or feed["latestAgeMs"] > 180_000:
                problems.append(f"feed {feed['feedId']} stale ({feed['latestAgeMs']} ms)")
            if feed["ticksLast30m"] < 20:
                problems.append(f"feed {feed['feedId']} only {feed['ticksLast30m']} ticks in 30 min")
            if feed["failedTicksLast30m"] >= 3:
                problems.append(f"feed {feed['feedId']} {feed['failedTicksLast30m']} failed ticks in 30 min")
        for series in health.get("series", []):  # type: ignore[union-attr]
            # Only attempts whose episode never published. A series that failed four times and
            # then published is the retry doing its job, and reporting it as degraded is how a
            # real outage would go unread.
            if series["enabled"] and series["failedUnpublishedAttemptsLast24h"] >= 3:
                problems.append(f"series {series['seriesId']} has {series['failedUnpublishedAttemptsLast24h']} failed attempts "
                                "and no published episode for those targets")
            nxt = series.get("nextEpisodeStartMs")
            if series["enabled"] and nxt is not None and nxt < now_ms:
                problems.append(f"series {series['seriesId']} missed episode start {nxt}")
        watch = health.get("sourceWatch") or {}
        if isinstance(watch, dict) and watch.get("stale", 0) > 0:
            problems.append(f"{watch['stale']} enabled watch sources stale")
    if not keeper.get("available"):
        problems.append("keeper journal unavailable")
    elif keeper.get("lastAgeMs") is not None and keeper["lastAgeMs"] > 600_000:  # type: ignore[operator]
        problems.append(f"keeper idle {keeper['lastAgeMs']} ms")
    elif keeper.get("feedStatus") == "unavailable":
        problems.append(f"keeper feed unavailable ({keeper.get('feedFailure')})")
    return problems


def warnings(health: dict[str, object] | None) -> list[str]:
    """Conditions worth recording that are not this pipeline failing: blocked upstream
    publishers, and forecasts that need a decision rather than a restart."""
    warned: list[str] = []
    watch = (health or {}).get("sourceWatch") or {}
    if isinstance(watch, dict) and watch.get("failing", 0) > 0:
        warned.append(f"{watch['failing']} enabled watch sources failing upstream (external)")
    stuck = (health or {}).get("stuckForecasts")
    if isinstance(stuck, int) and stuck > 0:
        # Reported, never alerted: these cannot clear themselves, so an alert would repeat
        # every five minutes forever and stop meaning anything.
        warned.append(f"{stuck} forecasts carry a job_error and nothing clears it automatically")
    return warned


def notify(title: str, text: str) -> None:
    script = f'display notification "{text[:180]}" with title "{title}"'
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=ORIGIN)
    parser.add_argument("--journal", type=Path, default=KEEPER_JOURNAL)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    now_ms = int(time.time() * 1000)
    try:
        health: dict[str, object] | None = fetch_health(args.origin, 40.0)
    except (urllib.error.URLError, TimeoutError, ValueError, AssertionError, KeyError):
        health = None
    try:
        keeper = keeper_status(args.journal, now_ms)
    except sqlite3.Error:
        keeper = {"available": False}
    problems = evaluate(health, keeper, now_ms)
    # A deployed copy that no longer matches the repository behaves like a change
    # nobody checked, which is exactly how the operator died on 2026-09-18.
    if REPO_SCRIPTS is not None and REPO_SCRIPTS.is_dir():
        problems += [f"deployment drift: {problem}" for problem in deployment_drift(DEPLOYED, REPO_SCRIPTS)]
    warned = warnings(health)
    previous = {}
    try:
        previous = json.loads(args.state.read_text())
    except (OSError, ValueError):
        pass
    degraded = bool(problems)
    if not args.no_notify and degraded != bool(previous.get("degraded", False)):
        notify("Forecast risk pipeline", ("DEGRADED: " + "; ".join(problems)) if degraded else "recovered")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps({"degraded": degraded, "at": now_ms, "problems": problems, "warnings": warned}))
    print(json.dumps({"event": "risk_pipeline_monitor", "at": now_ms, "degraded": degraded, "problems": problems,
                      "warnings": warned, "keeper": keeper, "feeds": (health or {}).get("feeds")}, sort_keys=True))
    # Report this monitor's own liveness. Without it, this process stopping and the
    # pipeline being quiet look exactly the same from anywhere else — which is how
    # a two-day outage went unnoticed. No-op until a check URL is configured.
    heartbeat_ping("monitor", failed=degraded, note="; ".join(problems)[:200])
    return 1 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
