# Three forecasts cannot resolve, and nothing can clear them

Observed 2026-09-18, still true on 2026-09-19. Three forecasts sit with a
`job_error` and retry forever, and the analysis below is why no amount of
retrying will change that.

| Forecast | State | Failure count | Recorded reason |
| --- | --- | --- | --- |
| `f_tkqvmogdeeITz0c_XMNJTB68` | RESOLVING | 8 | evidence publication time and receipt eligibility under review |
| `f_kFOj9FjHp6oP2C-h3B4mNNZy` | RESOLVING | 6 | same |
| `f_FvhBuS1YUvmFzbMJRuIi76z7` | PAUSED | 5 | evidence or independent review insufficient |

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
