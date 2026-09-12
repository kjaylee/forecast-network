# Web service implementation contract

Status: deployed Cloudflare web beta; current release verification is tracked in
[the verification record](../verification-web.md).
The product handoff remains authoritative. This extends the implemented Python domain.
The participation-points API **0.5.0** is deployed. Production migration and
onboarding grants were verified; stake and settlement paths were checked in
isolated runtimes. The actual live 48-hour lifecycle remains unverified.
The `0.6.0` four-language interface update is deployed. It adds no endpoint,
database migration or domain-contract change; its integration and production
verification must be recorded separately.

## Runtime and ownership

- Cloudflare Python Worker imports the existing `forecast_domain` unchanged through
  a reproducible staging build. D1 holds persistent accounts, sessions, immutable
  artifacts, snapshots, audit events, command receipts, projections and outbox work.
- Static HTML/CSS/ES modules provide a mobile-first interface with English default;
  `0.6.0` adds explicit Korean, Japanese and Traditional Chinese UI packs.
- `packages/application/src/forecast_application/` owns portable async use cases,
  D1/SQLite SQL contracts, projection logic and AI adapters. It never reads Keychain.
- Worker glue owns request validation, Origin checks, cookies, public routing and
  runtime I/O. Root alone provisions resources and reads exact authorized secrets.
- Authentication uses random high-entropy recovery codes and HttpOnly sessions,
  avoiding passwords and a mandatory wallet. Codes are shown once and stored only
  as keyed hashes. Login accepts the recovery code; profile display name is editable.
- All write endpoints require same-origin requests, JSON and a stable idempotency key
  where domain mutations occur. Public errors never expose secrets or stack traces.

## JSON API

Responses: `{ "data": ... }`; errors `{ "error": { "code": "...", "message": "..." } }`.
JSON field names are camelCase at the web boundary; Python domain wire names remain snake_case.
Dates returned to browsers are epoch milliseconds. Probabilities are numeric percentages,
or null when there are no observations. Never invent participants, probabilities or experts.

| Endpoint | Input | Data result |
| --- | --- | --- |
| GET /api/status | — | `{serverTime, providers, chain, challengeHours, version}` |
| POST /api/auth/register | `{displayName}` | `{user,recoveryCode,points}` plus session cookie |
| POST /api/auth/login | `{recoveryCode}` | `{user,points}` plus session cookie |
| POST /api/auth/logout | `{}` | `{ok:true}` clears session |
| GET /api/me | — | `{user:null\|User,reputation,myForecasts,activity,points}`; points is null when signed out |
| PATCH /api/me | `{displayName}` | `{user,points}` |
| GET /api/points | Authenticated session | Private `PointsSummary` for the signed-in account |
| POST /api/me/share-card | `{expectedUserId}` | Owner-published immutable profile snapshot, canonicalJson and snapshotHash |
| GET /api/profile-cards/:hash | — | Only a published profile-share snapshot with a verified hash |
| GET /api/wallet | — | `{wallet:null\|{address,chain,linkedAt},points}` for the authenticated user |
| POST /api/wallet/challenge | `{address,expectedUserId}` | `{challengeId,address,message,expiresAt,chain}` |
| POST /api/wallet/link | `{challengeId,address,signature,expectedUserId}` | `{wallet,points}`; signature is canonical base64 |
| POST /api/wallet/unlink | `{expectedUserId}` | `{wallet:null,points}`; retains ownership and award history |
| GET /api/forecasts | `q,category,sort,cursor` | `{items,nextCursor,counts,dailyIds}` |
| POST /api/forecasts/compile | `{question}` | `{draftId,specification,assessment,duplicateCandidates,aiForecast,expiresAt}` |
| POST /api/forecasts | `{draftId,idempotencyKey}` | `{forecast}` |
| GET /api/forecasts/:id | — | `{forecast,resolution,disputes,audit,comments,myForecast,history,points,stake}`; private points/stake are null when signed out |
| GET /api/forecasts/:id/integrity | — | Canonical specification/resolution JSON, hashes, commitment profile and audit head |
| POST /api/forecasts/:id/forecast | `{outcome,confidence,revision,idempotencyKey,stakePoints,expectedUserId}`; see legacy behavior below | `{forecast,myForecast,points,stake}` |
| POST /api/forecasts/:id/disputes | `{claim,evidenceUrl,ruleClauseId,explanation,revision,idempotencyKey}` | `{forecast,dispute}` |
| POST /api/forecasts/:id/comments | `{text,idempotencyKey}` | `{comment}` |
| POST /api/forecasts/:id/share | `{}` | `{ok:true}` |
| GET /api/activity | — | `{items}` |
| POST /api/activity/read | `{}` | `{ok:true}` |
| GET /api/creators/:id | — | `{creator,forecasts,isFollowing}` |
| POST /api/creators/:id/follow | `{following:boolean}` | `{following}` |

`User`: `{id,displayName,handle,createdAt}`.

`ForecastCard`: `{id,title,question,category,state,revision,openAt,closeAt,createdAt,
creator:{id,displayName,handle},crowd:{probability,count},top:{probability,count},
ai:{probability,provider,model},commentCount,shareCount,specificationHash,chain}`.

`WebSpecification`: `{canonicalQuestion,shareTitle,category,openAt,closeAt,
rules:[{clauseId,outcome,condition}],primarySources:[{name,url}],fallbackSources:[{name,url}],
invalidationRules,ambiguityScore}`.

Detail `forecast` extends ForecastCard with `specification`, `challengeUntil`,
`finalizedOutcome`, `pauseReason`. `resolution` includes proposedOutcome,
confidence, reasonSummary, evidence URLs/content commitments, reviewedAt and provider
provenance. `audit` includes command, old/new state, at, hash and artifactHash.

Public forecast and creator reads work without authentication. `/api/points` and
`/api/wallet` require authentication; `/api/me` returns its signed-out representation.
Authenticated-only requests return 401 with a usable login flow.
Stale revisions return 409 and require refreshed state; expired forecasts/disputes
are rejected even if a scheduled sweep is delayed.

## Participation points — API 0.5.0

The [policy contract](participation-points.md) defines `participation-points-v1`:
profile grant 1,000 once per account; wallet grant 500 once per account and once per
address lifetime; explicit practice at 0 or integer stakes from 1 to 1,000; correct
finalized outcomes return 2× including principal, incorrect outcomes return 0,
and INVALID returns principal. Points have no economic value, purchase, transfer,
redemption or crowd/reputation weighting. Existing commitments retain their policy.

`PointsSummary` contains `{userId,available,committed,total,policy,onboarding,entries}`.
`total` is available plus committed points, not a wallet balance. `policy` returns
its version, grant amounts, stake limit, return multipliers and capability flags.
`onboarding.profile` and `.wallet` describe award completion, amounts and times;
wallet status also reports linkage, eligibility and the reason. `entries` contains
up to 30 recent private ledger rows with available/committed deltas and resulting
balances, stake, returned amount, forecast reference and time.

`stake` is `{amount,status,policyVersion,returned,outcome,forecastRevision}`.
An absent position is practice with amount 0; a settled position records its return.
These account-specific projections are not part of public crowd signals or profile
share snapshots. `GET /api/points` reads the authenticated account only; it accepts
no client-selected account as authority.

Supplying `stakePoints`, including explicit 0, requires string `expectedUserId`.
The Worker compares it with its authenticated user before calling the application.
A missing precondition is `400 account_precondition_required`; an account change
is `409 account_changed`. The number must be an integer, not a boolean or fraction.
Insufficient available points is `409 insufficient_points`; the client refreshes
balances while preserving entered outcome, confidence and stake.

Legacy requests omitting `stakePoints` retain their original command/retry digest
and remain practice when there is no positive commitment. A new update to an
existing positive commitment without an explicit amount returns `409 stake_required`.
An exact legacy retry still resolves through its trusted original receipt. Changing
the amount under an already-used idempotency key is a conflicting operation, not
a new reservation.

Profile creation, its points account and its grant are atomic. Verified wallet
linking and any eligible grant are also atomic; permanent uniqueness survives
unlinking, relinking, profile edits and login. An already-awarded account does not
consume a fresh address's eligibility. Migration `0004_participation_points.sql`
backfills existing profiles and eligible verified links once without charging
historical forecasts.

Before closing, the new total stake reserves or releases only the delta, and the
position follows the latest accepted outcome. Forecast CAS, balance guards, receipt
and immutable ledger append share the same D1 batch. Per-account guards prevent
overspending across different questions. No partial forecast success, negative
balance, duplicate debit or silent reduction is accepted.

Proposals, disputes and pauses do not settle points. A FINALIZED/ARCHIVED record
with its finalization event is required. Settlement runs in the durable outbox
batch with unique position identities and unsettled-state guards, so repeated
delivery cannot credit twice. INVALID refunds remain distinct from correct returns.
The append-only ledger reconstructs all changes. No Solana transaction is involved.

Release verification passed 285 Python tests, 69 frontend tests, 40 schema checks,
Ruff and strict mypy. Production applied migration `0004` through normal pinned
Wrangler processing and reconciled five existing 1,000-point accounts. HTTPS checks
verified a new private account's 1,000-point grant, a real unfunded Ed25519 wallet's
500-point grant, and no additional credit on unlink/relink.

Isolated local HTTP tests covered 700-point cross-question races, retries, legacy
requests, account preconditions and 100-point settlement returns of 200/0/100.
Repeated settlement caused zero new effects, and two local scheduled-trigger
requests returned 200. A disposable remote D1 database also passed full migrations
and ledger semantics and was then deleted. Production tests made no public forecast
or comment writes, so these results do not claim live production stake settlement.
See [points verification](../verification-points.md) for the evidence split.

## Application interface

`Database` async interface: `first(sql,params=()) -> dict|None`,
`all(sql,params=()) -> list[dict]`, `execute(sql,params=()) -> dict`,
`batch(statements: list[tuple[str,tuple]]) -> list[dict]`.
The local test adapter uses SQLite transactions. The Worker adapter bridges D1 methods.

`Application(db, ai, *, now_ms, token_hash, random_token)` exposes named async
methods for endpoint use cases and `run_due_jobs(limit=...)`. Root Worker dispatches
to these methods. Agree exact constructor/method signatures between owners early.
Authentication and SQL services may be split inside the application package.

`AiCoordinator` portable interface: `compile_question(question,candidates,now_ms)`,
`propose_resolution(forecast,now_ms)`, `review_dispute(forecast,dispute,now_ms)`.
Use injected async JSON/text fetchers and configured provider keys; keep credential
values out of artifacts. Return strict domain objects plus retained artifact records.
Compiler output contains specification and ValidationAssessment bound to exact hashes.
Resolution/dispute output must satisfy existing domain provenance and independent-review guards.
Outage pauses automatic resolution; errors must never manufacture evidence or a final result.

## Global content and immutable originals

### Display-only language packs — 0.6.0 deployed

`i18n.mjs` supports `en`, `ko`, `ja` and `zh-Hant`, with English on first visit and
an explicit localStorage preference in `forecast.locale.v1`. There is no browser-
language detection, server-side locale state, or AI request on language switching.
The existing API body, enum values, error codes and idempotency contracts remain
unchanged. Known server error codes map to explicit plain-text messages in all four
catalogs; add error coverage whenever an exposed code changes.

`Intl` affects display formatting only. Locale strings are allowlisted, catalog
messages contain no HTML, and interpolated text is escaped at rendering boundaries.
Document navigation is localized, while original article bodies retain a separate
`lang` and explicit language notice. A switch preserves drafts, recovery and wallet
state and is blocked while sensitive operations are busy. It cannot trigger a
mutation, wallet transaction or repeated grant.

Questions, comments, rules, evidence, source URLs, canonical data, commitments and
exact wallet-signature bytes stay unchanged. No on-demand AI translation button is
included. Existing English compilation and hash-bound historical translations
continue under their own contract below. See [localization architecture](localization.md).

### Compilation and historical translations

The compiler accepts multilingual input and returns an English question, title and
resolution criteria for the existing prepublication preview. It retains the original
input in the compiler artifact. The ambiguity review checks both English output and
preserved intent; exact stated UTC deadlines are also checked deterministically.
English sentences may retain quoted names in other scripts.

Historical Korean specifications are not rewritten. The admin-only
`POST /api/admin/forecasts/:id/translations/en` saves a display translation bound to
the exact published specification hash, including all rule clause identifiers and
their order. The body contains `specificationHash`, `language`, `sourceLanguage`,
`attribution`, `title`, `question`, `rules:[{clauseId,condition}]`,
`invalidationRules`, and nullable `aiRationale`. It retains translation provenance
and correction history. Canonical rules, timestamps, outcomes and hashes stay intact.

Cards use translated text when available; detail includes a separate
`displayTranslation`. The browser verifies its hash binding, labels translated
content, and links to the original integrity record. Display translation does not
participate in resolution or redefine the published contract.

## Optional wallet ownership linking

Wallet linking requires the existing authenticated profile and same-origin write
protections. A five-minute challenge binds the user, origin, address and linking
purpose. The server verifies an Ed25519 signature over its exact retained message,
then atomically consumes the single-use challenge and stores the address link.
One address cannot be attached to multiple profiles. Invalid, expired, mismatched
and replayed challenges cannot create a link. The service retains audit history.

In API 0.5.0, every wallet mutation also requires `expectedUserId`. It is checked
against the authenticated account before challenge creation, link or unlink.
For linking, the Worker strips this transport precondition before passing the
strict `{challengeId,address,signature}` proof to `WalletService`. It does not
alter the signed message or let a client choose the proof's owner. The verified
link can grant participation points only under the once-per-account and
once-per-address rules; unlinking never resets those awards.

The browser uses Wallet Standard discovery and message signing; the Worker uses
the runtime's Ed25519 verifier. See the official
[Wallet Standard discovery protocol](https://github.com/wallet-standard/wallet-standard/blob/master/packages/core/app/src/wallets.ts),
[Solana message-signing interface](https://github.com/anza-xyz/wallet-standard/blob/master/packages/core/features/src/signMessage.ts),
and [Cloudflare Web Crypto support](https://developers.cloudflare.com/workers/runtime-apis/web-crypto/).

The application target is labeled `solana:devnet`. Message signing proves address
control, not a blockchain transaction, a balance, or a selected wallet network.
The service does not request a private key or wallet recovery phrase. Wallet linking
does not replace the recovery-code login and does not imply a deployed Solana program.

## Profile record cards

`expectedUserId` is a publication precondition, never an authority. The Worker
compares it with the authenticated account before creating an artifact. This
prevents a stale tab from publishing another account after a shared cookie changes.
Additional client metrics or identifiers are rejected. Publication is limited to
10 snapshots per account per hour; changing image format or theme reuses one snapshot.

The application reads identity, all-time ledger totals, the newest 20 scored
results, and a selected standout call in one D1/SQLite statement. Pending and
INVALID results do not enter accuracy, Brier or calibration scores. The standout
is the correct call with lowest Brier error, with deterministic date/ID ties;
it is explicitly labeled as a selected example. Fewer than ten scored results
are provisional; zero scored results have null scores.

Owner-triggered publication saves `profile-share-snapshot` in the existing
immutable artifact store. No private account credentials, wallet information or
ongoing individual choices enter this record. Public retrieval accepts only that
artifact kind and verifies its schema, canonical bytes and hash.

Profile snapshots use a separate display commitment format that permits finite
fractional scores; domain integer-only commitments stay unchanged. The prefix is
`forecast-network:sha256:profile-card-json:v1` followed by a newline. Hash the exact
UTF-8 `canonicalJson` bytes after that prefix. The canonical object includes its
commitment profile and excludes the envelope's `canonicalJson` and `snapshotHash`.
Browsers verify these retained bytes before deriving the displayed card data.

The image is generated locally at 1200×675 or 1080×1350 in paper/ink styles. Its
caption links to `/creators/:id?record=:hash`; that page displays the original
snapshot and the recorded result list beside the current profile. Downloads,
native image sharing and caption copying are supported. X's composer receives
text and the link; the user attaches the saved PNG and decides whether to post.
No X credentials, third-party widget script, or automated posting is involved.
[X Web Intents](https://docs.x.com/x-for-websites/web-intents/overview).

## Durable mutations and jobs

Use D1 atomic batches for snapshot CAS, events, receipts and projections/outbox.
A zero-row CAS must force the batch to fail or gate every dependent statement; SQL
success alone is insufficient. Store immutable artifact bytes before anchoring hashes.
Finalization effects and notification insertion are idempotent and transactionally
associated with unique event/effect keys. User forecasts retain revision history but
aggregate only the latest accepted forecast per person.

Scheduled expiry/resolution/review/finalization is lease-protected and bounded.
Provider calls carry timeouts, retry/backoff, per-user/IP and daily cost limits.
Source fetching rejects private/internal targets and revalidates redirects.
Only source-backed, reviewed, immutable published specifications enter the public feed.

## Public information and chain status

Publish `/blueprint`, `/roadmap`, `/privacy` and `/terms` as readable static pages.
The first two derive from `docs/blueprint.md` and `docs/roadmap.md` and must describe
actual status, not imply completed future milestones.
No Solana transaction or verified badge may be fabricated. Chain status must show
the actual adapter/network/transaction confirmation, including pending/unconnected.
Mainnet SOL spending is not authorized by this web deployment request alone.

## Integration details

The Python Workers runtime requires manual redirect handling and retained Python
lists for D1 batches; the adapter follows the platform binding API. Administrative
adjudication accepts strict Resolution/AIProvenance plus retained artifacts at
`POST /api/admin/forecasts/:id/adjudicate` with an explicit revision and idempotency
key. It enters PROPOSED and requires a fresh challenge; it never finalizes directly.
All admin calls require the Keychain-backed ADMIN_TOKEN.
