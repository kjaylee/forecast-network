"""Bounded public-source retrieval, without trusting URLs supplied by a model.

The injected transport MUST disable automatic redirects and limit decoded response
bytes while streaming. This boundary also checks both limits after transport.
Only explicitly registered authoritative hosts can be requested; no wildcard DNS,
user-controlled host, IP literal or arbitrary proxy service is permitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from forecast_domain.models import EvidenceSnapshot, ForecastSpecification, Source

# Publisher pages can include large inline assets; retain complete bytes within
# the same 512 KiB ceiling enforced by the immutable artifact store.
MAX_SOURCE_BYTES = 512 * 1024
MAX_EXCERPT_BYTES = 24000
MAX_SOURCE_REDIRECTS = 3
SOURCE_POLICY_VERSION = "public-official-hosts-v1"

# Host registrations assert authority, not that every page proves every question.
# SourceVerifier and the immutable outcome clauses must still assess each page.
OFFICIAL_HOSTS: dict[str, str] = {
    "www.apple.com": "Apple", "apple.com": "Apple",
    "blogs.nvidia.com": "NVIDIA", "nvidianews.nvidia.com": "NVIDIA",
    "www.nvidia.com": "NVIDIA", "openai.com": "OpenAI",
    "blog.google": "Google", "deepmind.google": "Google DeepMind",
    "www.microsoft.com": "Microsoft", "blogs.microsoft.com": "Microsoft",
    "news.microsoft.com": "Microsoft", "news.samsung.com": "Samsung",
    "www.nasa.gov": "NASA", "science.nasa.gov": "NASA",
    "www.esa.int": "European Space Agency", "www.spacex.com": "SpaceX",
    "www.noaa.gov": "NOAA", "www.climate.gov": "NOAA Climate",
    "www.who.int": "World Health Organization", "www.un.org": "United Nations",
    "www.federalreserve.gov": "Federal Reserve", "www.bls.gov": "US BLS",
    "www.bea.gov": "US BEA", "www.ecb.europa.eu": "ECB",
    "www.bok.or.kr": "Bank of Korea", "kostat.go.kr": "Statistics Korea",
    "www.kostat.go.kr": "Statistics Korea", "www.kma.go.kr": "KMA",
    "solana.com": "Solana", "ethereum.org": "Ethereum",
    "www.fifa.com": "FIFA", "www.olympics.com": "Olympics",
}
FALLBACK_HOSTS: dict[str, str] = {
    "www.reuters.com": "Reuters", "reuters.com": "Reuters", "apnews.com": "AP",
    "www.bbc.com": "BBC", "www.bbc.co.uk": "BBC",
}


class SourceRejected(ValueError):
    """An unsafe, missing, unsupported, changed or incomplete source."""


class SourceUnavailable(RuntimeError):
    """A transport outage or timeout, eligible for a later bounded job retry."""


@dataclass(frozen=True, slots=True)
class TextResponse:
    status: int
    body: str
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Artifact:
    content_hash: str
    kind: str
    body: str
    media_type: str = "application/json"


@dataclass(frozen=True, slots=True)
class CollectedSource:
    snapshot: EvidenceSnapshot
    artifact: Artifact
    excerpt: str


TextFetcher = Callable[[str, str, dict[str, str]], Awaitable[TextResponse]]


def validate_public_url(url: str, *, official: bool = False) -> str:
    """Return an exact registered hostname or reject before network activity."""
    if type(url) is not str or len(url) > 2048 or re.search(r"[\s\x00-\x1f\\]", url):
        raise SourceRejected("Source URL is malformed")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise SourceRejected("Source URL is malformed") from exc
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.fragment or port not in (None, 443) or host.endswith(".")):
        raise SourceRejected("Source requires public HTTPS without credentials or custom ports")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SourceRejected("IP source addresses are not permitted")
    allowed = OFFICIAL_HOSTS if official else {**OFFICIAL_HOSTS, **FALLBACK_HOSTS}
    if host not in allowed:
        raise SourceRejected("Source host is not in the approved authoritative-source registry")
    return host


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def evidence_excerpt(body: str) -> str:
    parser = _Text()
    parser.feed(body)
    text = " ".join(" ".join(parser.parts).split())
    return text.encode("utf-8")[:MAX_EXCERPT_BYTES].decode("utf-8", errors="ignore")


class SourceCollector:
    def __init__(self, request_text: TextFetcher, *, timeout_seconds: float = 15) -> None:
        self.request_text = request_text
        self.timeout_seconds = timeout_seconds

    async def collect(self, source: Source, now_ms: int, *, url: str | None = None) -> CollectedSource:
        start = source.url if url is None else url
        expected_host = validate_public_url(source.url, official=source.is_official)
        current = start
        for attempt in range(MAX_SOURCE_REDIRECTS + 1):
            if validate_public_url(current, official=source.is_official) != expected_host:
                raise SourceRejected("Evidence redirect or URL changed its published source host")
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self.request_text(current, "GET", {
                        "Accept": "text/html,application/json,text/plain,application/xml",
                        "User-Agent": "ForecastNetwork-Evidence/1.0",
                    })
            except (TimeoutError, OSError, RuntimeError) as exc:
                raise SourceUnavailable("Evidence source is temporarily unavailable") from exc
            headers = {key.lower(): value for key, value in response.headers.items()}
            if response.status in {301, 302, 303, 307, 308}:
                if attempt == MAX_SOURCE_REDIRECTS or not headers.get("location"):
                    raise SourceRejected("Source exceeded the safe redirect limit")
                current = urljoin(current, headers["location"])
                continue
            if response.status in {429, 500, 502, 503, 504}:
                raise SourceUnavailable("Evidence source is temporarily unavailable")
            if response.status != 200:
                raise SourceRejected(f"Published evidence source returned HTTP {response.status}")
            length = headers.get("content-length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > MAX_SOURCE_BYTES:
                        raise SourceRejected("Evidence exceeds the retained-source byte limit")
                except ValueError as exc:
                    raise SourceRejected("Evidence has an invalid content length") from exc
            if type(response.body) is not str:
                raise SourceRejected("Evidence transport must return retained UTF-8 text")
            raw = response.body.encode("utf-8")
            media_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if media_type not in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
                raise SourceRejected("Evidence response is not a supported text document")
            if not raw.strip() or len(raw) > MAX_SOURCE_BYTES:
                raise SourceRejected("Evidence is empty or exceeds the retained-source byte limit")
            excerpt = evidence_excerpt(response.body)
            if len(excerpt) < 40:
                raise SourceRejected("Evidence document contains insufficient readable content")
            digest = hashlib.sha256(raw).hexdigest()
            snapshot = EvidenceSnapshot(
                evidence_id="evidence-" + digest[:32], source_id=source.source_id,
                url=current, content_sha256=digest, snapshot_uri="urn:sha256:" + digest,
                collected_at_ms=now_ms,
            )
            return CollectedSource(snapshot, Artifact(digest, "source", response.body, media_type), excerpt)
        raise SourceRejected("No complete evidence response")

    async def collect_dispute(self, specification: ForecastSpecification, url: str,
                              now_ms: int) -> CollectedSource:
        host = validate_public_url(url)
        source = next((item for item in specification.source_policy.sources
                       if urlsplit(item.url).hostname == host), None)
        if source is None:
            source = Source(source_id="dispute-" + hashlib.sha256(host.encode()).hexdigest()[:20],
                            name=OFFICIAL_HOSTS.get(host, FALLBACK_HOSTS.get(host, host)),
                            url=url, is_official=host in OFFICIAL_HOSTS)
        return await self.collect(source, now_ms, url=url)
