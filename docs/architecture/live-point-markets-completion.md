# Active participation-point market readiness

Date: 2026-09-15. Scope: local application/SQLite verification and bounded
activation handoff. This record does not claim production activation, actual
user participation, or 48 hours of elapsed live operation.

## Existing implementation verified

The application adapter is `PointMarkets` in `markets.py`. There is no separate
`D1LiveMarketAdapter` to introduce: `Default.application()` supplies the existing
`D1Database` prepared-SQL adapter, and local verification runs the same migrations,
queries and transactional guards through `SQLiteDatabase`.

`Application` enables active buys only when both `LIVE_MARKETS_ENABLED` and
`SOURCE_WATCH_ENABLED` are true. The checked configuration has source watching
enabled and active markets disabled. This slice did not deploy or change flags.

- Active markets debit/credit `point_accounts`, and their history is included in
  `PointsService.summary`. Shadow markets use `market_shadow_accounts`; treasury
  balances and fractional remainders are independently keyed by mode.
- Quotes bind market/specification/policy/state revision, owner, exact atomic
  claims, acquisition cost and expiry. Accept rechecks identity, revision,
  expiry and the caller's minimum claims before committing.
- The transaction repeats forecast openness, participation holds, pending
  official-source review, source freshness and active polling-lease gates.
  Account/fraction snapshots and all caps are checked in that same transaction.
- Treasury subsidy is explicitly funded and reserved. Market fills, account
  ledger entries, positions, market revision and reserve changes commit
  atomically. A failed batch cannot leave a deducted balance or orphan fill.
- Actual positive legacy stakes block creating an active market on that
  forecast; an active market then rejects new positive legacy stakes. Practice
  submissions remain possible. No shadow/active ledger conversion is performed.
- Normal settlement needs an actual stored finalization event and matching
  immutable specification/outcome. INVALID refunds principal exactly. Evidence
  cutoff voids preserve original receipts and have independent compensation
  records. Payout fractions persist, and treasury remainder is returned once.
- Disabling active buys preserves prior-receipt lookup, compensation and
  settlement. An unsettled accepted liability is not stranded by that switch.

Existing implementation met these application requirements; no new pricing,
ledger, persistence abstraction or change to `markets.py` was needed.

## Full application/SQLite evidence

Two new tests in `tests/test_markets.py` exercise the actual application's
`_mutate` and `_advance_job` paths, including evidence-timing admission and the
unchanged `CHALLENGE_MS = 172800000` (48 hours). They use explicit local source,
provenance and clock fixtures; advancing that injected clock proves the gate,
not elapsed wall-clock operation or an actual external judgment.

The YES episode:

1. Three users receive the existing 1,000 participation-point profile grants.
   The active treasury receives 700 points, while a separately funded shadow
   treasury receives 13 points.
2. User A accepts a 100-point YES quote from their actual participation account.
   Available/committed becomes 900/100; the market probability changes from
   5,000 to 5,476 basis points. Another user's prior-revision quote is rejected.
3. Retained local evidence is published after every fill. The actual scheduler
   opens a normal 48-hour challenge. At deadline minus one millisecond,
   finalization and market settlement remain blocked, with no settlement row.
4. New active buys are disabled. The original fill receipt still reconciles;
   another fill cannot be admitted. At the exact challenge deadline, ordinary
   finalization succeeds and accepted positions can settle despite the switch.
5. The winning claim pays 190.902828 points, leaving user A with 1,090 whole
   available points plus 902,828 atomic remainder. Active treasury remainder is
   609.097172 points; shadow treasury remains 13 points.
6. Repeating settlement creates no second payout/closure. There is exactly one
   fill, settlement and closure; no shadow account was created. Global active
   available points + fractional remainders + returned treasury total exactly
   3,700 points, preserving all profile grants plus the treasury grant.

The INVALID episode independently waits through the same application policy,
blocks early settlement, then settles in bounded batches with active buys
disabled. Both users recover principal, committed balances and fractions become
zero, and the full 700-point subsidy returns once. Shadow issuance remains zero.

Retained local evidence is in
`tmp/live-point-markets-1789433201742567000/evidence.json` with a SQLite backup
`verified.sqlite`. A second read-only SQLite connection independently checked
the fill/settlement/closure counts and both treasury modes. The JSON records
`actual48HoursElapsed: false` and the database digest.

Verification: 78 market, market-eligibility, point and HTTP-transport tests pass;
both new integrated episodes also pass under Python `-O`. Scoped Ruff and mypy
checks pass. The existing race, stale/expired quote, slippage, hold, source-poll,
late-evidence void, treasury limit and lost-acknowledgment tests remain passing.

## Bounded activation handoff to the root owner

The root owns deployment, environment flags, API wiring, migrations and UI.
The admin treasury GET handler previously always selected the shadow budget;
root was given the exact correction to accept `?mode=active` and owns that
transport change/test. The following sequence uses existing interfaces:

| Step | Existing interface / acceptance |
| --- | --- |
| Inspect enablement | `GET /api/status`: `features.sourceWatch` and `features.liveMarkets` must reflect the intended deployment. |
| Inspect mode-specific treasury | Authorized `GET /api/admin/markets/treasury?mode=active`; compare separately with `mode=shadow`. |
| Fund one pilot | Authorized `POST /api/admin/markets/treasury` with `amountPoints: 700`, `mode: "active"` and a retained unique `idempotencyKey`. The same request replays safely; a changed amount under that key conflicts. |
| Open one eligible question | Authorized `POST /api/admin/forecasts/{id}/market` with `mode: "active"` and exact `specificationHash`. It must be OPEN, within its published participation window, without positive legacy stakes. |
| Confirm actual sources | Existing official watch bindings must have current successful checks, no active poll lease, no pending source review and no participation hold. Local fixture sources are not suitable for this step. |
| Inspect actual account | Authenticated `GET /api/me/markets?forecastId={id}` shows participation-point availability and positions. |
| Quote | Authenticated `POST /api/forecasts/{id}/market/quote` with `expectedUserId`, `side` and `spendPoints`. Retain `quoteId`, `revision`, `expiresAt`, `claimsAtomic`, cost and before/after prices. |
| Accept once | Authenticated `POST /api/forecasts/{id}/market/fill` with the same `expectedUserId`, `quoteId`, a decimal-string `minClaimsAtomic`, and a unique retained `idempotencyKey`. Use the accepted quote's exact claims as the strict minimum. |
| Reconcile uncertainty | `GET /api/forecasts/{id}/market/receipt?quoteId=...&idempotencyKey=...`; never infer failure from a missing acknowledgment and submit a new logical purchase. |
| Complete normally | Existing automation runs lifecycle/dispute gates and bounded settlement; actual evidence must show the real 48-hour window and resulting balance/receipt/treasury states. |

The enforced pilot limits are 20,000 treasury-issued points per mode, 100 points
per fill, 300 gross points per owner/question, 1,000 owner unsettled points,
2,000 lifetime gross points per question, 10,000 global unsettled gross points
per mode, 20 unsettled markets and five new markets per UTC day. The default
LMSR subsidy is 700 points; quotes expire after 30 seconds.

For a stop affecting already in-flight requests, use the existing durable
participation hold on the affected open forecast before or alongside disabling
new buys; the hold is rechecked inside the fill batch. A deployment flag alone
does not cancel a request already executing in an old instance. Preserve
receipts, eligibility compensation and normal settlement while paused.

Points remain non-purchasable, non-transferable and non-redeemable. No Solana
tokens, stablecoin balances or market-value collateral participate in these
ledgers. Production fill receipts, live UI verification and an actual elapsed
48-hour settlement remain root-owned evidence gates for closing F17.
