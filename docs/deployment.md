# Cloudflare deployment and operations

The live public hostname is forecast.eastsea.xyz. The initial verification host
is forecast-network.k-jaylee.workers.dev. Both were verified on 2026-09-09; see
[release verification](verification-web.md) for the tested scope and remaining limits.

## Components

- Cloudflare Python Worker with static assets. Python Workers is an open-beta
  platform dependency; the existing domain package is staged without rewriting it.
- Dedicated D1 database forecast-network-enam (region ENAM), isolated from the user's
  other databases. The original forecast-network database (APAC) was deleted on
  2026-09-12 after a final content comparison; its pre-cutover export is archived
  off-site by the operator (sha256 ac613a28…6036e8). Originally,
  every D1 query from us-east4 to it cost a cross-Pacific round trip (~250 ms), which
  multiplied by sequential reads produced multi-second API responses. D1 regions are
  fixed at creation, so the database was recreated in ENAM on 2026-09-12 and loaded
  from a full export (schema, data in foreign-key order, then indexes/views/triggers;
  artifact rows above the 100 KB statement limit were inserted with bound parameters).
  All 72 tables matched by row count and content digest before cutover.
- Gemini compiler and Cloudflare Workers AI independent review.
- Five-minute scheduled sweeps for expiry, review, challenge completion and outbox.
- No placement hint on the main Worker since 2026-09-12: it executes at the edge next
  to the user. Gemini rejects some default execution locations, so Gemini requests are
  relayed through `forecast-ai-proxy` (`apps/ai-proxy`), a separate JavaScript Worker
  pinned to gcp:us-east4 that accepts only authenticated POSTs for the Gemini host and
  forwards only the content type and API key headers. The main Worker holds
  `AI_PROXY_URL` (var) and `AI_PROXY_TOKEN` (secret); the relay holds `PROXY_TOKEN`.
  Both come from the same Keychain entry through the deploy script.
  `GET /api/admin/ai/health` proves the relay path with one tiny Gemini call.
- D1 read replication is enabled (`read_replication.mode=auto`). Every request opens a
  Sessions API session: browser GETs read from the nearest replica at or after the
  bookmark carried in the `__Host-forecast_d1` cookie (set whenever a request advances
  it, so a browser always sees its own writes); non-GET and operator requests use
  `first-primary`. Scheduled events dispatch through SCHEDULED_JOBS, a self service
  binding to the authenticated sweep fetch handler.
- Public blueprint, roadmap, privacy, terms and source research documents.

Credentials come from macOS Keychain services named in the operator-local file
`~/.config/forecast-network/keychain.json` (override with `FORECAST_KEYCHAIN_CONFIG`),
which maps `cloudflare` (a scoped API token: Workers Scripts, D1, Routes, Observability,
read-only account and user details — never a Global API Key), `GEMINI_API_KEY`,
`SESSION_SECRET`, `ADMIN_TOKEN` and `AI_PROXY_TOKEN` to Keychain service names and
carries the Cloudflare `account_id`; `scripts/cloudflare_keychain.py` reads it.
The main Worker holds no Gemini credential: the relay Worker alone does, and the
daily editorial seed uses the same dedicated, API-restricted key from a GitHub
Actions secret. Rotation touches three places per key (Keychain, `deploy_web.py`,
`gh secret set`). The Cloudflare global key remains in deployment
tooling only. It is not a Worker secret or browser asset. Runtime secrets are
GEMINI_API_KEY, SESSION_SECRET and ADMIN_TOKEN, plus SOLANA_RELAYER_SEED when the
Devnet registry is enabled. Cold Solana signing roles are never Worker secrets.
Z.ai was not enabled after its
inference endpoint reported insufficient balance; no recharge was performed.
OpenAI credentials were not deployed after an authentication failure.

The deploy script applies migrations to whichever database wrangler.jsonc binds and
deploys the relay Worker before the main Worker.

The relay's region is an execution hint, not a guarantee of residency. Keep the documented
provider-region restrictions in mind before changing it, and verify
`/api/admin/ai/health` and scheduled dispatch after any placement change. Static assets remain at the edge.
See [Cloudflare placement](https://developers.cloudflare.com/workers/configuration/placement/).

## Build and checks

Use Python 3.11+, Node 22+, the pinned tooling declarations in apps/web, and
markdown-it-py 4.0.0 for public document generation.

    python3 scripts/check.py --tools
    node --test apps/web/tests/*.mjs
    ruff check apps/web/src
    python3 scripts/build_web.py --preview

The build writes to tmp/web-build, keeps the domain/application source under
python_modules, and renders Markdown with raw HTML disabled. The entrypoint is
under src to prevent Wrangler from discovering virtual environments and public
documents as extra Python modules.

Pinned deployment tooling:

- Wrangler 4.130.0; its development-only Sharp dependency is overridden to 0.35.4
  to remove the upstream image-decoder advisory. Package-lock audit passed.
- workers-py 1.17.2, including the external runtime SDK.
- uv 0.12.3 or newer; the preinstalled uv 0.11.2 is too old for this SDK.

Install tooling into tmp/cloudflare-tools and tmp/uv-tool. Copy apps/web/package.json
and package-lock to the former and run npm ci. Install the uv wheel into the latter
using uv pip install --target tmp/uv-tool uv==0.12.3. Point UV_CACHE_DIR,
UV_PYTHON_INSTALL_DIR, UV_PROJECT_ENVIRONMENT, npm cache and TMPDIR under the
repository's absolute tmp path. Do not change system Python or global CLIs.

    python3 scripts/deploy_web.py --preview --dry-run
    python3 scripts/deploy_web.py --preview
    # After the workers.dev deployment passes verification:
    python3 scripts/deploy_web.py

Deployment verifies Cloudflare authentication, applies versioned D1 migrations,
deploys the app, and sends runtime secrets from Keychain through stdin. New production
migrations require review; never modify an already-applied initial migration to
upgrade an existing database.

Local testing may use an owner-readable tmp/web-build/.dev.vars populated from
Keychain. The deployment script removes it before uploading. Never commit or print
its values. Use a dedicated free port; do not stop another project's server on 8787.

## The edge Worker — what serves the domain

Both Workers reach the Gemini relay through the `AI_RELAY` service binding, never by its
`workers.dev` URL: a fetch to another Worker's `workers.dev` hostname on the same account is
answered 404 before that Worker runs (observed 2026-09-21 by tailing the relay). The URL in
`AI_PROXY_URL` is only the binding's fallback. `GET /api/admin/ai/health` proves the path.


`apps/web-rs` is the Worker on `forecast.eastsea.xyz`. It is built with
`worker-build` (rustup toolchain; Homebrew cargo lacks the `wasm32` target) and
deployed by:

    python3 scripts/deploy_edge.py --preview       # forecast-network-edge-preview, workers.dev only
    python3 scripts/deploy_edge.py --take-domain   # the custom domain

The script runs fmt, clippy on wasm32 and `check.py` first (unless `--skip-checks`),
renders the same assets the Python build renders, deploys, then pushes the secret
set from Keychain. That set is data in `scripts/worker_secrets.py` and is the same
set the Python Worker is deployed with; `scripts/config_parity.py --check` (in
`check.py`) fails when the crate reads a var, secret or binding that the edge's
`wrangler.jsonc` or that list does not provide, or that the reference reads under
another name. The edge's `wrangler.jsonc` declares both crons; the Python Worker's
declares none since 2026-09-21 (`wrangler triggers deploy` from the stage removes
a trigger without a redeploy). `head_sampling_rate` stays 1.0 while the `LEGACY`
binding exists, and the deploy script asserts it.

Before a surface moves, compare answers, not claims:

    python3 scripts/deploy_edge.py --preview --skip-checks
    python3 scripts/http_parity.py --edge https://forecast-network-edge-preview.k-jaylee.workers.dev \
                                   --reference https://forecast-network.k-jaylee.workers.dev

Rollback is a redeploy of the previous edge commit with `--take-domain`; the
Python Worker keeps serving through the binding for whatever that edge does not
claim. The operator host's launchd jobs for the tick and the sweep
(`com.forecast-network.risk-v2-operator`, `com.forecast-network.sweep-trigger`)
were booted out on 2026-09-21 and their plists kept; `launchctl bootstrap` restores
them if the edge's schedule has to be taken off. The plists carry `RunAtLoad`, so a
reboot of the host restores them too — which happened on 2026-09-22. They are left
running on purpose until the scheduled tick fits the plan's CPU limit (see
`docs/plans/2026-09-22-todo.md`); an HTTP tick is allowed more CPU than a
scheduled one on the Free plan, so today the host's tick is the one that publishes
more often. Retiring them for good means moving the plists out of
`~/Library/LaunchAgents/`, not only booting them out.

The GitHub `Feed fallback` workflow, which POSTed an operation tick from a runner
once the public feed was stale, was removed on 2026-09-22. It existed because
nothing but the operator's Mac drove the feed; the edge's own `* * * * *` schedule
now retries the tick every minute in-process, so a runner retrying the same POST
against the same failure (a D1 daily read limit, a CPU limit) added a failing run
every five minutes and no publication. Rescue is the schedule's; observation stays
with the `Watchdog` workflow and `scripts/monitor_risk_pipeline.py`.

## Operator actions

Operator routes require a Bearer ADMIN_TOKEN and are not public account privileges.
Pass credentials through the Keychain helper, never command-line arguments, source,
browser storage or public docs.

- POST /api/admin/seed runs normal AI compiler/publication checks. It creates no
  invented user forecasts and is idempotent by question.
- POST /api/admin/sweep runs a bounded due-work sweep.
- POST /api/admin/forecasts/:id/translations/en saves a reviewed English display
  translation bound to the immutable specification hash. The repeatable editorial
  payload is apps/web/content/editorial-translations.en.json. Verify it with
  scripts/publish_editorial_translations.py; pass --apply to publish after migration.
- POST /api/admin/forecasts/:id/adjudicate takes revision, idempotencyKey,
  strict domain resolution, adjudicator, and optionally retained artifacts.
  Evidence must exist with matching hashes, and the domain enforces independent
  adjudication. Submit only real reviewed artifacts. Success is PROPOSED, followed
  by a new 48-hour challenge. This endpoint cannot finalize directly.

Initial judge disagreement remains visibly RESOLVING with backoff. Material
reviewed disputes remain ESCALATED pending operator adjudication. Provider-wide
failure pauses processing and preserves remaining challenge opportunity.
Outbox consumers deliver in-app activity and idempotent reputation scoring.
Migration 0004 adds profile/wallet participation grants, available and committed
balances, an immutable ledger and settlement in the same reputation outbox batch.
Existing accounts receive their eligible grants; historical forecasts remain
practice. Apply the migration with the pinned Wrangler version after verifying it
locally. Its nested trigger `CASE`/`END` whitespace is deliberate for Wrangler's SQL
splitter; see [points verification](verification-points.md).
Earlier releases left Solana effects in `awaiting_adapter`. Release `0.9.0` adds
the Devnet registry adapter and durable delivery records; only verified finalized
RPC observations may mark a particular revision confirmed. All three current
public revisions were confirmed through a one-time Mac operator run; hosted
automatic delivery was blocked by RPC access at that time and resumed once the
authenticated RPC proxy was deployed. See
[Devnet verification](verification-devnet.md).

AI limits are 240 workflows/day globally and 10/day per user. These are workflows,
not raw calls or a dollar budget. Additional request limits protect registration,
login, profiles, creation, comments and forecasts. Source collection uses a bounded
registry of authoritative hosts. Unsupported sources fail closed.

## Release verification

API 0.11 applies migration0016 for wallet authentication. Keep
`WALLET_LOGIN_REQUIRED=true`; new guest registration and old optional link
mutations are disabled. Build now runs the pinned mobile SDK bundler, installs
with `--ignore-scripts --omit=peer`, and publishes its exact CSP style hashes.
See [wallet authentication verification](verification-wallet-login.md) for
session races, migration, actual local signatures and mobile approval limits.

1. Health/status work over HTTPS with D1 and both providers configured.
2. Genuine AI compilation produces a deterministically correct UTC deadline.
3. Registration, session, recovery, logout and CSRF rejection work.
4. Forecast/comment/exact retry/stale revision work in the real runtime.
5. Desktop/mobile views, PNG sharing and deep links work.
6. Blueprint/roadmap distinguish deployment from research and Devnet plans.
7. Upload modules/public assets contain no credentials or local virtualenv files.
8. Custom domain resolves to this Worker with a valid certificate.
9. Existing point grants reconcile against the ledger; new signup and verified-wallet
   awards are one-time. Verify reservations and all settlement outcomes in isolated
   local D1, including a repeated scheduled dispatch with no duplicate effects.

Local QA data is isolated. Do not report test participation as public adoption.
Live seed questions are editorial content from genuine compiler output, with an
initially empty crowd.

## Content translation — 0.7.0

Apply migration `0005_display_translations.sql` before publishing the new Worker.
The existing Gemini/Workers AI configuration supplies translation and review; no
additional credential or dependency is required. Public GET reads the translation
cache; same-origin POST generates a source-bound copy. Verify all three non-English
targets, cache replay, Original restoration and unchanged specification hashes.
See [translation architecture](architecture/display-translations.md).

## Autonomous runtime — 0.8.0

Apply migrations0007–0010 before this Worker. The deployment enables official
source watching and nonbillable administrative billing tests. `LIVE_MARKETS_ENABLED`
stays false, so only explicitly funded shadow markets accept test-point orders.
No treasury is funded by migration. Operators use the documented authenticated
market endpoints to reserve a capped shadow budget and attach previews to open
questions; existing v1 point promises remain unchanged.

The existing five-minute scheduled sweep now runs source observation and lifecycle
advancement together. Preserve `SCHEDULED_JOBS`, `self.env` binding semantics and
GCP US-East placement. New source watches bootstrap from supported published Apple/
Microsoft specifications; pinned hold evidence is observed directly. Two distinct
providers must agree on an early positive event; errors leave held/queued records.

See [runtime architecture](architecture/autonomous-runtime.md) and
[verification](verification-autonomous-runtime.md). A52-schema check must preserve
all original40 schema bytes; do not decode v2 snapshots as v1-only Forecast records.

## Devnet registry and eligibility containment — 0.9.0

The native registry is deployed at
`BvZLYrSmzDGTP5jYb14cjreRfHqfMo2sRBfUPpAi5Sgp` on Solana Devnet. Its exact
101,008-byte binary was verified at finalized commitment. Public identities and
the pinned cluster are in [the deployment manifest](../infra/solana/devnet.json);
the [protocol](architecture/devnet-registry-protocol.md) documents encoding,
authority, transitions and the independent 48-hour chain delay. This is an
upgradeable, authority-attested registry, not independent verification of AI truth.

Keep all four seeds in separate macOS Keychain entries through
[solana_keychain.py](../scripts/solana_keychain.py):

| Role | Keychain service | Runtime use |
| --- | --- | --- |
| Relayer | `forecast-network-devnet-relayer-seed-v1` | Pays Devnet rent/fees and signs registry updates; provisioned as `SOLANA_RELAYER_SEED` |
| Upgrade / administrator | `forecast-network-devnet-upgrade-seed-v1` | Cold local authority; may upgrade code and rotate the relayer |
| Program | `forecast-network-devnet-program-seed-v1` | Stable program identity for deployment |
| Buffer | `forecast-network-devnet-buffer-seed-v1` | Stable identity for resumable uploads |

`python3 scripts/solana_keychain.py inventory` reports public identities only.
The helper reads and writes through Security.framework without putting seed values
in command arguments. When deployment tools require keypair files, they are
temporary owner-only files under repository `tmp/` and are removed by the context
manager. Keychain persistence does not establish an off-device recovery backup.

Apply new migrations `0011_eligibility_containment.sql` and `0012_registry.sql`
before publishing the corresponding Worker. Never edit the already-applied
source-watch or point migrations. Migration 0011 protects ordinary expiry,
finalization, reputation and payouts from unresolved known-result timing findings.
Legacy intake also rejects unhealthy bound watches and unresolved reviews.
Releasing a participation notice cannot erase an immutable timing finding.

The Worker uses `SOLANA_REGISTRY_ENABLED`, `SOLANA_RPC_URL`, `SOLANA_PROGRAM_ID`
and `SOLANA_RELAYER`; the current transport restricts requests to the approved
Devnet RPC endpoints and verifies cluster identity. The deployment helper checks that the hot Keychain identity
matches configuration before sending its seed through Workers Secrets. Only the
administrator can request registry enrollment with
`POST /api/admin/registry/run` and a `forecastId`; the same route without an ID
retries bounded delivery. Enrollment is not confirmation. Check the forecast's
integrity response against finalized account revision, event and snapshot hashes.

Current blocker: the official public Devnet RPC returned HTTP 403 from Cloudflare,
and the tested OnFinality alternative also failed from that runtime. No working
authenticated RPC credential was found during the release audit. Obtain a
supported provider endpoint, keep any credential in Keychain and Workers Secrets,
then verify its real Worker requests before claiming automatic delivery. Do not
place credential-bearing RPC URLs in public configuration or logs.

The one-time [Mac synchronization command](../scripts/sync_devnet_registry.py)
confirmed the current histories of all three public forecasts through the same
durable adapter. It is an operator recovery/bootstrap tool, not a background
replacement for Cloudflare scheduled execution. Its success cannot prove that
subsequent revisions will be delivered automatically.

Release configuration keeps `SOLANA_REGISTRY_ENABLED=true` for history reads and
durable intents and sets **`SOLANA_REGISTRY_RELAY_ENABLED=true`**, with
`SOLANA_RPC_PROXY_URL` pointing at the authenticated Devnet RPC proxy
(`https://forecast-rpc.eastsea.xyz/rpc`), which rejects unauthenticated requests
and runs under launchd alongside its tunnel. `SOLANA_RPC_URL` remains the official
`https://api.devnet.solana.com` default; the failed alternate public endpoint is not
promoted. Do not treat confirmed historical revisions as evidence of continuous
future delivery — the 48-hour finalization interval is still unobserved.

The authenticated `POST /api/admin/registry/run` remains available for explicit
operator probes. `GET /api/admin/registry/health` checks the hot signing identity
using a fixed message that cannot authorize a transaction and reports
`rpcAvailable` separately. The deployed diagnostic returned `signerVerified=true`
and `rpcAvailable=false` on 2026-09-10: the actual Worker successfully loaded and
used its Keychain-provisioned signing identity, while hosted RPC access remains
unavailable. A working signer does not establish RPC access.

Before reporting a release complete, verify the deployed binary and authorities,
real signature rejection, stale replay, exact account storage, public-record
correspondence and pending/finalized UI distinctions. An imported historical
challenge may acquire a later chain deadline. Never backdate the chain clock or
shorten its challenge window to make a test finish. The real 48-hour finalization
interval has not yet been observed for this release.

The current rollout used 2 Devnet SOL from an existing local wallet and spent no
mainnet SOL. It does not activate purchasable points, economic payouts, live point
markets or billing. Recovery drills, stronger Sybil admission and actual native
MWA wallet verification remain separate gates. See
[storage and recovery boundaries](architecture/storage-and-trust.md).
