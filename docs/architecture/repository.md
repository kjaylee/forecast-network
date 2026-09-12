# Repository architecture

The product handoff (private) is authoritative.
This document records implementation choices; it does not expand the milestone scope.

## Dependency direction

```text
mobile / API / background jobs
              |
       application use cases
          /         \
    domain core    ports
                     |
            infrastructure adapters
       D1 / AI / future Solana / in-app activity
```

The domain core imports only Python's standard library. It owns invariants,
immutable value objects, lifecycle decisions, and auditable events. It never reads
a clock, generates an identifier, performs I/O, or calls a provider on behalf of a
transition. Callers supply those inputs explicitly.

## Implemented in Milestone 1

| Path | Responsibility |
| --- | --- |
| `packages/domain/src/forecast_domain/` | Domain records, validation, strict codecs, commitments, lifecycle |
| `schemas/v1/` | Generated, versioned JSON Schema contracts for non-Python consumers |
| `tests/` | Domain invariants, contract conformance, lifecycle and failure scenarios |
| `scripts/` | Contract generation and repository verification |
| `docs/architecture/` | Decisions, invariants, persistence and integration obligations |
| `.github/workflows/` | Automated verification |

Python 3.11+ permits frozen, slotted dataclasses, explicit enum types, and exhaustive
type checking with no runtime framework dependency. Versioned JSON is the language
boundary; mobile and Solana implementations need not share the Python runtime.

## Deployed web application

The user requested a Cloudflare web deployment after Milestone 1. The live beta
keeps the domain intact and advances the M3/M4/M5 responsibilities in one modular
Worker. [Web API contracts](web-api.md) describe the implemented transport and
[release verification](../verification-web.md) records which paths were tested.

| Path | Responsibility |
| --- | --- |
| `packages/application/src/forecast_application/` | Auth, transactional use cases, projections, AI routing, evidence, durable job processing |
| `apps/web/src/` | Worker HTTP transport, D1 adapter, provider transport, scheduled trigger |
| `apps/web/public/` | Mobile and desktop web UI, share-card rendering |
| `apps/web/migrations/` | Versioned D1 schema changes |
| `scripts/build_web.py`, `scripts/deploy_web.py` | Staging, document rendering and Keychain-based deployment |

D1 replaces the initially proposed PostgreSQL adapter for this deployment. Its
batch transaction includes a guard that fails the transaction on a stale revision;
the snapshot, receipt, events, projections and outbox cannot partially succeed.
The local SQLite adapter and deployed D1 adapter implement the same application
storage interface. Provider and source clients remain outside domain transitions.

The Worker owns scheduled execution; persisted leases, retry state and idempotent
outbox effects survive requests. In-app activity is the implemented notification
surface. Solana outbox entries remain awaiting an adapter. Native mobile apps and
separate infrastructure deployments are not prerequisites for the mobile web beta.

## Remaining milestone locations

These paths are reserved in this design, not generated as empty services or fake
implementations. Add each when its milestone introduces working code and tests.

| Path | Milestone | Boundary |
| --- | --- | --- |
| `programs/forecast_registry/` | 2 | Solana state and commitment verification, no economic asset layer |
| `apps/api/` | Later extraction if needed | The current transport is implemented in `apps/web/src/` |
| `packages/adapters/postgres/` | Optional scale change | Add only if D1 no longer satisfies measured storage requirements |
| `packages/adapters/solana/` | 2–3 | Signing, submission, indexer and commitment reconciliation |
| `apps/jobs/` | Later extraction if needed | Current orchestration is in the application and Worker scheduler |
| `packages/ai/` | Later extraction if needed | Current routing, provenance and structured outputs are in the application package |
| `apps/mobile/` | 5 | Mobile experience; separate crowd, expert, and AI signals |
| `packages/adapters/notifications/` | 5–6 | Additional delivery channels beyond the implemented in-app activity |
| `infra/` | 6 | Deployment, observability and operational policy |
| `simulations/` | 7 | Load and adversarial multi-service scenarios |

Begin with a modular application and durable jobs. Split deployment units only
when load, fault isolation, or ownership warrants it. The conceptual service list
in the handoff establishes responsibility boundaries, not a requirement to run
nine independently deployed services in the first milestone.

## Product constraints

There is no purchase, transfer, redemption, monetary balance or settlement,
tradable token, or prize API. Private participation-point balances and settlement
follow the versioned non-economic ledger contract. Forecast confidence is an
informational signal. Reputation records
measure quality, not economic value, and do not expose ownership transfer.

## Production persistence obligations

The pure domain engine cannot make storage durable or authenticate an actor.
The application boundary must verify caller authority, load and validate
the persisted aggregate, and transactionally save the resulting aggregate,
command receipt, audit events, and outbox messages. A compare-and-swap on aggregate
revision and a unique command key are mandatory. The same transaction must reject
conflicting command reuse. Retrying external delivery uses stable event identifiers.

Solana confirmation and indexer lag must not authorize an otherwise invalid
transition. Off-chain artifacts must be stored durably before their commitments
are submitted. Hashes prove integrity, not the truth of a claim, availability of a
source, or authority of a model or reviewer.
