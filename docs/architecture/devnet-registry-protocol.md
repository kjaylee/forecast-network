# Devnet forecast registry protocol v1

This native Solana program stores authority-attested application commitments. It enforces signed authority, exact account addresses, immutable publication identity, contiguous application revisions, event predecessor commitments, legal lifecycle transitions, and a chain-clock challenge delay. It does **not** verify the truth of news, independently inspect AI signatures, prove the completeness of disputes, or replace application authentication. A compromised relayer can attest fabricated content within these structural constraints. The separate cold administrator can rotate that hot relayer. The upgrade authority remains a separate Solana loader authority and can change program code; clients must expose that trust boundary.

No transferable tokens, purchases, redemption, monetary payouts, registry deletion or account-rent withdrawal exist. Lamports only fund transaction fees and rent. User identity is an application identity hash, not falsely presented as a wallet ownership proof.

## Addresses and authorities

- Config PDA: seeds `[b"config"]` under the deployed program ID.
- Forecast PDA: seeds `[b"forecast", forecast_id_hash_32]`.
- Instruction account order is exact; extra accounts fail.
- Initialize requires the signer compiled into `src/initial_admin.rs`. That file contains a public key only. The configured cold administrator is `BoByBfy1fA7nj85iUW8p3dGqbqyXKwnf8qzhGnG4CgLo`; the deployed Devnet program ID is `BvZLYrSmzDGTP5jYb14cjreRfHqfMo2sRBfUPpAi5Sgp` and initial hot relayer is `5GDUY5uyo1BVPHca6HJLYmZwbLyC5Ee78N7BkuWd5ULf`. These are public addresses, not private key material. No arbitrary caller can front-run initialization.
- Administrator and relayer must be nonzero and distinct. Only the administrator rotates the relayer. Administrative handover requires propose then acceptance by the proposed key; a relayer cannot become administrator through this path. The administrator may replace a pending proposal by proposing another distinct key.
- Program-owned accounts must have exact data lengths, matching magic bytes and PDA. Writes require writable accounts. Creation accepts an otherwise-empty system-owned PDA even if someone has prefunded it; it tops up rent, allocates and assigns via signed PDA CPI. Thus unsolicited lamports cannot prevent initialization.

## Encoding

All numbers are little-endian. Timestamps are signed i64 milliseconds since epoch in `0..9007199254740991`; revisions are u64 in `1..9007199254740991`. Hashes and public keys are raw 32 bytes, not base58/hex strings. Zero hashes mean absent only where documented. Instructions have exactly the specified length and reject trailing bytes.

States follow domain `LifecycleState` declaration order: DRAFT=0, VALIDATING=1, OPEN=2, LOCKED=3, RESOLVING=4, PROPOSED=5, CHALLENGE=6, DISPUTED=7, ESCALATED=8, PAUSED=9, FINALIZED=10, ARCHIVED=11. DRAFT and VALIDATING are never stored on-chain. Outcomes: NONE=0, YES=1, NO=2, INVALID=3. The outcome field contains the **proposal** from PROPOSED onward; it becomes final only when state is FINALIZED or ARCHIVED.

| Tag | Payload after one-byte tag | Accounts |
| --- | --- | --- |
| 0 Initialize | relayer pubkey32 | administrator signer+writable, config writable, executable system program |
| 1 Register | layout below, 192 bytes | relayer signer+writable, config readonly, forecast writable, executable system program |
| 2 Advance | layout below, 254 bytes | relayer signer, config readonly, forecast writable |
| 3 SetRelayer | new relayer pubkey32 | administrator signer, config writable |
| 4 ProposeAdmin | proposed administrator pubkey32 | administrator signer, config writable |
| 5 AcceptAdmin | empty | proposed administrator signer, config writable |

Register payload offsets, excluding instruction tag:

| Offset | Field | Bytes |
| --- | --- | --- |
| 0 | forecast identity hash | 32 |
| 32 | creator application identity hash | 32 |
| 64 | immutable specification hash | 32 |
| 96 | original openAtMs | 8 |
| 104 | original closeAtMs | 8 |
| 112 | published application revision | 8 |
| 120 | publication event occurredAtMs | 8 |
| 128 | publication event hash | 32 |
| 160 | publication snapshot hash | 32 |

Register always creates OPEN, never imports an already-finalized snapshot. The published revision may exceed 1 because draft/validation events are off-chain. The adapter must replay **every** subsequent event. Publication must precede original closeAt and not be ahead of chain time. Publication before openAt is permitted because the domain allows scheduling.

Advance payload offsets, excluding instruction tag:

| Offset | Field | Bytes |
| --- | --- | --- |
| 0 | application revision | 8 |
| 8 | application event occurredAtMs | 8 |
| 16 | previous event hash | 32 |
| 48 | new event hash | 32 |
| 80 | new snapshot hash | 32 |
| 112 | target state | 1 |
| 113 | proposal/final outcome | 1 |
| 114 | resolution hash | 32 |
| 146 | dispute/review collection commitment | 32 |
| 178 | reputation commitment | 32 |
| 210 | early positive trigger commitment | 32 |
| 242 | application challengeUntilMs, 0 if absent | 8 |
| 250 | pending dispute count | 2 |
| 252 | material dispute count | 2 |

Config account: 104 bytes: magic ASCII `FNCONF01` at 0, administrator32 at 8, relayer32 at 40, pending-administrator32 at 72 (zero means absent).

Forecast account: 360 bytes:

| Offset | Field | Bytes |
| --- | --- | --- |
| 0 | ASCII `FNFORE01` | 8 |
| 8 | forecast identity hash | 32 |
| 40 | creator identity hash | 32 |
| 72 | specification hash | 32 |
| 104 | original openAtMs | 8 |
| 112 | original closeAtMs | 8 |
| 120 | latest application revision | 8 |
| 128 | latest event occurredAtMs | 8 |
| 136 | state | 1 |
| 137 | proposed/final outcome | 1 |
| 138 | paused-from state, 0 unless paused | 1 |
| 139 | reserved, must be zero | 1 |
| 140 | latest event hash | 32 |
| 172 | latest snapshot hash | 32 |
| 204 | resolution hash | 32 |
| 236 | dispute/review collection commitment | 32 |
| 268 | reputation commitment | 32 |
| 300 | early positive trigger commitment | 32 |
| 332 | application challengeUntilMs | 8 |
| 340 | chainFinalizeNotBeforeMs | 8 |
| 348 | chain pause-start time, 0 unless paused | 8 |
| 356 | pending dispute count | 2 |
| 358 | material dispute count | 2 |

## Enforced transitions and time

OPEN → OPEN (forecast event) or LOCKED; LOCKED → RESOLVING; RESOLVING → PROPOSED; PROPOSED → CHALLENGE; CHALLENGE → DISPUTED or FINALIZED; DISPUTED → DISPUTED, CHALLENGE or ESCALATED; ESCALATED → PROPOSED; FINALIZED → ARCHIVED. RESOLVING/PROPOSED/CHALLENGE/DISPUTED/ESCALATED can pause, and PAUSED can only resume its recorded prior state. ARCHIVED is terminal.

Every advance requires exact previous revision + 1, exact predecessor event hash, new nonzero event/snapshot hashes, monotonically nondecreasing application event time, and application event time no later than `Clock.unix_timestamp * 1000`. The adapter should retry after the next second if application millisecond precision is briefly ahead of chain time. Historical replay is allowed; chain receipt time is separately authoritative for the challenge delay.

Identity, creator, specification, opening time and closing time never appear in an advance instruction and cannot change. An early OPEN → LOCKED event before original close requires a nonzero immutable positive-trigger commitment. Pre-close proposals must be YES. The program validates the commitment's presence; the application remains responsible for verifying its official evidence and independent semantic review. Ordinary locking occurs at/after original close.

Resolution/outcome can change only at RESOLVING → PROPOSED or ESCALATED → PROPOSED. Adjudication requires a new nonzero resolution hash and clears pending/material counts and the old application challenge deadline. It creates a fresh chain delay. Proposal and challenge receipt each establish at least **48 hours from chain time**; the later deadline and application deadline prevail. A delayed import therefore cannot make a historic challenge immediately finalizable. Pausing prevents finalization, and recovery extends the chain deadline by chain-observed pause duration. The application must independently extend its own challenge deadline.

CHALLENGE and terminal states require zero pending and material disputes; ESCALATED requires completed reviews and a positive material-conflict count. Pending plus material-conflict counts total at most 256, and nonzero counts require a dispute commitment. Finalization preserves proposal, dispute commitment and application deadline, requires both chain and application windows elapsed, and requires a nonzero reputation commitment. The adapter must hash the canonical per-forecast reputation collection, including an empty collection; absence cannot be encoded as an all-zero finalized hash. Before finalization the reputation field must be zero. Archive cannot change final outcome, resolution, disputes, reputation or deadline. There is no direct ESCALATED → FINALIZED shortcut.

A transaction retry after successful execution returns a stale-revision error; the adapter achieves idempotence by first reading the canonical account and comparing the intended revision/event/snapshot. It must reject a different hash at the same revision and never call that a successful retry. Account locks serialize competing submissions.

## Builds and verification

Pinned SDK: `solana-program = 2.2.0`, with all transitive versions locked in root `Cargo.lock`. The installed SBF platform-tools v1.51 uses Rust 1.84.1, so the package declares Rust 1.84 and the lock retains compatible `blake3 1.8.2`, `zeroize 1.8.2`, and `proc-macro-crate 3.3.0`; blindly updating them pulls edition-2024 dependencies the SBF compiler cannot parse. No Anchor dependency. Release overflow checks remain enabled. Unit and processor-entry guard tests cover exact decoding, snapshot semantic corruption, unauthorized callers, foreign-owned config, revision forks, forbidden transitions, early positive triggers, immutable outcomes, pending/material disputes, chain-clock delay, adjudication reset, pause extension and archive terminality. The 23-test native suite includes the actual processor storage path for a full lifecycle with a substituted chain-clock syscall, stale replay attempts, failed-write atomicity, bounded arbitrary-byte parsing, and overflow. Native tests do not replace actual SBF/local-validator and Devnet transaction verification.

Run all artifacts/caches under the repository:

```sh
TMPDIR="$PWD/tmp" CARGO_HOME="$PWD/tmp/cargo-home" CARGO_TARGET_DIR="$PWD/tmp/registry-target" cargo test --workspace --locked
```

The deployed binary, rent estimate, program-data authority and live transaction receipts belong in the release verification record. Never claim AI truth or proof of an exhaustive off-chain event history from a registry commitment alone.

Local build toolchain staging: the installed SDK contained dangling links and an empty `syscalls.txt`. Platform tools and Criterion were downloaded, relocated to repository `tmp/`, and only the staged SDK installer was redirected to a workspace cache. The staged binaries are at `tmp/registry-platform-tools`; do not override `HOME` or mutate global Rust configuration. The SBF build's standard-syscall warnings arise from the empty shipped listing and still require runtime validation, rather than suppressing evidence. The builder generates an unused keypair unless one exists; remove that generated temporary key and use the independently Keychain-managed deployment program key.

```sh
TMPDIR="$PWD/tmp" CARGO_HOME="$PWD/tmp/cargo-home" CARGO_TARGET_DIR="$PWD/tmp/registry-target" \
RUSTC="$PWD/tmp/registry-platform-tools/rust/bin/rustc" \
PATH="$PWD/tmp/registry-platform-tools/rust/bin:$PATH" \
cargo-build-sbf --manifest-path programs/forecast_registry/Cargo.toml \
  --sbf-sdk "$PWD/tmp/registry-sbf-sdk" --sbf-out-dir "$PWD/tmp/registry-deploy" \
  --no-rustup-override --skip-tools-install --optimize-size -- --locked
```
