# Milestone 1 verification

The repository follows the product handoff (private). The implementation is a
production domain foundation; live adapters remain in Milestones 2–7.

## Delivered

- Seven required immutable domain models, supporting evidence/provenance/review records.
- Twelve lifecycle states (including required PAUSED) and eighteen typed commands.
- Versioned strict codecs, canonical commitments and forty generated standalone schemas.
- Shared structural contracts, optimistic revisions, durable-receipt interface and audit events.
- Repository architecture, lifecycle/persistence obligations, domain contract documentation,
  source-layout package metadata, root verification commands and CI matrix.

## Executed verification

| Check | Result |
| --- | --- |
| Python 3.11.2 full suite | 76 tests passed |
| Python 3.14 optimized mode (`-O`) full suite | 76 tests passed |
| Ruff 0.15.8 | Passed |
| mypy 1.19.1 strict domain checking | 7 source files passed |
| Generated schema drift | 40 schemas matched |
| Installed jsonschema 4.26.0 Draft 2020-12 validation | All 40 schemas valid; 34 record fixtures valid |
| Strict JSON fixture round trips | All 34 record fixtures passed |
| Independent Node 22 commitment implementation | All 5 portable golden vectors matched |
| Offline package build and isolated wheel import | Passed; wheel bytes match current source, seven public models import |
| Independent invariant review | No outstanding material findings after corrections |

The lifecycle suite exercises every forbidden state/command pair as well as all
legal branches, time boundaries, provider failure/recovery, explicit invalid-evidence
dismissal, changed-proposal rechallenge, stale writes, concurrent-finalization
contracts and exact retries. The constructor tests also reject inconsistent
snapshots whose public integrity hashes have been recomputed.

## Review corrections

- Bind AI outputs to exact result fields and prevent contradictory outcome matches.
- Provide explicitly attested invalid-evidence dismissal to avoid dispute deadlock.
- Require complete reviews in ESCALATED snapshots and reject newline identifier aliases.
- Validate feasible residual reputation counts across domain subsets.
- Bind retry receipt submissions exactly to the original command and preserve current state.

## Practical limits

Authorization, trusted clocks/provider identity registries, evidence retention,
transactional compare-and-swap/outbox persistence, real provider/RPC failure handling,
Solana verification and notification delivery require the later adapters. CI is
configured for Python 3.11–3.14; only 3.11 and 3.14 were run locally. No deployment,
remote CI run or external integration is claimed.
