# Evidence timing and receipt eligibility

The question's result and a person's eligibility are separate. A genuine YES
result stays YES even when a late submission is void. Ordinary related news is
not enough: the retained evidence must establish the original decisive condition,
with the existing independent qualification and source checks.

## Current cutoff policy

`evidence-cutoff-v1` uses the verified public availability time of decisive
evidence. Server-accepted submissions strictly before a precise cutoff remain
eligible. Submissions and edits at or after it are void. Client timestamps and
the time the collector happened to notice a precise publication do not move it.

An observation upper bound proves only that content was public by observation.
Entries at or after that bound are definitely late; earlier entries need timing
review. A date-only or missing publication timestamp is never silently replaced
with midnight or an invented exact time.

One-hour early closure is a separate prospective rule for scheduled announcements.
It must be disclosed before participation. This release does not retroactively
subtract an hour from existing questions' cutoffs or change their published rules.

## Preserving earlier predictions

Original accepted commands, receipts, forecasts, fills and ledgers remain intact.
Immutable classification and compensation records are appended. Current crowd
probabilities, personal projections and reputation use the eligible projection.
Old owner-published share snapshots remain historical artifacts with their original
timestamps and bytes; they are not rewritten into new claims.

For a person who edited after the cutoff, restore the last eligible choice,
confidence and stake. For example, NO with 100 points before publication followed
by YES with 900 afterward restores NO with 100 and returns the additional 800.
A late withdrawal to zero also restores the original 100-point commitment. It
cannot turn a previously losing prediction into a free cancellation.

When withdrawn points have already been used elsewhere, correction remains pending
until the original commitment can be restored without negative balances. Further
commitments are blocked; unrelated settlements can still supply funds for a retry.
There are no purchases, transferable points, debt collection or economic redemption.

A person with only late submissions receives their still-committed points back,
with no winner bonus, accuracy credit or reputation credit. Correction transfers
points between available and committed balances; it does not mint a reward.

## Market fills

Active and shadow balances remain isolated. A verified late buy suffix is refunded
in reverse receipt order, preserving every original fill and price. Only refunded
claims and their original spend are removed from live positions and reserves.
Remaining reserves must still cover every valid YES, NO and INVALID liability.
Earlier valid claims keep their original quantities and settlement terms.

Ambiguous timestamps, nonchronological fills, inconsistent reserves or previous
settlement keep the market under review. Existing pricing snapshots are retained,
not rewritten to fabricate a new trading history. Displayed prices freeze at the
last valid original receipt; unavailable timing shows no price instead of zero.

Receipt reconciliation is read-only. An uncertain response is not retried as a
new order during suspension. A missing receipt is definitively unaccepted only
when the same database observation also proves a cutoff prevents later insertion.

## Ordinary resolution and late discovery

Expiry does not bypass timing checks. Ordinary proposals and finalization with
participants inspect exact retained evidence bytes. Precise publication after all
accepted receipts can pass this bounded check. Earlier, equal, missing or
date-only publication requires further timing review before rewards or reputation.
This is a review hold, not a claim that every contextual article was decisive.

A newer article cannot erase a prior unresolved timing review. Delayed semantic
timing adjudication is not implemented in this release: unsupported cases remain
held. Already settled histories also require a separately audited correction;
there is no silent clawback, rewritten result or retroactive grant.

## Integrity and retries

Database barriers independently check classification completeness, the original
stake receipts, the restored target and actual position before certifying a
correction. Compensation identifiers make retries and lost acknowledgements safe.
Bounded retry scheduling prevents unfunded corrections from monopolizing the queue.

For forecasts with an eligibility decision, the final Solana reputation commitment
uses an explicit new payload version containing the policy, trigger, receipt
classifications and eligible predictions. Forecasts without such a decision retain
the prior commitment format. No deployed program layout or historical hash changes.

Implementation is in `eligibility.py`, `resolution_timing.py`, market compensation,
and migrations 0013–0015. Tests cover inclusive boundaries, late edits/withdrawals,
partial-classification attacks, account and reserve races, mixed valid/void positions,
duplicate refunds, missing history, ordinary-resolution bypasses and public projections.
