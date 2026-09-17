# Forecast quality v2 read models

`reputation.py` and `discovery.py` are pure application projections. They do not change domain records, published `profile-card-v1` JSON, balances, permissions, or finalized outcomes. Their versioned results supplement existing Brier/calibration/domain, creator invalidity, and event-linked dispute metrics. API/UI integration and deployed verification are separate acceptance gates.

## Eligible history and replay boundary

`eligible_history(rows, *, as_of_ms, window_ms=None, exclude_forecast_id=None)` returns chronological records containing only `userId`, `forecastId`, lowercase `category`, integer `probabilityBp`, binary `outcome`, `submittedAt`, `finalizedAt`, and `eligibilityAt`. This is also a feed-compatible scoring-history representation: no floating scores, private profile data, wallet addresses, or credentials are exported. The caller must choose the feed's allowed category/scope and redact public participant identifiers when its feed contract requires that.

Required input columns:

| Column | Meaning |
|---|---|
| `user_id`, `forecast_id`, `category` | Stable IDs and immutable specification category |
| `probability` | Recorded YES probability, integer percent, not choice confidence |
| `outcome`, `finalized_outcome` | Equal finalized YES/NO outcomes; INVALID is excluded |
| `state` | FINALIZED or ARCHIVED at the read cutoff |
| `submitted_at` | Accepted eligible receipt time, at or before finalization |
| `finalized_at` | Finalization event time |
| `eligibility_at` | Latest time necessary to establish this scored eligible record: maximum of finalization, score persistence, and any eligibility completion |
| `eligible` | Explicit integer 1/bool true, established by the adapter at the cutoff |

Missing eligibility, missing/invalid timestamps, future evidence, unresolved outcomes, mismatched results, invalid probabilities and ineligible records do not enter scoring. Duplicate `(user_id, forecast_id)` results raise an error instead of inflating sample counts. Stored `brier_score` and `correct` are not trusted by the new metrics; Brier/calibration are recalculated from probability and outcome.

For current-state integration, join `eligible_reputation_scores s` to `forecasts f`, `eligible_user_forecasts u`, and `MIN(events.created_at)` for `command_name='finalize'`. Join `forecast_eligibility_decisions d` to its completion `c`. Require `u.yes_probability=s.probability`, `s.created_at<=:as_of`, finalization at/before the cutoff, and no uncompleted eligibility decision or active participation hold. Supply `eligibility_at=MAX(s.created_at, finalized_at, COALESCE(c.created_at,0))`. Use one database statement/snapshot for the joined data.

**Historical replay is not a current-view query with a date filter.** When a later eligibility decision changes the accepted receipt, the adapter must reconstruct the earlier eligible receipt from immutable events/receipts and the decisions known at that earlier cutoff, or use an already retained input snapshot. Do not label a current materialized view as a complete historical reconstruction. These functions cannot prove the truth of a caller-supplied eligibility flag; the immutable retained input rows and query provenance belong with the resulting snapshot.

## Reputation and demonstrated category expertise

`reputation_quality(rows, *, as_of_ms, category=None)` accepts exactly one user's joined history and returns a supplement: `methodologyVersion`, `asOf`, `consistencyScore`, `consistency`, `expertise`, and `eligibleHistoryCount`. Existing projection owners continue to calculate their existing accuracy/dispute/creator metrics.

Mean Brier is `sum((p_bp-y_bp)^2)/(100000000*n)`. Calibration is `1 - sum(abs(sum(p_bp)-sum(y_bp)))/(10000*n)` over ten bins (100% enters bin 9). No results means null metrics.

Expert qualification is category-specific and uses trailing 365 days, including the lower boundary. It requires all of:

- At least 20 distinct eligible finalized questions.
- Results finalized on at least three distinct UTC dates.
- Mean Brier at most 0.20.
- Calibration score at least 0.70.

Statuses are `new`, `provisional`, `not-qualified`, or `qualified`. Passing another category cannot confer expertise. Qualifying demonstrates forecasting history; it is not a professional credential, investment qualification, or externally verified identity. Thresholds are disclosed with each result and versioned `forecast-quality-v2`.

Consistency covers the three consecutive 30-day windows ending at `asOf`. Each needs at least five results. Windows are left inclusive/right exclusive, except the final window includes exactly `asOf`; no result enters two windows. With enough data, consistency is `1 - (maximum window mean Brier - minimum window mean Brier)`. Without enough data, it is null with a `new` or `provisional` status. This measures **stability of realized forecasting error**, not accuracy. Fifteen consistently wrong 0%-YES forecasts resolving YES produce consistency 1.0 and Brier 1.0. Windows with losses 0, .25, and 1 produce consistency 0.0. Both examples are tests.

## Separate probability cohorts

`qualified_cohorts(history_rows, *, forecast_id, category, as_of_ms)` exports the exact sorted internal member-ID tuples for TOP and expert feed producers; `cohort_statistics` reuses it. `cohort_statistics(submissions, history_rows, *, forecast_id, category, as_of_ms, ai=None)` produces independent `crowd`, `top`, `expert`, and `ai` objects with probability/count. Each uses a null probability and zero count when unavailable. They may overlap in people; their counts must never be summed as independent participants or evidence sources.

Submission columns are `forecast_id`, `user_id`, `yes_probability` (integer YES percent), `submitted_at`, `revision`, `eligible`, and `eligibility_at`. Future and ineligible receipts are excluded; the most recent eligible receipt per user is selected by submission time then revision. Equal receipt identities with conflicting probabilities raise an error. Top users need ten eligible historical results and are the lowest-Brier ceiling-10% of the qualified global population, with user ID as the stable tie-break. Experts use the category rules above. The target forecast's own result is excluded from all qualification history. The cohort is qualified **as of the response cutoff**; for a historical prediction feed, use the prediction's cutoff, not a later response cutoff.

AI requires a bounded probability plus persisted `created_at`, nonempty string `provider`, and `model`; a future/missing timestamp or malformed value yields no AI count. Supplying a model output does not establish independent evidence. Keep source provenance and correlation checks in the feed/AI adapters.

Example: Alice is a science expert with Brier .01 and votes 80%; Bob is a politics expert and globally top with Brier 0 and votes 20%; a new forecaster votes 50%. The science question reports crowd 50%/3, expert 80%/1, top 20%/1. A separately retained AI forecast 65% contributes AI 65%/1 only.

## Discovery formula and inventory

`score_forecast(row, *, as_of_ms)` provides the full formula inputs/components/weights and score. `rank_forecasts(rows, *, as_of_ms, user_id=None)` ranks the **complete candidate set before pagination**. `recommendations(..., limit=5)` selects diverse daily picks. Ranking only a page from an older SQL ordering is not equivalent and must not be used to claim this formula ranks the inventory.

Input rows preserve existing card columns and require an explicit `eligible=1`, OPEN state, `created_at<=asOf`, `open_at<=asOf<close_at`, and no `participation_hold` to enter active ranking. Actual measured ambiguity is required, from `ambiguity_score_bp` or the persisted specification in `snapshot`; there is no fabricated clarity fallback.

Additional quality counts (at the same cutoff) are:

- `creator_finalized_count`, including INVALID, and `creator_invalid_count` as its subset. Count actual finalization events on eligible creator questions, not unfinalized projections or raw participation.
- `creator_reviewed_disputes` and `creator_material_disputes` as its subset. Count distinct event-linked completed review artifacts for that creator's questions, preferably evidence-validated reviews. Never count unresolved dispute submissions or confuse the creator's own challenger success with the quality of their questions. Deduplicate artifact hashes across event joins.

Each component is an integer in 0..10000:

| Component | Formula | Weight |
|---|---|---:|
| Clarity | `10000 - ambiguity_score_bp` | 40% |
| Creator | `floor(10000*(finalized-invalid+2)/(finalized+4))` | 20% |
| Adjudication | `floor(10000*(reviewed-material+2)/(reviewed+4))` | 15% |
| Engagement | `floor(10000*(3*min(participants,50)+min(comments,20)+min(shares,20))/190)` | 15% |
| Freshness | `floor(10000/(1+max(0,asOf_utc_day-created_utc_day)))` | 10% |

Creator/adjudication use a neutral four-observation prior. Zero samples are visibly `new`; 1–4 are `provisional`; 5+ are `established`. Popularity contributes at most 1500 score basis points even with arbitrarily many comments/shares/participants. The final score is `floor(sum(component_bp*weight_bp)/10000)`. A 90%-clear, one-day-old question with no history or engagement scores 5850; the returned inputs independently reproduce this exact result.

Equal scores use a portable SQL/Python key: SHA-256 of compact JSON `[version,UTC-day,user_id-or-empty]` yields 32 byte coefficients (each incremented by one); sum each coefficient times the Unicode codepoint of the corresponding ID character, through the first 32 characters. Forecast ID breaks collisions. This is a deterministic tie key, not a cryptographic commitment. User-specific tie-breaking changes only ties, not quality or eligibility. Freshness uses UTC dates so unchanged inputs do not reorder during a day merely because a question's creation-hour anniversary passes.

Recommendations take the highest-ranked candidate, preferring categories with fewer than two picks while such candidates remain. Every fifth slot reserves the best remaining cold-start candidate (fewer than five finalized creator questions) with clarity at least 7000, when available. Category diversity also applies within that pool. The selected object includes `recommendationReason` (`quality` or `clear-cold-start`). Return the actual available count; never invent inventory or filler picks.

## Verification boundary

The dedicated reputation/discovery tests cover independent golden calculations, invalid/future/duplicate exclusion, small samples, category isolation, current-target result leakage, temporal window edges, accuracy/consistency distinction, independent cohort counts, bounded manipulation, deterministic ties, cold-start/category diversity, and active eligibility. These pure-function tests do not close F05/F09/F11 until root integrates and tests actual SQL, API, UI and deployment behavior. Old profile-card code/schema is untouched.


## SQL/API integration

Migration `0023_forecast_quality.sql` creates `forecast_quality_history` and `forecast_quality_finalizations` plus scoring/finalization indexes. `Application.reputation` now returns the quality supplement; `me` and public creator profiles reuse it. All card paths expose independent `expert` and `ai.count` plus `cohortMethodology`. `quality_card_sql` aggregates target-excluded top/expert memberships over at most 100 selected IDs; it never fetches all participant history into Python. Detail and list cards expose `quality` evidence.

Trending uses the exact formula and portable tie expression in SQL over the complete filtered inventory before `LIMIT/OFFSET`, retaining inactive questions after active ones. Other sort/search/filter conventions remain intact. Daily selection queries at most five candidates for each `(category,clear-cold-start)` partition; this is sufficient for five recommendations, including category diversity/cold-start reservation. The API includes `dailyRecommendations` with reasons and `discoveryMethodologyVersion`. Every Python database binding await is sequential; independent list/detail reads use one `D1.batch` promise.

These endpoints are current-state observations. They do not advertise arbitrary historical-as-of queries. The view filters finalized binary outcomes, current eligibility completion, receipt/score probability agreement, and active holds. The response timestamp additionally excludes future score/eligibility evidence. Existing immutable profile-card-v1 SQL, payload, and commitment encoding remain untouched.
