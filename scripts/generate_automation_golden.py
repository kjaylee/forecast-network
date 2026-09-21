#!/usr/bin/env python3
"""Export the automation layer's portable half, so a Rust port can be held to it.

Three things here, and each is a place where "close enough" is a different behaviour rather than a
different style.

`families` is a *bounded* hint list, and deliberately never semantic proof that an event happened.
It matches on `casefold()`, which is not lowercasing — `ß` folds to `ss` and `İ` to `i` plus a
combining dot — and the corpus carries both. Note what that does *not* buy: none of the six hints
contains a character where the two folds differ, so the vector cannot tell `casefold` from
`to_lowercase`, and the port reproduces the fold because the reference names it rather than because
this corpus would notice its absence.

`publisher_feed_url` maps a publisher *root* to the feed the watcher can actually read — a newsroom
page is not a crawlable index and its RSS feed is. The mapping is keyed on the host *and* the path,
so `/newsroom` maps and `/newsroom/2026/09/x` does not; a port that matched on the host alone would
rewrite an article URL to the feed and the watcher would never see the article.

`status` is the operator view, and the shape matters as much as the numbers: `reviews` is keyed by
state and only carries the states that exist, and `sources` counts every registered source while
`enabled` is not part of the answer.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.automation import (  # noqa: E402
    ForecastAutomation,
    families,
    publisher_feed_url,
)
from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_domain.serialization import to_dict  # noqa: E402
from golden_cli import golden_main  # noqa: E402

from tests.test_web_ai import specification  # noqa: E402

GOLDEN = ROOT / "tests/golden/automation-golden.json"

# Every branch of the hint list, plus the foldings that tell `casefold` from `lowercase`, plus the
# near-misses that must not match at all.
QUESTIONS = [
    "Will the next iPhone be a foldable design?",
    "이 회사는 폴더블 노트북을 출시할까?",
    "새로 접는 노트북이 나올까?",
    "Will Apple release an M6 MacBook Pro?",
    "Will Windows 12 ship before the deadline?",
    "윈도우 12가 출시될까?",
    "Will the next Google phone launch?",
    "Will NVIDIA announce a new GPU?",
    "Will SpaceX launch Starship again?",
    "Will NASA confirm the landing?",
    "Will a Windows update break printing?",
    "Will the STRASSE project break ground?",       # `ß` folds to `ss`, and neither contains a hint
    "Will the next iPhone ship with a Straße name?",  # folds to `strasse`; still only `iphone`
    "İstanbul will host the summit",                 # `İ` folds to `i` + combining dot
    "Will nothing at all happen before the deadline?",
    "FOLDING is not folding?",                       # casefold makes these equal
]

URLS = [
    "https://www.apple.com/newsroom/",
    "https://www.apple.com/newsroom",
    "https://apple.com/newsroom/",
    "https://news.microsoft.com/",
    "https://news.microsoft.com/source/",
    "https://news.microsoft.com/source",
    # A path the mapping does not key on: an article under the newsroom is not the newsroom root.
    "https://www.apple.com/newsroom/2026/09/product-x/",
    # A known host with an unknown path, an unknown host, and a URL that does not split at all.
    "https://www.apple.com/support/",
    "https://example.test/newsroom/",
    "not a url",
    "https://NEWS.MICROSOFT.COM/source/",
    "https://www.apple.com/newsroom/?page=2",
]


async def build() -> dict:
    cases = []
    for question in QUESTIONS:
        spec = replace(specification(), canonical_question=question, share_title=question)
        cases.append({"call": "families", "question": question, "result": list(families(spec))})
    for url in URLS:
        cases.append({"call": "publisher_feed_url", "url": url, "result": publisher_feed_url(url)})

    connection = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
        connection.executescript(migration.read_text())
    db = SQLiteDatabase(connection)
    # The reviews reference a forecast and an observation, so the fixture supplies the whole chain
    # rather than just the rows the status view counts — and in dependency order, which the foreign
    # keys enforce.
    await db.execute(
        "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','U','u','r',1)", ())
    await db.execute(
        "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
        "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) "
        "VALUES('f','u','d','{}',1,'OPEN','CRYPTO','t','q','q',?,0,?,1,1,'k')",
        ("a" * 64, 1_800_000_000_000))
    for index, (state, enabled) in enumerate([("pending", 1), ("reviewed", 1), ("reviewed", 0), ("complete", 1), ("exhausted", 1)]):
        # The columns the status view reads, and only those: a fixture that invented its own schema
        # would be testing a different query.
        await db.execute(
            "INSERT INTO official_watch_sources(id,url,kind,interval_ms,next_poll,enabled,failure_count) "
            "VALUES(?,?,'index',60000,0,?,0)",
            (f"source-{index}", f"https://example.test/{index}", enabled))
    # An observation points at retained bytes, so the artifact has to exist first: the foreign key
    # is the rule that an observation nobody kept the evidence for cannot be recorded.
    await db.execute(
        "INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'source','{}','application/json',?)",
        ("c" * 64, 1_800_000_000_000))
    await db.execute(
        "INSERT INTO official_source_observations(id,source_id,url,content_hash,artifact_hash,body,observed_at) "
        "VALUES('observation','source-0','https://example.test/0',?,?,'{}',?)",
        ("b" * 64, "c" * 64, 1_800_000_000_000))
    for index, (state, _) in enumerate([("pending", 1), ("reviewed", 1), ("reviewed", 0), ("complete", 1), ("exhausted", 1)]):
        await db.execute(
            "INSERT INTO official_source_reviews(id,observation_id,forecast_id,specification_hash,content_hash,"
            "policy,state,attempts,next_attempt) VALUES(?,?,?,?,?,'official-source-watch-v1',?,0,0)",
            (f"review-{index}", "observation", "f", "a" * 64, f"{index:064d}", state))

    class Minimal:
        db = None
        now_ms = staticmethod(lambda: 1_800_000_000_000)
        random_token = staticmethod(lambda: "t" * 32)

        class _AI:
            collector = None

        ai = _AI()
        # `SourceWatch` is constructed when a collector exists, so the application has to supply
        # the two things it is wired with even though this fixture never runs it.
        _artifact_sql = staticmethod(lambda artifacts: [])

    Minimal.db = db
    # A collector is what makes `enabled` true: the flag alone is not enough, and the pair of
    # results below is the whole of that rule.
    class WithCollector(Minimal):
        class _AI:
            collector = object()

        ai = _AI()

    WithCollector.db = db
    statuses = {
        "disabled": await ForecastAutomation(Minimal(), enabled=False).status(),
        # `enabled=True` with no collector is disabled: the flag alone is not enough, and a port
        # that read only the flag would report a watcher that cannot fetch anything as running.
        "no-collector": await ForecastAutomation(Minimal(), enabled=True).status(),
        "collector": await ForecastAutomation(WithCollector(), enabled=True).status(),
        "collector-disabled": await ForecastAutomation(WithCollector(), enabled=False).status(),
    }
    connection.close()

    return {
        "description": "The automation layer's portable half: the family hints, the publisher-root "
                       "→ feed mapping, and the operator status view.",
        # The record the hints are read from, so a port parses the reference's own specification
        # rather than hand-building one — the hints read two fields, and a fixture that filled in
        # the other twenty by guesswork would be testing a different record.
        "specification": to_dict(specification()),
        "cases": cases,
        "statuses": statuses,
    }


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
