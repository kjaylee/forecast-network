#!/usr/bin/env python3
"""Export what Python's article parser decides, so a Rust port can be held to it.

`source_watch.article_content` turns a fetched page into the article text and the
publication time the resolution guard reasons about. The time it returns decides whether
evidence can be placed relative to participation, which decides whether a reward may be
credited — so a Rust port that disagrees by one character of one page is not a port, it is a
second opinion.

The parser is Python's `html.parser.HTMLParser`, which is not an HTML5 parser and does not
recover from malformed input the way a browser does. Reproducing it means reproducing that
state machine, and the only way to know you have is to compare against it. This exports the
comparison: a corpus that isolates each rule, with the reference answer for each.

Regenerate with `--write`; CI runs `--check` so a change to the Python behaviour fails
rather than leaving the vectors describing a parser that no longer exists.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.source_watch import (  # noqa: E402
    MAX_JSONLD_BYTES,
    MAX_JSONLD_SCRIPTS,
    _jsonld_publication,
    _publication_date,
    article_content,
)

GOLDEN = ROOT / "tests/golden/article-content-golden.json"


def meta(attributes: str, body: str = "<main>Product announced.</main>") -> str:
    return f"<html><head><meta {attributes}></head><body>{body}</body></html>"


FIXTURES: list[tuple[str, str]] = [
    # --- publication time: where it is read from, and what wins -------------------
    ("meta_property_instant", meta('property="article:published_time" content="2026-09-15T10:00:00Z"')),
    ("meta_name_date", meta('name="date" content="2026-09-15T10:00:00Z"')),
    ("meta_name_date_published", meta('name="datePublished" content="2026-09-15T10:00:00Z"')),
    ("time_element_datetime", "<html><body><main><time datetime=\"2026-09-15T10:00:00Z\">today</time>Product announced.</main></body></html>"),
    ("meta_beats_time", "<html><head><meta property=\"article:published_time\" content=\"2026-09-01T00:00:00Z\"></head>"
                        "<body><main><time datetime=\"2026-09-15T10:00:00Z\">today</time>Product announced.</main></body></html>"),
    ("time_ignored_once_meta_seen", "<html><body><main><time datetime=\"2026-09-15T10:00:00Z\">x</time>"
                                    "<meta property=\"article:published_time\" content=\"2026-09-01T00:00:00Z\">Product announced.</main></body></html>"),
    ("offset_timezone_is_an_instant", meta('property="article:published_time" content="2026-09-15T10:00:00+09:00"')),
    ("a_bare_day_is_a_day_not_midnight", meta('property="article:published_time" content="2026-09-15"')),
    ("a_bare_day_with_a_trailing_z_is_still_a_day", meta('property="article:published_time" content="2026-09-15Z"')),
    ("an_impossible_day_is_unknown", meta('property="article:published_time" content="2026-09-32"')),
    ("an_impossible_month_is_unknown", meta('property="article:published_time" content="2026-13-01"')),
    ("missing_seconds_is_unknown", meta('property="article:published_time" content="2026-09-15T10:00"')),
    ("an_empty_content_is_unknown", meta('property="article:published_time" content=""')),
    ("no_metadata_at_all_is_unknown", "<html><body><main>Product announced.</main></body></html>"),
    ("an_unrelated_meta_is_not_a_date", meta('property="og:title" content="2026-09-15T10:00:00Z"')),

    # --- JSON-LD: the other place a date hides ------------------------------------
    ("jsonld_news_article", '<html><head><script type="application/ld+json">'
                            '{"@type":"NewsArticle","datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                            "<body><main>Product announced.</main></body></html>"),
    ("jsonld_type_with_schema_prefix", '<html><head><script type="application/ld+json">'
                                       '{"@type":"https://schema.org/NewsArticle","datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                                       "<body><main>Product announced.</main></body></html>"),
    ("jsonld_type_as_a_list", '<html><head><script type="application/ld+json">'
                              '{"@type":["Thing","BlogPosting"],"datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                              "<body><main>Product announced.</main></body></html>"),
    ("jsonld_graph_is_searched", '<html><head><script type="application/ld+json">'
                                 '{"@graph":[{"@type":"Organization"},{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}]}</script></head>'
                                 "<body><main>Product announced.</main></body></html>"),
    ("jsonld_conflicting_dates_are_unknown", '<html><head><script type="application/ld+json">'
                                             '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}</script>'
                                             '<script type="application/ld+json">'
                                             '{"@type":"Article","datePublished":"2026-09-16T10:00:00Z"}</script></head>'
                                             "<body><main>Product announced.</main></body></html>"),
    ("jsonld_agreeing_dates_are_kept", '<html><head><script type="application/ld+json">'
                                       '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}</script>'
                                       '<script type="application/ld+json">'
                                       '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                                       "<body><main>Product announced.</main></body></html>"),
    ("jsonld_a_non_article_type_is_ignored", '<html><head><script type="application/ld+json">'
                                             '{"@type":"WebPage","datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                                             "<body><main>Product announced.</main></body></html>"),
    ("jsonld_malformed_is_skipped", '<html><head><script type="application/ld+json">{not json</script></head>'
                                    "<body><main>Product announced.</main></body></html>"),
    ("jsonld_with_a_charset_parameter", '<html><head><script type="application/ld+json; charset=utf-8">'
                                        '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                                        "<body><main>Product announced.</main></body></html>"),
    ("jsonld_is_not_article_text", '<html><head><script type="application/ld+json">'
                                   '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z",'
                                   '"headline":"SENTINEL_HEADLINE"}</script></head>'
                                   "<body><main>Product announced.</main></body></html>"),
    ("jsonld_is_the_fallback_when_meta_is_absent", '<html><head><script type="application/ld+json">'
                                                   '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}</script></head>'
                                                   "<body><main>Product announced.</main></body></html>"),

    # --- what counts as the article -------------------------------------------------
    ("only_main_text_when_a_main_exists",
     "<html><body>NAVIGATION_NOISE<main>Product announced.</main>FOOTER_NOISE</body></html>"),
    ("article_element_scopes_text_like_main",
     "<html><body>NAVIGATION_NOISE<article>Product announced.</article>FOOTER_NOISE</body></html>"),
    ("the_whole_document_when_neither_exists", "<html><body><div>Product announced.</div></body></html>"),
    ("script_style_and_noscript_are_not_text",
     "<html><body><main><script>SCRIPT_SENTINEL</script><style>STYLE_SENTINEL</style>"
     "<noscript>NOSCRIPT_SENTINEL</noscript>Product announced.</main></body></html>"),
    ("nav_footer_and_header_are_not_text",
     "<html><body><main><nav>NAV_SENTINEL</nav><header>HEADER_SENTINEL</header>"
     "<footer>FOOTER_SENTINEL</footer>Product announced.</main></body></html>"),
    ("hidden_elements_are_excluded_wherever_they_are",
     "<html><body><div><script>SCRIPT_SENTINEL</script></div><main>Product announced.</main></body></html>"),
    ("text_is_whitespace_collapsed",
     "<html><body><main>  Product\n\n   announced.\t</main></body></html>"),
    ("character_references_are_decoded", "<html><body><main>AT&amp;T and &#65;pple</main></body></html>"),
    ("an_unknown_character_reference_is_kept", "<html><body><main>AT&NOTAREAL;T</main></body></html>"),
    ("an_unclosed_tag_does_not_lose_the_text",
     "<html><body><main>Product <b>announced.</main></body></html>"),
    ("a_stray_closing_tag_does_not_lose_the_text",
     "<html><body><main>Product announced.</div></main></body></html>"),
    ("an_attribute_without_a_value_is_not_a_date",
     meta("property=article:published_time")),
]

# Limits are the part of the parser a corpus of ordinary pages never reaches.
FIXTURES.append((
    "jsonld_beyond_the_script_limit_is_ignored",
    "<html><head>"
    + "".join(f'<script type="application/ld+json">{{"@type":"Thing","n":{index}}}</script>'
              for index in range(MAX_JSONLD_SCRIPTS))
    + '<script type="application/ld+json">{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}</script>'
    + "</head><body><main>Product announced.</main></body></html>",
))
FIXTURES.append((
    "jsonld_beyond_the_byte_limit_is_ignored",
    "<html><head><script type=\"application/ld+json\">"
    + '{"@type":"Article","pad":"' + "x" * (MAX_JSONLD_BYTES + 1) + '","datePublished":"2026-09-15T10:00:00Z"}'
    + "</script></head><body><main>Product announced.</main></body></html>",
))


# `_publication_date`: what a raw metadata string is allowed to mean. The distinction that
# matters is `instant` against `date`: only an instant can be placed against the moment
# participation closed, and a bare calendar day never can.
DATE_INPUTS: list[object] = [
    "2026-09-15T10:00:00Z",
    "2026-09-15T10:00:00+09:00",
    "2026-09-15T10:00:00-05:00",
    "2026-09-15T23:59:59Z",
    "2026-09-15",
    "2026-09-15Z",
    "2026-02-29",              # not a leap year
    "2024-02-29",              # a leap year
    "2026-09-15T10:00",        # no seconds
    "2026-09-15T10:00:00",     # no zone
    "2026-09-15T25:00:00Z",    # no such hour
    "2026-09-15T10:00:60Z",    # no such second
    "2026-09-32",              # no such day
    "2026-13-01",              # no such month
    "2026-9-15",               # unpadded
    "15-09-2026",              # wrong order
    "2026-09-15 10:00:00Z",    # space instead of T
    "",                        # empty
    "2026-09-15T10:00:00.500Z",  # fractional seconds
    "2026-09-15T10:00:00z",    # lowercase zone
    None,                      # no metadata at all
    20260915,                  # not a string
]

# `_jsonld_publication`: the other place a publication time hides, and the limits that stop
# a page deciding it by burying one.
JSONLD_INPUTS: list[list[str]] = [
    ['{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}'],
    ['{"@type":"NewsArticle","datePublished":"2026-09-15T10:00:00Z"}'],
    ['{"@type":"BlogPosting","datePublished":"2026-09-15T10:00:00Z"}'],
    ['{"@type":"WebPage","datePublished":"2026-09-15T10:00:00Z"}'],
    ['{"@type":["Thing","Article"],"datePublished":"2026-09-15T10:00:00Z"}'],
    ['{"@type":"https://schema.org/NewsArticle","datePublished":"2026-09-15T10:00:00Z"}'],
    ['{"@type":"Article","datePublished":"2026-09-15"}'],
    ['{"@type":"Article","datePublished":"not a date"}'],
    ['[{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}]'],
    ['{"@graph":[{"@type":"Organization"},{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}]}'],
    ['{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}',
     '{"@type":"Article","datePublished":"2026-09-16T10:00:00Z"}'],
    ['{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}',
     '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}'],
    ['{not json'],
    [''],
    ['{"@type":"Article"}'],
    ['{"nested":' * 12 + '{"@type":"Article","datePublished":"2026-09-15T10:00:00Z"}' + '}' * 12],
]


def fuzz_dates(count: int = 800) -> list[object]:
    """Timestamp-shaped strings, mostly one change away from valid.

    Hand-written vectors cover the rules someone thought of. These cover the boundaries
    between them, which is where a port goes wrong: a leap day, an offset of `+24:00`, a
    zone written in lowercase, a valid shape with one digit changed.
    """
    rng = random.Random(20260920)
    out: list[object] = []
    while len(out) < count:
        style = rng.randrange(4)
        if style == 0:  # a day, perturbed
            value = f"{rng.randrange(0, 10000):04d}-{rng.randrange(0, 14):02d}-{rng.randrange(0, 32):02d}"
            if rng.random() < 0.4:
                value += rng.choice(["Z", "z", "", " ", "T", "Z ", "ZZ"])
        elif style == 1:  # an instant, perturbed
            zone = rng.choice(["Z", "z", "+00:00", "-05:00", "+24:00", "+09:60", "+9:00", "Z+00:00", ""])
            value = (f"{rng.randrange(0, 10000):04d}-{rng.randrange(0, 14):02d}-{rng.randrange(0, 32):02d}"
                     f"{rng.choice(['T', 't', ' '])}{rng.randrange(0, 26):02d}:{rng.randrange(0, 62):02d}:"
                     f"{rng.randrange(0, 62):02d}{zone}")
        elif style == 2:  # a real date, one field changed
            year = rng.choice([2024, 2026, 1900, 2000, 2100, 0, 9999])
            month = rng.randrange(1, 13)
            day = rng.choice([1, 28, 29, 30, 31])
            value = f"{year:04d}-{month:02d}-{day:02d}"
            if rng.random() < 0.5:
                value += f"T{rng.randrange(0, 24):02d}:00:00Z"
        else:  # arbitrary characters from the alphabet that matters
            alphabet = "0123456789-+:TZz.  "
            value = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 30)))
        out.append(value)
    return out


def fuzz_jsonld(count: int = 250) -> list[list[str]]:
    """Structured data of every shape, including the ones the limits exist to refuse."""
    rng = random.Random(20260921)
    kinds = ["Article", "NewsArticle", "BlogPosting", "WebPage", "Organization", "Thing"]
    dates = ["2026-09-15T10:00:00Z", "2026-09-15", "2026-09-15Z", "", "nonsense", None]
    out: list[list[str]] = []
    while len(out) < count:
        depth = rng.randrange(1, 12)
        node: object = {"@type": rng.choice(kinds), "datePublished": rng.choice(dates)}
        for _ in range(depth):
            node = {"@graph": [node, {"@type": rng.choice(kinds), "datePublished": rng.choice(dates)}]}
        script = json.dumps(node)
        if rng.random() < 0.15:
            script = script[: rng.randrange(0, len(script))]  # truncated, so unparseable
        scripts = [script] * rng.randrange(1, 3)
        if rng.random() < 0.1:
            scripts.append("")
        out.append(scripts)
    return out


# The committed corpus, sized so the file stays reasonable. `--hunt` regenerates far more,
# which is how the two divergences this port had were found; it is not committed because a
# repository is not a fuzzing corpus.
COMMITTED_DATES = 800
COMMITTED_JSONLD = 250


def vectors(dates: int = COMMITTED_DATES, jsonld: int = COMMITTED_JSONLD) -> dict[str, object]:
    return {
        "note": "Generated by scripts/generate_article_golden.py; the Python parser is the reference.",
        "cases": [
            {"name": name, "html": html, "text": article_content(html)[0],
             "date": article_content(html)[1], "precision": article_content(html)[2]}
            for name, html in FIXTURES
        ],
        # The two decisions the parser feeds, exported apart from it so each can be ported
        # and held to its own vectors before the tokenizer exists.
        "datetimes": [
            {"input": raw, "date": _publication_date(raw)[0], "precision": _publication_date(raw)[1]}
            for raw in DATE_INPUTS
        ],
        "jsonld": [
            {"scripts": scripts, "date": _jsonld_publication(scripts)[0],
             "precision": _jsonld_publication(scripts)[1]}
            for scripts in JSONLD_INPUTS
        ],
        # Deterministic, so the vectors are the same on every machine and a failure is
        # reproducible from the seed rather than from a saved example.
        "fuzz": {
            "datetimes": [
                {"input": raw, "date": _publication_date(raw)[0], "precision": _publication_date(raw)[1]}
                for raw in fuzz_dates(dates)
            ],
            "jsonld": [
                {"scripts": scripts, "date": _jsonld_publication(scripts)[0],
                 "precision": _jsonld_publication(scripts)[1]}
                for scripts in fuzz_jsonld(jsonld)
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the vectors no longer match Python")
    parser.add_argument("--write", action="store_true", help="Regenerate the vectors")
    parser.add_argument("--hunt", type=int, default=0,
                        help="Regenerate an oversized corpus to search for divergence, then rerun cargo test")
    args = parser.parse_args()
    size = args.hunt or COMMITTED_DATES
    produced = json.dumps(vectors(dates=size, jsonld=max(COMMITTED_JSONLD, size // 4)),
                          indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if args.write:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(produced, encoding="utf-8")
        print(f"Article golden written: {len(FIXTURES)} cases, {size} fuzzed datetimes")
        return 0
    if not args.check:
        parser.error("pass --write to regenerate or --check to verify")
    committed = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
    if committed != produced:
        print(f"{GOLDEN.relative_to(ROOT)} no longer describes what the Python parser does.", file=sys.stderr)
        print("Regenerate it with: python3.11 scripts/generate_article_golden.py --write", file=sys.stderr)
        return 1
    print(f"Article golden verified: {len(FIXTURES)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
