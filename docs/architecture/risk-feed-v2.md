# Risk feed v2: typed measurement targets for operational risk

Status: live end to end since 2026-09-15 12:21Z — producer on 0.12.13, consumer verifier and
keeper cutover in `forecast-risk` (`token-policy-input-v3` evidence). Design source: R11 measurement-window review
(`.omx/plans/risk-feed-window-v2.md`, decision B "containing-window estimated-upper-bound").

## Why v1 was insufficient

A v1 binding carried `channel` and `horizon_hours` but no target interval. A published question
"during [S, E) …" answered at a later as-of A > S estimates the *remaining* event of that fixed
window, not a rolling `[T, T+24h)` at policy time T. Re-signing it every two minutes did not
change its meaning. v1 stays frozen (prefix `forecast-risk-feed-v1:signature:`, schemas, fixtures,
publications, tables); v2 is additive.

## Shared contract (`forecast_domain.risk_feed`, schemas/v1/*V2.schema.json)

| Record | Purpose |
| --- | --- |
| `CanonicalRiskDefinitionV2` | What a channel measures: predicate, threshold, sampling grid, `[start,end)` rule, source and invalid-data policy, policy horizon H, mapping profile identity, freshness bounds, calibration cohort. Hash = registry identity. |
| `RiskMappingProfileV2` | Reviewed relationship between question event and policy event: `containing_upper_estimate` (monotone any-event predicate, no policy lag) or `exact_dated` (declared max policy lag). |
| `RiskFeedBindingV2` | Immutable question + `specification_hash` + typed target `[S,E)` + series/episode + definition/profile hashes. Authorization validity (advance forecasting allowed) is separate from operational validity (policy use). Containing mode requires `operational_valid_until + H ≤ E`; exact mode requires `E − S = H`. |
| `RiskFeedSignalV2` | `value_kind = question_probability` only. Clock roles recorded separately: `forecast_as_of_ms` (A), `information_cutoff_ms`, evaluation start/completion (G), source capture start/completion, optional `source_watermark_ms` (absence is declared, never invented), pool constituent provenance for crowd/top. |
| `ChannelCoverageV2` | Every required channel is reported: `covered` (names its binding) or `unavailable` / `saturated` / `unsupported` with a reason. Missing mappings stay in the denominator. |
| `RiskFeedPayloadV2` / `SignedRiskFeedV2` | `purpose = forecast-risk-feed-v2`, prefix `forecast-risk-feed-v2:signature:`, TTL ≤ 120 s, feed inside every binding's operational validity, every signal's evaluation completed before issue, exact-dated estimates as of ≤ S. |

`freshness_as_of_ms(signal)` is the shared rule: a pool is only as fresh as its oldest member.
Golden vectors live in `tests/test_risk_feed_v2_contract.py` and are imported verbatim by
`forecast-risk/tests/test_window_projection.py`.

## Producer (`forecast_application.risk_feed_v2`, migrations 0026–0027)

- `POST /api/admin/risk/v2/definitions|profiles` admit immutable registry artifacts per feed.
- `POST /api/admin/risk/v2/bindings` approves a binding only when the typed `[S,E)` equals the
  published question's own `[start, end)` (compiler `exact-utc-window-and-candidate-reference-v4`),
  `E` equals the published deadline, and the definition/profile hashes are admitted and consistent.
- `POST /api/admin/risk/v2/bindings/{id}/refresh` produces a new estimate and retains a
  `risk-prediction-clock` artifact (capture start/completion, evaluation start/completion, cutoff,
  source bundle hash) keyed by the estimate artifact. Compile-time estimates have no clock and are
  never published as v2 signals.
- `POST /api/admin/risk/v2/feeds/{id}/publish` signs a payload with the relayer key. Expiry is
  bounded by TTL, operational validity and `freshness_as_of + max_forecast_age`. A feed with no
  eligible estimate still publishes signed `unavailable` statuses; it never manufactures a number.
- `POST /api/admin/risk/v2/feeds/{id}/operate` stores the admitted weight reference and enables the
  scheduled tick; `POST /api/admin/risk/v2/operate` runs one tick: at most one budgeted refresh per
  feed (stale = no clock-backed estimate or age ≥ half the profile's max, prewarmed half an age
  before operational start; exact-dated never after S), then one publication, logged in
  `risk_feed_operation_log_v2`. The tick is driven every 60 s by the supervised operator host
  (`scripts/operate_risk_v2.py` under launchd `com.forecast-network.risk-v2-operator`), not by a
  Worker cron: a per-minute Python Worker cron collided with in-flight requests in the same isolate
  (`SystemError: Cannot enter a promising task…`) and also broke the `*/5` sweep when both fired.
- `GET /api/risk/v2/feeds/{id}` returns the latest envelope with `protocol` and `current|stale|unavailable`.

## Consumer (`forecast_risk.window_projection`)

`verify_feed_v2` checks signature/scope/sequence/key, approved definition/binding/profile hashes,
clock skew, forecast age (oldest pool member), source capture age, then per channel at policy time T:
containment `S ≤ T ∧ T+H ≤ E` (or dated target with declared lag), this host's `WindowCoverageV2`
(observed from ≤ S, no YES witness, and recent: coverage ends with the last *completed* grid candle,
so the allowed lag is one `sampling_grid_ms` plus the profile's source freshness), and explicit
value-kind admission. Producer-attributable faults reject the publication; T-dependent or host-side
ineligibility withholds the channel with a visible reason. Each admitted signal carries a
`WindowProjectionProofV2` (original `[S,E)`, A, T, derived `[T,T+H)`, value kind, containment proof)
bound transitively into the kernel `RiskSignal.evidence_hash`; the number is never altered, only
its meaning is tagged (`forecast-risk-feed-v2-upper-bound` / `-exact-dated`).

## Recurring episodes (`RiskFeedSeriesV2`, `forecast_application.risk_feed_series`)

- The operator approves one shared series template per channel (`POST /api/admin/risk/v2/series`):
  definition/profile hashes, `window_ms` (48 h), `cadence_ms` (12 h), `lead_ms` (3 h) and the question
  template with `{start}`/`{end}` placeholders. The same record, hash-identical, sits in the keeper
  config as `approved_series`.
- Every operate tick calls `create_due_episodes`: when the next cadence-aligned start is within the lead
  window, the question goes through the ordinary canonical seed path (compiler, review, retained
  artifacts) and is bound with the deterministic typed target; the outcome is logged in
  `risk_feed_series_log_v2` and retried next tick on failure. One episode per series per tick.
- Overlapping episodes pass duplicate review by the distinct-measurement-interval rule (canonical seeds
  only). The consumer accepts any binding that conforms to an approved series (`RiskFeedSeriesV2.conforms`:
  same hashes, aligned start, exact window, `[S, E−H)` operational reservation), so no keeper restart per
  episode; `approved_series_hash` is part of the attested evidence.
- `GET /api/admin/risk/v2/health` reports feed age, tick failures, series schedule and source-watch
  staleness; `scripts/monitor_risk_pipeline.py` (launchd, 5 min) combines it with the keeper journal and
  raises a macOS notification on degrade/recover. Upstream publishers that block Workers egress
  (Microsoft newsroom returns 403 to Workers) are reported as warnings, not degradation.

## Calibration (R10) — `GET /api/admin/risk/v2/feeds/{id}/training`

The producer exports finalized, eligible canonical questions with the exact first signed signal per
source. `forecast-risk/scripts/train_calibration.py` turns that into `TrainingObservation`s and a
`WeightSnapshot` candidate; admitting a snapshot (producer artifact + keeper `weights_path` + operation
config) stays an explicit operator step. Until the first canonical question finalizes there are zero
observations and weights remain provisional.

## Keeper (`forecast-risk/scripts/risk_keeper.py`, `token_policy_v2.py`, `window_coverage.py`)

- Config `feed_protocol: "v2"` carries the trusted relayer key, admitted definitions/profiles/bindings,
  `profile_set_hash` and an explicit `admitted_value_kinds` list; v1 replay paths are unchanged.
- Each tick: market quotes → v2 feed fetch → chain clock read (host time captured at the same instant;
  the profile bounds skew at 5 s, Devnet lags ~1–2 s) → `collect_window_coverage` fetches Kraken and
  Bitstamp 5-minute candles from each binding's S (raw bytes retained under `tmp/keeper-stress/sources`)
  → `prepare_token_policy_v2` → `TokenPolicyEvidenceV3` (feed, weights, coverage, proofs, withheld
  channels, cached-acceptance proof) hashed into the 277-byte record attestation → replay before signing.
- First live v3 action 2026-09-15 12:21Z: `CRISIS → DEFENSIVE` (`relaxation_step`, forecast available);
  subsequent ticks `dwell_not_elapsed` while the 24-hour dwell accrues on real inputs (R13 evidence).

## Live state (2026-09-15)

- Feed `devnet-stable-risk-v2`, key `forecast-relayer-67a5b5a42ed9a813`, genesis pinned to Devnet.
- Episode 1 (exact dated): `f_BHbIOkOLWiMNIEGD3sjxrOSf`, `[2026-09-15T09:00Z, 2026-09-16T09:00Z)`;
  compile-time AI estimate has no clock → channel reported `unavailable`.
- Episode 2 (containing): `f_tkqvmogdeeITz0c_XMNJTB68`, `[2026-09-15T12:00Z, 2026-09-17T12:00Z)`,
  operational `[12:00Z 15th, 12:00Z 16th)`; clock-backed refreshes hourly, AI signal live from 12:02Z.
- Keeper `com.forecast-risk.keeper.stress-devnet` runs `~/.config/forecast-network/keeper-stress-devnet-v2.json`
  (v1 config kept as `keeper-stress-devnet.json`); one episode per channel is selected by newest target
  start with fallback to an older eligible episode (0.12.11).

- Episode 3 (containing): `f_FvhBuS1YUvmFzbMJRuIi76z7`, `[2026-09-16T00:00Z, 2026-09-18T00:00Z)`. Overlapping
  canonical episodes pass duplicate review through a deterministic rule (0.12.14): for operator canonical
  seeds only, a candidate whose published question declares a *different* explicit `[start, end)` is a
  distinct measurement contract (`materially_different_rules=true`, receipt lists
  `distinct_measurement_windows`); the raw model verdict is retained unchanged and the counter-judge is
  told the policy. User questions never get this treatment.

- Second channel (R12): series `btc-crash-1d-w48` on `btcCrashRisk` with an absolute threshold
  (USD 61,000, ≈20% below the admission-time price; monotone any-event predicate, so containment
  applies unchanged). First episode `f_kFOj9FjHp6oP2C-h3B4mNNZy` `[2026-09-16T00:00Z, 2026-09-18T00:00Z)`.
  Thresholds are fixed per definition; re-admit a new definition/series when the price regime moves.
- Series episode attempts back off 10 minutes after a failed compile (Kraken rate-limits Cloudflare
  egress transiently; the compile-time source check then fails honestly).

## Not yet done

- R13 evidence: the 24 h dwell on real v2 inputs started 2026-09-15 12:21Z; the first natural
  relaxation (or its honest absence) is recorded by the keeper journal.
- Calibration v2 partitions (source question outcome vs. rolling evaluation), episode creation cadence
  (48 h every 12 h) and the real 24 h dwell evidence (R13).
