# Operational criticality and where the observers live

The pipeline monitor reported `degraded` for two days and the branch checks were
red for four. Neither was hidden: both were written down, in a log file and on a
CI page, the whole time. Nobody looked, and nothing made anybody look.

That is not an operations mistake. It is a placement mistake, and this is the
record of it: what runs where, what actually has to keep running, and which
observers sit inside the thing they are supposed to observe.

## The placement problem

A monitor that shares a fate with what it watches is not a monitor. Today:

| Watched | Watches it | Shared fate |
| --- | --- | --- |
| The operator host's five launchd jobs | `risk-pipeline-monitor`, on the same host | host down → both down |
| The Worker's source watch and registry relay | `GET /api/admin/risk/v2/health` | nobody polls it |
| `checks.yml` on `main` | GitHub's own notifications | nobody reads them |

Three failure domains carry five production-critical processes between them, and
there is no observer standing outside any of them. When the operator host stops,
the process that would say so stops with it.

## The inventory

Measured on 2026-09-17 from `launchctl list` and the Worker configuration.

| Job | Cadence | Stop means | Class |
| --- | --- | --- | --- |
| `com.forecast-network.risk-v2-operator` | 60 s | Feed `expires_at_ms − issued_at_ms` is 120 s, so publication stops being current within two minutes | **liveness** |
| `com.forecast-network.devnet-rpc` | keep-alive | The Worker's `SOLANA_RPC_PROXY_URL` has nothing behind it | **liveness** |
| `com.forecast-network.rpc-tunnel` | keep-alive | Same, from the public side | **liveness** |
| `com.forecast-risk.keeper.stress-devnet` | keep-alive | On-chain policy transactions stop being submitted | **liveness** |
| Worker cron `/api/admin/sweep` | 5 min | Official sources go stale, registry delivery stops | **liveness** |
| `com.forecast-network.risk-pipeline-monitor` | 300 s | Nobody is told | **observer** |
| `com.forecast-risk.keeper.shadow-rs` | 300 s | The Rust keeper's shadow parity evidence pauses | **deferrable** |
| `Daily editorial seed` (GitHub) | daily | Fewer questions get published | **deferrable** |

Four liveness processes and one liveness cron run on a single Mac. Two more are
deferrable and happen to share the same host, which is how a development parity
check ends up competing with production publication for the same machine.

## What "always-on" costs, and what can avoid it

Triggers can be borrowed. Liveness cannot.

- **Lazy execution** — do scheduled work as a side effect of a request someone
  else already paid for. This is how Compound, Aave and Uniswap V3 avoid
  interest and oracle crons entirely. It converts "at 12:00" into "on the next
  interaction", so the guarantee becomes probabilistic.
- **Borrowed schedulers** — Cloudflare cron and GitHub's scheduled workflows are
  included in plans already paid for. GitHub treats scheduled runs as best
  effort and disables the schedule after 60 days without repository activity.
- **Incentives** — make the work permissionless and rewarding, and strangers'
  bots do it. Not free, but not operated by us either.

None of these removes the need for something to be running. The risk feed has a
120-second expiry and the watch exists to notice a known outcome while nobody is
looking; neither can be deferred to an interaction that may never come. For
those, the cost is paid in uptime — today, in this Mac's.

## What was implemented

`.github/workflows/watchdog.yml` and `scripts/watchdog.py`: an observer on
GitHub's runners, which is a third failure domain, outside the operator host and
outside Cloudflare. It reads only public endpoints, so it holds no credential and
can live in a public workflow:

- `GET /api/health` — the service is up
- `GET /api/status` — the version answers
- `GET /api/risk/v2/feeds/devnet-stable-risk-v2` — `status` is `current`, and
  `serverTime − envelope.payload.issued_at_ms` is inside ten minutes

The feed's own clock is used for the age, never the runner's, so a runner with a
wrong clock cannot invent an outage or hide one.

It opens one issue and edits that issue in place rather than commenting on every
run, and closes it on recovery. A second issue tracks `checks.yml` on `main`:
a single red run on a push is ordinary, an unchanged red run means nobody looked.

**This is a backstop, not a pager.** GitHub delays scheduled runs under load and
disables the schedule after 60 days without activity. It is here so that the next
outage lasts minutes rather than the days this one did.

## What remains, in the order it is worth doing

The obvious first move — take the per-minute trigger off this Mac — turns out to
buy less than it looks like, and it is worth writing down why before doing it.

**This Mac carries three separate production dependencies, not one.** Verified on
2026-09-17:

1. The per-minute `risk-v2-operator`, which triggers feed publication. The feed
   payload's genesis hash is a hard-coded constant, so publication itself needs
   no RPC — only the trigger.
2. `devnet-rpc` plus `rpc-tunnel`, which serve `forecast-rpc.eastsea.xyz` →
   `http://127.0.0.1:8788`. This is the production registry delivery path: it
   exists because the public Devnet RPC answers Cloudflare's Workers with HTTP
   403, and the alternates that were tried failed the same way.
3. `keeper.stress-devnet`, holding a Keychain signing identity on this host.

Removing dependency 1 alone does not remove the host — 2 and 3 keep it on the
critical path, so the Mac must stay up either way. What it does buy is that the
**public risk feed survives a host outage** while the registry relay and the
keeper degrade. That is a real improvement and worth having, because the feed is
the consumer-facing surface, but it is not the biggest lever.

**The blocker is a secret boundary, and it should be a decision, not an
edit.** `scripts/deploy_edge.py` states the rule: the edge carries session
hashing and provider presence, and *every other secret stays with the Python
Worker*. A cron on the edge that calls `POST /api/admin/risk/v2/operate` needs
`ADMIN_TOKEN` there, which crosses that line. The options, and what each costs:

- **Give the edge `ADMIN_TOKEN`.** Smallest change. Widens the edge's blast
  radius from session forgery to admin.
- **Add a third Worker that holds only `ADMIN_TOKEN` and one cron.** Keeps the
  edge's surface intact and creates a new thing to deploy and monitor.
- **Re-enable `* * * * *` on the Python Worker.** The routing already exists
  there and the secret is already there, so nothing new is exposed. But this is
  what was removed in 0.12.10: the scheduled handler awaits a fetch from a Python
  invocation, and Cloudflare runs several invocations per isolate, so a
  concurrent request collides with it. The 1101 fixed in 0.12.22 was one instance
  of that class; this one is structural, and re-enabling it would most likely
  reproduce the original incident rather than avoid it.

None of these should be taken without the custodian of that boundary saying so.

**2. Take the RPC path off this Mac.** This is the deeper item: it is dependency
2 above, it is on the production registry delivery path, and it is the reason
the host cannot be allowed to sleep. Resolving it means finding a provider that
answers Cloudflare's Workers, which was attempted before and failed for the
alternates tested. Research first, then code.

**3. Move the keeper's key, deliberately.** The keeper signs with a Keychain
identity on this host. Moving it is a key-custody decision, not an ops tidy-up,
and `scripts/backup_recovery.py` exists to make that move survivable. It needs a
decision before it needs code.

**4. Relegate the deferrable work.** `keeper.shadow-rs` is development parity
evidence running on the production host every five minutes. It should not
compete with the risk feed for this machine.

## Rules

- An observer must not run on the host it observes, or on the platform it
  observes. If it must, say so explicitly and add an observer from elsewhere.
- Say which class a job is: liveness, observer or deferrable. A job whose class
  is unstated will be treated as liveness and will be scheduled like production.
- Liveness is paid for in uptime. Do not plan to free-ride on it; free-riding
  works on triggers.
- A failure that nobody is told about is not monitored, however well it is
  logged.
