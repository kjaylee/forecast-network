# Improvement review — 2026-09-21

What the tree looks like at `fc72a4a`, measured rather than recalled, and what is worth
changing next, in the order it is worth doing. This is a review, not a plan of record: each
finding names the evidence, the consequence, and the smallest change that removes it. The
strangler plan (`2026-09-16-rust-rewrite.md`) and the criticality record
(`../architecture/operational-criticality.md`) stay authoritative for *why* the shape is what
it is.

## How it was examined

- `python3 scripts/check.py`: exit 0 — **1,031 Python tests**, 34 goldens current, SQL parity
  **515 edge statements against 529 Python, 7 known differences**, route parity **59 served
  routes, all claimed**, 6 v2 overrides each with a reviewed dispatch.
- `cargo test` in `apps/web-rs`: **222 passed**; `packages/domain-rs` and the root workspace
  (`programs/forecast_registry`) pass.
- The live service was probed, not assumed: `GET /api/health` on `forecast.eastsea.xyz` answers
  from the Rust edge (compact JSON, `0.13.0`); `POST /api/auth/logout` without an origin answers
  `{"error": {"code": "origin_denied", …}}` — **Python's `json.dumps` spacing**, so the write
  reached the Python Worker through `LEGACY`. The route surface the tree claims is not the
  surface that is deployed.
- Every `env.var(…)` / `env.secret(…)` name in `apps/web-rs/src` was diffed against every
  `getattr(env, …)` name in `apps/web/src` and `packages/application/src`, and both against the
  two `wrangler.jsonc` files and the two deploy scripts.

## Where the tree stands, in numbers

| Surface | Size | Verification |
| --- | --- | --- |
| `apps/web-rs` (edge, serves production) | 69 files, 50,765 lines, of which 13,793 are `#[cfg(test)]`; 21 files over 800 lines (`writes.rs` 2,514) | 222 unit tests, 33 goldens shared with Python, SQL + route parity |
| Python Worker + application (reference) | `entry.py` 1,094; `service.py` 1,676; `ai.py` 1,620; application package ≈ 12.6k lines | 1,031 tests; the acceptance suite |
| Goldens | 48 files under `tests/golden/`, 35 generator scripts | Regenerated only by Python |
| Browser modules | 22 test files, `app.js` 1,003 lines | `node --test` |
| Operator host (this Mac) | 9 launchd jobs; per-minute feed tick and five-minute sweep both POST to the public domain | `deployed_drift.py`, `watchdog.py`, `heartbeat.py` |

Everything green is green. What follows is what the checks are not shaped to see.

## P0 — the tree cannot be deployed to the edge as it stands

The port made every route native, and the deploy tooling still describes the edge as
"session hashing and provider presence only". `scripts/deploy_edge.py` pushes two secrets
(`AI_PROXY_TOKEN`, `SESSION_SECRET`); `apps/web-rs/wrangler.jsonc` declares eight vars and no
`ai` binding. The code now reads more than that, and nothing checks the difference.

**1. The configuration surface is not provisioned.** Read by the edge crate but absent from
both its `wrangler.jsonc` and its deploy script:

| Kind | Name | What reads it | What happens when it is unset |
| --- | --- | --- | --- |
| secret | `ADMIN_TOKEN`, `SCHEDULER_TOKEN` | `admin::authorized` | Every `/api/admin/*` request is `403`. The per-minute `operate_risk_v2.py` and five-minute `sweep_trigger.py` on this Mac POST to the public domain; once the edge owns those routes they are refused, and the feed's 120-second expiry is exceeded within two ticks. |
| secret | `SOLANA_RELAYER_SEED` | `registry_chain`, publication | Registry delivery has no signer. |
| secret | `GEMINI_API_KEY` | `application::providers` | Only relevant without the relay; harmless today. |
| secret | `SOLANA_RPC_PROXY_TOKEN`, `SOLANA_MAINNET_RPC_KEYED`, `SOLANA_DEVNET_RPC_KEYED` | `registry_urls`, mainnet reads | Registry RPC resolves to an empty endpoint list. |
| var | `SOLANA_RELAYER` | `detail`, attestations, key id | `attestationAvailable: false`; the feed key id cannot be derived. |
| var | `GEMINI_MODEL`, `BILLING_SANDBOX_ENABLED`, `SOLANA_MAINNET_RPC`, `WALLET_LOGIN_REQUIRED` | providers, billing, mainnet reads, auth | See findings 2 and 3. |
| binding | `ai` (Workers AI) | `application::providers` | The `cloudflare` fallback provider does not exist at the edge. |

**2. Two variable names drifted during the port, and the semantics with them.** The reference
reads `SOLANA_RPC_URL` (pinned: anything but `https://api.devnet.solana.com` raises) and
`SOLANA_RPC_PROXY_URL`. The port reads `SOLANA_DEVNET_RPC` (a comma-separated failover list, no
pin) and `SOLANA_RPC_PROXY`. Neither name exists in either `wrangler.jsonc`. The SQL and route
parity gates cannot see this: an environment read is neither a statement nor a route.

**3. One switch is read two ways.** `WALLET_LOGIN_REQUIRED` is `!= "false"` at
`writes.rs:389` (register) and `== "true"` at `writes.rs:1104` (wallet migration); the reference
is `getattr(env, name, "true") == "true"` at both sites. Unset — which is what the edge
`wrangler.jsonc` is — the port refuses registration and **permits** the migration writes the
reference refuses. Two readings of one flag are a divergence waiting for the value that
separates them.

**4. `/api/status` reports a provider by its model name, not its binding.** `routes::status`
lists `cloudflare` whenever `CLOUDFLARE_AI_MODEL` is set; the reference lists it only when the
`AI` binding exists. The live status therefore already reports a fallback the edge cannot call.
Harmless while the pipeline runs in Python; a false statement once it does not.

**The change.** A third parity gate, alongside SQL and routes: `scripts/config_parity.py
--check` that reads every `var("…")` / `secret("…")` / `get_binding("…")` in the edge crate and
every `getattr(env, "…")` in the reference, fails on a name one side reads and the other does
not (with a reviewed-differences list, like `sql_parity.py`), and fails on a name the edge
reads that neither `wrangler.jsonc` nor `deploy_edge.py` provides. Then: one
`admin::switch(env, name, default)` helper so a flag has one reading, the missing
declarations, and `deploy_edge.py` pushing the full secret set. **Before that lands, the
secret boundary the criticality record leaves to its custodian has to be decided** — the port
has already crossed it in code (`ADMIN_TOKEN` is read by the edge), so the decision is now
whether to deploy, not whether to write it.

## P1 — the strangler's last step has no switch, and its evidence is sampled

**5. Flipping the writes is all-or-nothing.** `routes::owns_write` is a compile-time table.
The next `deploy_edge.py --take-domain` moves every POST and PATCH — auth, submissions,
markets, disputes, the operator surface, the two scheduler routes — in one deploy, and the
rollback is the previous wasm. The plan's rule ("every phase ships behind a route/process
switch with a rollback command written down") was honoured for reads via the preview Worker
and 39/43 byte comparisons; nothing equivalent exists for writes, because `route_parity.py`
proves *claims*, not *answers*. The smallest honest gate: deploy the preview Worker, and run
the write surface against it and against the Python Worker with a disposable session and a
disposable forecast, comparing status, error code and body per route — the read parity
harness, extended to methods with bodies.

**6. Retiring `LEGACY` is decided on 10% of the evidence.** `cf1172a` logs every request the
binding still answers, so retirement can be "observed rather than believed". The edge's
`observability.head_sampling_rate` is `0.1`: nine in ten invocations' logs are dropped before
they are written. A week of silence is a week at one-tenth sensitivity. Either raise the rate
to `1.0` for the retirement window, or count forwarded requests somewhere sampling cannot
reach (a D1 counter, or the analytics events table).

**7. The scheduler is ported; the schedule is not.** `apps/web-rs` has no `#[event(scheduled)]`;
`apps/web/wrangler.jsonc` still owns `*/5 * * * *`, dispatched through the Python Worker's
self-binding. After the writes flip, the five-minute sweep runs in Python while every operator
route runs in Rust: two codebases over the same job tables, with the Pyodide cold start the
port exists to remove still on the sweep. The criticality record's objection to moving the
tick — "a cron on the edge that calls `POST /api/admin/risk/v2/operate` needs `ADMIN_TOKEN`
there" — does not apply to a scheduled event: it is not an HTTP request and needs no bearer.
A `scheduled` handler that calls `application::sweep` and the v2 tick in-process needs no
token at all, takes dependency 1 (the per-minute trigger) off this Mac, and ends the
per-isolate collision that removed the per-minute cron in 0.12.10, because it is not Pyodide.
This is the single change that converts the port from "same behaviour, faster" into the
operational improvement it was started for.

## P2 — verification debt the checks report as green

**8. 20 `#[allow(dead_code)]` sites.** `eligibility.rs` (8), `projections.rs` (3),
`reputation.rs` (3), `scheduler.rs`, `writes.rs`, `points.rs` (2 each). Each is a ported
function with no caller, which means no route reaches it and no vector exercises it through
the surface. Inventory them; either the automation that was to call them has landed (then
remove the allowance and let the compiler prove the wiring) or it has not (then the port's
"complete" is complete minus these).

**9. The v2 arm of the scheduler is covered by reading only.** Recorded in `scheduler.rs` and
in the status notes: the fixture in which the early pipeline *succeeds* on a `Snapshot::V2`
does not exist, and a test that merely reaches the arm passes with the arm deleted. This stays
the one place where "documented as unverified" is the state.

**10. The goldens are Python's, and Python is scheduled for retirement.** All 48 vectors are
regenerated by `scripts/generate_*_golden.py` from the reference. Retiring the Python Worker
from production is fine; retiring the Python *package* freezes every golden at its last
regeneration. Decide it now, in writing: the reference stays in the tree as the generator
(dev-only, still under `check.py`) for as long as the vectors are the acceptance suite.

**11. CI runs the edge unlocked and on the wrong target.** The `registry` job uses
`--locked`; `edge` and `domain-rs` do not, although both lockfiles are committed, so a
resolver drift passes CI and fails `deploy_edge.py`. `edge` also lints on the host target,
while the deploy lints on `wasm32-unknown-unknown`; the `cfg(not(wasm32))` dev-dependency
boundary means a warning can exist on one and not the other. Add `--locked` to both jobs and
run clippy on the wasm target as the deploy does.

**12. `check.py` is 40 sequential subprocesses.** Each golden check re-imports the application
and rebuilds its fixture. The Python suite itself takes 87 s; the generator chain adds a
comparable amount serially. A single driver that imports each generator's `build()` once and
runs them in a process pool would halve the wall clock without touching a vector.

## P3 — shape and documents

**13. 21 of 69 edge files exceed 800 lines.** `writes.rs` (2,514) holds every non-admin write;
`scheduler.rs`, `dispute_wire.rs`, `registry_chain.rs`, `source_watch.rs`, `point_markets.rs`,
`automation.rs`, `risk_feed_v2.rs` are each over 1,600. The admin side already split by
cluster (`admin_markets`, `admin_ops`, `admin_registry`, `admin_risk`, `admin_risk_v1`);
`writes.rs` should follow the same seam — auth, submissions, comments/follows, disputes,
wallet, registry. Note `sql_parity.py`'s constraint: it truncates at the first
`#[cfg(test)]`, so each new file keeps its tests last.

**14. Documents describe the deployment before the port.** `docs/deployment.md` has no section
for `deploy_edge.py` or `apps/web-rs` — the Worker that serves production is absent from the
deployment record. `README.md` says "everything else is forwarded untouched … to the Python
Worker", true of what is deployed and false of what is committed. `docs/roadmap.md` is
headed "Version 0.11 · September 14" and `docs/blueprint.md` "Version 0.9 · September 10"
under a `0.13.0` service. Each should say which surface it describes, deployed or tree, and
the deployment record needs an edge section with the secret set from finding 1.

**15. 35 generator scripts share the same `main`.** The `--write` / `--check` / print tail is
copied per file, ≈ 20 lines × 35. A `scripts/lib/golden_cli.py` taking `(build, GOLDEN)`
removes the copies and gives finding 12 its driver.

**16. Small duplications in the edge crate.** `MAX_BODY_BYTES` is defined in both `lib.rs`
and `admin.rs`; `api_response` and `api_response_with` build the same header set twice
(`api_response` is `api_response_with(…, Cookies::None)`). Cosmetic, but each is a place two
values can drift.

## Suggested order

1. Decide the secret boundary (finding 1's last paragraph). Nothing below it can ship without
   that decision, and the decision is the custodian's.
2. `config_parity.py`, the switch helper, the declarations, the deploy script — findings 1–4.
   One commit, gated in `check.py` like the other two parity checks.
3. Write-surface parity against a preview deploy — finding 5. Then flip.
4. Sampling to 1.0 or a counter — finding 6 — *before* the flip, so the retirement clock
   starts with full evidence.
5. `#[event(scheduled)]` — finding 7. Move `*/5` and the per-minute tick to the edge; retire
   `operate_risk_v2.py` and `sweep_trigger.py` from this Mac once a 48-hour window shows the
   edge ran them unattended.
6. Dead-code inventory, CI `--locked` + wasm clippy, golden-generator driver, `writes.rs`
   split, document sync — findings 8, 11, 12, 13, 14, 15 — in any order, each its own commit.

## What was not examined

The Android shell, the browser modules beyond their test count, `forecast-risk` (a separate
repository), the Solana program, and the private handoff. No production write was made; the
probes were a health read and an origin-less logout that both Workers refuse by design.
