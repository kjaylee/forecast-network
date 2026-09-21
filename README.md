# Forecast Network

Production foundation and deployed web service for a global, mobile-first forecasting network.
The product and architecture handoff (maintained privately) is the source of truth. **Milestone 1** is complete, and the M3/M4/M5 web beta is deployed.

**Live service:** [forecast.eastsea.xyz](https://forecast.eastsea.xyz) ·
[Whitepaper](https://forecast.eastsea.xyz/blueprint) ·
[Roadmap](https://forecast.eastsea.xyz/roadmap)

## How it is deployed (0.13.0)

`forecast.eastsea.xyz` is served by [`apps/web-rs`](apps/web-rs), a Rust Worker
compiled to `wasm32` with workers-rs. Since 2026-09-21 it owns the whole surface:
every public read, every POST and PATCH, the operator routes, the AI pipeline,
the registry delivery, and both schedules — the per-minute risk tick and the
five-minute sweep run as its own scheduled events, in-process, with no bearer
and no operator host in the path. The Python Worker stays deployed behind a
`LEGACY` service binding as the rollback; nothing is forwarded to it, and every
request that would be is logged so retiring it is decided on evidence.

The Python Worker moved behind the Rust edge because Pyodide cannot run
concurrent promising tasks: it returned an empty 500 at roughly 25 concurrent
requests and could not hold a per-minute cron. Python remains the reference
implementation and its test suite remains the acceptance suite.

Reads were ported behind measured route parity before the first cutover — 18/18
forecast query combinations, 17/17 detail ids and 39/43 read paths byte-equal
([cutover record](docs/evidence/completion/rust-edge/cutover-2026-09-16.json)).
The writes followed behind three source-level gates (`scripts/sql_parity.py`,
`route_parity.py`, `config_parity.py`) and one live one: `scripts/http_parity.py`
compared 167/167 answers between a preview edge and the Python Worker before the
flip ([write-flip record](docs/evidence/completion/rust-edge/write-flip-2026-09-21.json)).
The [strangler plan](docs/plans/2026-09-16-rust-rewrite.md) holds the history and
the [improvement review](docs/plans/2026-09-21-improvement-review.md) what remains.

The web service supports wallet-authenticated profiles, multilingual
input compiled into English for preview/publication, discovery, probability forecasts and history,
comments, disputes, profiles, activity, PNG sharing and canonical specification
verification. Dedicated Cloudflare D1 persistence protects atomic changes and retries.
**Share my record** adds landscape/portrait profile PNGs, paper/ink styles, native
image sharing and X-ready captions. The owner-published record link preserves
the original statistics and finalized calls; it never exposes wallet or recovery data.
Wallet sign-in uses a single-use ownership signature; new profiles have no site
recovery code. Existing guest profiles can migrate while preserving their history.
See [wallet authentication](docs/architecture/wallet-authentication.md). Separately, the
Forecast registry is deployed on Solana Devnet, and all three current public
forecast revisions were confirmed using a one-time Mac operator synchronization.
Automatic delivery from Cloudflare is blocked by RPC access. Historical specifications retain their original hashes
with separately bound English display translations.
Evidence-cutoff eligibility in API 0.10 excludes confirmed late entries, restores
the last valid pre-evidence forecast and stake, and returns only invalid committed
points. Ordinary resolution also holds uncertain publication timing before rewards.
See the [receipt eligibility policy](docs/architecture/receipt-eligibility.md).
Scheduled processing implements expiry, resolution, challenge completion and outbox
delivery. The [web verification record](docs/verification-web.md) separates live
checks from local runtime tests and unverified long-running behavior.

Three editorial questions were published using actual Gemini compilation. Human
participation was zero at verification; missing crowd data remains empty.

**Devnet registry — 0.9.0:** The actual 101,008-byte program is deployed and its
bytes were verified at finalized commitment. Real Devnet transactions exercised
publication, early locking, resolution proposal and challenge entry, and rejected
unauthorized, duplicate, stale and premature-finalization requests. The real
48-hour finalization interval has not yet been observed. The public iPhone question
is confirmed at revision 6 / CHALLENGE; Windows and M6 Mac are confirmed at revision
2 / OPEN. These records were synchronized from this Mac through the same durable
adapter. Cloudflare RPC access was subsequently restored through an authenticated
Devnet RPC proxy, and automatic relay is enabled
(`SOLANA_REGISTRY_RELAY_ENABLED=true`), which the live `/api/status` reports. The
five-minute scheduled sweep has delivered and confirmed canonical registry
revisions unattended. The 48-hour finalization interval has still not been observed
end to end, so continuous delivery of future revisions is not claimed. Existing
confirmed records remain readable.
See [Devnet verification](docs/verification-devnet.md) and
[storage and trust](docs/architecture/storage-and-trust.md).

Four signing roles are stored separately in this Mac's Keychain. Only the hot
relayer seed is supplied to Workers Secrets. Funding used **2 Devnet SOL** from
an existing local wallet; **no mainnet SOL** was spent and no economic asset was
issued. The registry attests application commitments and remains upgradeable.

**Participation points, introduced in `0.5.0`:** Policy v1
grants 1,000 points once per account and 500 for verified wallet ownership, once per
account and once per address over its lifetime. Forecasts explicitly choose practice
with 0 points or commit 1–1,000. A correct finalized outcome returns twice the amount
including the original commitment; an incorrect outcome returns 0; INVALID refunds
the original amount. Atomic reservations and an immutable ledger protect updates
and retries. Points have no economic value and never weight crowd probability or
reputation. See the [participation-points contract](docs/architecture/participation-points.md).
Production migration and HTTPS checks verified one-time profile and wallet grants.
Stake races, retries and all settlement outcomes were verified in isolated runtimes,
not through public production forecasts. See the [points verification](docs/verification-points.md).

**Deployed in `0.6.0`:** English, Korean, Japanese and Traditional Chinese
interface packs (`en`, `ko`, `ja`, `zh-Hant`). English remains the first-visit
default; only an explicit saved choice changes it. UI labels, errors, image copy
and document navigation are localized. User content, article language, canonical
records and wallet-signature bytes stay unchanged. See
[localization architecture](docs/architecture/localization.md).

**Content translation in `0.7.0`:** Forecast titles now have a Translate / Original
control outside the English interface. English-source questions do not show a
redundant translation button in English. The server translates the question, conditions and existing AI rationale
into Korean, Japanese or Traditional Chinese, reviews the result and caches it
against the exact English display source. Published rules remain authoritative.
See [translation architecture](docs/architecture/display-translations.md).

**Known-outcome containment in `0.7.1`:** Audited participation holds stop new
forecast/stake changes without changing original rules or declaring a final outcome.
The already-announced foldable iPhone question is held for review. See
[verification](docs/verification-participation-holds.md).

**Runtime 0.8.0 — deployed:** Official-source
observation now connects containment to a versioned early positive-review path.
A persistent funded LMSR and explicit quote/confirmation interface run with isolated
test points; active participation-point markets remain disabled. A nonbillable
service-reserve sandbox tests uncertain costs and full refunds without accepting
payments. The original 40 schemas remain unchanged; 52 contracts are generated.

Actual local Worker/D1 checks verified a 100-test-point order returning 190.902828
claims if correct, a YES price change from 50% to 54.76%, and a shadow balance change
from 1,000 to 900 with actual participation points unchanged. This is local evidence,
not a live market or completed real-provider resolution claim. Later production
checks verified the iPhone question's early YES proposal; normal 48-hour challenge
gates remain in force.

See [runtime architecture](docs/architecture/autonomous-runtime.md) and
[verification and rollout gates](docs/verification-autonomous-runtime.md).
The earlier economics proposal (private research record) is not part of this repository. Advertising is excluded; question creation is still
free, no payment adapter is enabled, and operating costs remain operator-funded.

## Solana Seeker app (CLOCK IN hackathon)

Forecast ships as a native Android shell for the Solana Seeker, built on
Mobile Wallet Adapter and the Seeker's Seed Vault. Everything below runs on a
real device today.

| | |
| --- | --- |
| APK | [forecast-0.9.0.apk](https://github.com/kjaylee/forecast-network/releases/download/v0.9.0/forecast-0.9.0.apk) (signed release, `xyz.eastsea.forecast`) |
| Source | [`apps/android`](apps/android) — Capacitor shell, [`MobileWalletPlugin.kt`](apps/android/android/app/src/main/java/xyz/eastsea/forecast/MobileWalletPlugin.kt), [`ResultNotifications.kt`](apps/android/android/app/src/main/java/xyz/eastsea/forecast/ResultNotifications.kt) |
| Docs | [docs/android.md](docs/android.md) |

What the Seeker does that the browser cannot:

- **Sign in with Seed Vault** — MWA `signMessagesDetached` ownership proof; no password, no recovery code, keys never leave the vault.
- **Stamp a forecast on-chain** — after recording a forecast, the wallet co-signs a Devnet memo carrying the receipt hash. The service pays the fee as a partially signed transaction; the phone fetches the blockhash, submits and reports the signature (`packages/application/.../attestation.py`). Nothing of value moves and the forecast itself stays private.
- **Seeker Genesis Token badge** — the service checks mainnet for the Token-2022 SGT in the signed-in wallet and shows a verified Seeker badge on the public record (`seeker.py`).
- **Native share sheet, App Links, result notifications** — record-card PNGs go straight to the Android share sheet; `forecast.eastsea.xyz/forecasts/*` links open in the app; a background job turns finalized outcomes into local notifications.

Everything else — multilingual questions, probability forecasts, points, evidence
reports, AI-assisted resolution with a 48-hour challenge window — is the same
production service the web uses.

## Run the checks

Python 3.11 or newer is required. The domain and its test suite have **no third-party
runtime dependencies**. Run from the repository root:

```sh
python3 scripts/check.py
```

The check runs domain/contract tests, parses Python sources and verifies generated
schemas. If Ruff and mypy are already installed, run the additional static checks:

```sh
python3 scripts/check.py --tools
node --test apps/web/tests/*.mjs
TMPDIR="$PWD/tmp" CARGO_TARGET_DIR="$PWD/tmp/cargo-target" cargo test --workspace --locked
```

Pull requests run the dependency-free Python matrix, Ruff and strict mypy,
the browser-module regression suite, and the Rust registry guard tests. The root
Python check also parses the Cloudflare entry point; `--tools` lints it.

The September 14 local review fixes and icon/card polish passed 697 Python tests,
250 browser-module tests and 23 native registry tests. See the
[verification record](docs/verification-review-polish.md) for the additive Seeker
migration, visual evidence and deployment limitations.

Regenerate schemas after an intentional contract change, then review the diff:

```sh
python3 scripts/generate_schemas.py
python3 scripts/generate_schemas.py --check
```

The package uses a standard `src` layout. Tools configure the import path directly;
an installation is unnecessary to run verification. Packaging uses setuptools as
its build tool and declares no runtime dependencies. All task scratch data and
tool caches belong in `tmp/`.

API `0.5.0` verification: **285 Python tests, 69 frontend tests, 40 schemas**, Ruff
and strict mypy passed. Production onboarding checks and isolated ledger/runtime
checks are recorded separately; the live 48-hour lifecycle remains unverified.
The `0.6.0` release passed 285 Python tests, 94 frontend tests and 40 schema checks.
Four-language production checks and Seeker Chrome touch/keyboard verification are
recorded in [localization verification](docs/verification-localization.md).
The `0.7.0` translation release passed 315 Python tests, 120 frontend tests and
40 schema checks. See [translation verification](docs/verification-display-translations.md).
The application/domain logic uses the Python standard library;
the web adapter uses the pinned Cloudflare platform and build tools in `apps/web`.

The `0.9.0` verification baseline passed **555 Python tests, 131 frontend tests,
23 Rust guard tests, 52 schemas**, Ruff and strict mypy across 28 source files.
The current tree passes **923 Python tests, 275 browser-module tests and 64
generated schemas**, plus Ruff and strict mypy. The branch checks additionally
build and lint the Rust edge that serves production and build the `domain-rs`
crate for `wasm32`.
Local-validator and Devnet transaction evidence are recorded separately in
[Devnet verification](docs/verification-devnet.md); these test counts do not
prove elapsed live finalization or resistance to every coordinated attack.

## Web structure and deployment

| Path | Responsibility |
| --- | --- |
| `packages/domain/src/forecast_domain` | Immutable models, lifecycle, serialization and audit contracts |
| `packages/application/src/forecast_application` | Authentication, use cases, persistence contracts, AI and sources |
| `apps/web/src` | Cloudflare HTTP, D1 and provider adapters |
| `apps/web/public` | Responsive UI and PNG sharing |
| `apps/web/migrations` | Versioned D1 changes; never edit an applied migration |
| `programs/forecast_registry` | Native Solana Devnet registry and authority/transition guards |
| `schemas/v1` | Generated versioned JSON contracts |
| `docs` | Whitepaper, roadmap, architecture, research and verification |
| `scripts` | Checks, generation, document build and Keychain-based deployment |

Follow [deployment and operations](docs/deployment.md) to prepare pinned tooling
under repository `tmp/`. Credentials come from macOS Keychain through deployment
environment/stdin; they are never public assets. With the tooling prepared:

```sh
python3 scripts/deploy_web.py --preview --dry-run
python3 scripts/deploy_web.py
```

The deployment command runs checks, renders documents, applies D1 migrations,
deploys the Worker and registers runtime secrets. Cloudflare Python Workers is a
beta platform dependency.

## What is implemented

- `ForecastSpecification`, `Forecast`, `UserForecast`, `Resolution`, `Dispute`,
  `UserReputation`, and `CreatorProfile`, with immutable supporting records.
- Deterministic lifecycle commands with validation, timing, challenge/dispute,
  escalation, provider pause/recovery, finalization and archive guards.
- Optimistic revisions, command receipts, hash-linked audit events, and explicit
  finalization effects for later reputation/notification adapters.
- Strict, versioned JSON decoding, generated Draft 2020-12 schemas and a documented
  canonical SHA-256 commitment format.
- Failure-path tests, contract drift checks and a dependency-free CI matrix.
- Versioned early positive triggers, conditional official-source observation and
  containment-before-review work queues; actual early YES proposal verified in production,
  with finalization awaiting its full challenge period.
- Atomic funded LMSR quotes/fills, isolated shadow accounts and retained receipts;
  an active adapter is present but gated off.
- Nonbillable service-cost and refund accounting sandbox; no real payment adapter.
- Application-level participation-point policy, private balance/history reads,
  one-time onboarding grants, atomic stake reservations and final-outcome settlement;
  deployed in `0.5.0`, with production grant checks and isolated stake/settlement evidence.

There are no purchasable or transferable prediction points, redemption, monetary
settlement, tradable reputation or asset-layer dependencies.

## Design and boundaries

Read [repository architecture](docs/architecture/repository.md) for ownership and
future milestone locations, [domain contracts](docs/architecture/domain-contracts.md)
for wire/commitment rules, and [lifecycle and persistence](docs/architecture/lifecycle.md)
for transitions and adapter obligations.

The [Solana compression and deployment budget study](docs/research/solana-compression-budget.md)
compares compact PDAs, state pages, SPL compression and Light Compressed PDAs using
dated mainnet rent observations. Its byte sizes and deployment budgets are design
scenarios for Milestone 2, not measured Forecast program builds.

The public API lives in `forecast_domain.models`, `forecast_domain.lifecycle` and
`forecast_domain.serialization`. A command receives an immutable aggregate, an
expected revision, an idempotency key, and an explicit execution timestamp. It
returns a new aggregate, an audit event and a receipt. An exact retry with a trusted
persisted receipt returns the current aggregate unchanged and no new event.

The deployed web beta advances M3/M4/M5 without renumbering the handoff milestones.
Production HTTPS authentication/recovery and genuine AI publication were checked;
participation, comments, contention and PNG export were checked in the local real
runtime/browser. The full live 48-hour lifecycle, long-term recovery, load testing,
and autonomous Devnet relay verification remain pending. Native wallet sign-in has
separate physical Seeker evidence; see [Android](docs/android.md) and
[wallet-login verification](docs/verification-wallet-login.md). See the [web API](docs/architecture/web-api.md)
and [roadmap](docs/roadmap.md) for their acceptance criteria.

The original web releases spent no SOL. The Devnet registry now uses test SOL;
no mainnet SOL was spent and no actual coins were issued. A separate synthetic-asset
risk research track exists outside this repository; it introduces no funds, coins or
payouts into the forecasting service.

Release0.8.0 production checks passed:487 Python tests,131 frontend tests,52schemas,
Ruff and strict mypy. Official observation and early YES proposal work on the live
iPhone question; finalization remains subject to the full challenge period.
Shadow markets are available; actual point-market activation and real billing
remain off. See [runtime verification](docs/verification-autonomous-runtime.md).
