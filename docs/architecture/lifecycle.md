# Lifecycle and persistence contract

The handoff's lifecycle is implemented by `forecast_domain.lifecycle`. All inputs
are explicit and all outputs are immutable. The engine performs no I/O, clock read,
random ID generation, signing, provider call or notification delivery.

## State transitions

| From | Command | To | Principal guard |
| --- | --- | --- | --- |
| DRAFT | EditSpecification | DRAFT | New immutable unpublished specification |
| DRAFT | BeginValidation | VALIDATING | Valid draft |
| VALIDATING | RejectValidation | DRAFT | Failed assessment bound to this specification |
| VALIDATING | Publish | OPEN | Bound passing checks and duplicate/ambiguity policy |
| OPEN | SubmitForecast | OPEN | Opening inclusive, closing exclusive |
| OPEN | Lock | LOCKED | Closing reached |
| LOCKED | BeginResolution | RESOLVING | Expired forecast |
| RESOLVING | ProposeResolution | PROPOSED | Verified evidence, judge and counter-judge, handled conflicts |
| PROPOSED | BeginChallenge | CHALLENGE | Positive configured duration, no timestamp overflow |
| CHALLENGE / DISPUTED | SubmitDispute | DISPUTED | Bound proposal/clause/evidence, strictly before deadline |
| DISPUTED | ReviewDispute | DISPUTED | Bound evidence, counter-analysis and independent re-judge, including explicit invalid-evidence dismissal |
| DISPUTED | RetainProposal | CHALLENGE | All disputes reviewed, no material conflict; original deadline |
| DISPUTED | Escalate | ESCALATED | Completed reviews with material conflict |
| ESCALATED | AdjudicateResolution | PROPOSED | Independent bound adjudication; fresh challenge required |
| CHALLENGE | Finalize | FINALIZED | Deadline reached, no pending or material disputes |
| FINALIZED | Archive | ARCHIVED | Preserve outcome and resolution commitment |

The word EXPIRED in the resolution narrative maps to the LOCKED lifecycle state.
An OPEN snapshot can remain OPEN while a delayed scheduler has not yet locked it;
submission checks the actual supplied execution time and rejects expired forecasts.

Section 22's PAUSED requirement extends the diagram: RESOLVING, PROPOSED,
CHALLENGE, DISPUTED and ESCALATED can pause when every configured provider is
unavailable. Recovery returns to the recorded prior state. Where a challenge
deadline exists it is extended by the pause duration, preserving challenge
opportunity. No mutation other than explicit recovery is allowed while paused.
The future orchestrator is responsible for detecting the outage and persisting
the pause before it schedules finalization; the domain has no network-health oracle.

Validation rejection, retained-dispute return and adjudication restart make the
handoff's exceptional paths explicit. Changed proposals cannot inherit an elapsed
challenge window. Pausing or review never silently changes the published criteria.

Challenge duration and provider configuration are trusted application policy,
not values an end user may arbitrarily choose. Milestone 1 requires a positive
duration and checks overflow; the application must select and version its actual
duration and apply it consistently to initial and renewed challenges.

## Invocation and trust boundary

`create_forecast(...)` creates a draft. Construct a `Command` containing a typed
payload, a stable `idempotency_key`, and `expected_revision`; call
`apply_command(forecast, command, now_ms=..., prior_receipt=...)`.

The API/application service must authenticate and authorize each operation before
calling the engine: creator-only draft edits/publication, self-owned user forecasts
and disputes, service-only validation/resolution/pause/finalization, and restricted
adjudication. The command API is an internal domain API, not a public endpoint.

`now_ms` is trusted server execution time. Commands cannot backdate history.
The idempotency digest includes the full versioned command and expected revision,
but excludes execution time. Retrying after a lost response therefore works even
when wall-clock time advanced, provided the application supplies the original
trusted persisted receipt.

## Required transaction

For each command the persistence adapter must:

1. Load the authenticated forecast and receipt scoped by `(forecast_id, key)`.
2. Decode/validate the snapshot, then decide the command using a trusted clock.
3. Atomically compare the stored revision to the expected revision and save the
   updated snapshot, immutable event and receipt with a unique command key.
4. Insert outbox entries for the event effects in that same transaction.
5. On a duplicate key, reload its receipt and verify the command digest. On a CAS
   conflict, reload state and return a concurrency error; never overwrite it.

Receipt replay returns the **current** aggregate plus the original receipt and an
empty event tuple. It does not roll back later mutations or emit a second final
outcome. Callers must not submit fabricated receipts or stale cached snapshots as
authoritative storage. Concurrent decisions against the same old snapshot are
possible; only transactional CAS can select one durable result.

The aggregate keeps a bounded active dispute/review set and only its latest event
plus audit head. Full events, receipts, adjudication history and retired proposals
remain in external append-only storage. The current active-dispute limit is 256;
the application must reject excess submissions explicitly and apply deduplication
and abuse controls. This limit does not authorize dropping or ignoring accepted
disputes. A later design may move active dispute processing to a separate aggregate
with transactionally maintained pending/material counters.

Events are auditable facts with old/new state, command, revision, time and
commitments. They are not yet a complete replay/event-sourcing implementation.
Consumers use a stable event commitment and effect kind as deduplication keys.
Finalization emits reputation-update, result-notification and Solana-resolution
commitment intents; processing them is later milestone work.

## Failure coverage boundary

Milestone 1 tests invalid rules and duplicate assessments, changed/deleted evidence
commitments, unresolved conflict, late dispute, provider pause/recovery, stale
revisions, competing finalization decisions and receipt replay after a lost response.
These are domain simulations.

Live RPC outage/fallback, indexer lag, signing crashes, transactional isolation,
real notification retries, provider circuit breakers and coordinated account abuse
require their actual adapters in Milestones 2–7. No no-op adapter substitutes for
those guarantees here.
