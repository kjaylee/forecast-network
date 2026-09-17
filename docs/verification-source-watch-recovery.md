# Source watch outage and recovery, 2026-09-15 to 2026-09-17

A production defect left the official-source watch unpolled for two days and
the risk pipeline monitor reporting degraded on every tick. This record states
what was observed, what caused it, what changed, and what is still unresolved.

## What was observed

The monitor log at
`~/.local/share/forecast-network/risk-v2-operator/tmp/com.forecast-network.risk-pipeline-monitor.out.log`
reported `degraded: true` on all 496 of its entries, from its first run at
2026-09-15 14:51Z. `"N enabled watch sources stale"` appeared on every line and
the count grew 6 → 11. From 2026-09-16 21:08Z it also reported
`series btc-crash-1d-w48 failed 7 episode attempts` and
`series usdc-depeg-1d-w48 failed 5 episode attempts`.

`lastStatus` on launchd for `com.forecast-network.risk-pipeline-monitor` was 1.

## What it was not

Not a fetch failure and not an upstream publisher problem. The health view
computes:

```sql
SUM(failure_count=0 AND (checked_at IS NULL OR checked_at<?-interval_ms-300000)) AS stale
```

and reported `{total: 11, failing: 0, stale: 11}`. Every enabled source had
`failure_count = 0`. A source that is polled and fails records a failure; a
source that is polled and succeeds advances `checked_at`. Neither had happened,
so the poller was not running at all — nothing was failing, nothing was being
attempted.

## Root cause

`registry_rpc` wrapped its fetch in `asyncio.wait_for(request(), timeout=20)`,
and `api()` already runs every route inside `asyncio.create_task(...)`. That is
a second Python task nested inside a running one, which Pyodide refuses:

```
SystemError: Cannot enter a promising task from inside another running promising task.
This is a bug in Pyodide.
```

Isolated on the live deployment, `GET /api/admin/registry/health` reproduced it
in 3 ms, every time. The five-minute Worker cron dispatches
`/api/admin/sweep`, which polls sources and then runs the registry sync. The
sync killed the invocation, and the sweep was classified as
`outcome: exceededCpu` — with only 720 ms of CPU consumed against a 38 s wall,
so the platform label described the broken state, not a real CPU overrun.

Because the failure was a hard kill, no `failure_count` was ever written, which
is exactly why the health view showed staleness rather than failure.

## What was verified after the change

Deployed as `forecast-network` version `42d08c89-fa3a-433e-ba46-3dfc6b36e397`.

| Check | Before | After |
| --- | --- | --- |
| `GET /api/admin/registry/health` | 500, 1101, 3 ms | 200, `signerVerified: true`, `rpcAvailable: true`, 0.8 s |
| `POST /api/admin/sweep` | 500, 1101 | 200, `polled: 2` |
| `sourceWatch.stale` | 11 | 1 |
| `POST /api/admin/registry/run` | — | `considered: 3, confirmed: 3` (31.9 s, was 54.9 s) |
| Monitor `problems` | stale sources + series | series counters only |

Three consecutive sweep runs returned `registry: {considered: 3, confirmed: 1}`,
`{considered: 3, confirmed: 1}`, `{considered: 3, confirmed: 3}`. The registry
relay is delivering and confirming revisions.

## The change

`registry_rpc` no longer creates a task. The deadline travels as
`AbortSignal.timeout(20_000)` on the fetch options, which needs no Python task.
A runtime that does not expose it falls back to the Worker's own limits and logs
`registry_rpc_timeout_unavailable`, so the weaker bound stays visible rather than
silent.

Two tests cover the new contract, including one that fails if any task-creating
primitive (`wait_for`, `gather`, `create_task`, `ensure_future`) reappears inside
`registry_rpc`.

## Not resolved

- **The series failure counters are a 24-hour rolling window.** They still read
  `failed 7` and `failed 5` because the last failures were at 2026-09-17 09:24Z.
  The episode those attempts targeted (`2026-09-17T12:00:00Z`) did publish, so
  the attempts eventually succeeded. The monitor will keep reporting degraded
  until 2026-09-18 09:24Z unless new failures occur. A monitor that alerts for a
  full day after a transient blip is worth revisiting, but the alerting
  threshold is a product decision and was left as it is.
- **The cause of those episode-creation failures is not established.** They were
  `failed:AppError` / `failed:ValidationError` between 2026-09-16 21:08Z and
  2026-09-17 09:24Z, and they stopped without a change on our side.
- **One sweep reported `registry: {status: "retry_pending"}`** on the first run
  after the deploy, with a `JsException` on `getGenesisHash`. It did not recur in
  three subsequent runs. Not reproduced, not explained.
- One `lifecycle: {processed: 1, failed: 1}` appeared during the investigation.
  Not investigated.
