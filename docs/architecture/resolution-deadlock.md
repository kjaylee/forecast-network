# Three forecasts cannot resolve, and nothing can clear them

Observed 2026-09-18, still true on 2026-09-19. Three forecasts sit with a
`job_error` and retry forever, and the analysis below is why no amount of
retrying will change that.

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

## What implementing it requires, traced

It is not a configuration change or a data fix, and the path is worth writing
down because each step closes an easier option:

1. **The review row cannot be edited or deleted.** `resolution_timing_reviews`
   carries `BEFORE UPDATE` and `BEFORE DELETE` triggers that abort.
2. **`ResolutionTiming.check()` blocks any resolution while a review row exists**,
   and does not read the row — its presence is the whole condition.
3. **The only way past it is `_completed()`**, which needs a
   `forecast_eligibility_decisions` row joined to a
   `forecast_eligibility_completions` row.
4. **A decision row's body must be a validated `EarlyResolutionTrigger`.** Its
   `proposed_outcome` is `Literal["YES"]`, its `irreversible` and
   `conditions_fully_satisfied` are `Literal[True]`: the record exists to say
   "an official announcement was observed and the outcome is YES". It cannot
   express "indeterminate".
5. **`Finalize` copies `resolution.proposed_outcome`**, so the outcome has to be
   INVALID in the resolution before finalization, not chosen at finalization.

So a new record type is needed — a resolution for a timing review that cannot be
determined — together with the transition that writes it, the completion
`_completed()` looks for, and settlement that refunds. That is roughly a day of
domain work with tests on the reward and reputation path.

**It is specified here rather than implemented.** The gate protects rewards and
reputation, the two forecasts are the first real exercise of it, and a rushed
change to the settlement path is worse than a precise handover. The decision is
made and unambiguous; what remains is code.

## Why this is not a bug to patch quietly

The gate exists to stop a result being finalized while the publication time of
its evidence is in question — that is, to stop rewards and reputation being
credited on evidence that may have arrived after participation closed. Loosening
it is a product decision about money and standing, not an engineering tidy-up,
and it should not be made by whoever happens to be editing the file.

What can be said without deciding anything: a state machine whose terminal
conditions cannot be reached from some of its own states is incomplete, and the
missing piece is a path that resolves a timing review for a forecast that has
already left `OPEN`. Whether that path completes the review, voids the forecast,
or refunds and closes it is the decision.

## Detecting it

`forecasts.job_error` is set on every failed attempt and never cleared, so these
three are visible and countable:

```sql
SELECT COUNT(*) AS total, SUM(job_error IS NOT NULL) AS with_error FROM forecasts;
```

Three of 24 forecasts are in this state. The pipeline monitor does not currently
report on it, which is why it went unremarked while the counts grew.
