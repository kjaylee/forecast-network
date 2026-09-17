# Forecast Network Roadmap

What ships, and what must be verified before it is complete.

Version 0.11 · Updated September 14, 2026

**Web beta deployed:** [forecast.eastsea.xyz](https://forecast.eastsea.xyz) ·
[Deployment and verification record](https://forecast.eastsea.xyz/docs/verification-web)

The product handoff (private) defines the principles
and Milestones 1–7. The [blueprint](blueprint.md) explains product, trust, and cost
design. Progress is measured by working features and acceptance evidence, not
promised dates. Phases 1–6 of the integrated risk design (private)
are a separate research track; their numbering and completion status are not shared
with Forecast milestones. Original handoffs retain their source language; the
primary public documentation and product default are English.

## Current position

| Stage | Status | Evidence and remaining boundary |
| --- | --- | --- |
| M1: domain | Complete | Seven models, 12 states, 18 commands, 40 schemas; 76 domain tests in the original verification |
| Compression and budget research | Research complete | Eight cost tests; earlier 84-test combined baseline; no program verification |
| M3: API and persistence | Beta deployed; key paths verified | Production HTTPS, D1, authentication and recovery; local real-HTTP submissions, comments, retries and races |
| M4: AI routing | Implemented; live compiler verified | Gemini publication, Cloudflare AI REST call and binding; independent-review regression tests; long-running production cycle pending |
| M5: mobile experience | Mobile web and native Android shell implemented | Mobile/desktop browser evidence and physical Seeker wallet sign-in; native sharing, app links and local result-notification adapters are documented in [Android](android.md) |
| English default and multilingual input | Deployed and verified | Live Korean-to-English preview preserves the exact deadline; historical translations preserve canonical hashes |
| Optional wallet linking | Deployed and verified | Real Ed25519 ownership proof, replay rejection and disconnect tested over HTTPS; separate from M2 |
| Profile record sharing | Deployed and verified | Owner-published immutable records, wide/portrait PNGs, truthful samples and public verification links |
| Participation points | Deployed in API 0.5.0 | Production migration/grants verified; stakes, races and settlement verified in isolated runtimes |
| Four-language interface | Deployed and verified in 0.6.0 | `en`, `ko`, `ja`, `zh-Hant`; state preservation, production checks and Seeker Chrome layout/input verified |
| Official-source observation and early positive review | Deployed in 0.8; eligibility hardening in 0.9 rollout | Actual early YES proposal verified; new transactional gates prevent unresolved timing reviews and unhealthy watches from being bypassed |
| Evidence-cutoff receipt eligibility | Implemented in API 0.10 | Inclusive late-entry exclusion, original-stake restoration, exact-once refunds, market solvency, ordinary-resolution timing holds and four-language notices; delayed semantic timing adjudication remains held |
| Wallet-first sign-in | Deployed; actual Seeker login verified in 0.11.1 | Signed login, existing-profile migration, no new site recovery codes, browser-bound sessions, signature-only wallet compatibility and restoration after reload |
| Funded point markets | Shadow runtime verified locally | Persistent quotes/fills and changing prices; isolated test balances; active point adapter disabled |
| Service-cost and refund accounting | Nonbillable sandbox verified locally | Uncertain costs, refunds and replay protection; no payment adapter or revenue claim |
| M2: Solana registry | Program deployed; three public revisions confirmed by operator synchronization, automatic delivery resumed via an authenticated RPC proxy | Exact binary and finalized current histories verified; scheduled sweep delivers and confirms revisions unattended; elapsed 48-hour finalization remains unverified |
| Key custody | Four roles stored in this Mac's Keychain | Separate relayer, upgrade/administrator, program and buffer identities; only the hot relayer is a Worker secret; off-device recovery is not yet verified |
| M6: reliability | Core safeguards tested; operational hardening continues | Retries, concurrency, CSRF, resolution and outbox regression coverage; 48-hour lifecycle, recovery drills and long-running operations pending |
| M7: load and simulation | Planned | Actual-adapter load and adversarial scenarios pending |
| Synthetic-asset research Phases 1–6 | New design; incomplete | Separate models, simulator, Safety Kernel and Devnet evidence required |

The deployed API `0.5.0` release passed **285 Python tests, 69 frontend tests,
40 schema checks, Ruff, and strict mypy**. English compilation, historical
translations and wallet ownership were verified over production HTTPS. There were
three editorial questions and zero human forecasts at verification; these are
not traffic or long-term resolution metrics. See the [release evidence](verification-web.md).
Production points checks verified migration and one-time grants without public
forecast or comment writes. Isolated runtime checks verified stakes and final-outcome
adjustments; the actual production 48-hour lifecycle remains unverified.
Later localization and translation releases have their own verification records.
The 0.8 runtime extension likewise separates isolated tests from production evidence;
earlier release counts do not certify it.

The `0.9.0` baseline passed **555 Python tests, 131 frontend tests, 23 Rust guard
tests and 52 schemas**, plus Ruff and strict mypy across 28 source files. The
Devnet program is real, and all three current public revisions were confirmed
through a one-time Mac operator run of the durable adapter, and a later
authenticated RPC proxy allowed automatic delivery to resume. See
[Devnet evidence and remaining gates](verification-devnet.md)
and [storage and trust](architecture/storage-and-trust.md).

Registry history stays enabled, and automatic cron relay is enabled following the
deployment of an authenticated Devnet RPC proxy; the live `/api/status` reports
`relayEnabled: true` and the scheduled sweep has delivered and confirmed canonical
revisions unattended. The official public Devnet URL remains the configured
default. Readable confirmed history is still not a claim of continuous delivery of
future revisions, and the 48-hour finalization interval remains unobserved.

Shipping the web portions of M3–M5 first does not renumber the handoff or complete
M2. Web records must not appear chain-verified before Solana integration. Native
mobile applications remain a separate deliverable.

## 1. Completed foundation — M1

The repository implements immutable domain records, strict serialization, versioned
schemas, deterministic lifecycle decisions, retry contracts, audit events, and
reputation and creator records. Errors preserve the original state, and validation
remains active under Python optimization.

Evidence is in the [Milestone 1 report](verification-milestone-1.md) and the later
[compression research](research/solana-compression-budget.md). Passing 76 domain
and eight cost tests does not establish external-service availability. A receipt
interface alone does not establish database atomicity.

## 2. A usable web service — M3 and M5

**User outcome:** Find a question on mobile, participate, review and publish a
specification, and retain activity in a real service.

Cloudflare web, API, authentication, D1 persistence, and mobile screens are deployed.
Production HTTPS checks covered health, storage, documents, registration, sessions,
recovery, logout, and CSRF. A local real-HTTP runtime covered forecast submission,
comments, retries, and revision conflicts. Mobile and desktop views were inspected,
and public specification commitments were independently recalculated.

The product scope includes discovery and detail, question creation and publication,
YES/NO with confidence, probabilities, activity, profiles, sharing, and the blueprint
and roadmap. The [verification record](https://forecast.eastsea.xyz/docs/verification-web)
distinguishes available features and test environments. Editorial questions are
not counted as human participation.

Acceptance and continuing safeguards:

- The deployed URL and health endpoint work, and saved state survives reload and reconnection.
- Unauthenticated writes, impersonation, and client-supplied administrative authority are rejected.
- Expired submissions, changed published specifications, and stale revisions are rejected by the server.
- Exact retries return the original result; competing writes commit only one accepted revision.
- State, receipts, events, and follow-up work are saved atomically, with recoverable failure handling.
- Mobile and desktop creation, participation, sharing, keyboard access, error states, and empty states are checked.
- Secrets stay out of browser bundles, responses, and logs, and a deployment rollback procedure exists.

### Deployed: English compilation and multilingual input

Users can write in a supported language. The compiler produces an English question
and resolution rules, preserves the original input, and presents the English
specification for review before publication. The published specification—not an
unreviewed translation—is what users forecast against.

Existing Korean specifications are not rewritten. Separately hash-bound English
display translations link to their original specifications, with an original-integrity
view. A display translation cannot replace the original commitment or alter the
rules used for resolution.

Acceptance requires multilingual compilation tests, preservation of subjects,
dates, thresholds and source meaning, review before publication, retained original
input, and verification that historical translations leave original bytes and hashes
unchanged. These checks passed in the current release, including a real Korean-to-
English preview and English desktop/mobile browser verification. Detailed evidence
and operational limits are recorded in the [verification record](verification-web.md).

### Deployed: optional wallet ownership linking

Linking uses a signature over a single-use challenge with a five-minute expiry.
It preserves the current profile, recovery code, and session authentication. The
service records the public address and challenge/audit information for ownership
verification; it does not collect private keys or recovery phrases. No transaction,
funds movement, balance assertion, or wallet-login replacement is included.

Acceptance requires correct signature and address binding, challenge expiry,
single use, replay rejection, and account-ownership isolation. Privacy and terms
explain the stored information and purpose. The current release passed regression,
production HTTPS, and isolated browser signing checks. It does not complete a
Solana program, Devnet deployment, or on-chain forecast recording.

Resolution and follow-up code do not establish a completed 48-hour production cycle
or delivered results. Web deployment and the complete lifecycle are verified separately.

### Deployed: participation points — M3, M5, M6

The implementation follows [participation-points policy v1](architecture/participation-points.md).
It grants 1,000 points once per account and 500 for verified wallet ownership once
per account and once per address lifetime. A forecast explicitly selects practice
with 0 points or an integer commitment from 1 to 1,000. Correct finalized outcomes
return 2× including the original amount, incorrect outcomes return 0, and INVALID
refunds the original amount. Existing commitments keep their policy version.

Before close, edits reserve or release only the difference. Practice is available
at zero balance. Profile and wallet grants, reservations and final-outcome processing
use atomic ledger changes and idempotent identities. Existing profiles and eligible
verified links are backfilled once; historical forecasts are not charged. Points
have no economic value and change neither crowd weighting nor reputation.

Verified release evidence:

- **Production:** Normal pinned-Wrangler migration applied `0004`; five existing profiles reconciled at 1,000 points each without errors.
- **Production HTTPS:** A new private profile received 1,000 points; a real unfunded Ed25519 wallet proof added 500. Unlink/relink added nothing further. The test wallet was unlinked and the session logged out.
- **Isolated local HTTP:** Practice/stake changes, competing 700-point commitments, retries, legacy requests and stale-account guards were checked.
- **Isolated local HTTP:** A 100-point commitment returned 200 for correct, 0 for incorrect and 100 for INVALID. Repeating settlement produced zero further effects; scheduled-trigger requests returned 200 twice.
- **Disposable remote D1:** Full migrations and ledger semantics were verified, then the database was removed.
- **Local browser and regression checks:** Point displays, retained inputs, private records and unchanged crowd/reputation weighting were checked. Public performance cards exclude point balances.

See the [points verification record](verification-points.md). No production forecast
or comment was created for these tests: human forecasts remain zero and editorial
questions remain three at verification. Actual production stake settlement over a
full 48-hour cycle is still a separate, unfinished gate. Production challenge
periods were not shortened to obtain test results.

This application feature does not complete M2, deploy a Solana program, or spend SOL.

### Deployed: four-language interface — 0.6.0

Add explicit English, Korean, Japanese and Traditional Chinese catalogs, with
English on first visit and a saved user choice under `forecast.locale.v1`. Do not
automatically select a language from browser settings. Use namespaced plain-text
messages, safe parameters, locale allowlisting and `Intl` display formatting.

Coverage includes complete UI flows, authentication/recovery, wallet and points
states, errors, image labels and accessibility. Questions, comments, rules, evidence,
wallet signatures and canonical records are unchanged. Article
navigation is localized; the original article has an explicit language notice and `lang`.

Acceptance gates:

- Match keys and named parameters across all four catalogs, including exposed server error codes; reject HTML and unsafe interpolation.
- Verify English first visit, saved choices, unsupported-locale fallback and unavailable localStorage.
- Check every visible and accessible state in all four languages at mobile and desktop widths, including image rendering and date/number formatting.
- Preserve question/comment/dispute drafts, confidence and points, recovery-code display, wallet state and share selections when switching.
- Block switching during busy operations without duplicating mutations, grants, signatures or retry identities.
- Verify unchanged canonical bytes, hashes, wallet-message bytes and point/reputation semantics.
- Check document notices and article language independently from translated navigation; record integration tests and actual deployment evidence.

The [localization contract](architecture/localization.md) and
[verification record](verification-localization.md) document the completed checks.
The live 48-hour lifecycle remains a separate verification gate. Native MWA and
Devnet attestation are now implemented in the [Android shell](android.md); they
were not part of the earlier localization release.

## 3. Evidence-connected AI — M4

**User outcome:** Reduce ambiguity before publication and inspect the evidence and
counter-analysis behind a resolution proposal.

Separate MarketCompiler, AmbiguityJudge, DuplicateDetector, SourceVerifier,
EvidenceCollector, ResolutionJudge, CounterJudge, and DisputeAnalyst. Use an efficient
default path, escalating uncertainty, conflict, and disputes.

The previous beta used live Gemini calls to publish three editorial questions and
verified their UTC dates. A Cloudflare AI REST model call and Worker binding were
also checked. Independent judgment and dispute paths passed regression tests. The
remaining gate includes the actual 48-hour production resolution, challenge, and
finalization cycle, plus provider failure and recovery.

Acceptance gates:

- Validate real provider responses against versioned contracts and retain model, version, and input/output hashes.
- Reject questions that cannot be resolved and materially duplicate specifications.
- Handle provider outages, truncated JSON, invalid fields, and instructions embedded in evidence.
- Retain source bytes, capture time, source identity, and hashes; live-page changes do not rewrite the evidence.
- Bind proposals to the exact specification, clauses, verified evidence, judge, and counter-judge records.
- Normalize provider identities so aliases cannot bypass independence requirements.
- Pause after total provider failure and preserve challenge opportunity on recovery.
- Measure request, creation, resolution, and dispute costs, and make budget-limit states explicit.

One connected provider does not complete the router. Disclose dependence on
operator-managed call records and attestations; do not claim unverified provider
signatures or an independent oracle.

## 4. Close the full user loop — M3, M4, M5

**User outcome:** Read evidence after closing, submit a challenge, and see a final
result reflected in reputation and notifications.

Closing, resolution, disputes, finalization, follow-up processing, and the outbox
have implementation and regression coverage. Evidence collection, proposals,
independent adjudication, renewed challenge periods, scoring, and notifications
still require end-to-end verification with actual elapsed time and retries.

Acceptance gates:

- Exercise create → validate → publish → forecast → close → propose → challenge → finalize → reputation → notification with real accounts and persistence. An internal notification event is not delivery.
- Verify ordinary finalization, proposal retention, explicit invalid-evidence dismissal, and adjudication after material conflict.
- Reject late disputes, finalization with pending reviews, finalization while paused, and competing finalization attempts.
- Preserve every accepted dispute and define pause or capacity expansion when fair intake is exhausted.
- Version scoring formulas, observation windows, and sample policies; exclude INVALID from binary scoring.
- Deduplicate reputation, notifications, and external effects when a finalized event is processed again.
- Resume interrupted jobs from durable records after redeployment without breaking the audit history.

This completes the first full web product loop. Following, ranking improvements,
weekly reports, and expert cohorts can grow from measured participation and quality;
missing statistics are never fabricated.

## 5. Verify the same rules on-chain — M2 Devnet

**User outcome:** Trace publication and resolution criteria to verifiable chain
records, with pending and confirmed states clearly distinguished.

The current baseline is a native registry with a 360-byte forecast account and
104-byte authority configuration. Its deployed SBF binary is 101,008 bytes.
It stores authority-attested specification, event, resolution, dispute and
reputation commitments; original artifacts remain in D1. Real local-validator
and Devnet checks exercised OPEN → early LOCKED → RESOLVING → PROPOSED → CHALLENGE.
They rejected unauthorized callers, stale revisions and premature finalization.
See [verified deployment](verification-devnet.md).

The public iPhone forecast now has confirmed revision 6 in CHALLENGE; Windows and
M6 Mac each have confirmed revision 2 in OPEN. This bootstrap used the Mac operator
command, not a successful Cloudflare scheduled delivery. The next operational gate
is authenticated RPC access that works from Workers, followed by actual scheduled
delivery and recovery verification. A usable public RPC from the Mac alone does
not meet that gate.

Acceptance gates:

- Match domain v1 canonical commitment vectors across Python, Rust, and web implementations.
- Record real SBF binary size, account sizes, and worst-case transaction size and compute use.
- Verify signer roles, owners, PDA derivation, account lengths, overflow handling, and duplicate-account protection.
- Exercise the full Devnet lifecycle, direct disputes, competing dispute/finalization, pause, and recovery.
- Check pre-close participation inclusion, proposal epochs, pending/material counters, and review reuse.
- Recover from stale proofs, indexer lag, RPC failures, client crashes after signing, and transaction retries.
- Prevent account or slot reuse from recreating an existing question, dispute, or reputation award.
- Recover complete artifacts, leaves, and proofs independently; replacing a current root must not erase history.

The earlier 160-byte direct-dispute account and 64 KiB executable were research
assumptions, not measurements of this registry. Direct user-signed dispute intake,
compression proofs and independent inclusion guarantees are not implemented by
the authority-attested registry. Completing the remaining gates must not remove
dispute, authority, timing or immutability checks.

## 6. Operational resilience — M6

**User outcome:** External failures do not erase forecasts or cause premature
finalization, and delays are visible.

Retries and deduplication apply from the first persistence feature. This stage
extends them into provider circuit breakers, RPC fallback, durable job replay,
observability, alerts, spending limits, abuse controls, backups, and deployment recovery.

Acceptance gates:

- Inject total provider failure, RPC outage, and indexer lag; verify pause, recovery, and state reconciliation.
- Handle lost responses, redeployment, and redelivery without duplicating committed effects.
- Exercise failed-job queues, replay paths, operator diagnostics, and redacted logs.
- Restore backups of data and evidence, then reconcile state, receipts, and audit heads.
- Test account manipulation, spam, duplicate questions, and dispute-capacity capture with fair intake policies.
- Document and exercise retention, deletion, public-history, account-recovery, and operational-key procedures.

## 7. Demonstrate scale and cost — M7 and mainnet decision

**User outcome:** Growing activity preserves fair challenges and consistent history.

Use synthetic creators and forecasters with real adapters to exercise thousands of
questions, simultaneous submissions, expiration sweeps, dispute surges, and provider
failures. Label synthetic data and exclude it from user statistics. Record throughput,
latency, AI cost, storage, chain cost, and artifact-recovery time.

Mainnet entry gates:

1. Resolve material findings from Devnet, authorization, concurrency, recovery, and data-availability tests.
2. Recalculate current rent and transaction costs using the real executable and explicit allocations.
3. Include configuration, active state, dispute surges, at least one upgrade, and failed retries.
4. Document operational and upgrade authority, emergency pause and recovery responsibilities, and security-review evidence.
5. Track chain funding separately from Cloudflare, AI, storage, RPC, proofs, and security review.

**Two SOL is a conditional on-chain target, not a confirmed spending quote.** The
research assumption of 64 KiB, 300 active forecasts, and 15 receipts is about 1.847 SOL
including an upgrade buffer. At 256 receipts it becomes about 2.286 SOL. Scope and
budget must be reconsidered if measured requirements exceed the limit.
[Observed costs and scenarios](research/solana-compression-budget.md),
[Solana rent RPC](https://solana.com/docs/rpc/http/getminimumbalanceforrentexemption).

At larger user scale, compare Light Compressed PDAs and SPL trees under identical
functionality and failure tests, including rent, consumed fees, proof latency, and
operational dependencies. A cheap single update does not establish low total cost.
[Light considerations](https://www.zkcompression.com/learn/considerations),
[SPL Account Compression](https://docs.rs/spl-account-compression/latest/spl_account_compression/).

## Separate research track — synthetic-asset Safety Kernel

As of September 14, 2026, the synthetic research implementation lives in the
separate private `forecast-risk` repository. Its phases 1–6 now include the pure
kernel, normalized signal adapter, Devnet memo audit trail and scenario laboratory.
These are separate research deliverables; the Forecast service does not consume
that repository or expose a signed risk-signal feed. Its separate synthetic reserve
program is now deployed on Devnet, with nine finalized transactions and five guard
simulations verified. The episode includes synthetic depeg and reserve-loss risk
signals; live provider integration and successful recovery after the real 24-hour
dwell remain open. The [combined deployment budget](research/solana-compression-budget.md)
now includes this program.

The separate token-linked v2 now also executes classic SPL TEST issuance and
redemption on Devnet: 17 successful and six deliberately rejected transactions
are finalized. Its observed-market pool used real Kraken/Bitstamp USDC quotes;
injected stress ran in a different mint/reserve. This is an experimental market
health indicator and valueless mock collateral, with no Forecast wallet coupling.
Actual 24-hour recovery and an always-on keeper remain separate work.

The integrated design (private) is limited to
research and Devnet. It preserves the current web product and the restrictions on
purchasing, transferring, and redeeming prediction points. Real-fund custody,
economic-value coin issuance, mainnet minting, and monetary settlement for forecasters
are excluded. STABLE, ALONG, and ASHORT are synthetic roles; no mapping to DONG,
HAE, or BADA, or final product names, has been agreed.

### Research Phase 1 — new domain

Implement `RiskSnapshot`, `RiskSurface`, `StableSystemState`, `SafetyState`,
`PolicyDecision`, `ReservePortfolio`, `StressScenario`, and `SimulationResult`.
**Implemented on 2026-09-12** as a pure package in the private companion repository
(13 generated contracts). It is separate from Forecast M1 and does not redefine published specifications, existing
schemas, or commitment interpretation.

Acceptance requires immutable records, precise units, freshness, source and version
binding, input hashes, no double counting of reserves and insurance, allowed risk
channels, strict snapshot decoding, and unchanged state on errors. An independent
regression test must show that setting ALONG's price to zero does not change STABLE
solvency.

### Research Phase 2 — reproduce the model first

**Implemented on 2026-09-12** as pure functions (`risk-sim-v1`). The static column
reproduces exactly; defensive and coverage differences are published, not fitted; the
27,000-scenario §22 grid runs reproducibly with a digest.

Implement `applyMarketShock`, `calculateSolvency`, `calculateLiquidCoverage`,
`calculateHedgePayoff`, and `evaluateSafetyState` as pure functions with no external
calls. Solvency and immediate redemption coverage are separate calculations.

The supplied A–E static-solvency results were reproduced using weighted losses.
Defensive solvency and run coverage were not. Defensive allocation without hedges
already exceeds the supplied defensive column, so missing costs, timing, or rules
must be identified. Fix hedge cost and payoff formulas, A–D effectiveness, liquidity
buckets, haircut order, denominators, reallocation costs and delays, post-redemption
balances, thresholds, and dwell time as versioned assumptions. Do not fit missing
parameters to the table and then present them as source facts. The
independent arithmetic review (private) records the differences.

Acceptance gates:

- Identical inputs, formulas, policies, precision, and rounding produce identical results and audit hashes.
- Publish differences from the supplied table and their causes; do not inherit its PASS labels.
- Exclude ALONG's future value and duplicated insurance assets, and support zero hedge effectiveness.
- Do not add the already-backed 10% insurance reserve again as ASHORT.
- Test zero liquidity, zero redemption demand or liabilities, extreme haircuts, runs, and correlated losses.
- Trace realizable assets, immediate liquidity, liabilities, hedge payouts, and costs.

### Research Phase 3 — deterministic Safety Kernel

**Implemented on 2026-09-12** (`safety-kernel-v1`), including the distinct 105%/110%
and 1.00×/1.20× zones, dwell, hysteresis, fallback and replayable decisions.

Implement `NORMAL`, `WATCH`, `DEFENSIVE`, and `CRISIS` with asymmetric tightening
and relaxation, rate and magnitude bounds, hysteresis, minimum dwell time, and
conservative fallback. Outputs change synthetic state; AI receives no authority
over funds, minting, or reserve actions.

A decision must reproduce from fixed risk snapshot, prior state, assets, liabilities,
policy version, evaluation time, and transition/adjustment history. Material
transitions need independent channels and observed evidence. False low-risk signals
cannot immediately expand risk. Multiple channels sharing one source require
special treatment. Poor-quality, stale, or unavailable forecasts trigger predefined
conservative handling.

Test behavior between the 105% unsafe solvency boundary and 110% target, and between
the 100% liquidity boundary and 120% target. These distinct thresholds must not be
conflated. Forecast resolution may be `PAUSED` while synthetic safety processing
continues. Neither indefinite waiting nor indefinite reuse of a low-risk snapshot
is acceptable. Include unnecessary adjustment costs and oscillation caused by false
high-risk signals.

### Research Phase 4 — probability-only adapter

**Normalization and synthesis implemented on 2026-09-12** (`risk-synthesis-v1`);
connection to live providers and calibration history remains open.

Normalize allowed canonical channels into `RiskSnapshot`. Preserve separate AI,
crowd, top-forecaster, and observed-market outputs, with versioned formulas and
historical-calibration weights. Arbitrary user-created questions cannot directly
affect policy.

Acceptance requires rejecting trading, minting, or transfer instructions embedded
in model text; invalid channels, forged identities, stale snapshots, and replays;
and continued conservative processing through total provider or oracle failure.
Every `PolicyDecision` binds its exact input hash and deterministic reason code.

### Research Phase 5 — Devnet audit history

Record synthetic reserve state, risk snapshots, and policy-transition hashes on
Solana Devnet. No economic asset issuance is required. Separate responsibilities,
authorities, and budgets from the Forecast Devnet program, and verify artifact
retention and hash/version links.

Acceptance requires independent replay of every transition from the audit log,
state agreement after delays, duplicates, RPC failures and retries, clear pending
and confirmed states, and disclosure of operator-attestation trust. Existing domain
tests or the source simulation table do not complete this phase.

### Research Phase 6 — at least 10,000 reproducible scenarios

The laboratory separates **solvency, immediate liquidity, insurance, and forecast
risk**. Users vary crypto crashes, stable-collateral impairment, run size, liquidity
haircuts, forecast lead time, and hedge failure, then inspect the path to failure.
Synthetic balances must not appear to be actual reserves or returns.

Acceptance gates:

- Reproduce **10,000 or more** scenarios, including deterministic shocks and seeded Monte Carlo runs.
- Cover collateral, exposure, lead time, hedges, redemption, impairment, and haircut dimensions. Disclose selection and coverage; 10,000 runs are not an exhaustive traversal of the source grid.
- Retain minimum solvency, minimum liquidity coverage, time to exhaustion, redemptions served, insurance usage, transitions, and recovery time; independently check representative boundaries.
- Include false low/high risk, zero hedge effectiveness, correlated reserve failure, and provider/oracle outages.
- Restore results from the same seed, inputs, policy, and code version, and replay every transition.
- Verify in implementation and deployment settings that no economic-value asset or monetary prediction functionality exists.

Passing these tests validates the defined synthetic model. It does not establish
real stablecoin solvency, redemption capacity, investment suitability, or mainnet
approval. Any later real-fund design requires a separate scope and review.

## How progress is updated

A completion claim records changed files, verification commands and environment,
results, deployment or execution evidence, and remaining limitations. Available
features are reflected in release records and the interface. Hypotheses must not
be presented as shipped functionality, actual users, or completed security audits.

Use observed participation per active forecast, retention, compiler quality,
INVALID and overturn rates, disputes, and costs to select the next work. Purchasable,
transferable, or redeemable points, tradable tokens or NFTs, and economic rewards
are not included in any product phase.

### On-demand reading translations — 0.7.0

Provide Translate / Original controls on feed and detail titles in non-English
interface modes. English-source questions need no translation button in English.
Translate the full
question and its conditions together, retain nullable rationale, verify source and
translation commitments, and cache reviewed results. Preserve drafts and numeric
inputs during translation and locale changes. Rate limits and source/language
leases bound generation. This feature does not deploy a Solana program or add MWA.

See [translation architecture](architecture/display-translations.md) and
[verification](verification-display-translations.md).

### Autonomous runtime — 0.8.0 implementation complete, rollout in progress

The new domain contracts support an early positive trigger without mutating old
specifications. The generated set contains 52 schemas while preserving the original
40. Official Apple/Microsoft observation uses conditional requests, retained evidence,
deduplication and bounded review work. Intake containment precedes AI review; only
an appropriately counter-reviewed watcher-owned hold can be dismissed automatically.

Funded LMSR persistence, account-bound quotes and explicit acceptance are implemented.
Actual local Worker/D1 and four-language UI checks verified a 100-test-point order,
190.902828 correct-outcome claims, a 50% → 54.76% YES price change and a 1,000 → 900
shadow balance. Actual points stayed unchanged. Active participation-point markets
remain disabled, and legacy fixed-return commitments remain intact.

The service-cost sandbox verifies reserved obligations, uncertain provider costs,
full refunds and idempotent operations. It is disabled by default and can be enabled
for administrator tests; it always remains nonbillable. No payment adapter, ads,
paid point balance or automatic income is delivered.

Completed in0.8.0:487 Python tests,131 frontend tests,52 schemas and static checks;
migrations0007–0010; actual Gemini/Cloudflare early YES proposal on the iPhone
question; live shadow preview quotes; four-language Seeker touch checks; and a
scheduled source poll observed after manual verification.

Remaining operational gates:

1. Observe a real48-hour challenge cycle before claiming live end-to-end
   finalization. Retain pending/exhausted jobs for operator attention.
2. Before any active-market or paid-service release, verify abuse controls,
   measured costs, genuine payment authentication, fulfillment and reconciliation
   under separately approved policies. There is no break-even guarantee.

See [runtime architecture](architecture/autonomous-runtime.md),
[verification](verification-autonomous-runtime.md), and the historical
design and admission gates (private research record).
