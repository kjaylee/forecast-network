# Rust rewrite plan (strangler, live never interrupted)

Decisions (2026-09-16): public service → **workers-rs (wasm32)** on the existing Cloudflare
account/D1/assets; forecast-risk → **all Rust** (domain, verifier, kernel, keeper binary);
migration **per route / per process**, each step deployable and reversible; Python stays the
reference implementation until the Rust side proves byte-identical behavior on golden vectors.

## Why this order

Every component shares one contract: canonical JSON (`sort_keys`, `,`/`:` separators, raw UTF-8,
integers only), `content_hash = sha256("forecast-network:sha256:canonical-json:v1\n" + canonical)`,
Ed25519 signature domains, and the record validation rules. If that layer is not bit-exact, nothing
downstream can be trusted, so it goes first and is proven with the Python golden vectors.

## Phase 0 — shared domain crate (`packages/domain-rs`, crate `forecast-domain`)

- `canonical`: serde_json `Value` (BTreeMap keys) → canonical bytes; floats rejected; `content_hash`.
- `records`: risk feed v1 + v2 (`RiskFeedBinding`, `RiskFeedSignal`, `RiskFeedPayload`,
  `SignedRiskFeed`, `CanonicalRiskDefinitionV2`, `RiskMappingProfileV2`, `RiskFeedSeriesV2`,
  `RiskFeedBindingV2`, `RiskFeedSignalV2`, `ChannelCoverageV2`, `RiskFeedPayloadV2`,
  `SignedRiskFeedV2`) with the same field constraints and `validate()` semantics; `signing_bytes`
  (v1/v2 prefixes); `freshness_as_of_ms`; `RiskFeedSeriesV2::conforms`.
- Parity: Python writes golden JSON + expected hashes/signatures (`tests/golden/*.json`); Rust
  tests must reproduce the v1 hash `06472bde…`, v1 signature `2e59bd25…`, v2 hash `89be80bb…`,
  v2 signature `d906de89…`, and reject the same malformed inputs. The Python schema generator
  stays the schema authority; a Rust test loads `schemas/v1/*.schema.json` and checks field sets.
- Exit: `cargo test` green, golden parity file checked into both repos.
- **Status 2026-09-16: done.** `packages/domain-rs` reproduces v1/v2 hashes, signing bytes and
  Ed25519 signatures, the canonical edge vector, the published schema field sets, and all 1,686
  single-field mutation verdicts in `tests/golden/risk-feed-mutations.json` (verdict + hash).
  Builds for `wasm32-unknown-unknown` (use the rustup toolchain: `PATH=~/.cargo/bin:$PATH`; the
  Homebrew `cargo` lacks the target). CI job `domain-rs` in `.github/workflows/checks.yml`.
  Finding: Python `Record` used `re.search` for `pattern`, so `$` accepted a trailing newline
  (`"key\n"` passed as an ID/hash) — fixed to `re.fullmatch`; published schemas already meant
  full-string anchoring, no accepted valid record changes hash.

## Phase 1 — forecast-risk in Rust (`forecast-risk/crates/…`)

1. `risk-models`/`risk-kernel`: `RiskSignal`, `RiskSnapshot`, `RiskSurface`, `PolicyDecision`,
   `evaluate_safety` (pinned `safety-kernel-v1`, `POLICY_HASH` must match), `synthesize_v2`.
   Parity: replay the 27,000-scenario grid digest `e4467a4c…` and the recorded keeper journal
   frames (evidence hash + 277-byte instruction) from `journal.sqlite3`.
2. `risk-verify`: `verify_feed_v2`, `WindowProjectionProofV2`, `WindowCoverageV2`,
   `coverage_from_candles`, Kraken/Bitstamp parsers; `prepare/replay_token_policy_v2` producing
   `TokenPolicyEvidenceV3` with identical hashes.
3. `risk-keeper` binary: journal (SQLite, same schema), lease/generation fencing, RPC, signing
   (operator key from Keychain via `security`), coverage collection, one-tick and run modes.
   Cutover: run Rust keeper against a **copy** of the journal in shadow (no send) until its
   prepared instructions equal Python's for N ticks, then swap the launchd job.
4. Calibration trainer and reserve/token wire as needed by the keeper.
- **Status 2026-09-16:** steps 1–3 done in `forecast-risk/crates/` (`forecast-risk` lib +
  `risk-keeper` bin; own workspace, root `Cargo.toml` excludes it). Parity proven by
  `tests/golden/kernel-golden.json` (880 kernel decisions covering all 16 reason codes, 12 synthesis
  surfaces, design scenarios A–E), the full 27,000-grid digest `e4467a4c…` (1.5 s in Rust),
  `tests/golden/verifier-golden.json` (coverage/parsers/market observation/audit) and
  `tests/golden/keeper-frames.json` (16 finalized live Devnet frames replayed byte-for-byte:
  evidence hash, decision, audit entry, 277-byte instruction). `check.py` runs `cargo test`.
  Keeper cutover: binary installed at `~/.local/share/forecast-network/keeper-runtime/bin/risk-keeper`;
  launchd `com.forecast-risk.keeper.shadow-rs` runs `--shadow` every 5 min (replays the Python
  journal's latest frames + builds a fresh frame, no journal writes, no sends; frames retained under
  `tmp/keeper-stress/shadow/`). Swap `com.forecast-risk.keeper.stress-devnet` ProgramArguments to
  the binary (`--run`) once shadow ticks stay `match`; rollback = restore the Python plist.
  Remaining: `train_weights` (calibration trainer) stays Python until the first finalizations.

## Phase 2 — public service in Rust (`apps/web-rs`, workers-rs)

Strangler by route on the same hostname (Cloudflare routes or a router Worker):
1. Read paths: `/api/status`, `/api/forecasts`, `/api/forecasts/{id}`, `/api/risk/v2/feeds/*`,
   `/api/risk/feeds/*` (D1 read replicas with bookmarks). Parity: compare JSON responses against
   Python for every live forecast; then flip the route. This alone removes the Pyodide
   concurrency failure for public traffic.
2. Auth + submissions + markets (wallet login, sessions, points, LMSR quote/fill/receipt).
3. Risk producer/operations (`risk_feed_v2`, series, refresh, publish with WebCrypto Ed25519).
4. AI pipeline (compiler/judges via the Gemini relay), lifecycle scheduler, source watch.
5. Retire the Python Worker; keep it deployable until one full 48-hour cycle ran on Rust.
- **Status 2026-09-16:** step 1 partially live. `apps/web-rs` (`forecast-network-edge`) owns
  `forecast.eastsea.xyz`: native `/api/health`, `/api/status`, `/api/risk/feeds/*`,
  `/api/risk/v2/feeds/*` and static assets; everything else is forwarded untouched through the
  `LEGACY` service binding to `forecast-network` (Python, still holds cron, secrets, AI, D1 writes).
  Deploy: `scripts/deploy_edge.py [--take-domain]`; evidence in
  `docs/evidence/completion/rust-edge/cutover-2026-09-16.json`. `apps/web/wrangler.jsonc` no
  longer declares the custom domain. `/api/forecasts` (list) is native too (discovery/projections
  ported verbatim; 18/18 query parity; `scripts/deploy_edge.py --preview` deploys
  `forecast-network-edge-preview` for route parity before touching the domain). `/api/forecasts/{id}` is native as well (17/17 parity) on top of the new
  `forecast-domain::pricing` (80-digit decimal LMSR with correctly rounded exp/ln, 25-fill sequence
  parity in `tests/golden/pricing-golden.json`). Every public GET read is native now (profile cards, integrity,
  market, translation, me, creators, points, activity, wallet, billing estimate; 39/43 parity with the
  4 diffs being Python 1101 flakes and clock-embedded windows). Remaining on Python: all POST/PATCH
  routes (auth, submissions, comments, disputes, markets quote/fill, wallet login, seeker), admin GETs,
  cron/automation, AI pipeline, risk producer.

## Rules

- Never change a published hash, schema, signature prefix, or D1 migration semantics.
- Every phase ships behind a route/process switch with a rollback command written down.
- Python tests remain the acceptance suite until the Rust port has an equivalent test.
