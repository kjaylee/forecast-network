# Authenticated off-device backup and recovery v1

This operational tool backs up the authorized D1 SQL snapshot and the four existing **public Forecast Devnet** signing roles. It does not change the application runtime, restore production, rotate keys, transfer assets, copy SSH identities, or back up Cloudflare environment/API credentials, mainnet keys, or risk-project keys.

## Format and trust boundary

`scripts/backup_recovery.py` uses the existing local and NAS `cryptography` packages. No dependency was installed. A fresh RSA-3072 wrapping key is generated on `poc-nas`; its private PKCS#8 file stays only in the NAS recovery-key directory. Only the public PEM and SHA-256 fingerprint of its DER SubjectPublicKeyInfo return to the Mac. Existing pinned SSH host authentication protects enrollment and transfer.

Each archive contains an in-memory ZIP with exactly two paths:

1. `d1.sql`: the unchanged source SQL export.
2. `devnet-roles.json`: the four 32-byte Devnet seeds and their fixed network scope, assembled and encrypted in process memory.

The ZIP is encrypted using a fresh random AES-256-GCM key and 96-bit nonce. RSA-OAEP with SHA-256/MGF1-SHA-256 and label `forecast-network:backup-wrap:v1` wraps the AES key. GCM authenticates the entire canonical JSON header: format, wrapping-key fingerprint, wrapped key, nonce, scope, backup ID, public role keys, and the manifest containing path count, byte counts and SHA-256 hashes. Decryption authenticates the envelope before ZIP parsing or database restore. Only the exact two allowlisted members are accepted; duplicate/extra paths and oversized unpacked data fail closed.

This protects a **stolen archive without the NAS wrapping key**. It is not an air gap, a separate custodian, threshold custody, or protection against compromise of the NAS account/root, which can access both the archive and wrapping key. The isolated restored SQLite database contains the snapshot's private application data, protected by owner-only filesystem access; the source D1 export also remains private on the Mac. Process-memory copies are not claimed to be securely erased or protected against a compromised operating system.

## Filesystem and transport

New, never-overwritten directories are created under:

- `/home/spritz/AI/forecast-network/backups/<backup-id>/`
- `/home/spritz/AI/forecast-network/recovery-keys/<backup-id>/`

All directories are explicitly chmod-verified as 0700. Files are explicitly fchmod-verified as 0600 **before writing sensitive bytes**. This is required because this NAS's inherited ACLs override requested mkdir/open modes. Existing unrelated directories, archives and keys are not chmodded, replaced or removed. The first failed setup's newly owned paths were corrected and its wrapping key is excluded from the successful recovery record.

The Mac first attempts only `/usr/bin/rsync`, with pinned SSH host checking. The NAS's vendor rsync rejects the canonical `/home/spritz/AI/...` destination. The fallback therefore streams **only encrypted archive bytes** through pinned SSH into an exclusive owner-only partial file. It checks exact length and SHA-256, creates the final file without overwriting an existing target, then removes the verified partial. It does not alter the NAS rsync service or redirect the archive into a different volume/path.

Private seeds are never serialized to a local or remote plaintext keypair file or command argument. The dedicated NAS wrapping-key file is the explicit exception: it is the off-device private recovery key, under 0700/0600 protection. No operational Devnet keys are newly generated or rotated.

## The wrapping key is a single point, and splitting fixes it

Everything above protects the archive. None of it protects the key that opens
it: the RSA-3072 private half lives on the NAS and nowhere else, so if the NAS
dies, its disks are lost or it is stolen, the archives survive and nobody can
open them. A second copy in the same house does not help — one fire takes both.

`scripts/recovery_key_shares.py` splits that key with a Shamir threshold over
GF(256): any K of N shares reconstruct it, any K-1 reveal nothing, and the
shares can be kept in different places, with different people, on different
media. The arithmetic is implemented in about fifty lines with no dependencies,
because it is worth being able to read the whole of something guarding the only
key.

```sh
# On the host that holds the key, so it never moves.
python3 scripts/recovery_key_shares.py split --key wrapping-key.pem \
    --shares 5 --threshold 3 --out ~/recovery-shares
python3 scripts/recovery_key_shares.py combine --share share-1-of-5.json \
    --share share-3-of-5.json --share share-5-of-5.json --out rebuilt.pem
```

Every share carries the fingerprint of the key it came from, so `combine`
refuses a set that is mixed, short or tampered rather than silently producing a
wrong key. Verify a reconstruction against the original with `cmp`, then destroy
the intermediate.

**A share set that all sits in one directory has not been split.** The point is
that no single location holds enough shares to reconstruct, so distribution is
the part that matters and it cannot be done by a script.

## Recovery proof

On NAS, the tool verifies ciphertext hash, public-key pin, authenticated manifest, member paths, byte counts and hashes. It decrypts the Devnet seeds in memory and derives each public key. Relayer, upgrade and program keys must match `infra/solana/devnet.json`. That deployment manifest has no buffer field, so the buffer pin is explicitly the public key obtained from the existing `forecast-network-devnet-buffer-seed-v1` Keychain item, not a claimed on-chain manifest binding.

Each recovered key signs:

```text
forecast-network:off-device-recovery-proof:v1\n<backup-id>\n<32 random challenge bytes>
```

Only public keys, signatures and the public nonce return. The Mac verifies all four signatures against its pinned source public keys. Role duplication, wrong public keys, a different backup ID or a different challenge fails verification. This proves possession of the recovered existing keys on the NAS; it does not perform an on-chain transaction or claim a successful authority rotation.

The SQL is restored into a **new isolated** `backups/<id>/tmp/restore-<challenge-hash>/restore.sqlite`. The SQLite authorizer rejects ATTACH/DETACH, extension/file functions and unrelated PRAGMA commands. After restore, integrity, foreign keys, all table counts and forecast count are checked. When a reference proof is supplied, every table count and source SQL hash must match that earlier isolated local restore. A separate read-only NAS verification inspects the retained database/hash/modes and independently verifies all four Ed25519 signatures without reading any operational or wrapping private key.

## Verified run

Successful archive: `forecast-20260915T000652Z-127b62155a3b`.

- Source export: **19,335,458 bytes**, SHA-256 `556aae93a6fb7302d2f41162a60143a71f6d110424198399bb9616540886758f`.
- Encrypted archive: **4,338,622 bytes**, SHA-256 `e60f9eef425120c291ad6589578cef64316c8691087ad96d85e46cdf32084df9`.
- NAS restore: **75 tables, 7 forecasts, integrity ok, zero foreign-key errors**, exact table-count match to the local reference restore.
- **All four recovered signatures verified locally and independently.** Directory/file modes checked as 0700/0600.
- NAS preflight: about **699 GiB free**, load average **0.20/0.38/0.40**. Only a sequential small archive/SQLite verification workload ran there.

Evidence: `docs/evidence/completion/F20/off-device-recovery.json`, `off-device-independent-check.json`, and `off-device-initial-attempt.json`. The initial failed ACL/rsync attempt remains explicitly excluded from successful evidence, with no archive transferred to the NAS during that attempt.

Run a new authorized export/reference through:

```sh
TMPDIR=/Volumes/workspace/forecast/tmp PYTHONDONTWRITEBYTECODE=1 \
python3 scripts/backup_recovery.py run \
  --snapshot tmp/complete-capabilities-backup/before-0.12.sql \
  --reference-proof docs/evidence/completion/F20/isolated-restore.json
```

Every invocation creates a new backup ID and wrapping key. Never rerun blindly after an unresolved operation: inspect retained stage/remote paths first. No automatic retention deletion exists. Keep the public pin with backup inventory, and restrict the NAS recovery-key directory independently of any broader archive distribution.

## Remaining F20 work

This is actual off-device backup/restore and Devnet key-possession evidence. It does **not** complete retention/privacy-tombstone recovery policy, a key-rotation/rollback drill, scheduled backup monitoring, or production restore authorization. Those gates remain with the broader operations work. None are inferred from successful archive encryption, unit tests or this isolated restore.

## Restore privacy guard and actual deletion gap

Source inspection for this follow-up found **no automatic account-deletion or private-data erasure workflow**. `docs/privacy.md` says so explicitly. Existing behavior comprises logout/session revocation and cleanup of expired sessions/rate limits; `wallet_identities.status='tombstone'` prevents reuse of retired wallet identities. That wallet rule is not a general privacy-erasure registry. Immutable wallet/audit triggers intentionally prevent silent identity rewrites.

Every new `restore_sql` workspace now receives an owner-only `RESTORE_QUARANTINE.json` **before SQL is loaded**, and returns `activationAllowed=false`. Missing a latest independently obtained tombstone registry leaves that status unchanged. There is no promotion-to-production action in this tooling.

`apply_restore_tombstones` / the `apply-tombstones` CLI consumes a separate Ed25519-signed registry and a separately supplied current-head anchor. Its domain-separated purpose is `restore-privacy-tombstones`; it binds sequence, issuance/expiry, and the complete tombstone set. The anchor supplies the exact head SHA-256, sequence, observation time and explicitly configured maximum age. Signature failure, stale/future data, head mismatch, rollback, same-sequence fork, duplicate entries, and omission of a previously applied marker fail closed. CLI registry/anchor files may not reside inside the backup storage tree. A registry stored in the old archive is never proof of the latest deletion state.

Supported **isolated restore** effects are:

- `session-revoked`: revoke the specified session and its active login context; prevent that old session hash being inserted again.
- `wallet-retired`: quarantine users associated with that wallet, revoke restored sessions/contexts/challenges, and prevent new active wallet/login records for that address.
- `account-erasure-requested`: quarantine that subject, revoke authentication, and block new sessions/challenges. **This does not erase all private payload or claim the absent account-deletion flow has been implemented.**

Markers and the latest applied head are persisted in `_recovery_*` tables in the isolated copy. Replay is append-only and idempotent; immutable source wallet identity/audit records are not bypassed or rewritten. Even after a valid replay, `privatePayloadErasureComplete=false` and `activationAllowed=false`: application-level erasure/read filtering and an authorized current registry producer are still required before serving restored private data. The implementation is a real restoration guard, not a new policy document claiming those missing features exist. Tests use the actual migrated SQLite schema and verify session removal plus database-level reinsertion guards.

Example invocation once root has provisioned a real independent registry and its trusted public key:

```sh
python3 scripts/backup_recovery.py apply-tombstones \
  --restore-directory /absolute/isolated/restore-directory \
  --tombstone-registry /independent/current-registry.json \
  --registry-anchor /independent/current-head-anchor.json \
  --registry-public-key /independent/registry-public-key-base64.txt
```

The placeholder paths above are operator-supplied configuration, not existing deployed integrations. Omitting registry inputs yields an explicit quarantine result. No registry signing key or fake “no deletions” head was created for production.

## Explicit retention, inventory and non-destructive planning

No archive-retention duration was found in the existing policy. The existing 30-day **live session** duration is not silently reused as a backup lifetime. `backup_schedule.py template` therefore leaves all schedule/retention durations unconfigured and disables execution.

`archiveRetentionDays` and `minimumVerifiedArchives` must be supplied explicitly. The `inventory` action hashes actual local ciphertext and validates retained off-device recovery signatures/pins; it distinguishes the first failed archive from the verified successor. This inventory is local ciphertext plus retained NAS proof, not a claim to have just inspected every remote object.

`retention-plan` produces only a deterministic **dry-run list** of old verified, unpinned archives beyond the configured lifetime, while preserving the configured newest verified minimum. It binds the entire input inventory and output plan with SHA-256. Failed/unverified archives, wrapping keys, tombstone registries, source databases and source exports are excluded. There is **no delete implementation**. Source-export/private-data retention and eventual physical deletion remain explicit policy/authorization work; no arbitrary 7/30-day default was introduced. Test-only seven-day examples are not the production policy.

```sh
python3 scripts/backup_schedule.py template
python3 scripts/backup_schedule.py inventory
python3 scripts/backup_schedule.py retention-plan --config /private/task.json --inventory /private/inventory.json
```

## Scheduled execution and partial-failure recovery

The task config supplies an absolute read-only export command with one `{snapshot}` argument, interval, export/backup timeouts, bounded retry delays, archive lifetime and verified-copy floor. The generated command uses the existing Wrangler D1 export surface and the real database name `forecast-network-enam`; authentication stays in the existing operator environment, never command arguments or reports. Root must review/fill the private config before enabling it.

`run-once` uses an OS `flock`, so overlapping supervisor launches do not start duplicate work and a crashed process releases its lock. Each interval has a stable backup ID. The private journal freezes the source export hash and configuration, records the encrypted-backup phase before invoking it, and reconciles a completed receipt after a crash before checkpointing. Already completed intervals do no more work. Read-only export failures retry with a fresh temporary path; failed source exports are retained, not overwritten.

Once an encrypted archive/public pin exists, a partial backup retries the **same prepared identity and ciphertext** through `backup_recovery.py resume`. It verifies an existing remote ciphertext hash rather than overwriting it, reuses the saved recovery challenge, and uses the NAS's cached proof if completion occurred before the response was received. Incomplete isolated restore attempts remain quarantined; a retry uses another isolated directory. It does not generate or rotate an operational/wrapping key during resume. Unknown initialization outcomes without a prepared ciphertext/public pin, changed frozen input, changed pending configuration, and exhausted retries become `reconciliation-required`; the scheduler does not create a replacement identity or silently skip the pending interval. Exit status and a bounded error type expose failed/reconciliation states without dumping private subprocess output.

`supervisor` renders a macOS LaunchAgent plist and creates only its private local log directory/files. It does not load it. `RunAtLoad=false`, an explicit interval and umask 077 are included. Root can review/install it in the correct logged-in operator account, which must have access to the existing macOS Keychain and NAS SSH identity. No service was installed or altered by this follow-up.

```sh
python3 scripts/backup_schedule.py supervisor --config /private/task.json
python3 scripts/backup_schedule.py run-once --config /private/task.json
```

Remaining runtime gates: root chooses the actual intervals/retention limits; installs/enables the supervisor; observes real fresh D1 exports on two intervals; interrupts a transfer and confirms prepared-intent recovery; checks restart/unlocked-Keychain behavior and local stale/failure monitoring. Until measured, configured intervals are not achieved RPO/RTO claims.

## Concrete rotation and erasure handoff

The existing off-device proof establishes recovery of four original Devnet keys, **not rotation**. No source keys, archives or production data were deleted or rotated in this follow-up. Root still needs to:

1. Implement and authorize the actual account-erasure workflow and current independent tombstone-registry publisher; reconcile immutable public hashes with private payload treatment. Do not activate a restore merely because its SQLite integrity is good.
2. Select a scoped hot Devnet relayer successor, preserve the cold program/upgrade authority, and use the program's supported administrator-authorized relayer transition. Verify the new signer succeeds and the old signer is rejected, with a prepared rollback and real Devnet receipts. Do not infer retirement from copying/recovering a seed.
3. The current worker uses `SESSION_SECRET` to HMAC token lookup values (`apps/web/src/entry.py`), not merely to sign a self-contained session token; it also derives the network-admission fingerprint. Inventory legacy recovery/context hash dependencies before changing it. Add a bounded current/previous-key lookup and explicit retirement mechanism, exercise rotation in an isolated worker/account first, then make an explicitly authorized production transition. The backup tool does not read or copy that secret.
4. Retain recovery material until retirement and backup-retention decisions are verified. Remove no existing source key or archive merely to manufacture a successful drill.

These are outstanding integration/runtime deliverables; the full F20 requirement is not marked complete by the new safeguards or templates.


## Key and token rotation runbook (added 2026-09-15)

Rotations are explicit, ordered operations; nothing rotates implicitly.

1. **Operator admin token** (`ADMIN_TOKEN`): store the new value in Keychain, `wrangler secret bulk`
   through `scripts/deploy_web.py`, then restart the launchd jobs that read it
   (`com.forecast-network.risk-v2-operator`, `com.forecast-network.risk-pipeline-monitor`). The old
   token stops working at deploy; in-flight ticks retry on the next minute.
2. **Relayer / feed signing key** (`SOLANA_RELAYER_SEED`): generate the new seed into Keychain,
   add its public key to the keeper config `trusted_keys` with `valid_from_ms` = planned cutover and
   keep the old key with `valid_until_ms` = cutover + one feed TTL; deploy the Worker with the new
   seed (feed `key_id` changes to `forecast-relayer-<sha256[:16]>`); after one verified keeper tick on
   the new key, set the old key's `revoked_at_ms`. Registry transactions signed by the old key remain
   valid history; new registry deliveries use the new authority only after the on-chain authority is
   rotated by the reviewed program path.
3. **Keeper operator key**: the token program's operator is bound in the reviewed manifest;
   rotating it is a program-level authority change (`forecast-risk/docs/keeper-operations.md`), never
   a config edit.
4. **Pilot wallet** (`forecast-network-f17-pilot-wallet-v1`): disposable; delete the Keychain item to
   retire it — the profile stays as public history.
5. After any rotation, run `scripts/monitor_risk_pipeline.py --no-notify` and confirm `degraded: false`.
