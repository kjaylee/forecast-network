#!/usr/bin/env python3
"""Publish reviewed display translations without changing canonical specifications."""

from __future__ import annotations

import argparse
import hashlib
import json
from urllib.request import Request, urlopen

from cloudflare_keychain import ROOT, secret

ORIGIN = "https://forecast.eastsea.xyz"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Publish the reviewed translations")
    args = parser.parse_args()
    records = json.loads((ROOT / "apps/web/content/editorial-translations.en.json").read_text())
    for record in records:
        identifier, translation = record["forecastId"], record["translation"]
        request = Request(ORIGIN + f"/api/forecasts/{identifier}/integrity",
                          headers={"User-Agent": "Mozilla/5.0 ForecastEditorial/1.0"})
        with urlopen(request, timeout=30) as response:
            original = json.load(response)["data"]
        canonical = original["specification"]["canonicalJson"]
        digest = hashlib.sha256((original["commitmentProfile"]["prefix"] + canonical).encode()).hexdigest()
        if digest != translation["specificationHash"]:
            raise SystemExit(f"Specification hash mismatch: {identifier}")
        if args.apply:
            request = Request(ORIGIN + f"/api/admin/forecasts/{identifier}/translations/en",
                data=json.dumps(translation).encode(), method="POST", headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 ForecastEditorial/1.0",
                    "Authorization": "Bearer " + secret("ADMIN_TOKEN"),
                })
            with urlopen(request, timeout=30) as response:
                json.load(response)
        print(f"{'Published' if args.apply else 'Verified'} English display translation: {identifier}")


if __name__ == "__main__":
    main()
