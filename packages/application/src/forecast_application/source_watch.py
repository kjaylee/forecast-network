"""Shared official-source observer; containment precedes bounded semantic review.

Polling never asks a model whether a page changed. Raw evidence is retained and
stable article text deduplicates decorative HTML changes. Callbacks own domain
commands: this adapter neither settles points nor finalizes a forecast.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from forecast_domain.models import Source
from forecast_domain.serialization import content_hash, to_dict

from .database import Database, Statement
from .sources import (
    Artifact,
    SourceCollector,
    SourceRejected,
    TextResponse,
    validate_public_url,
)

POLICY = "official-source-watch-v1"
LEASE_MS = 240_000
MAX_ATTEMPTS = 3
MAX_DISCOVERED = 6
MAX_BINDINGS = 30
MIN_INTERVAL_MS = 60_000
MAX_JSONLD_SCRIPTS = 8
MAX_JSONLD_BYTES = 65536
MAX_JSONLD_NODES = 128


class _BudgetExhausted(Exception):
    pass


class _NotModified(Exception):
    pass


class _Article(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.hidden = 0
        self.main = 0
        self.publication_date: str | None = None
        self.jsonld: list[str] = []
        self._jsonld_active = False
        self._jsonld_parts: list[str] = []
        self._jsonld_count = 0
        self._jsonld_bytes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").casefold().split(";", 1)[0].strip() == "application/ld+json":
            self._jsonld_count += 1
            self._jsonld_active = self._jsonld_count <= MAX_JSONLD_SCRIPTS and self._jsonld_bytes < MAX_JSONLD_BYTES
            self._jsonld_parts = []
        if tag in {"script", "style", "noscript", "nav", "footer", "header"}:
            self.hidden += 1
        if tag in {"main", "article"}:
            self.main += 1
        if tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))
        if tag == "link" and values.get("href"):
            self.links.append(str(values["href"]))
        key = values.get("property") or values.get("name")
        if tag == "meta" and key in {"article:published_time", "date", "datePublished"}:
            self.publication_date = values.get("content")
        if tag == "time" and self.publication_date is None:
            self.publication_date = values.get("datetime")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._jsonld_active:
                self.jsonld.append("".join(self._jsonld_parts))
            self._jsonld_active = False
            self._jsonld_parts = []
        if tag in {"script", "style", "noscript", "nav", "footer", "header"}:
            self.hidden = max(0, self.hidden-1)
        if tag in {"main", "article"}:
            self.main = max(0, self.main-1)

    def handle_data(self, data: str) -> None:
        if self._jsonld_active:
            self._jsonld_bytes += len(data.encode("utf-8"))
            if self._jsonld_bytes <= MAX_JSONLD_BYTES:
                self._jsonld_parts.append(data)
            else:
                self._jsonld_active = False
                self._jsonld_parts = []
        if not self.hidden:
            self.parts.append(data)
            if self.main:
                self.main_parts.append(data)


def _publication_date(raw_date: Any) -> tuple[str | None, str]:
    if type(raw_date) is not str:
        return None, "unknown"
    # Apple's NewsArticle datePublished may end in Z despite providing only a
    # calendar day. Preserve that precision; it is never a midnight instant.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}Z?", raw_date):
        normalized = raw_date.removesuffix("Z")
        try:
            datetime.strptime(normalized, "%Y-%m-%d")
            return normalized, "date"
        except ValueError:
            return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", raw_date):
        try:
            datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            return raw_date, "instant"
        except ValueError:
            pass
    return None, "unknown"


def _jsonld_publication(scripts: list[str]) -> tuple[str | None, str]:
    dates: set[tuple[str | None, str]] = set()
    nodes = 0
    article_types = {"Article", "NewsArticle", "BlogPosting"}
    for script in scripts:
        try:
            document = json.loads(script)
        except (ValueError, RecursionError):
            continue
        pending = [(document, 0)]
        while pending and nodes < MAX_JSONLD_NODES:
            item, depth = pending.pop()
            nodes += 1
            if depth > 8:
                continue
            if isinstance(item, list):
                pending.extend((entry, depth+1) for entry in item[:MAX_JSONLD_NODES])
            elif isinstance(item, dict):
                kinds = item.get("@type", [])
                if isinstance(kinds, str):
                    kinds = [kinds]
                if isinstance(kinds, list) and any(isinstance(kind, str) and
                        kind.removeprefix("https://schema.org/").removeprefix("http://schema.org/") in article_types
                        for kind in kinds):
                    published = _publication_date(item.get("datePublished"))
                    if published[0] is not None:
                        dates.add(published)
                if "@graph" in item:
                    pending.append((item["@graph"], depth+1))
    # Conflicting article publication metadata remains unknown for review.
    return next(iter(dates)) if len(dates) == 1 else (None, "unknown")


def article_content(body: str) -> tuple[str, str | None, str]:
    parser = _Article()
    parser.feed(body)
    text = " ".join(" ".join(parser.main_parts or parser.parts).split())
    date, precision = (_publication_date(parser.publication_date) if parser.publication_date is not None
                       else _jsonld_publication(parser.jsonld))
    return text, date, precision


def discover_articles(body: str, url: str) -> tuple[str, ...]:
    """Only publisher article paths on the watched exact official host."""
    parser = _Article()
    parser.feed(body)
    # RSS <link> text, unlike Atom href, is plain character data.
    links = parser.links + re.findall(r"<link>\s*(https://[^<\s]+)\s*</link>", body)
    host = validate_public_url(url, official=True)
    found: list[str] = []
    for value in links:
        candidate = urljoin(url, value).split("#", 1)[0]
        try:
            if validate_public_url(candidate, official=True) != host:
                continue
        except SourceRejected:
            continue
        path = urlsplit(candidate).path
        # Date folders also appear in WordPress media uploads. Matching any date
        # suffix accidentally registers logos/images as permanent polling jobs.
        # Supported publishers use extensionless article slugs at these roots.
        if "/wp-content/" in path.casefold() or "/uploads/" in path.casefold():
            continue
        slug = r"[A-Za-z0-9][A-Za-z0-9_-]*"
        if host in {"www.apple.com", "apple.com"}:
            eligible = bool(re.fullmatch(r"/newsroom/\d{4}/\d{2}/" + slug + r"/?", path))
        elif host == "blogs.microsoft.com":
            eligible = bool(re.fullmatch(r"/(?:blog/)?\d{4}/\d{2}/(?:\d{2}/)?" + slug + r"/?", path))
        elif host in {"news.microsoft.com", "www.microsoft.com"}:
            eligible = bool(re.fullmatch(r"/(?:source/)?\d{4}/\d{2}/(?:\d{2}/)?" + slug + r"/?", path))
        else:
            eligible = False
        if eligible and candidate not in found:
            found.append(candidate)
    return tuple(found[:MAX_DISCOVERED])


def relevant(text: str, families: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(family.casefold()) + r"(?![a-z0-9])", lowered)
               for family in families)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SourceWatch:
    def __init__(self, db: Database, collector: SourceCollector, now_ms: Callable[[], int],
                 token: Callable[[], str], *,
                 load_forecast: Callable[[str], Awaitable[dict[str, Any]]],
                 hold: Callable[[str, dict[str, Any]], Awaitable[None]],
                 review: Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]],
                 accept: Callable[[str, dict[str, Any]], Awaitable[None]],
                 dismiss: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
                 artifact_sql: Callable[[tuple[Artifact, ...]], list[Statement]] | None = None) -> None:
        self.db, self.collector, self.now_ms, self.token = db, collector, now_ms, token
        self.load_forecast, self.hold, self.review, self.accept = load_forecast, hold, review, accept
        self.dismiss = dismiss
        self.artifact_sql = artifact_sql or self._artifact_sql

    def _artifact_sql(self, artifacts: tuple[Artifact, ...]) -> list[Statement]:
        statements = []
        for artifact in artifacts:
            hashes = {_hash(artifact.body)}
            if artifact.media_type == "application/json":
                hashes.add(content_hash(json.loads(artifact.body)))
            if artifact.content_hash not in hashes or len(artifact.body.encode()) > 524288:
                raise ValueError("Invalid retained source artifact")
            statements.append(("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
                               (artifact.content_hash, artifact.kind, artifact.body, artifact.media_type, self.now_ms())))
        return statements

    async def register(self, source_id: str, url: str, *, kind: str = "index",
                       interval_ms: int = 300_000, parent_id: str | None = None, pinned: bool = False) -> None:
        validate_public_url(url, official=True)
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", source_id) or kind not in {"index", "article"}
                or type(interval_ms) is not int or not MIN_INTERVAL_MS <= interval_ms <= 86400000):
            raise ValueError("Invalid official source registration")
        if type(pinned) is not bool:
            raise ValueError("Pinned watch configuration must be boolean")
        if pinned:
            count = await self.db.first("SELECT COUNT(*) AS n FROM official_watch_sources WHERE pinned=1 AND id<>?", (source_id,))
            if count and count["n"] >= MAX_BINDINGS:
                raise ValueError("Pinned official source limit reached")
        if parent_id:
            parent = await self.db.first("SELECT * FROM official_watch_sources WHERE id=?", (parent_id,))
            if not parent or urlsplit(parent["url"]).hostname != urlsplit(url).hostname or parent["kind"] != "index":
                raise ValueError("An article must belong to an exact watched publisher")
        existing = await self.db.first("SELECT * FROM official_watch_sources WHERE id=? OR url=?", (source_id, url))
        if existing:
            if existing["id"] != source_id or existing["url"] != url or existing["kind"] != kind or existing["parent_id"] != parent_id:
                raise ValueError("Source registration is immutable")
            if pinned:
                await self.db.execute("UPDATE official_watch_sources SET pinned=1,enabled=1 WHERE id=?", (source_id,))
            return
        await self.db.execute("INSERT OR IGNORE INTO official_watch_sources(id,url,kind,parent_id,interval_ms,next_poll,pinned) VALUES(?,?,?,?,?,?,?)",
                              (source_id, url, kind, parent_id, interval_ms, self.now_ms(), int(pinned)))

    async def bind(self, forecast_id: str, source_id: str, families: tuple[str, ...]) -> None:
        if not families or len(families) > 8 or any(not re.fullmatch(r"[A-Za-z0-9 -]{2,50}", value) for value in families):
            raise ValueError("A bounded product family is required")
        # The caller provides canonical specification source URLs, never model URLs.
        forecast = await self.load_forecast(forecast_id)
        source = await self.db.first("SELECT url FROM official_watch_sources WHERE id=?", (source_id,))
        hosts = {validate_public_url(value, official=True) for value in forecast["officialSourceUrls"]}
        if not source or urlsplit(source["url"]).hostname not in hosts:
            raise ValueError("Watcher is not bound to a published official source")
        await self.db.execute("INSERT OR IGNORE INTO official_watch_bindings(forecast_id,source_id,families) VALUES(?,?,?)",
                              (forecast_id, source_id, _json(families)))
        await self._queue_known(forecast_id, source_id, families)

    async def _queue_known(self, forecast_id: str, source_id: str, families: tuple[str, ...]) -> None:
        observations = await self.db.all("SELECT body FROM official_source_observations WHERE source_id=? ORDER BY observed_at DESC LIMIT 100",
                                         (source_id,))
        for row in observations:
            observation = json.loads(row["body"])
            if relevant(observation["excerpt"], families):
                await self._enqueue(forecast_id, observation)

    async def check_known(self, forecast: dict[str, Any]) -> list[dict[str, Any]]:
        """Cheap compile gate candidates; caller must reject/hold pending assessment."""
        hosts = {validate_public_url(url, official=True) for url in forecast["officialSourceUrls"]}
        families = tuple(forecast.get("families", ()))
        rows = await self.db.all("SELECT body FROM official_source_observations ORDER BY observed_at DESC LIMIT 100")
        return [value for row in rows if urlsplit((value := json.loads(row["body"]))["url"]).hostname in hosts
                and relevant(value["excerpt"], families)]

    async def _enqueue(self, forecast_id: str, observation: dict[str, Any]) -> None:
        forecast = await self.load_forecast(forecast_id)
        if forecast.get("state") not in {"OPEN", "LOCKED"}:
            return
        key = _hash(observation["contentHash"] + forecast["specificationHash"] + POLICY)
        existing = await self.db.first("SELECT state FROM official_source_reviews WHERE id=?", (key,))
        if existing and existing["state"] == "complete":
            return
        await self.db.execute("INSERT OR IGNORE INTO official_source_reviews(id,observation_id,forecast_id,specification_hash,content_hash,policy,next_attempt) VALUES(?,?,?,?,?,?,?)",
                              (key, observation["id"], forecast_id, forecast["specificationHash"], observation["contentHash"], POLICY, self.now_ms()))
        # Crucially the hold happens here, before scheduling or awaiting ANY AI.
        await self.hold(forecast_id, observation)

    async def _poll(self, source: dict[str, Any], lease: str) -> str:
        captured: dict[str, str] = {}

        async def request(url: str, method: str, headers: dict[str, str]) -> TextResponse:
            if url == source["url"]:
                if source["etag"]:
                    headers["If-None-Match"] = source["etag"]
                if source["last_modified"]:
                    headers["If-Modified-Since"] = source["last_modified"]
            response = await self.collector.request_text(url, method, headers)
            if response.status == 304:
                if url != source["url"] or not (source["etag"] or source["last_modified"]):
                    raise SourceRejected("Unconditional source returned 304")
                raise _NotModified()
            captured.update({key.lower(): value for key, value in response.headers.items()})
            # RSS/Atom are XML documents, subject to the same collector byte and
            # redirect bounds. Normalize only the media label, never the bytes.
            if captured.get("content-type", "").split(";", 1)[0].lower() in {"application/rss+xml", "application/atom+xml"}:
                response = TextResponse(response.status, response.body,
                                        {**captured, "content-type": "application/xml"})
            return response

        collector = SourceCollector(request, timeout_seconds=self.collector.timeout_seconds)
        try:
            collected = await collector.collect(Source(source_id=source["id"], name="Official publisher",
                                                        url=source["url"], is_official=True), self.now_ms())
        except _NotModified:
            await self._poll_done(source, lease, source["etag"], source["last_modified"])
            return "unchanged"
        if source["kind"] == "index":
            discovered = discover_articles(collected.artifact.body, collected.snapshot.url)
            if not discovered:
                raise SourceRejected("Official feed no longer exposes readable article links")
            active_ids = []
            for url in discovered:
                article_id = "article-" + _hash(url)[:32]
                active_ids.append(article_id)
                await self.register(article_id, url, kind="article",
                                    interval_ms=max(3600000, source["interval_ms"]), parent_id=source["id"])
                await self.db.execute("UPDATE official_watch_sources SET enabled=1,next_poll=? WHERE id=? AND enabled=0",
                                      (self.now_ms(), article_id))
            # A feed has a bounded live window. Older observations remain fully
            # retained for publication guards and review; polling doesn't grow
            # with the publisher's lifetime article count.
            placeholders = ",".join("?" for _ in active_ids)
            await self.db.execute("UPDATE official_watch_sources SET enabled=0 WHERE parent_id=? AND pinned=0 AND id NOT IN ("+placeholders+")",
                                  (source["id"], *active_ids))
            await self._poll_done(source, lease, captured.get("etag"), captured.get("last-modified"))
            return "index"
        text, date, precision = article_content(collected.artifact.body)
        if len(text) < 40:
            raise SourceRejected("Article text is incomplete")
        content_hash = _hash(_json({"text": text, "publicationDate": date, "datePrecision": precision}))
        root_id = source["parent_id"] or source["id"]
        identity = _hash(root_id + source["url"] + content_hash)
        observation = {"id": identity, "sourceId": root_id, "url": collected.snapshot.url,
                       "contentHash": content_hash, "artifactHash": collected.artifact.content_hash,
                       "excerpt": text.encode("utf-8")[:24000].decode("utf-8", errors="ignore"), "observedAt": self.now_ms(),
                       "publicationDate": date, "datePrecision": precision, "policy": POLICY}
        stored = await self.db.first("SELECT body FROM official_source_observations WHERE id=?", (identity,))
        if stored is None:
            statements = self.artifact_sql((collected.artifact,))
            statements.append(("INSERT OR IGNORE INTO official_source_observations(id,source_id,url,content_hash,artifact_hash,body,observed_at) VALUES(?,?,?,?,?,?,?)",
                               (identity, root_id, source["url"], content_hash, collected.artifact.content_hash, _json(observation), self.now_ms())))
            await self.db.batch(statements)
            stored = await self.db.first("SELECT body FROM official_source_observations WHERE id=?", (identity,))
        if stored:
            observation = json.loads(stored["body"])
        bindings = await self.db.all("SELECT * FROM official_watch_bindings WHERE source_id=? LIMIT ?", (root_id, MAX_BINDINGS))
        for binding in bindings:
            if relevant(text, tuple(json.loads(binding["families"]))):
                await self._enqueue(binding["forecast_id"], observation)
        await self._poll_done(source, lease, captured.get("etag"), captured.get("last-modified"))
        return "article"

    async def _poll_done(self, source: dict[str, Any], lease: str, etag: str | None, modified: str | None) -> None:
        await self.db.execute("UPDATE official_watch_sources SET etag=?,last_modified=?,checked_at=?,next_poll=?,failure_count=0,last_error=NULL WHERE id=? AND lease_token=? AND lease_until>?",
                              ((etag or "")[:512] or None, (modified or "")[:128] or None, self.now_ms(),
                               self.now_ms()+source["interval_ms"], source["id"], lease, self.now_ms()))

    async def run(self, limit: int = 2) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 6:
            raise ValueError("Source job bound must be between one and six")
        summary = {"polled": 0, "reviewed": 0, "failed": 0}
        sources = await self.db.all("SELECT * FROM official_watch_sources WHERE enabled=1 AND next_poll<=? AND lease_until<=? ORDER BY next_poll,id LIMIT ?",
                                    (self.now_ms(), self.now_ms(), limit))
        for source in sources:
            lease = self.token()
            claim = await self.db.execute("UPDATE official_watch_sources SET lease_token=?,lease_until=? WHERE id=? AND enabled=1 AND next_poll<=? AND lease_until<=? RETURNING id",
                                          (lease, self.now_ms()+LEASE_MS, source["id"], self.now_ms(), self.now_ms()))
            if not claim.get("results"):
                continue
            try:
                await self._poll(source, lease)
                summary["polled"] += 1
            except Exception as exc:
                summary["failed"] += 1
                # Operator diagnostics: exception class and code locations only, never source bytes or URLs.
                print(json.dumps({"event": "source_poll_failed", "sourceKind": source["kind"], "errorType": type(exc).__name__,
                                  "detail": str(exc)[:120] if isinstance(exc, (AttributeError, TypeError, KeyError)) else None,
                                  "frames": [{"function": f.name, "line": f.lineno, "file": f.filename.rsplit("/", 1)[-1]}
                                             for f in traceback.extract_tb(exc.__traceback__)[-5:]]}))
                await self.db.execute("UPDATE official_watch_sources SET failure_count=failure_count+1,last_error=?,next_poll=? WHERE id=? AND lease_token=?",
                                      (type(exc).__name__, self.now_ms()+min(3600000, source["interval_ms"]*2**min(source["failure_count"], 4)), source["id"], lease))
            finally:
                await self.db.execute("UPDATE official_watch_sources SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?", (source["id"], lease))
        jobs = await self.db.all("SELECT * FROM official_source_reviews WHERE state IN ('pending','reviewed') AND next_attempt<=? AND lease_until<=? ORDER BY next_attempt,id LIMIT ?",
                                 (self.now_ms(), self.now_ms(), limit))
        for job in jobs:
            lease = self.token()
            claim = await self.db.execute("UPDATE official_source_reviews SET lease_token=?,lease_until=?,attempts=attempts+1 WHERE id=? AND state IN ('pending','reviewed') AND next_attempt<=? AND lease_until<=? RETURNING id",
                                          (lease, self.now_ms()+LEASE_MS, job["id"], self.now_ms(), self.now_ms()))
            if not claim.get("results"):
                continue
            try:
                row = await self.db.first("SELECT body FROM official_source_observations WHERE id=?", (job["observation_id"],))
                if row is None:
                    raise ValueError("Retained observation is missing")
                observation = json.loads(row["body"])
                forecast = await self.load_forecast(job["forecast_id"])
                if forecast["specificationHash"] != job["specification_hash"]:
                    raise ValueError("Published specification changed")
                await self.hold(job["forecast_id"], observation)
                if job["result"] is not None:
                    result = json.loads(job["result"])
                else:
                    bucket = self.now_ms() // 86400000
                    await self.db.execute("INSERT OR IGNORE INTO rate_limits(scope,bucket,count,expires_at) VALUES('official-watch-ai',?,0,?)",
                                          (bucket, (bucket+2)*86400000))
                    budget = await self.db.execute("UPDATE rate_limits SET count=count+3 WHERE scope='official-watch-ai' AND bucket=? AND count<=69 RETURNING count", (bucket,))
                    if not budget.get("results"):
                        raise _BudgetExhausted()
                    async with asyncio.timeout(150):
                        result = dict(await self.review(forecast, observation))
                    artifacts = tuple(result.pop("artifacts", ()))
                    if result.get("trigger") is not None:
                        result["trigger"] = to_dict(result["trigger"])
                    if type(result.get("accepted")) is not bool or len(_json(result).encode()) > 65536:
                        raise ValueError("Invalid bounded source review result")
                    statements = self.artifact_sql(artifacts)
                    statements.append(("UPDATE official_source_reviews SET state='reviewed',result=? WHERE id=? AND lease_token=? AND lease_until>?",
                                       (_json(result), job["id"], lease, self.now_ms())))
                    await self.db.batch(statements)
                owner = await self.db.first("SELECT id FROM official_source_reviews WHERE id=? AND lease_token=? AND lease_until>?", (job["id"], lease, self.now_ms()))
                if owner is None:
                    raise ValueError("Source review lease expired")
                if result.get("accepted") is True:
                    await self.accept(job["forecast_id"], {**result, "observation": observation})
                elif result.get("accepted") is False and result.get("dismissible") is True and self.dismiss is not None:
                    await self.dismiss(job["forecast_id"], {**result, "observation": observation})
                await self.db.execute("UPDATE official_source_reviews SET state='complete',result=?,last_error=NULL WHERE id=? AND lease_token=? AND lease_until>?",
                                      (_json(result), job["id"], lease, self.now_ms()))
                summary["reviewed"] += 1
            except _BudgetExhausted:
                await self.db.execute("UPDATE official_source_reviews SET attempts=attempts-1,next_attempt=?,last_error='daily_budget' WHERE id=? AND lease_token=?",
                                      ((self.now_ms()//86400000+1)*86400000, job["id"], lease))
            except Exception as exc:
                summary["failed"] += 1
                retained = tuple(getattr(exc, "artifacts", ()))
                if retained:
                    await self.db.batch(self.artifact_sql(retained))
                await self.db.execute("UPDATE official_source_reviews SET state=?,last_error=?,next_attempt=? WHERE id=? AND lease_token=?",
                                      ("exhausted" if job["attempts"]+1 >= MAX_ATTEMPTS else "pending",
                                       type(exc).__name__, self.now_ms()+60000*2**job["attempts"], job["id"], lease))
            finally:
                await self.db.execute("UPDATE official_source_reviews SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?", (job["id"], lease))
        return summary
