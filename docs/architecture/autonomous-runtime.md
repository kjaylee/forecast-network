# Autonomous runtime — 0.8.0

Status: deployed and verified in production, including a real-provider early YES
proposal. Finalization remains subject to the full48-hour challenge period.
This document records the implementation boundary, not a claim that the service
has started earning revenue or completed a live outcome.

The product handoff (private) remains authoritative.
The earlier pricing and operating-cost proposal (private)
is the research record. This release implements a limited operating path from that
proposal while preserving existing participation promises.

## Three separate responsibilities

1. Observe approved official sources, pause entry when new evidence needs review,
   and bring qualifying evidence into the existing dispute process.
2. Rehearse changing prices with funded, isolated test-point accounts. The active
   participation-point adapter exists but remains disabled.
3. Test whether service obligations and refunds remain funded. The billing ledger
   is a nonbillable sandbox with no payment provider or customer charge path.

Solana, server and AI costs remain operator expenses. A working cost ledger alone
cannot create demand or income. Neither advertising nor sales work is introduced,
and no automatic break-even claim follows from this implementation.

## Official-source observation and containment

The initial source adapters cover approved Apple and Microsoft official sources.
They share observations across bound questions rather than ask an LLM to read the
same unchanged page for every forecast. Conditional requests use retained ETag and
Last-Modified values. Stable article text hashes suppress repeated work caused by
irrelevant markup changes; discovered articles are bounded and relevant historical
articles can be pinned so index rotation does not silently remove their coverage.

Raw fetched evidence is limited to 512 KiB and retained by hash. Model-facing
excerpts are bounded at 24 KiB. Discovery, source bindings, polling frequency,
retries and leases have explicit limits. This is a deliberately narrow observer,
not comprehensive monitoring of the web or a guarantee of instantaneous detection.

A source lease closes the observation-to-review intake gap. When changed evidence
is relevant enough for review, the observer records a participation hold before
calling a model. Both legacy forecast updates and market fills must respect the
current intake and hold guards. A model timeout or unavailable counter-review
leaves entry contained; it cannot silently restore participation or settle points.

Evidence qualification and counter-review require distinct configured providers.
A dismissal can release only a hold owned by the watcher and associated with that
review. It cannot clear an administrator's hold, a different observation's hold,
or a hold whose scope changed concurrently. Operator diagnostics retain failures
and pending work so a contained question is not mistaken for a resolved one.

## Versioned early positive resolution

An early trigger is suitable for a condition that, once satisfied, remains true:
for example an official announcement before a specified deadline. It is not a
shortcut for predicting that something will never happen or for conditions whose
answer can reverse before the deadline.

The new versioned contracts bind the qualification to the immutable specification,
retained evidence, review provenance and supported event-time basis. A complete
publication timestamp can establish an instant. A calendar date alone must not
be invented into an exact midnight timestamp; observation can instead establish
an upper bound when the deadline conditions permit it. The public view distinguishes
these cases and keeps the original closing time visible.

The early path proposes a supported outcome. Evidence linkage, independent review,
normal disputes and the 48-hour challenge period remain required before finality.
No clock is fast-forwarded in production. Existing specifications and the original
40 generated contracts remain unchanged; the generated contract set now contains
52 schemas, including separate early-trigger and pricing records.

The existing iPhone question remains on hold until its actual review and release
path succeeds. The presence of an announcement, new code, or a healthy observer
endpoint is not itself a completed YES resolution.

## Funded point markets

The pricing module implements buy-only LMSR using integer atomic point units and
certified cost bounds. One point is 1,000,000 atomic units. A question's price moves
as accepted YES or NO claims change its inventory. Crowd confidence, top-forecaster
estimates, AI estimates and reputation remain separate signals.

The initial policy uses liquidity of 1,000 points, a preallocated subsidy of 700
points, and orders of 1–100 points. An initial 100-point YES order produces
190.902828 claims at the tested policy and moves the displayed YES price from
50% to 54.76%. This is total correct-outcome return, including the order's cost,
not a universal fixed multiplier. A later order receives its own quote.

The reserve covers the maximum permitted final obligation. New markets allocate
subsidy from a finite treasury before opening. Account, question and aggregate
limits constrain exposure; rounding and fractional entitlements are explicit
rather than floating-point balance adjustments. These controls bound issued-point
obligations. They do not solve multiple-account abuse or make an active point
market economically risk-free.

Every quote binds the owner, question, specification, policy, market revision,
side, cost, claim amount and expiry. Confirmation sends the quote identifier,
minimum acceptable claims and a stable operation identifier. One atomic database
batch checks current safety gates and funding, consumes the quote, updates state
and balances, and records an immutable receipt. A changed price or balance requires
a fresh explicit quote. An uncertain response is retried with the same identity;
the client does not assume a fill or debit optimistically.

Shadow accounts are isolated from participation points. In the local runtime,
a 100-test-point fill moved the shadow account from 1,000 to 900 while the actual
participation balance stayed unchanged. The interface says “Market preview” and
“Test points”; it never presents a rehearsal as a real participation-point order.

Legacy fixed-return commitments remain under their original policy. Adding a
shadow preview does not remove or release them. If active markets are enabled in
a future controlled rollout, the confidence form records 0-point practice
separately from the market order. Active participation-point trading is disabled
in this release. There are no transferable or redeemable claims, purchasable
points, middle-of-period sell orders, or Solana settlement transactions.

## Nonbillable service-reserve sandbox

The public estimate explicitly returns `mode: sandbox` and `billable: false`.
It is an accounting illustration, not an offer to charge a customer. Sandbox
mutations are disabled by default and require an explicit host flag plus existing
administrator authorization. References and operation keys use the sandbox-only
namespace. No method authenticates a real payment receipt.

The ledger separates a hypothetical service payment from separately allocated
operator capital for the cost envelope. Publication does not immediately make the
payment spendable. Provider work reserves an attempt budget before it starts;
an uncertain provider bill retains that reservation until reconciled. Refunds,
completion, timeout and retry outcomes preserve the funded-obligation invariant.
Atomic guards and immutable operation results protect concurrent reservations and
replays. Full-refund and uncertain-billing paths passed isolated Worker/D1 checks.

A paid release still needs an authenticated payment adapter, measured provider
costs, fulfillment and refund handling, reconciliation and an explicitly approved
service policy. This release does not sell points, introduce ads, collect money,
or claim that operating expenses are covered.

## Runtime and API boundaries

All responses retain the existing `{data: ...}` envelope. Public reads do not
require an administrator credential. Browser writes retain same-origin protection;
funded quotes and fills also bind the expected signed-in account.

| Route | Boundary and purpose |
| --- | --- |
| `GET /api/status` | Reports source-watch, active-market and nonbillable sandbox flags |
| `GET /api/forecasts/:id/market` | Current funded market or null |
| `POST /api/forecasts/:id/market/quote` | Anonymous nonbinding preview; signed-in account-bound quote |
| `POST /api/forecasts/:id/market/fill` | Authenticated acceptance of a retained quote |
| `GET /api/me/markets` | Current account's positions; optional `forecastId` query |
| `GET /api/billing/estimate` | Nonbillable accounting estimate |
| `GET /api/admin/automation` | Authorized observer and work diagnostics |
| `POST /api/admin/automation/run` | Authorized bounded automation pass |
| `POST /api/admin/sweep` | Bounded scheduled-work entrypoint |
| `GET/POST /api/admin/markets/treasury` | Authorized budget inspection and capped funding |
| `POST /api/admin/forecasts/:id/market` | Authorized, specification-bound market creation |
| `GET/POST /api/admin/billing/sandbox` | Authorized sandbox inspection and accounting commands |

A quote request contains `side`, integer `spendPoints`, and, when signed in,
`expectedUserId`. A fill contains `quoteId`, decimal-string `minClaimsAtomic`,
`idempotencyKey` and `expectedUserId`. Quotes and fills never contain a user-supplied
market price. Automation and treasury credentials remain server-side.

The planned rollout flags are `SOURCE_WATCH_ENABLED=true`,
`LIVE_MARKETS_ENABLED=false` and `BILLING_SANDBOX_ENABLED=true`. The third flag
allows administrator accounting tests only; it cannot enable real billing.
Constructor defaults disable each optional capability. The scheduler invokes the
bounded authenticated sweep through the existing Worker self-binding.

## Migrations and rollout gates

Apply new migrations in order, without modifying applied historical migrations:

| Migration | Responsibility |
| --- | --- |
| `0007_source_watch.sql` | Shared source observations, bindings, retained review work and observation state |
| `0008_point_markets.sql` | Funded market state, isolated accounts, quotes, fills, positions and settlement guards |
| `0009_service_billing.sql` | Nonbillable capital, service invoices, attempt and replay accounting |
| `0010_watch_intake_guard.sql` | Database-level intake containment for legacy forecast inserts and updates |

Before describing the release as live, run the full host checks, apply the same
migrations in the actual Worker/D1 runtime, verify HTTPS flags and static assets,
and record a real-provider observer outcome without bypassing disputes. A bounded
failed provider call must remain visible as a pending or failed review. Verify
that shadow fills leave actual points unchanged and that billing stays nonbillable.
The [verification record](../verification-autonomous-runtime.md) distinguishes
completed local/production checks from the remaining full-cycle evidence.

Apple observation uses its official `/newsroom/rss-feed.rss` feed because the
Newsroom landing page does not expose its article list as readable server HTML.
Published source URLs and specification hashes remain unchanged; evidence articles
stay on the exact approved publisher host. Microsoft uses its readable official
article listing.

Deployment verified:0.8.0 serves the production domain with source observation
enabled, live markets disabled, and administrative billing tests explicitly
nonbillable. Actual Gemini/Cloudflare early review entered the iPhone question
into CHALLENGE. Any rollback must retain the v2 decoder;0.7-only code cannot read
upgraded v2 snapshots.
