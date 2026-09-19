# Three forecasts cannot resolve, and nothing can clear them

Observed 2026-09-18, diagnosed and fixed 2026-09-19. Three forecasts sat with a
`job_error` and retried forever; the analysis below is why no amount of retrying
could change that, and what was built to end it.

| Forecast | State | Failure count | Recorded reason |
| --- | --- | --- | --- |
| `f_tkqvmogdeeITz0c_XMNJTB68` | RESOLVING | 9 | evidence publication time and receipt eligibility under review |
| `f_kFOj9FjHp6oP2C-h3B4mNNZy` | RESOLVING | 7 | same |
| ~~`f_FvhBuS1YUvmFzbMJRuIi76z7`~~ | ~~PAUSED~~ | — | **cleared itself on 2026-09-19.** It was held for insufficient evidence, a different path that a later review could satisfy; the two above cannot. |

The difference between the three is worth keeping: the PAUSED one was waiting for
something that can arrive, and it did. The two RESOLVING ones are waiting for a
transition that does not exist.

The failure count is a retry counter, not an error rate: each sweep that finds
one due increments it, backs off up to six hours, and tries again. The counts
were 2, 4 and 6 when first measured and 5, 6 and 8 a day later.

## The mechanism

1. `ResolutionTiming.check()` blocks a normal resolution whenever a
   `resolution_timing_reviews` row exists for the forecast and its specification.
   It does not examine the row; its presence is the whole condition.
2. The only way past that is `_completed()`, which looks for a
   `forecast_eligibility_decisions` row joined to a `forecast_eligibility_completions` row.
3. Nothing in the two RESOLVING forecasts has either row. Checked directly in D1:
   two timing reviews exist, zero decisions, zero completions.
4. The only writer of a completion is `Eligibility.finish()`, and its only caller is
   the early-resolution trigger path in `automation.py`.
5. That caller requires the forecast to be in state `OPEN`. Both stuck forecasts
   are in `RESOLVING`.

So the review blocks resolution, clearing the review needs a completion, a
completion needs the early-resolution path, and that path needs a state these
forecasts have already left. Nothing in the system can move them.

## Root cause, found 2026-09-19: the judge was told to return what the resolver refuses

The earlier reading — that a new record type is needed — was wrong. The
capability was already there. Two lines apart, the system contradicted itself:

```python
"policy": "... Choose UNRESOLVED for insufficient or conflicting evidence, never guess. ..."
```

```python
outcome, status = Outcome(output["proposed_outcome"]), ConflictStatus(output["conflict_status"])
if status != ConflictStatus.CLEAR or output["rule_conflicts"] or output["confidence_bp"] < 8000:
    raise AIRejected("Evidence does not support a clear, sufficiently confident resolution")
```

The judge is asked for UNRESOLVED when the evidence is insufficient, and the
resolver then refuses UNRESOLVED outright. There is no terminal outcome for
"the evidence is authentic but cannot establish the result", so the job raises,
retries on the next sweep, and does so forever. Both stuck forecasts have
exactly that shape: `hashVerified: true`, `publication: null`,
`reason: publication_time_unknown`.

The judge's own schema already permits INVALID — `proposed_outcome` is an enum
built from `Outcome`, which has three members. Nothing was missing except the
instruction, and the counter-judge made it worse by requiring `agrees=false` for
incomplete evidence, which is precisely the case INVALID exists for.

Both prompts now draw the line: UNRESOLVED means more evidence could still
settle the question; INVALID means the evidence cannot settle it however long it
is kept, and is terminal — every commitment returned, no reputation credited.
The result stays a judgement. The prompts give the judge the answer it was
missing rather than making it for them.

## The decision: INVALID

Decided 2026-09-19. Both forecasts are to be finalized **INVALID**, which refunds
the original stake.

The review's recorded reason is `publication_time_unknown`, and its proof shows
`hashVerified: true` with `publication: null` and `publishedAt: null` on every
evidence item. The evidence is authentic and its publication time cannot be
determined — so it cannot be established that it post-dates the last receipt,
and it cannot be established that it does not.

That rules out the other two outcomes rather than being a preference between
them. Finalizing YES or NO would require a timing judgment the system explicitly
could not make, and would reward forecasts that may have been made in
possession of the outcome. INVALID is the only result the evidence supports: it
takes nothing from anyone and gives nothing to anyone. One of the two forecasts
has two real participants and the other has none, out of seven submissions in
the whole system, so this is small — and it is the same answer either way.

## What implementing it required, traced

Correcting the first draft of this section, which guessed at a new *resolution*
record. The three gates were real, but the shape of the fix was wrong, and the
traced path is kept because each step closed an easier option:

1. **The review row cannot be edited or deleted.** `resolution_timing_reviews`
   carries `BEFORE UPDATE` and `BEFORE DELETE` triggers that abort.
2. **`ResolutionTiming.check()` blocks any resolution while a review row exists**,
   and does not read the row — its presence is the whole condition.
3. **The only way past it is `_completed()`**, which needs a
   `forecast_eligibility_decisions` row joined to a
   `forecast_eligibility_completions` row.
4. **A decision row is not a general-purpose escape.** Its validation trigger
   binds it to `command_receipts` and the receipt-classification machinery, so
   writing one to close a timing review drags the whole eligibility settlement
   path in behind it.
5. **`Finalize` copies `resolution.proposed_outcome`**, so the outcome has to be
   INVALID in the resolution before finalization, not chosen at finalization.

### The gate is a boolean, and that was the actual problem

The first attempt at a fix — exempt an INVALID outcome inside
`ResolutionTiming.check()` — did not work, and the reason is the design rather
than the edit. The guard is not one gate. It is a view, `forecast_resolution_blockers`,
derived from `unresolved_resolution_timing_reviews`, and **five separate
consumers** read it:

| Consumer | Reads it to decide |
| --- | --- |
| `ResolutionTiming.check()` | whether a resolution may be committed |
| `resolution_timing_state_guard` trigger | whether the state may advance to PROPOSED/CHALLENGE/FINALIZED/ARCHIVED |
| `_advance_job` entry guard | whether the scheduler may touch the forecast at all |
| `_process_outbox` | whether effects may be published |
| `solana_registry._deliver` | whether the result may go on chain |

Carving an exception into one of them is four chances to leak a reward, and each
one found later is a wider hole than the one before. The commit that tried it
(`b21d68d`..`c7286b5`) changed nothing in production, because the forecast never
reached the changed line.

### What was built instead

**Close the review, so the boolean turns false everywhere at once.** Migration
`0029_resolution_timing_closure.sql` adds `resolution_timing_closures` and
redefines the one view — `unresolved_resolution_timing_reviews` now also excludes
a review that has a closure. `forecast_resolution_blockers` and
`resolution_timing_state_guard` both name that view and SQLite resolves views at
query time, so redefining it opened all five consumers together. No gate was
loosened.

A closure is narrow by construction. It is refused unless the review it names
exists, its reason is exactly `publication_time_unknown`, the closure quotes that
review's `proof_hash`, and it names an evidence item the review itself recorded as
unplaceable. It cannot be edited, deleted, or written after the forecast has a
finalized result.

It also **does not licence a reward.** `ResolutionTiming.check()` admits only the
outcome the closure determined, so a closure releases the forecast without making
YES or NO supportable — the two properties the first attempt conflated.

### The ordering that actually caused the deadlock

Writing the closure turned out not to be enough on its own. The scheduler's entry
guard reads the blocker view *before* the RESOLVING branch runs, and the closure
could only be written from inside that branch — so the guard refused entry to the
state the closure had to be written from. The call moved above the guard. That
chicken-and-egg is the whole deadlock in one sentence, and it is why the failure
was stable rather than intermittent: every attempt hit the same wall in the same
order.

## Why this was not a bug to patch quietly

The gate exists to stop a result being finalized while the publication time of
its evidence is in question — that is, to stop rewards and reputation being
credited on evidence that may have arrived after participation closed. Loosening
it is a product decision about money and standing, not an engineering tidy-up.

That decision was made: **INVALID**, because the review's own proof already
settles that no rewarded outcome is supportable. What the implementation adds is
only the path from that decision to the record — the decision itself was never
delegated to the code, and the closure asserts it rather than deriving it from a
model's opinion.

## Detecting it

`forecasts.job_error` is set on every failed attempt and never cleared, so these
three are visible and countable:

```sql
SELECT COUNT(*) AS total, SUM(job_error IS NOT NULL) AS with_error FROM forecasts;
```

Three of 24 forecasts are in this state. The pipeline monitor does not currently
report on it, which is why it went unremarked while the counts grew.
