#!/usr/bin/env python3
"""Daily operator seed: propose objectively resolvable questions and publish them as
Forecast Editorial through the authenticated compile-and-review path.

The service remains the gate: every candidate goes through the same Gemini
compilation and independent AI review as a user question; candidates that need
review or duplicate an existing question are skipped. Runs from GitHub Actions
(secrets ADMIN_TOKEN and GEMINI_API_KEY) or locally. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The statuses that mean the service refused, rather than that this job had nothing to offer.
# 409 is work already in flight; 5xx is the publication path being unavailable. A 4xx that is
# not 409 is this job sending something the service will never accept, and stays a failure.
SERVICE_REFUSAL = frozenset({409, 500, 502, 503, 504})

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = os.environ.get("FORECAST_ORIGIN", "https://forecast.eastsea.xyz")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
USER_AGENT = "forecast-editorial-seed/1.0"
MAX_CANDIDATES = 12


def request_json(url: str, *, method: str = "GET", body: dict | None = None,
                 headers: dict[str, str] | None = None, timeout: int = 180) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"error": {"code": "non_json", "message": raw[:200].decode(errors="replace")}}


def existing_questions(admin_token: str) -> list[str]:
    status, payload = request_json(ORIGIN + "/api/forecasts?sort=newest",
                                   headers={"Authorization": "Bearer " + admin_token})
    if status != 200:
        raise SystemExit(f"feed unavailable: {status}")
    return [item.get("question") or item.get("title") or "" for item in payload["data"]["items"]]


def latest_headlines(feed_url: str, limit: int = 5) -> list[str]:
    """Titles from an RSS/Atom feed, newest first as published; empty on any failure."""
    req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0 (compatible; " + USER_AGENT + ")"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read(400_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", body, flags=re.S)
    cleaned = [" ".join(re.sub(r"<[^>]+>", "", t).split()) for t in titles]
    return [t for t in cleaned if t][1:limit + 1]   # the first title names the feed itself


def propose(themes: list[dict], existing: list[str], count: int, gemini_key: str, today: datetime) -> list[str]:
    horizon = (today + timedelta(days=45)).date().isoformat()
    latest = (today + timedelta(days=150)).date().isoformat()
    prompt = (
        f"Today is {today.date().isoformat()}. Propose {min(MAX_CANDIDATES, count * 4)} forecasting questions for a "
        "public forecasting app. Requirements for every question: a single YES/NO fact that an ordinary reader can "
        "verify from ONE named official public source (a company newsroom, agency site or official blog), a fixed "
        f"deadline between {horizon} and {latest} written as an ISO date, no opinions, no prices or market values, "
        "no gambling, elections, wars, deaths, crime or health outcomes. Prefer scheduled or plausible announcements "
        "whose timing is genuinely uncertain: a reasonable person should put the odds between 25% and 75%. "
        "BANNED: questions that are true every cycle anyway ('a new version', 'a new generation', 'a new model', "
        "'an update', 'a new event') unless they pin a concrete distinguishing fact that could fail: a named "
        "product or program, a numeric threshold (price, capacity, count, percentage), a specific feature, an "
        "ordering ('before X does Y'), or a deadline clearly tighter than the publisher's usual cadence. Never "
        "invent product names or version numbers; use names the publisher has itself announced or clearly "
        "generic categories. Do not repeat or paraphrase any existing question. Write each question "
        "in one English sentence that names the source domain and the deadline, for example: "
        "'Will Apple publish a press release on apple.com/newsroom announcing new AirPods before 2026-11-30?'.\n\n"
        "Themes to draw from (rotate across them). Each lists the publisher's LATEST headlines: treat those as "
        "already announced and ask about the next uncertain step, never about something these headlines settle.\n"
        + "\n".join(
            f"- {t['name']}: sources {', '.join(t['sources'])}; hints {', '.join(t['hints'])}; latest headlines: "
            + (" | ".join(h for feed in t.get('feeds', []) for h in latest_headlines(feed)) or "(none fetched)")
            for t in themes)
        + "\n\nExisting questions (do not duplicate):\n"
        + "\n".join(f"- {q}" for q in existing[:40])
        + '\n\nReturn JSON only: {"questions": ["...", "..."]}'
    )
    status, payload = request_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        method="POST", headers={"x-goog-api-key": gemini_key},
        body={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9, "maxOutputTokens": 8192,
                                   "thinkingConfig": {"thinkingBudget": 0},
                                   "responseSchema": {"type": "object", "properties": {"questions": {
                                       "type": "array", "items": {"type": "string"}}}, "required": ["questions"]}}})
    if status != 200:
        raise SystemExit(f"gemini unavailable: {status} {json.dumps(payload)[:200]}")
    candidate = payload.get("candidates", [{}])[0]
    text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []))
    try:
        questions = json.loads(text).get("questions", [])
    except ValueError:
        raise SystemExit(f"gemini returned malformed JSON (finishReason={candidate.get('finishReason')})")
    clean = []
    for question in questions:
        if not isinstance(question, str):
            continue
        question = " ".join(question.split())
        if 20 <= len(question) <= 400 and re.search(r"\d{4}-\d{2}-\d{2}", question) and question not in clean:
            clean.append(question)
    return clean[:MAX_CANDIDATES]


def seed(question: str, admin_token: str) -> tuple[str, str, int]:
    """(outcome, detail, status) for one candidate offered to the service."""
    status, payload = request_json(ORIGIN + "/api/admin/seed", method="POST", body={"question": question},
                                   headers={"Authorization": "Bearer " + admin_token}, timeout=240)
    if status in (200, 201):
        forecast = payload.get("data", {}).get("forecast", {})
        return "published", forecast.get("id", "?"), status
    error = payload.get("error", {})
    return "skipped", f"{status} {error.get('code', 'unknown')}", status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=3, help="questions to publish today")
    parser.add_argument("--dry-run", action="store_true", help="propose only; publish nothing")
    args = parser.parse_args()
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if len(admin_token) < 32 or not gemini_key:
        print("ADMIN_TOKEN and GEMINI_API_KEY are required", file=sys.stderr)
        return 2
    themes = json.loads((ROOT / "apps/web/content/editorial-topics.json").read_text())["themes"]
    today = datetime.now(timezone.utc)
    # Rotate the theme order daily so consecutive days do not lean on the same publisher.
    offset = today.timetuple().tm_yday % len(themes)
    themes = themes[offset:] + themes[:offset]
    existing = existing_questions(admin_token)
    print(f"{len(existing)} existing questions read")
    candidates = propose(themes, existing, args.count, gemini_key, today)
    print(f"{len(candidates)} candidates")
    published = 0
    refused = 0
    for question in candidates:
        if published >= args.count:
            break
        if args.dry_run:
            print("  candidate:", question)
            continue
        outcome, detail, status = seed(question, admin_token)
        print(f"  {outcome:9s} {detail:32s} {question}")
        if outcome == "published":
            published += 1
        elif status in SERVICE_REFUSAL:
            refused += 1
        time.sleep(2)
    if args.dry_run:
        return 0
    print(f"published {published}/{args.count}")
    if published:
        return 0
    # Nothing was published. Whose failure is that?
    #
    # For six of nine days the answer was the service: every candidate came back 409
    # `ai_work_in_progress`, 500 `non_json`, 502 `artifact_invalid` or 503
    # `source_temporarily_unavailable` — the AI publication path refusing, which this job
    # neither caused nor can fix. A red run a day for that is the noise the feed fallback
    # made: an alarm that fires so regularly nobody reads it. The service's own health is
    # the watchdog's alarm, and this run reports what it saw and ends green.
    #
    # Red is kept for what this job *is* answerable for: no candidate to offer at all.
    if refused:
        print(f"the service refused every candidate ({refused}); this is the service's alarm, not this job's")
        return 0
    print("no candidate was produced to offer", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
