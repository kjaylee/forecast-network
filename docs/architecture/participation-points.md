# Participation Points

Implementation contract for the profile/wallet onboarding and optional prediction
stakes requested on September 10, 2026. The original forecasting handoff remains
authoritative; points have no economic value and do not determine reputation.

## Policy v1

| Action | Participation points |
| --- | --- |
| Create a profile | 1,000, once per account |
| Verify wallet ownership | 500, once per account and once per wallet address |
| Practice forecast | Zero points committed |
| Point-backed forecast | An explicit integer amount from 1 to 1,000 |
| Correct finalized result | Return twice the committed amount, including the original commitment |
| Incorrect finalized result | The committed amount is consumed |
| INVALID finalized result | Return the original committed amount |

These defaults are versioned. Any later policy must preserve the rule attached to
existing commitments. Points cannot be bought, transferred, withdrawn, redeemed,
or exchanged for coins, assets, prizes, discounts or other economic benefits.

Confidence remains an independent probability. Crowd averages and reputation
continue to use the existing forecasting rules, without weighting by points.
Practice remains available when the spendable balance is zero.

## Onboarding awards

Profile creation, its points account and the profile grant commit in one database
transaction. Wallet ownership must be cryptographically verified before the wallet
link and award commit. Database uniqueness records each milestone permanently.
Renaming a profile, logging in, unlinking/relinking, or connecting another wallet
cannot award the same account again. An address that has already funded a wallet
award cannot fund another account. A fresh address is not consumed when its current
account has already received the wallet award.

The migration gives existing profiles the same starting grant and handles eligible
currently verified wallet links once. Historical predictions remain practice
forecasts; no points are retroactively taken from users.

A wallet proves address control, not that an account represents a unique person.
Existing registration and request limits remain part of abuse prevention.

## Commitments and settlement

Available points and committed points are separate nonnegative, bounded integer
balances. Before closing, an explicit stake update moves only the difference
between those balances. The user's latest selected outcome is attached to the
commitment. Reducing the amount to zero changes it to practice.

The forecast update, point reservation, immutable point entry and command receipt
share one atomic D1 batch. Forecast revision checks protect competing updates to
one question. An account balance guard prevents simultaneous commitments to
different questions from spending the same available points. No negative balance,
partial forecast success, duplicate debit or silent reduction is allowed.

New point actions identify the account displayed by the client. The server compares
that precondition with its authenticated account before mutating; client identity
is never authority. A stale tab cannot spend or release a different account's points.
Legacy requests without a stake retain their original retry identity and remain
practice when no positive commitment exists. Updating an existing positive stake
without an explicit amount requires a refresh.

Resolution proposals, disputes and pauses do not settle points. Only an immutable
FINALIZED/ARCHIVED outcome may settle. Settlement shares the durable outbox batch
and uses unique position identities and unsettled-state guards so retries cannot
credit twice. INVALID refunds are distinct from correct-call rewards. All changes
remain reconstructable from an append-only point ledger.

## Presentation and limits

The authenticated profile shows available/committed balances, one-time onboarding
steps and a recent transaction history. The forecast form shows the stake and its
result rules before submission. Insufficient funds produce a clear error and a
balance refresh; confidence and stake inputs remain intact.

Public reputation cards continue to show accuracy, Brier/calibration, sample size
and finalized calls. They do not turn a starting grant or point balance into a
quality badge. No Solana transaction is needed for these participation points.

## Required verification

Verify one-time grants, existing-account backfill, atomic registration and wallet
linking, wallet reuse across accounts, practice/positive stake transitions,
cross-question overspending, replay with changed amounts, stale-account actions,
all three final outcomes, settlement retry, invalid refunds, rollback and unchanged
crowd/reputation weighting. Use real SQLite/D1 queries and the existing domain
lifecycle; do not shortcut challenge periods in production.
