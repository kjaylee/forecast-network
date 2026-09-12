# Forecast content translation verification

Date: September 10, 2026. Release: 0.7.0.

## Automated and isolated runtime checks

The complete Python suite passes 315 tests, with 40 generated schemas, Ruff and
strict mypy across 18 modules. The frontend suite passes 120 tests. New tests cover
public/same-origin transport, cache reuse, source corrections, immutable storage,
concurrency, expired/replaced leases, lost commit acknowledgements, failed reviews,
artifact integrity, numeric guards, locale changes and stale browser responses.
The new backend translation suite also passed under Python optimization (`-O`).

Migration 0005 applied successfully using pinned Wrangler against isolated D1.
Actual Gemini requests produced Korean, Japanese and Traditional Chinese reading
copies. Browser verification at 400 and 1440 pixels checked title/rule translation,
Original restoration, exact unsubmitted comment preservation, no horizontal overflow
and no JavaScript errors. Cache replay retained the same translation hash.

## Production acceptance

Cloudflare migration 0005 and Worker 0.7.0 are deployed at
https://forecast.eastsea.xyz. Actual production generation and review passed for
Korean, Japanese and Traditional Chinese on a genuine published question. Six
cache replays (GET and repeated POST for each language) returned the exact accepted
translation. All three published forecasts retained their original integrity
responses byte-for-byte at the parsed-response level.

The production browser confirmed feed and detail translation, AI-rationale display,
Original restoration and exact preservation of an unsubmitted comment. On the
connected Seeker (Android 16, Chrome 152, 400 × 797 CSS pixels, DPR 3), actual touch
input verified all three targets, the English target chooser and Original controls.
No horizontal overflow or JavaScript errors occurred. The original interface
preference was restored; no test forecast, comment, wallet signature or transaction
was submitted to production.

A live Japanese result exposed full-width parentheses around an unchanged URL.
The URL validator now recognizes Japanese/Chinese delimiters; regression tests
accept that formatting while still rejecting a changed source address.

## Limits

AI translation remains a reading aid. A second review pass may use the same provider;
it is not a guarantee of semantic equivalence. Original published rules govern
resolution. No Devnet program, Mobile Wallet Adapter, transaction, on-chain proof,
paid points or transferable rewards is added by this release.
