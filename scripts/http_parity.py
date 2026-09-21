#!/usr/bin/env python3
"""Compare what two deployed Workers *answer*, route by route, before a surface moves.

`route_parity.py` proves every route the reference serves is one the edge claims. A claim is
not an answer: the read flip was preceded by 39/43 byte comparisons against a preview Worker,
and nothing equivalent existed for the writes. This is that comparison, for the whole surface.

Every case is one a Worker refuses or answers *without a session* — there is no credential
here and nothing to mutate with. For each write route: the wrong content type, a missing
origin, a body that is not an object, and an anonymous well-formed request. For each admin
route: no bearer and a wrong bearer. For each public read: the read itself, with the volatile
keys removed. The status, the error code and the message are compared exactly; a data body is
compared as parsed JSON, because the two runtimes serialise JSON with different spacing and
the clients parse it.

    python3 scripts/http_parity.py --edge https://<preview>.workers.dev \\
                                   --reference https://<python>.workers.dev

Exit 1 on any difference that is not in `REVIEWED`. The anonymous write cases count against
each Worker's per-client rate limit (180 an hour), so the script is not something to loop.
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

import route_parity  # noqa: E402

# Keys whose value is the request's own clock or the runtime's, and cannot agree.
VOLATILE = {"serverTime", "generatedAt", "observedAt", "retryAfter", "expiresAt", "nowMs", "asOf",
            # The edge is 0.13.0 by design: the version is the surface's, and the reference's 0.12.x
            # is the Worker behind it. Compared by config_parity's var check instead.
            "version"}

# (method, path, case) rows whose difference was read and accepted, with the reason.
REVIEWED: dict[tuple[str, str, str], str] = {}

PUBLIC_READS = [
    "/api/health", "/api/status", "/api/forecasts", "/api/forecasts?state=OPEN&sort=recent",
    "/api/points", "/api/me", "/api/activity", "/api/wallet", "/api/billing/estimate",
    "/api/me/markets", "/api/risk/feeds/", "/api/risk/v2/feeds/",
    "/api/profile-cards/" + "0" * 64, "/api/creators/nobody",
]
DETAIL_READS = ["/api/forecasts/{id}", "/api/forecasts/{id}/integrity", "/api/forecasts/{id}/market",
                "/api/forecasts/{id}/translation", "/api/forecasts/{id}/market/receipt"]
# Writes the entry serves under a forecast id, which route_parity's surface lists as prefixes.
DETAIL_WRITES = ["/api/forecasts/{id}/comments", "/api/forecasts/{id}/disputes", "/api/forecasts/{id}/follow",
                 "/api/forecasts/{id}/translation", "/api/forecasts/{id}/market/quote",
                 "/api/forecasts/{id}/market/fill", "/api/forecasts/{id}"]


class Answer:
    def __init__(self, status: int, body: bytes):
        self.status = status
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"_raw": body[:200].decode("utf-8", "replace")}
        self.body = scrub(parsed)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Answer) and (self.status, self.body) == (other.status, other.body)

    def __repr__(self) -> str:
        return f"{self.status} {json.dumps(self.body, sort_keys=True)[:300]}"


def differences_between(a, b, path: str = "") -> list[str]:
    """Every leaf where two parsed bodies differ, as a path — the diff a truncated dump hides."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}/{key}: only on the {'reference' if key not in a else 'edge'}")
            else:
                out += differences_between(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items on the edge, {len(b)} on the reference"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in differences_between(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: edge {json.dumps(a)[:60]} != reference {json.dumps(b)[:60]}"]


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in sorted(value.items()) if k not in VOLATILE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


# Cloudflare's own page for a Worker that threw before it answered. The Python Worker produces it
# when Pyodide refuses a second promising task on the isolate — the failure this port exists to
# remove — so it is retried here rather than compared, and counted, because the count is evidence.
RUNTIME_FAILURES: dict[str, int] = {"edge": 0, "reference": 0}


def call(label: str, origin: str, method: str, path: str, *, headers: dict[str, str] | None = None,
         body: bytes | None = None) -> Answer:
    request = urllib.request.Request(origin + path, method=method, data=body,
                                     headers={"User-Agent": "forecast-http-parity/1", **(headers or {})})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return Answer(response.status, response.read())
        except urllib.error.HTTPError as error:
            raw = error.read()
            if error.code == 500 and raw.startswith(b"error code: 1101") and attempt < 3:
                RUNTIME_FAILURES[label] += 1
                time.sleep(1.5)
                continue
            return Answer(error.code, raw)
    raise AssertionError("unreachable")


def browser(origin: str, content_type: str = "application/json") -> dict[str, str]:
    return {"Origin": origin, "X-Forecast-Client": "web", "Content-Type": content_type}


def first_forecast_id(origin: str) -> str | None:
    answer = call("reference", origin, "GET", "/api/forecasts")
    items = answer.body.get("data", {}).get("items") if isinstance(answer.body, dict) else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0].get("id")
    return None


def cases(forecast_id: str | None) -> list[tuple[str, str, str, dict, bytes | None]]:
    """(method, path, case name, headers-per-origin factory marker, body)."""
    rows: list[tuple[str, str, str, dict, bytes | None]] = []
    for path in PUBLIC_READS:
        rows.append(("GET", path, "read", {}, None))
    if forecast_id:
        for path in DETAIL_READS:
            rows.append(("GET", path.replace("{id}", forecast_id), "read", {}, None))
    writes: list[tuple[str, str]] = []
    for pattern, methods in sorted(route_parity.python_surface().items()):
        if pattern in route_parity.NOT_A_ROUTE:
            continue
        for method in sorted(methods):
            if method in {"GET", "ANY"}:
                continue
            for one in route_parity.instantiate(pattern):
                writes.append((method, one))
    if forecast_id:
        writes += [("POST", p.replace("{id}", forecast_id)) for p in DETAIL_WRITES if not p.endswith("{id}")]
        writes.append(("PATCH", "/api/forecasts/" + forecast_id))
    for method, path in writes:
        admin = path.startswith("/api/admin/")
        if admin:
            rows.append((method, path, "no-bearer", {"Content-Type": "application/json"}, b"{}"))
            rows.append((method, path, "wrong-bearer",
                         {"Content-Type": "application/json", "Authorization": "Bearer " + "x" * 40}, b"{}"))
            continue
        rows.append((method, path, "wrong-content-type", {"_browser": "text/plain"}, b"{}"))
        rows.append((method, path, "no-origin", {"Content-Type": "application/json"}, b"{}"))
        rows.append((method, path, "not-an-object", {"_browser": "application/json"}, b"[]"))
        rows.append((method, path, "anonymous", {"_browser": "application/json"}, b"{}"))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge", required=True, help="the Worker under test, e.g. the preview edge")
    parser.add_argument("--reference", required=True, help="the Python Worker")
    parser.add_argument("--report", help="write every answer pair to this JSON file")
    parser.add_argument("--reads-only", action="store_true", help="the GET cases only; costs no rate limit")
    arguments = parser.parse_args()
    forecast_id = first_forecast_id(arguments.reference)
    if forecast_id is None:
        print("no published forecast to instantiate the detail routes with", file=sys.stderr)
    rows = cases(forecast_id)
    if arguments.reads_only:
        rows = [row for row in rows if row[0] == "GET"]
    differences = []
    report = []
    for method, path, case, headers, body in rows:
        answers = {}
        for label, origin in (("edge", arguments.edge), ("reference", arguments.reference)):
            sent = dict(headers)
            if "_browser" in sent:
                sent = browser(origin, sent.pop("_browser"))
            answers[label] = call(label, origin, method, path, headers=sent, body=body)
        same = answers["edge"] == answers["reference"]
        report.append({"method": method, "path": path, "case": case, "match": same,
                       "edge": {"status": answers["edge"].status, "body": answers["edge"].body},
                       "reference": {"status": answers["reference"].status, "body": answers["reference"].body}})
        if not same and (method, path, case) not in REVIEWED:
            differences.append((method, path, case, answers))
    if arguments.report:
        Path(arguments.report).write_text(json.dumps(report, indent=2) + "\n")
    for method, path, case, answers in differences:
        edge, reference = answers["edge"], answers["reference"]
        print(f"differs: {method} {path} [{case}]: edge {edge.status}, reference {reference.status}", file=sys.stderr)
        for line in differences_between(edge.body, reference.body)[:8]:
            print(f"    {line}", file=sys.stderr)
    matched = len(rows) - len(differences)
    print(f"http parity: {matched}/{len(rows)} cases match" + (" — differences above" if differences else "")
          + f"; runtime failures retried: edge {RUNTIME_FAILURES['edge']}, reference {RUNTIME_FAILURES['reference']}")
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
