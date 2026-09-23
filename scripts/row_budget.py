#!/usr/bin/env python3
"""Which route spends the database's daily row budget, from this host's own log.

D1's free tier grants 5,000,000 rows read a day and this service reaches it: on 2026-09-22
by 20:00Z, on 2026-09-23 by mid-morning, after which every statement fails and the feed, the
sweep and the AI publication path all stop together under one cause. The per-minute tick was
measured at 61 rows over 20 statements — some 88,000 a day — so the budget goes somewhere the
tick cannot see.

The edge counts rows per route and serves them at `/api/admin/ops/d1`; the pipeline monitor
reads that every five minutes and writes it into its own log. This aggregates those samples.

Each sample is one isolate's view, and Cloudflare runs many, so no single sample is the day.
Summing them is not the day either — it is a sample of it. What survives that is the ranking
and `rowsPerRequest`, which is what decides where an index goes: a route reading three hundred
rows a request says so in every isolate that served it.

    python3.11 scripts/row_budget.py
    python3.11 scripts/row_budget.py --since 2026-09-24T00:00:00Z
"""

from __future__ import annotations

import argparse
import calendar
import collections
import json
import time
from pathlib import Path

LOG = Path.home() / ".local/share/forecast-network/risk-v2-operator/tmp/com.forecast-network.risk-pipeline-monitor.out.log"


def samples(path: Path, since_ms: int) -> list[dict]:
    """Every `rowBudget` a monitor run recorded at or after `since_ms`."""
    found = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"rowBudget"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        budget = entry.get("rowBudget")
        if isinstance(budget, dict) and int(entry.get("at", 0)) >= since_ms:
            found.append(budget)
    return found


def totals(found: list[dict]) -> list[tuple[str, int, int, int]]:
    """(path, rows, requests, rows per request) across the samples, heaviest first."""
    rows: collections.Counter[str] = collections.Counter()
    requests: collections.Counter[str] = collections.Counter()
    for budget in found:
        for row in budget.get("paths", []):
            path = str(row.get("path", "?"))
            rows[path] += int(row.get("rows", 0))
            requests[path] += int(row.get("requests", 0))
    ranked = sorted(rows.items(), key=lambda pair: -pair[1])
    return [(path, count, requests[path], count // max(1, requests[path])) for path, count in ranked]


def parse_since(text: str | None) -> int:
    """Midnight UTC today by default: the budget resets there, so the day starts there."""
    if text:
        return int(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) * 1000)
    now = time.gmtime()
    return int(calendar.timegm((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, 0)) * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=LOG)
    parser.add_argument("--since", help="UTC instant as 2026-09-24T00:00:00Z; default is today's reset")
    args = parser.parse_args()
    if not args.log.is_file():
        print(f"no monitor log at {args.log}")
        return 1
    found = samples(args.log, parse_since(args.since))
    if not found:
        print("no rowBudget samples in the window; the monitor writes one every five minutes")
        return 1
    print(f"{len(found)} samples")
    print(f"{'rows':>10}  {'requests':>9}  {'per request':>11}  route")
    for path, rows, requests, each in totals(found):
        print(f"{rows:>10}  {requests:>9}  {each:>11}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
