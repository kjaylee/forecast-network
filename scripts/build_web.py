#!/usr/bin/env python3
"""Stage deployable sources/assets under tmp and render public documents."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/web"
STAGE = ROOT / "tmp/web-build"
ORIGIN = "https://forecast.eastsea.xyz"


def mobile_wallet_assets(public: Path) -> str:
    subprocess.run(["node", str(ROOT / "scripts/build_mobile_wallet.mjs")], cwd=ROOT, check=True)
    directory = ROOT / "tmp/mobile-wallet-assets"
    metadata = json.loads((directory / "csp.json").read_text())
    bundle = directory / "mobile-wallet-sdk.mjs"
    if (metadata["sdkVersion"] != "0.6.0" or metadata["bundlerVersion"] != "0.28.1"
            or metadata["sha256"] != hashlib.sha256(bundle.read_bytes()).hexdigest()):
        raise ValueError("Mobile wallet bundle metadata does not match reviewed dependencies")
    elements, attributes = metadata["styleElementHashes"], metadata["styleAttributeHashes"]
    if not elements or any(not re.fullmatch(r"'sha256-[A-Za-z0-9+/]{43}='", item) for item in [*elements, *attributes]):
        raise ValueError("Mobile wallet style hashes are invalid")
    for name in ("mobile-wallet-sdk.mjs", "mobile-wallet-sdk.LICENSE.txt"):
        shutil.copy2(directory / name, public / name)
    return ("default-src 'self'; script-src 'self'; style-src 'self'; "
            "style-src-elem 'self' " + " ".join(elements) + "; "
            "style-src-attr 'unsafe-hashes' " + " ".join(attributes) + "; "
            "img-src 'self' data:; connect-src 'self' ws://localhost:*/solana-wallet http://localhost; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; upgrade-insecure-requests")


def replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def documents() -> dict[Path, str]:
    mapping = {path.resolve(): "/docs/" + path.relative_to(ROOT / "docs").with_suffix("").as_posix()
               for path in (ROOT / "docs").rglob("*.md")}
    for name in ("blueprint", "roadmap", "privacy", "terms"):
        mapping[(ROOT / "docs" / f"{name}.md").resolve()] = "/" + name
    return mapping


def render_document(path: Path, route: str, mapping: dict[Path, str]) -> str:
    source = path.read_text(encoding="utf-8")
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = md.parse(source)
    ids: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            text = tokens[index + 1].content
            base = re.sub(r"[^\w -]", "", text.lower()).strip().replace(" ", "-") or "section"
            ids[base] = ids.get(base, 0) + 1
            token.attrSet("id", base + (f"-{ids[base]}" if ids[base] > 1 else ""))
        if not token.children:
            continue
        local_links: list[bool] = []
        for child in token.children:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                parsed = urlsplit(href)
                converted = True
                if not parsed.scheme and not parsed.netloc and parsed.path and not href.startswith("/"):
                    target = (path.parent / unquote(parsed.path)).resolve()
                    if target in mapping:
                        child.attrSet("href", mapping[target] + ("#" + parsed.fragment if parsed.fragment else ""))
                    elif target.suffix == ".json" and target.is_relative_to(ROOT / "docs/research/evidence"):
                        child.attrSet("href", "/docs/research/evidence/" + target.name)
                    else:
                        child.tag, child.attrs = "span", {"title": "Implementation material in the repository"}
                        converted = False
                if parsed.scheme in {"http", "https"}:
                    child.attrSet("rel", "noopener noreferrer")
                local_links.append(converted)
            elif child.type == "link_close" and local_links:
                if not local_links.pop():
                    child.tag = "span"
    rendered = md.renderer.render(tokens, md.options, {})
    title = next((line.removeprefix("# ") for line in source.splitlines() if line.startswith("# ")), "Forecast documentation")
    original_language = "en"
    language_key = "common.documentOriginal" if original_language != "en" else "common.documentLanguage"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Forecast</title><meta name="description" content="Public design and operating principles for the Forecast network">
<meta property="og:title" content="{html.escape(title, quote=True)} · Forecast"><meta property="og:type" content="article">
<link rel="canonical" href="{ORIGIN}{route}"><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/documents.css">
<script type="module" src="/document-i18n.mjs"></script></head>
<body><a class="skip-link" href="#document" data-document-i18n="common.skip">Skip to content</a><div class="document-layout">
<header class="document-top" translate="no"><a class="brand" href="/">forecast<span class="brand-dot">.</span></a>
<nav aria-label="Public documentation" data-document-nav><a href="/blueprint" data-document-i18n="common.whitepaper">Whitepaper</a><a href="/roadmap" data-document-i18n="common.roadmap">Roadmap</a><a href="/" data-document-i18n="common.explore">Explore forecasts</a></nav>
<span id="document-language-control" class="language-control" aria-hidden="true"></span></header>
<p class="document-language-note" translate="no" data-document-i18n="{language_key}">This document is shown in its original language.</p>
<main id="document" class="document" lang="{original_language}">{rendered}</main>
<footer class="document-footer" translate="no"><a href="/" data-document-i18n="common.network">Forecast network</a><a href="/blueprint" data-document-i18n="common.whitepaper">Whitepaper</a><a href="/roadmap" data-document-i18n="common.roadmap">Roadmap</a>
<a href="/privacy" data-document-i18n="common.privacy">Privacy</a><a href="/terms" data-document-i18n="common.terms">Terms</a></footer></div></body></html>'''


def build(*, preview: bool = False) -> Path:
    STAGE.mkdir(parents=True, exist_ok=True)
    modules = STAGE / "python_modules"
    modules.mkdir(exist_ok=True)
    for package, base in (("forecast_domain", "domain"), ("forecast_application", "application")):
        replace_tree(ROOT / f"packages/{base}/src/{package}", modules / package)
    replace_tree(SOURCE / "public", STAGE / "public")
    content_security_policy = mobile_wallet_assets(STAGE / "public")
    replace_tree(SOURCE / "src", STAGE / "src")
    replace_tree(SOURCE / "migrations", STAGE / "migrations")
    stale_entry = STAGE / "entry.py"
    if stale_entry.exists():
        stale_entry.unlink()
    for name in ("pyproject.toml", "package.json", "package-lock.json"):
        shutil.copy2(SOURCE / name, STAGE / name)
    config = json.loads((SOURCE / "wrangler.jsonc").read_text())
    if preview:
        config.pop("routes", None)
    (STAGE / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    mapping = documents()
    for path, route in mapping.items():
        target = STAGE / "public" / route.lstrip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_document(path, route, mapping), encoding="utf-8")
    replace_tree(ROOT / "docs/research/evidence", STAGE / "public/docs/research/evidence")
    shutil.copy2(SOURCE / "documents.css", STAGE / "public/documents.css")
    (STAGE / "public/_headers").write_text(f"""/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  Content-Security-Policy: {content_security_policy}
  Cache-Control: no-cache
""")
    (STAGE / "public/robots.txt").write_text(f"User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {ORIGIN}/sitemap.xml\n")
    routes = ["/", "/explore", "/blueprint", "/roadmap", "/privacy", "/terms"]
    (STAGE / "public/sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{ORIGIN}{r}</loc></url>" for r in routes) + "</urlset>\n")
    print(f"Web staged: {STAGE}; {len(mapping)} rendered documents; original domain reused")
    return STAGE


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Use workers.dev before assigning the custom domain")
    build(preview=parser.parse_args().preview)
