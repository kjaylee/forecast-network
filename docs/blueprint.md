# Forecast Network Blueprint

A network that turns questions about the future into verifiable forecasts.

Version 0.9 · Updated September 10, 2026 · Product and technical whitepaper

**Web beta:** [Forecast Network](https://forecast.eastsea.xyz) ·
[Web verification record](https://forecast.eastsea.xyz/docs/verification-web)

This public blueprint is the public expression of the product and architecture handoff.
Its restrictions on purchasing, transferring, or redeeming points, and its resolution
rules, remain authoritative. The [roadmap](roadmap.md) records delivery scope and
acceptance gates. A separate, privately maintained risk research track experiments with
synthetic assets on Devnet. It does not introduce real funds or coin issuance into the
forecasting service.

The public product uses English by default. Four-language interface packs are
deployed in `0.6.0`; these public articles remain English. Original handoffs
and research sources retain their original language. Localized document navigation
explicitly identifies the article language rather than imply a translated article.

## 1. What we are building

Forecast Network lets people ask questions about the future, compare human and AI
forecasts, and examine outcomes against public evidence. A record of well-calibrated
judgment builds long-term reputation. Every published question specifies what will
be decided, when, and using which sources.

“Will Apple announce a foldable iPhone before 2028?” requires a distinction between
announcement and retail availability, a closing time, official sources, and explicit
YES, NO, and INVALID conditions. AI turns the question into a clear specification.
The creator reviews it before publication. Published criteria cannot be changed
after the outcome becomes known.

Prediction points cannot be purchased or transferred between users. There is no
redemption for cash, SOL, USDC, tokens, NFTs, prizes, discounts, or other economic
benefits. Reputation is also non-transferable. Probabilities and scores help people
compare and improve their judgment. Solana's intended role is verifiable state and
history, not monetary settlement.

## 2. Implementation status

| Area | Status | Evidence and boundary |
| --- | --- | --- |
| Domain, lifecycle, schemas | Original foundation preserved; 0.8 extension implemented | Original 40 schemas unchanged; 52 generated contracts including early positive triggers and pricing |
| Domain and compression cost research | Verified baseline | 76 domain tests plus eight cost tests in the earlier research release |
| Web, API, authentication, persistence | Beta deployed; key paths verified | HTTPS, D1, registration, sessions, recovery, logout, CSRF, mobile and desktop checks |
| AI calls and task routing | Implemented; live compiler verified | Three editorial questions published through Gemini; Cloudflare AI REST call verified and binding configured |
| English default and multilingual input | Deployed and verified | English compilation and preview; separate display translations for immutable historical specifications |
| Optional wallet ownership linking | Deployed and verified | Five-minute single-use signature challenge; profile and recovery authentication retained |
| Shareable forecasting record | Deployed and verified | Owner-published all-time metrics, selected finalized calls, PNG exports and a dated public record link |
| Participation points, policy v1 | Deployed in API 0.5.0 | Production migration and grant checks; stake, race and settlement checks in isolated runtimes |
| Four-language interface | Deployed and verified in 0.6.0 | English, Korean, Japanese and Traditional Chinese; production and Seeker Chrome layout/input checks passed |
| Solana registry | Deployed; three public current revisions confirmed | Exact binary and public histories verified through a one-time Mac operator run; Cloudflare automatic delivery is blocked by RPC access; elapsed 48-hour finalization remains unverified |
| Signing-key custody | Four roles stored in this Mac's Keychain | Only the hot relayer seed enters Workers Secrets; cold administrator/upgrade authority remains local; independent recovery backup not yet verified |
| Resolution, disputes, follow-up jobs | Implemented with regression coverage | Independent review and outbox paths tested; live 48-hour full cycle and load validation remain |
| Official-source observer and early review | Deployed and verified in0.8.0 | Official-source observation and actual Gemini/Cloudflare early YES proposal; finalization awaits the full challenge period |
| Funded changing-price markets | Persistent shadow path verified locally | 100 test points → 190.902828 correct-outcome claims; active point adapter remains disabled |
| Service-cost and refund accounting | Nonbillable sandbox verified locally | Uncertain billing and full refunds checked; no customer payment integration |
| Synthetic risk models and Safety Kernel | Separate research design | New domain, simulator, and Devnet experiment remain incomplete |

The deployed API `0.5.0` release passed **285 Python tests, 69 frontend tests, and 40 schema
checks**, plus Ruff and strict mypy. Production HTTPS tests verified English
compilation from Korean input with an unchanged deadline, separately bound
historical translations, and real Ed25519 wallet ownership signatures. The
[verification record](verification-web.md) distinguishes these checks from remaining
long-running lifecycle and real-wallet-extension testing.
Those counts describe `0.5.0`, not verification of the upcoming language packs.
Production migration `0004` reconciled all five existing profiles at 1,000 points
each. A new private profile received 1,000 points over HTTPS, then 500 after a real
signature from an unfunded test wallet. Unlinking and relinking produced no additional
grant; the wallet was subsequently unlinked and the session logged out. No public
forecast or comment was written during these checks.

Stake races, exact retries, legacy/account guards, 100-point settlement returns of
200/0/100 for correct/incorrect/INVALID, and two successful scheduled-trigger requests
were verified in isolated local runtimes. Full migrations and ledger semantics were
also checked in a disposable remote D1 database, which was removed afterward.
These are not production stake-flow or elapsed 48-hour lifecycle results.
[Points verification](verification-points.md).

The previous release verified production HTTPS responses, D1, documents, and
authentication. Forecast submissions, comments, retries, and stale-revision races
were tested through a real local HTTP runtime. Public canonical hashes and UTC
dates were checked. At that verification point there were three editorial questions
and zero human forecasts; these figures are a dated baseline, not live usage metrics.

The following sections include design targets and unfinished work. A generated
hash does not establish on-chain verification, and a deployed website does not
establish completion of the full lifecycle. See the [web verification record](https://forecast.eastsea.xyz/docs/verification-web),
[Milestone 1 evidence](verification-milestone-1.md), and
[compression research](research/solana-compression-budget.md).

For `0.9.0`, the verification baseline is **555 Python tests, 131 frontend tests,
23 Rust guard tests, 52 schemas**, Ruff and strict mypy across 28 source files.
The [Devnet verification record](verification-devnet.md) separates those checks
from real transactions and the remaining hosted-relayer access problem. Two confirmed cheating
paths are now covered by application and database regression tests: unresolved
known-result timing reviews cannot later award points through ordinary expiry,
and watched legacy questions reject participation while source checks are unhealthy
or relevant reviews remain unresolved. Cloudflare rollout verification is in progress.
Fresh accounts and newly generated wallets still require a stronger Sybil policy.

All three current public forecast revisions are confirmed on Devnet: iPhone at
revision 6 / CHALLENGE, Windows and M6 Mac at revision 2 / OPEN. A one-time operator
command on this Mac replayed their histories through the same durable adapter.
Cloudflare's direct public RPC requests had been failing. An authenticated Devnet
RPC proxy is now deployed and scheduled delivery has been exercised, so hosted
anchoring runs with the automatic relay flag true while history reads and durable
delivery intents remain enabled. The 48-hour finalization interval is still
unobserved, so registry operation is not described as fully autonomous.

## 3. The user loop

A user discovers a question, chooses YES or NO, and records confidence. They compare
their judgment with available crowd, expert, and AI signals. A share link opens that
specific question. After finalization, the user can see what they got right and
where their confidence was misplaced, then return for another forecast.

```text
Write → Compile → Preview → Publish → Forecast → Compare → Share
                                      ↓
Return ← Notify and update reputation ← Finalize ← Challenge ← Propose ← Close
```

Participation should be quick on mobile without requiring blockchain knowledge.
The intended primary surfaces are Home, Explore, Create, Activity, and Profile.
“Today's five forecasts” is a product hypothesis for concentrating participation
around a useful daily set.

Crowd, top-forecaster, domain-expert, and AI probabilities remain separate. Missing
samples or undefined expert criteria must not be filled with invented numbers or
badges. Signals with different timestamps, sample sizes, or formula versions must
not be presented as one undifferentiated consensus.

Creation is open; distribution is ranked by clarity, uniqueness, freshness,
participation momentum, creator quality, and resolution quality. A featured set of
30–50 and an active set of 300–500 are target densities, not current inventory or
verified capacity. Similar questions should direct users to an existing forecast
unless their resolution rules materially differ.

## 4. AI compilation and language

### Interface language is separate from content

The `0.6.0` update adds English (`en`), Korean (`ko`), Japanese (`ja`) and Traditional
Chinese (`zh-Hant`) interface packs. English is used on first visit, without browser-
language detection. Only an explicit saved choice changes it. Labels, errors,
authentication and recovery instructions, wallet and points screens, image labels,
and accessible announcements are included. Number and date presentation follows
the chosen locale without changing stored values.

Language packs do not translate questions, comments, published rules, evidence or
wallet-signature bytes. The existing compiler
and separately bound English historical translations remain distinct features.
Document navigation is localized while articles retain their original English or
research language, with an explicit notice and article language metadata.

Switching must preserve form drafts, recovery information and wallet state, and is
blocked while sensitive operations are busy. It cannot submit a forecast, repeat a
grant or request a wallet transaction. Integration and production checks passed,
including Seeker Chrome layout and input. Devnet and Mobile Wallet Adapter remain
separate work.
[Localization architecture](architecture/localization.md).

### On-demand reading translations — 0.7.0

Translate / Original controls on forecast titles request a server-side AI translation
of the question, conditions, invalidation rules and existing rationale. Targets are
Korean, Japanese and Traditional Chinese. A separate review checks fidelity before
the source-bound result is cached. Errors retain the original; published rules
remain authoritative. Interface switching itself does not request translations.
The 0.8 interface hides this control for English-source questions in English.
This uses existing AI providers and spends no SOL.
[Translation architecture](architecture/display-translations.md).

### Existing compilation and immutable translations

The Market Compiler produces YES, NO, and INVALID conditions, UTC deadlines,
primary and fallback sources, ambiguity findings, duplicate candidates, category,
and share title. It must not disguise an unverifiable question with polished prose.

The English-default update follows this flow:

```text
Input in a supported language → AI compiles English question and rules
                             → User reviews English specification → Publish
```

The original input is retained. Translation must preserve the intended subject,
time window, thresholds, sources, and outcome conditions. If meaning remains
ambiguous, the creator must resolve it before publication. English is the shared
reading experience, while input is not restricted to English.

Previously published Korean specifications remain immutable. Their English display
translations are separate artifacts, bound by hash to the original specification
and to the translated content. They do not replace the original commitment. An
original-integrity view lets readers inspect the authoritative specification and
its hash. A translation cannot silently change resolution rules or authorize a
state transition. English compilation and historical translations were verified
in the deployed release; the verification record distinguishes their tested paths.

Compilation, ambiguity assessment, duplicate detection, source verification,
evidence collection, resolution, counter-judgment, and dispute analysis are separate
tasks. Routing considers cost, latency, ambiguity, source reliability, and conflicting
evidence. Every request does not call every provider.

The default path uses a lower-cost model and deterministic checks. Uncertain or
conflicting cases escalate; material disputes require an independent provider's
review. Structured reasoning, source-grounded analysis, public-signal analysis,
and overflow capacity are distinct provider roles. The previous beta verified the
live Gemini compiler and a Cloudflare AI REST call with the Worker binding configured.
Independent resolution and dispute paths have regression coverage, not yet a
completed long-running production lifecycle.

Every AI decision records task, provider, model and version, policy and schema
versions, input and output hashes, and decision time. Provider text is untrusted.
Only validated, structured results can become domain commands. Instructions in
external pages or model outputs cannot change permissions, published criteria,
or tool execution policy.

## 5. An immutable lifecycle

The core models are `ForecastSpecification`, `Forecast`, `UserForecast`, `Resolution`,
`Dispute`, `UserReputation`, and `CreatorProfile`. The implemented domain has no
framework, database, network, or provider-SDK dependency. It validates an explicit
command, execution time, and current state, then returns new immutable state and
auditable events.

```text
DRAFT → VALIDATING → OPEN → LOCKED → RESOLVING
      → PROPOSED → CHALLENGE → FINALIZED → ARCHIVED
```

Disputes enter `DISPUTED`; material conflicts enter `ESCALATED` for independent
adjudication. A changed proposal starts a fresh challenge period. If all configured
providers fail, the relevant resolution state is preserved as `PAUSED`. Recovery
extends the challenge opportunity by the paused duration. A successful model call
or scheduler run alone cannot finalize a forecast.

Publication binds the complete specification and its assessment. There is no
command to rewrite published rules, sources, or closing time. Submissions are
rejected after the trusted server deadline even if a delayed scheduler has not yet
changed OPEN to LOCKED. Finalization requires an elapsed challenge period, no
unreviewed disputes, and no unresolved material conflicts.

Versioned structural contracts are generated from the models. Separate semantic
checks verify identity, hashes, causal timing, and state-dependent conditions.
Persisted snapshots receive those checks when decoded. See the
[lifecycle contract](architecture/lifecycle.md) and
[data and commitment contract](architecture/domain-contracts.md).

## 6. Evidence and challenges

Resolution must explain how preserved evidence satisfies the original clauses.
The collector records source bytes, capture time, source identity, hashes, and
retention location. A URL locates material; it is not the material itself. Evidence
used for a decision must remain reviewable when its live page changes or disappears.

After source verification, a judge proposes an outcome and a counter-judge examines
it. The proposal includes YES, NO, or INVALID, confidence, matched and conflicting
clauses, and a reason summary. A user challenges a specific rule application with
a claim, evidence URL, clause reference, and explanation.

Review validates the submitted evidence, performs counter-analysis, and obtains an
independent judgment. Rejection of evidence also requires an explicit disposition
and review record. Unresolved material conflicts escalate rather than automatically
finalizing the existing proposal. Submission, review, retention, adjudication, and
finalization belong in a visible audit timeline.

Fetching, retaining, authenticating provider records, and enforcing actor roles
remain adapter responsibilities. The domain checks relationships among records;
it cannot independently establish which bytes a remote server served or whether
a named provider actually performed a judgment.

## 7. Participation points and forecasting quality

### Participation points — deployed policy v1

Policy `participation-points-v1` adds optional commitments without changing the
meaning of confidence or reputation:

| Action or finalized result | Points |
| --- | --- |
| Create a profile | 1,000, once per account |
| Verify wallet ownership | 500, once per account and once per address lifetime |
| Practice | Explicitly commit 0 |
| Participate with points | Explicitly commit an integer from 1 to 1,000 |
| Correct outcome | Return 2× the amount, including the original commitment |
| Incorrect outcome | Return 0; the commitment is consumed |
| INVALID | Return the original commitment in full |

Committing 100 points returns 200 total if correct, not 300. Points cannot be
purchased, transferred, withdrawn, redeemed, or exchanged for economic benefits.
They never weight crowd probability or reputation. Practice remains available
when the available balance is zero.

Available and committed points are separate. Before closing, edits move only the
difference and bind the commitment to the latest accepted YES/NO choice. Reducing
it to zero returns to practice. Proposals, disputes and pauses do not settle points;
only an immutable finalized outcome does. The policy attached to an existing
commitment remains fixed.

Profile creation and its grant commit together. Wallet grants require verified
ownership and lifetime uniqueness: renaming, signing in, unlinking, relinking or
using another wallet cannot repeat the account's award, and an already-awarded
address cannot earn another account's grant. A fresh address is not consumed when
its account has already received the award. Migration handles existing profiles
and eligible verified links once; historical forecasts remain practice.

Forecast changes, reservations, receipts and append-only ledger entries share an
atomic D1 batch. Account-level guards prevent simultaneous questions from spending
the same points, and unique settlement identities prevent duplicate credits.
The authenticated profile exposes balances, onboarding and recent entries; public
performance cards do not include point balances. This is application accounting
deployed in API `0.5.0` and requires no Solana transaction. Production grants and
isolated stake/settlement behavior have separate verification evidence.
[Implementation contract](architecture/participation-points.md).

### Reputation

Profiles distinguish participation from resolved performance: total and resolved
forecasts, accuracy, Brier score, calibration, domain performance, consistency,
dispute accuracy, and creator quality. Insufficient observations remain unmeasured.

Confidence is the probability assigned to the selected outcome. YES at 70 means
`P(YES) = 0.70`; NO at 70 means `P(YES) = 0.30`. For binary outcome `y` and YES
probability `p`, a Brier observation is `(p − y)²`; lower is better. INVALID outcomes
are excluded from binary scoring. The system should distinguish habitual 51%
forecasts from unjustified 99% confidence.

Observation timing, revised forecasts, minimum samples, calibration bins, and
domain classifications require versioned scoring policies. A model field alone
does not establish that a metric is being calculated in production. Reprocessing
the same finalized event must not apply reputation twice.

A reputation commitment binds owner, formula version, snapshot revision, and the
range of finalized events included. Inputs must support independent recalculation.
Publishing a root does not by itself prove correct scoring.

## 8. Cloudflare and application boundaries

The mobile web and API are deployed at [forecast.eastsea.xyz](https://forecast.eastsea.xyz).
A Cloudflare Worker reuses the Python domain and stores state in D1. UI, transport,
storage, AI, and chain adapters sit outside the domain so deployment or provider
changes do not redefine lifecycle rules. The [verification record](https://forecast.eastsea.xyz/docs/verification-web)
distinguishes production checks, local runtime checks, and remaining work.

Clients cannot choose trusted execution time, creator identity, provider reliability,
or administrator roles. The API authenticates users and restricts submissions and
disputes to their own identity. Publication, validation, resolution, pause, finalization,
and exceptional adjudication each require the appropriate authority.

The optional wallet-linking update proves control of a public address using a
single-use signature challenge that expires after five minutes. It retains the
existing profile, recovery code, and session authentication. The service stores
the public address and challenge/audit records for ownership verification, never
private keys or wallet recovery phrases. This flow does not submit transactions,
move funds, claim a balance, or replace account login. Linking an address is separate
from deploying a Solana program or recording forecasts on-chain. The existing
ownership flow, account preconditions and one-time points award are deployed in
API `0.5.0`; ownership and award checks do not establish on-chain records.

A retried command must recover its original receipt. Revision checks and atomic
persistence cover the snapshot, receipt, event, and follow-up work. A late write
must not overwrite newer state. AI jobs, chain submissions, reputation updates,
and notifications use stable event identities and deduplication.

Authorized service credentials are read from the operator's Keychain and passed to
server secret settings. They do not belong in browser code, public responses,
documents, source control, or logs. Sessions, data access, and external calls use
limited permissions. Evidence fetching requires internal-address protection,
redirect revalidation, and size and time bounds.

## 9. On-chain compression and the 2 SOL target

The deployed Devnet registry contains specification hash, creator identity hash,
lifecycle state, timestamps, outcome, dispute and reputation commitments, and
audit hashes.
Full questions, long evidence, AI reports, comments, search, and social data stay
off-chain and are linked by exact hashes. Its 360-byte forecast account and 104-byte
configuration are measured layouts. The finalized deployed executable is 101,008
bytes, with SHA-256 `20e3140d28bc5438f0292983b96a0cbc30896074dcbb63678be3233f01a97890`.
See [Devnet verification](verification-devnet.md).

The registry checks authority, ordering, immutable identity, permitted transitions
and an independent chain-clock challenge delay. It does not determine news truth,
verify independent AI signatures or prove complete off-chain dispute inclusion.
The hot relayer attests application records, and the cold upgrade authority can
change program code. A configured program is not evidence that a particular web
record has reached finalized chain confirmation. These distinctions are part of
the [storage and trust model](architecture/storage-and-trust.md).

The earlier research direction combined small individual active accounts, direct
dispute receipts, and shared commitments for participation, reputation, and archives.
The 160-byte direct-dispute receipt remains a candidate layout outside the current
registry. Letting a server replace
one root does not establish correct transitions or complete dispute intake.

State compression does not eliminate program storage costs. The executable needs
its own on-chain allocation. [Solana deployment documentation](https://solana.com/docs/programs/deploying).

The actual rollout was funded with **2 Devnet SOL from an existing local wallet**.
No mainnet SOL was spent. This is test-network funding, not revenue or proof that
mainnet hosting, AI and transaction costs will fit the same budget. Active point
markets remain disabled, and billing remains a nonbillable sandbox.

Using the September 9, 2026 mainnet rent observations, a hypothetical 64 KiB program,
300 active forecasts, 15 dispute receipts, small configuration and commitment
accounts, and a planned 0.05 SOL transaction allowance require about **1.431 SOL**
initially, or **1.847 SOL** with a same-size upgrade buffer. These are assumed-layout
calculations, not a measured program quote. With 256 simultaneous receipts, the
same 300-forecast scenario reaches about **2.286 SOL** including the buffer.
[Assumptions and observations](research/solana-compression-budget.md),
[Solana rent RPC](https://solana.com/docs/rpc/http/getminimumbalanceforrentexemption).

Light Compressed PDAs and SPL Account Compression are scaling candidates. They
introduce proof, indexer, state-fee, and data-availability dependencies. Existing
compression-protocol security work does not verify a new Forecast program. No NFT
or economic asset issuance is introduced. [Light Compressed PDAs](https://www.zkcompression.com/compressed-pdas/overview),
[Light considerations](https://www.zkcompression.com/learn/considerations),
[SPL Account Compression](https://docs.rs/spl-account-compression/latest/spl_account_compression/).

Before finalization, users need dispute intake that does not depend on inclusion
in the operator's batch. Tests must cover participation inclusion time, proposal
epochs, duplicate review, stale proofs, account recreation, and durable archives.
Funding or capacity shortages must not allow valid challenges to be lost while
finalization proceeds.

Two SOL is an initial on-chain funding target. It excludes Cloudflare, AI, evidence
storage, RPC, prover and indexer services, security review, and indefinite operation.
Actual executable size and current network costs must be measured before deployment.

## 10. Trust boundaries

| Boundary | Intended assurance | Remaining trust |
| --- | --- | --- |
| Domain rules | Immutable criteria, valid transitions, consistent time and hash relationships | Server authentication, authorization, and clock |
| Evidence hashes | Captured bytes have not changed | Source truthfulness and honest capture and retention |
| AI provenance | Links inputs, configuration, and outputs | Operator attestations correspond to actual provider calls |
| Solana commitments | Recorded ordering and integrity under the deployed program | Program correctness, authority policy, data availability |
| Compression proofs | State inclusion and permitted updates | Truth of AI judgments and completeness of submitted inputs |

The initial service has operator-managed web, storage, and AI paths. It is not a
fully decentralized resolution network. Provider identities must be normalized so
aliases cannot bypass independent-review requirements. Hashes are not signatures;
false data can also be hashed. Authenticated attestations and role enforcement
require explicit implementation and testing.

Evidence-storage, provider, RPC, and indexer failures must appear as delayed or
paused states. Local acceptance, chain submission, chain confirmation, and final
resolution are distinct. Recovery evaluates commands again against current state
and proofs rather than blindly replaying an old decision.

## 11. Privacy, authority, and change policy

Collect only what public forecasting and auditable evidence require. Email,
session tokens, private source material, and API keys do not belong on-chain.
Linking identifiers to public behavior can expose personal information; even a
hash can permit guessing or correlation of sensitive input.

Evidence access, retention, account deletion, and the persistence of public records
must be explained and tested as operational policies. Deleting a web account must
not be presented as deleting previously published blockchain records. Logs exclude
secrets and remain limited to recovery and audit needs. See the [privacy notice](privacy.md).

The operator is responsible for initial policy, deployment, and exceptional cases.
There is no economic-token voting or paid influence. Policy changes cannot rewrite
a published specification. Provider policy, reputation formula, and schema changes
are versioned with explicit applicability. Pause, recovery, and adjudication reasons
belong in the audit history. Future upgrade authority requires documented custody,
rotation, and recovery; no completed multisignature setup or external audit is claimed.

## 12. Measuring success

The north-star metric is **active forecasters per active forecast**. More questions
are not useful growth if they fragment participation. D1, D7, and D30 retention,
weekly participation, sharing conversion, duplicate prevention, INVALID outcomes,
overturned resolutions, disputes, and AI costs provide context.

Initial hypotheses are D7 retention of at least 15–20%, at least five weekly forecasts
per user, at least 80% publishable compiler outputs, INVALID below 3%, overturned
resolutions below 2%, and disputes below 5%. These are neither achieved metrics nor
guarantees. They require measurement with disclosed samples and time windows.

Clear questions, retained evidence, fair challenges, and reproducible reputation
are completion criteria. The [roadmap](roadmap.md) defines the delivery gates.

## 13. Separate research: synthetic-asset safety

A privately maintained integrated design explores using
Forecast probabilities in reserve-risk experiments. Its scope is Devnet SOL,
synthetic reserve assets, non-transferable reputation, and synthetic STABLE, ALONG,
and ASHORT state. It excludes economic-value asset issuance, custody of user funds,
mainnet minting, and rewards for forecast participants.

```text
Forecast Network → Allowed risk channels → Risk Synthesizer
                 → Versioned risk snapshot → Safety Kernel → Synthetic state and audit log
```

**AI estimates risk; it never moves funds directly.** Outputs are probabilities for
events such as depegging, reserve loss, or liquidity stress, plus confidence and
evidence quality. They cannot be sell, mint, transfer, or collateral-change commands.
The deterministic kernel consumes validated structured inputs, not raw model text.
Its research outputs change synthetic policy and state, not real funds.

### Canonical channels and independence

Arbitrary user questions cannot affect policy. Only canonical channels with fixed
definition, measurement window, primary and fallback sources, confidence, timestamp,
and version are eligible. Candidates cover depeg horizons, reserve loss, liquidity,
collateral families, BTC/ETH/SOL crashes, and oracle, bridge, or counterparty failure.

AI, crowd, top-forecaster, and observed-market signals remain separate. Averaging
multiple models does not establish safety. Calibration, source quality, and domain
weights require versioned validation. Material transitions require independent
channels and observed evidence. Several models repeating one source are not
independent confirmations.

### State and outage behavior

The research states are `NORMAL`, `WATCH`, `DEFENSIVE`, and `CRISIS`. Tightening can
be rapid; relaxation requires improved forecasts, observed stability, and a minimum
dwell time. Hysteresis prevents oscillation and immediate expansion after a false
low-risk signal. Policy changes have magnitude and rate limits.

Reproducibility requires more than `RiskSnapshot`: prior state, assets, liabilities,
policy version, evaluation time, and previous transition and adjustment times must
also be fixed.

Disagreement, low source quality, or provider/oracle outages trigger deterministic
conservative fallback. The research direction forbids risk expansion and collateral
relaxation and applies more conservative liquidity and exposure limits. One signal
cannot arbitrarily decide a material transition; unavailable-input handling must
also be a predefined policy branch.

Forecast resolution may enter `PAUSED` when all judgment providers fail. The Safety
Kernel's synthetic state processing must continue without waiting for AI. It must
operate conservatively when no valid new probability is available.

### Synthetic capital roles

| Research role | Meaning | Boundary |
| --- | --- | --- |
| STABLE | Senior synthetic liability targeting one reference-currency unit | No real redemption claim or mainnet asset is issued |
| ALONG | Junior synthetic capital that absorbs losses first | Its future market value is excluded from STABLE solvency |
| ASHORT | Separate synthetic insurance and tail-risk capital | Not a reward for forecasters or an extra copy of reserve assets |

These are research-role labels. No mapping to DONG, HAE, or BADA has been agreed.
Insurance is synthetic input for loss-absorption and liquidity experiments, not a
fundraising or return-distribution promise. Setting ALONG's value to zero must not
change STABLE solvency. The 10% insurance reserve is already included in total
backing and must not be added again under the ASHORT label.

### Solvency and immediate liquidity

Calculate and display two independent quantities:

- Solvency: realizable total assets divided by STABLE liabilities.
- Immediate redemption coverage: immediately liquid assets divided by stressed redemption demand.

Sufficient accounting assets do not establish cash availability at the required
time. Experiments distinguish recovery times, haircuts, correlations, insurance
access, and counterparty failure. Zero denominators, post-redemption asset and
liability reductions, and hedge settlement time need explicit rules. Zero hedge
effectiveness is a required scenario.

### Status of the supplied simulation table

The 135% initial backing, crypto exposure reduction from 15% to 8%, 80% hedge
coverage, and scenarios A–E are synthetic assumptions. The static-solvency column
was reproduced using weighted losses. Forecast-defensive solvency and run coverage
remain unverified because their definitions are incomplete. PASS and MARGINAL are
not completed model validation or real survival evidence.

The independently calculated defensive allocation without hedges already exceeds
the supplied defensive results. Under one explicit interpretation, scenario E is
117.585% before a hedge; applying 80% coverage and 20% effectiveness to remaining
crypto losses raises it to 118.9998%, not the supplied 111.7%. Missing costs, execution
timing, or loss rules must be identified. The larger number must not replace the
original as a performance claim.

If all of E's stated 111.7% assets receive a further 30% liquidity haircut, coverage
of an 80% redemption run is at most about 0.977×. Explaining the supplied 1.08×
requires asset-level liquidity buckets, timing, and haircut definitions.
The arithmetic review and missing specifications are recorded with that design.

Before reproduction, fix A–D hedge effectiveness, hedge cost and payoff formulas,
liquidity buckets, haircut order, percentage denominators, lead time, reallocation
cost and delay, state thresholds, dwell times, and rate limits. Define behavior
between the 105% unsafe solvency boundary and 110% target, and between the 100%
liquidity boundary and 120% target. These are research parameters with different
purposes, not guarantees of real safety, returns, or redemption.

First reproduce results with pure deterministic functions, then run **10,000 or more
stress scenarios** with retained seeds, policies, and input versions. Include false
low- and high-risk signals, hedge failure, correlated losses, concentrated redemptions,
and source or oracle outages. Unnecessary defensive actions also cost resources;
a false high-risk signal must not simply be assumed harmless.

Completion requires the new risk domain, simulator, kernel, probability-only adapter,
Devnet audit trail, and separate laboratory interface. Forecast Milestone 1 does not
complete this new implementation. The [research roadmap](roadmap.md) defines its gates.

## Pricing and autonomous operation — 0.8.0

A separate research design (private) compares fixed odds, pooled settlement, independent scoring and a funded LMSR.
The current implementation rehearses LMSR with isolated test points while retaining
separate accuracy and reputation scores. Existing fixed-return commitments keep
their original promises; they are not converted into market positions.

Participants explicitly review a quote before confirming. An accepted order moves
the next price and retains its own return. With the initial tested policy, 100 test
points yield 190.902828 claims if correct and move YES from 50% to 54.76%. The local
Worker/D1 check confirmed that actual participation points did not change. Active
point markets remain gated off pending their operational and abuse controls.

Official-source observation connects changed evidence to an intake hold and bounded
AI review. A versioned early positive-trigger path can propose an outcome when the
original conditions have already been satisfied. It preserves the original closing
time, records evidence-time precision and retains the normal 48-hour challenge
period. This is not a promise of complete news coverage or instant detection.

The existing iPhone question entered CHALLENGE after actual independent reviews
proposed YES. Its original specification remains unchanged. No final outcome or
payout is claimed before the full challenge period completes.

Advertising is excluded. The new service-reserve sandbox rehearses cost allocation,
uncertain provider bills and refunds, but explicitly reports that it is nonbillable.
There is no payment adapter, paid question publication or purchased point balance.
Question creation remains free and the operator still pays running costs. Automatic
income would require real customer demand and a separately verified service-payment
system; accounting alone cannot cover expenses.

Implementation, isolated runtime and production rollout checks are complete.
Actual early review produced a YES proposal; full-cycle finalization remains to
be observed after the challenge window. See the
[runtime architecture](architecture/autonomous-runtime.md) for exact limits and
[verification record](verification-autonomous-runtime.md) for completed checks and
remaining release gates.
