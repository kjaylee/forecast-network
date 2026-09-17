# Product analytics v1

`packages/application/src/forecast_application/analytics.py` implements F22 as a
read-only projection of retained application evidence. It does not collect clicks,
modify domain records, infer provider prices, convert Devnet SOL to money, or
claim that fixture retention is real-user retention.

## Admin integration

```python
from forecast_application.analytics import product_analytics

report = await product_analytics(
    db,
    as_of_ms=now_ms,
    window_start_ms=completed_utc_window_start,
    window_end_ms=completed_utc_window_end,
    cohort_start_ms=cohort_window_start,  # optional; defaults to metric window
    cohort_end_ms=cohort_window_end,
    population_kind="application",
    excluded_user_ids=tuple(configured_staff_ids),
)
```

Root owns authentication, `entry.py`, admin UI and production deployment. The
function executes **one database batch containing only SELECT/WITH statements**;
it never starts concurrent binding promises. Both D1 and the actual SQLite adapter
use the same SQL. `aggregate_product_analytics(snapshot, ...)` is the pure replay
counterpart. The response contains aggregate counts, no user IDs, handles, wallets,
session data, provider credentials or raw evidence bodies.

Each requested window is `[startMs, endMs)`, starts and ends at UTC midnight, is at
most 366 days, and must be fully complete at `as_of_ms`. Evidence timestamps are
strictly before the explicit `as_of_ms`; no wall clock is read by this module.
`inputHash` binds the read snapshot, requested windows, population and configured
exclusions. Empty denominators produce null, never a fabricated zero percentage.

The adapter caps each returned relation at 200,000 rows and fails explicitly if
the limit is exceeded. It never silently truncates a numerator or cohort. This is
a bounded admin aggregate, not an unlimited warehouse or a claim of a measured
production capacity beyond the actual load evidence.

## Sources and eligibility

- Accounts and question publication: `users.created_at` and `forecasts.created_at`.
  Question state at the cutoff comes from the latest retained lifecycle event.
- Participation: every immutable `submit_forecast` event joined to its artifact,
  exact forecast/specification and accepted timestamp. Using only the latest
  `user_forecasts` row would lose earlier active days after a forecast edit.
- A timing decision requires a completed eligibility overlay and an explicitly
  `eligible` receipt; `void`, `review`, unclassified and incomplete overlays do not
  qualify. Active participation holds also exclude the affected question's activity.
- Results: immutable `finalize` event time plus the retained outcome. Missing
  outcomes are reported unavailable and cannot improve validity.
- Disputes: event-linked submission/review artifacts; repeated references to the
  same dispute/review do not multiply counts. A review must belong to the same
  forecast as its dispute.
- Question clarity: the published specification's integer ambiguity basis points,
  transformed to `10000 - ambiguity_score_bp`. Missing/malformed values stay null.

Late-arriving retained events are assigned to their actual event day when the
report is recomputed. Idempotent domain receipt/event identities prevent retries
from adding duplicate events; analytics further collapses activity to one
identity/question/UTC-day tuple. Legitimate same-day edits do not inflate activity
frequency, while edits on a later day preserve that later active day.

## Population, identity and deletion

Migration `0024_product_analytics.sql` adds only the necessary append-only overlays:

- `product_analytics_exclusions`: user/question exclusions for staff, test, load or
  deletion. These are permanent, including after a restore.
- `product_analytics_identity_links`: admitted alias→canonical account links with
  an evidence hash. Cycles or chains over 128 hops are rejected by the reader.
- `product_cost_receipts`: explicitly admitted operation costs, described below.

Admissions require an authenticated administrative/provider boundary owned by the
caller. These tables are not self-service endpoints. An evidence hash alone does
not prove that two accounts are the same person or that an expense was paid.

Reserved ID prefixes `staff:`, `staff_`, `test:`, `test_`, `load:`, `load_`,
`sandbox:`, `fixture:`, `e2e:` and `smoke:` are excluded. Display names and shared IPs
are never used for classification or identity merging. Random normal account IDs
need explicit staff/test exclusions; no heuristic guesses that a person is staff.
The known service account `system_editorial` is explicitly excluded from user
activation, activity, retention and creator statistics. No broad `system_` heuristic
is applied.

An exclusion/deletion of any alias excludes its entire admitted canonical identity,
even in earlier windows. Current verified identity links and exclusion markers
therefore intentionally restate that actor's historical metrics. **Actor exclusion
does not delete its public questions or other users' participation.** Question stock
is filtered by question-level exclusion rows and reserved question-ID namespaces,
independently of its creator's classification or continued account presence. A
genuine editorial question remains in stock, quality and the publication→external
participation funnel. Only non-excluded application creators enter creator statistics.
An excluded test question cannot establish another user's activation cohort.

A test/load publisher does not by itself prove every question synthetic. Test/load
fixtures must use a reserved question ID or an explicit question exclusion, even
when the publishing actor is already excluded. Root's load/fixture admissions own
that declaration; analytics does not guess content population from the author.

`population.kind` distinguishes `application`, `fixture` and `isolated-load` data.
The latter two always carry `evidenceClass=fixture_or_load_only`. Application data
is labelled `application_database_observations`; unclassified-account count and
`humanIdentityVerified=false` prevent an account count from becoming a claim of
verified unique humans. Raw test/load activity belongs in load reports, not product
retention. The append-only deletion overlay must be included in backup/restore.

## Exact metrics

Every ratio includes numerator, denominator, unit, scale and availability status.
`valueScaled` rounds half up at scale 10,000. `valueBp` is set only for dimensionless
shares; cost/frequency averages use their declared units and are not percentages.
If the scaled result exceeds the exact JSON integer limit (2^53−1), both displayed
scaled fields are null with `status=out_of_range`; the original numerator and
denominator remain intact. This is neither a zero result nor saturated success.

| Metric | Definition and denominator |
|---|---|
| Active predictor | Distinct admitted canonical identity with at least one eligible prediction in the window. |
| Active question | Distinct question with at least one such prediction in the window. This is not merely published inventory. |
| Daily activity | The same counts by complete UTC day, plus distinct identity/question/day tuples. |
| Open inventory | Lifecycle state OPEN at cutoff, inside its open/close interval and not held. Separate from active questions. |
| Seven-day activation | Newly registered canonical accounts whose first eligible prediction occurs in `[createdAt, createdAt + 7 days)`, divided only by accounts whose full seven-day interval has elapsed. |
| Creation→participation | Published questions receiving an eligible prediction from someone other than the creator's canonical identity within seven days, divided only by mature published questions. This does not claim an uninstrumented draft/view/click funnel. |
| D1/D7/D30 | Cohort anchor is the UTC day of first eligible prediction. Numerator is identities active on exactly day 1/7/30; denominator is cohort members whose entire target UTC day has elapsed. |
| Pooled retention | Sum of mature cohort numerators / sum of mature cohort members. Immature users are separate, not failures. |
| Weekly frequency | Distinct active UTC days / active identity-week pairs, using complete Monday→Monday UTC weeks fully inside the metric window. |
| Invalidity / validity | INVALID / known finalized outcomes, and YES-or-NO / known finalized outcomes, based on finalization event time. Unknown finalized outcomes are separate. |
| Creator validity | The same exact validity ratio per creator with finalized results, returned without identifying the creator. |
| Published clarity | Mean published clarity among measured questions; missing-question count is explicit. |
| Disputes | Distinct submitted disputes and reviewed disputes in the event window; material conflict / reviewed disputes; disputed questions / known finalized questions. |

At one millisecond before the end of the D30 target day, D30 remains immature and
its value is null. Zero mature members stays unavailable even if younger cohorts
are large. The historical fixtures verify this rule; they do not establish any
actual D30 retention in the deployed product.

## Actual, unknown and reserved cost

Inspection found no durable complete AI invoice/usage ledger. `rate_limits` counts
AI lease admissions, expires, and does not identify actual billable requests.
`forecasts.ai_forecast` proves retained AI forecast records, not every provider call.
These cannot be multiplied by an invented price to fill the cost KPI.

`registry_delivery` records transaction signatures, but no actual transaction fee.
`registry_spend` is a conservative budget reservation, not settled expense. Its
all-populations subtotal is shown separately and never enters actual-cost ratios.

The root may admit an independently obtained actual fee/invoice into
`product_cost_receipts` using prepared SQL. Required fields are:

```
source_kind: provider | chain
operation_id: actual provider operation identity, or chain transaction signature
revision: 1, then strictly sequential admitted revisions
population_kind: application | fixture | isolated-load
forecast_id / user_id: optional attributed application scope
occurred_at / recorded_at: exact milliseconds, recorded_at >= occurred_at
status: known | unknown | estimated
amount_atomic: integer (null only for unknown)
unit: USD_MICRO for provider, DEVNET_LAMPORT for chain
evidence_hash: retained admitted receipt hash; required for known expense
```

The source, operation, scope, population, occurrence time and unit cannot change
across revisions. A later admitted actual receipt supersedes an unknown estimate;
only the latest revision known before the cutoff is counted, once per operation.
Repeated registry rows sharing a signature still represent one transaction fee.
Provider USD micro-units and Devnet lamports are never combined or converted.
An operator's real application expenses remain included when that operator is
excluded from engagement metrics or its account is later deleted. Cost population
admission (`application` versus fixture/load) and explicit question scope govern
expense inclusion; membership in the active-user denominator does not. Fixture or
load operation receipts must be labelled accordingly at admission, including work
performed by staff/service accounts.

Reports expose known-recorded subtotal, unknown operation count, separate estimated
subtotal, and known-subtotal/active-user or /active-question ratios. Actual fees
missing for observed registry signatures are counted explicitly. Provider historical
operation count remains null. **Complete actual total remains null and coverage
partial**, even if every currently retained receipt is known, because older omitted
operations cannot be disproved. A zero known subtotal is not a zero-spend claim.
Fixture receipts cannot enter an application-population cost report.

## Verification and remaining integration

`tests/test_product_analytics.py` runs against the exact SQLite migrations with all
eligibility, immutable receipt and point-adjustment guards enabled. Coverage
includes UTC boundaries, seven-day denominators, exact D30 maturity, history edits,
late events, duplicate receipts, alias deletion/cycles, reserved namespaces,
creator self-participation, eligibility completion/voiding, hold/release, unknown
quality, distinct dispute reviews, actual/estimated/unknown cost and signature dedup.
The reader test rejects any attempted write in its single DB batch.

Admin API/UI wiring, live receipt admission and live-data evidence belong to root.
No production write or deployment was performed by this implementation slice.
