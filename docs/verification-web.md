# Web Deployment and Verification Record

The latest wallet-first authentication changes are documented in the
[0.11 verification record](verification-wallet-login.md). Earlier recovery-code
onboarding and optional-link checks below describe their historical releases.

First deployed September 9, 2026 · Document updated September 10, 2026 ·
[Public service](https://forecast.eastsea.xyz)

Cloudflare Workers, D1, and the static web interface were deployed on the operator's
domain. The product is named **Forecast Network**. The original handoff's immutable
specifications, evidence, challenge rules, and restrictions on point purchase,
transfer, redemption, and monetary rewards remain in force.

The English interface, multilingual compilation, historical display translations,
optional wallet ownership linking and participation points are deployed in API
`0.6.0`. This record distinguishes
production HTTPS checks, isolated browser tests, local runtime tests, and work that
still needs long-running validation.

## Verified deployment

| Item | Verified configuration |
| --- | --- |
| Public URL | [forecast.eastsea.xyz](https://forecast.eastsea.xyz) |
| Default URL | [forecast-network.k-jaylee.workers.dev](https://forecast-network.k-jaylee.workers.dev) |
| Worker | `forecast-network`, Python entry point and static assets |
| Storage | Dedicated `forecast-network` D1 database, migrations 0001–0004 applied |
| AI | Live Gemini 2.5 Flash compilation and review; Workers AI independent-review adapter |
| Scheduled processing | Closing, resolution, disputes, finalization, and follow-up work every five minutes |
| Secrets | Supplied from macOS Keychain; excluded from public files and browser code |
| AI execution | Placement hint near `gcp:us-east4`; scheduled work dispatches through a placed fetch handler |

Production registration, session lookup, logout, and recovery login were checked
through real HTTPS requests. Secure and HttpOnly cookie settings and rejection of
cross-Origin mutations were verified. Operational verification accounts did not
submit public forecasts or comments.

## Current release: automated checks

| Area | Verified result |
| --- | --- |
| Domain | 76 tests |
| Cost calculations | Eight tests |
| Application, persistence, authentication, translations, profile records and points integration | 87 tests |
| AI, sources, independent review and English titles | 51 tests |
| Wallet ownership, curve validation, grants and concurrency | 26 tests |
| Participation-points ledger and migrations | 26 tests |
| Scheduled, profile-publication and points transport | 11 tests |
| Python total | **285 passed** |
| Frontend | **94 passed** |
| Generated schemas | 40 contracts matched |
| Static checks | Ruff, strict mypy, and Python parsing passed |
| Web build dependencies | Zero vulnerabilities reported by npm audit for the pinned lockfile |

Unit and regression tests covered time boundaries, specification tampering, invalid
snapshots, retries, revision races, provider outages, truncated responses, date
mismatches, source restrictions, evidence hashes, independent review, and resolution
follow-up work. These checks are distinct from a production test with 48 hours of
actual elapsed time. The audit result is a point-in-time dependency check, not an
external security assessment.

Verification commands from the repository root:

```text
python3 scripts/check.py --tools
node --test apps/web/tests/*.mjs
ruff check apps/web/src
python3 scripts/deploy_web.py --preview --dry-run
```

## Runtime and browser evidence

- **Production:** HTTPS health and storage responses; public question list and detail; blueprint, roadmap, terms, and privacy pages; registration, login, recovery, logout, and Origin checks.
- **Production:** Three editorial questions published through live Gemini review. Each closes at December 31, 2026, 23:59 UTC, stored as `1798761540000` milliseconds.
- **Production:** SHA-256 commitments independently recalculated from public canonical JSON and the domain prefix. These records were not labeled as Solana commitments.
- **Production browser:** Three questions displayed at 1440px desktop and 390px mobile widths, completed loading, no horizontal overflow, and no console errors.
- **Local real Worker and D1:** Forecast creation, update and history; comments; exact retries; stale-revision conflicts; profiles, recovery, and logout. Test data was not copied into production.
- **Local browser:** Registration followed by forecast submission and update, PNG card preview, and image export. The exported PNG was 153,386 bytes; a canceled share was not recorded as completed.
- **Cloudflare AI:** A real REST model call and Worker binding configuration were verified. Complete independent resolution and dispute paths had regression coverage; long-running production validation remains.

The initial editorial questions concern a foldable Apple iPhone, an Apple M6 Mac,
and Microsoft Windows 12 before the end of 2026. Published AI probabilities are
model estimates, not human participation statistics. There were zero human forecasts
at verification. Rejected questions were not published by
bypassing validation.

## English and wallet release evidence

### English default and multilingual input

The new flow accepts input in a supported language, retains that input, compiles an
English question and resolution rules, and requires a user preview before publication.
Published historical Korean specifications remain unchanged. Separate English
display translations are hash-bound to the original specification and translated
content, with an original-integrity view.

A real Korean request about an OLED iPad mini produced an English title, question,
YES/NO/INVALID rules, source policy and AI rationale. The requested deadline,
June 30, 2027 at 23:59 UTC, remained exactly `1814399940000`. The test stopped at the
private preview and did not publish a test question. New share titles omit time
phrases; the authoritative deadline is displayed separately. This prevents a short
title from silently suggesting a different closing date.

The three original editorial forecasts now display English translations. Their
canonical Korean bytes and specification hashes were independently checked and
remain unchanged. Desktop 1440px and mobile 390px feed/detail views and the English
whitepaper/roadmap passed browser checks with no page overflow or console errors.
The original-integrity link and translation attribution are visible. Original
handoffs keep their source language and are not the primary product copy.

### Optional wallet ownership link

Wallet linking preserves profile, recovery-code, and session authentication. A
single-use signature challenge expires after five minutes. The service records the
public address and challenge/audit information to verify ownership. Private keys
and recovery phrases are not collected. No transaction, funds movement, balance
claim, or replacement wallet-login flow is included.

Production HTTPS tests used a newly generated, unfunded Ed25519 key to verify a
real signature, reject a changed message and a replay, persist the verified link,
unlink it, and reject a previously signed pending request after disconnect. The
small-order identity public key was rejected before a challenge was issued.

The production browser flow also passed with an isolated Wallet Standard test
provider using real browser Ed25519 signatures: connect, choose an account, review
the message, explicitly sign, receive server confirmation, and disconnect. This
does not claim testing every real browser extension or hardware wallet.

Independent code checks rejected all eight low-order point encodings and 40
noncanonical encodings, accepted 256 real signing public keys, and matched 1,024
independent curve checks. Race regressions cover stale unlink after a fresh
same-address link, pending-request revocation, late responses after logout, and
wallet account changes. Raw signatures are not retained in audit records; their
hashes and verification metadata are retained. No private key or transaction was
sent to the service.

### Provider routing

Live investigation identified Gemini's `FAILED_PRECONDITION` location restriction
from some default Worker execution locations. Local provider requests and schemas
were valid. Placing HTTP execution near a supported region restored the same
English compilation flow. Scheduled handlers use a self service binding to the
authenticated sweep endpoint because placement applies to fetch handlers.
[Cloudflare placement documentation](https://developers.cloudflare.com/workers/configuration/placement/).

Transport failures now produce an availability error and bounded provider-failure
provenance, rather than an unexplained validation error. Logs retain safe status
codes/categories, never provider credentials or raw error bodies. Provider outages
and future changes to region availability can still interrupt a review; publication
must continue to fail closed.

## Profile record cards

The profile-card release adds owner-published public records, wide/portrait PNGs,
paper/ink variants, native sharing and X-ready captions. Its actual API, image and
privacy evidence is recorded in [Profile Record Card Verification](verification-profile-cards.md).

## Participation points

Policy v1 adds 1,000 points for profile creation and 500 for verified wallet ownership.
Each milestone can be awarded once per account; wallet addresses also have a lifetime
award limit. Commitments are optional, explicitly chosen and independent of confidence.
The [points verification record](verification-points.md) covers actual local D1
migrations, simultaneous HTTP commitments, real wallet signatures and all three
settlement outcomes.

Production migration funded all five existing profiles with 1,000 points each.
A new private verification profile received 1,000 through HTTPS signup, then 500
after a real Ed25519 wallet signature. Unlinking and relinking did not grant again.
The verification wallet was unlinked and its session logged out. Account balances
reconciled with the ledger, and unauthenticated points access returned 401.
Public human forecasts remained zero; no test question or comment was published.

## API latency: D1 co-location and batched reads (2026-09-12)

Measured from Korea with `curl` against forecast.eastsea.xyz, warm isolates, time to
first byte. Before: the Worker ran in gcp:us-east4 while the D1 database lived in APAC
(Singapore), so each of the strictly sequential D1 reads cost roughly 250 ms.

| Request | Sequential D1 reads before | Before | D1 in ENAM, batched | Edge execution + replicas |
| --- | ---: | ---: | ---: | ---: |
| `GET /api/status` (no database) | 0 | ~0.5 s | ~0.35 s | ~0.15 s |
| `GET /api/health` | 1 | ~0.7 s | ~0.43 s | ~0.18 s |
| `GET /api/forecasts` | 3 | ~1.2 s | ~0.46 s | ~0.18 s |
| `GET /api/forecasts/{id}` (signed out) | ~18 | ~5.1 s | ~0.72 s | ~0.30–0.40 s |

The last column is the deployed configuration: no placement hint on the main Worker,
Gemini relayed through the region-placed `forecast-ai-proxy`, D1 read replication with
per-request sessions and a bookmark cookie. `GET /api/admin/ai/health` returned
`{"provider": "gemini", "ok": true, "proxied": true}` from an HKG-executed request, the
manual sweep and six POSTs returned 200, and a GET carrying the bookmark cookie from a
preceding POST returned in 0.18 s. Occasional 0.7–1.3 s samples are isolate cold starts
at edge colos that had not served the Worker before.

Changes: the database was recreated in ENAM and loaded from a verified export
(see [deployment](deployment.md)); the feed, detail and registry-status reads now
travel in one `D1.batch()` round trip each; the rate limiter increments and reads
in one `RETURNING` statement; the points summary probes optional tables once
instead of three times. 652 Python tests, 186 frontend tests, Ruff and strict mypy
passed; 40 consecutive live detail/feed/logout requests returned 200 after deploy.

An intermediate build used `asyncio.gather` over D1 promises instead of `batch()`.
On the Workers Python runtime it failed roughly one request in four with
`SystemError: Cannot enter a promising task from inside another running promising
task. This is a bug in Pyodide.` raised at `initPyInstance` of the *next* request
on the same isolate, returning an empty 500 before the handler ran. Concurrent
Python tasks that each await a binding promise are therefore not used anywhere in
the Worker; one awaited JS promise at a time, with `batch()` for fan-out.

Python Worker cold starts still add 2–4 s to the first request on a fresh isolate;
that is a platform cost and was not changed here.

## Remaining limitations

The full 48-hour proposal, challenge, and finalization cycle, long-running provider
failure and recovery, actual load, and disaster-recovery drills are not complete.
UI and HTTP checks are not an external security audit or complete accessibility
certification. Cloudflare Python Workers is a beta platform.

The Solana program and live on-chain receipt adapter are not implemented. Current
commitments are audit records in web storage; related follow-up work awaits its
adapter. This deployment used no SOL and issued no DONG, HAE, BADA, or other
real coins. Optional wallet linking does not change that implementation boundary.

The synthetic-asset Safety Kernel is research on the [separate roadmap](roadmap.md).
Its implementation, simulation, and Devnet validation are not complete. Arithmetic
differences and missing definitions are documented in the
integrated-design review (private).


## Four-language release

API `0.6.0` adds English, Korean, Japanese and Traditional Chinese interface packs,
with 645 messages per locale. It adds no dependency or database migration. Tests
verified saved language preference, form/recovery preservation, localized errors
and stable signatures and record hashes. Production desktop/mobile checks and
physical Seeker Chrome touch/keyboard checks passed; see
[localization verification](verification-localization.md). MWA and Devnet remain
unimplemented, independently of the verified mobile screen behavior.
