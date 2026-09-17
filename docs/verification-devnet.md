# Devnet registry verification — 0.9.0

Updated September 10, 2026. This record separates the deployed Solana program,
actual transaction checks, confirmed public histories and unfinished hosted delivery.
The product remains a non-monetary forecasting network: points cannot be bought,
transferred or redeemed, and the registry does not issue an economic asset.

## Verified deployment

| Item | Recorded evidence |
| --- | --- |
| Network | Solana Devnet |
| Program | `BvZLYrSmzDGTP5jYb14cjreRfHqfMo2sRBfUPpAi5Sgp` |
| Program-data account | `37BxPnmJRQNXjeodFtPq5bAcMs17CGw4qCsPDixyqTX4` |
| Configuration PDA | `2atXcLChWsNpjGRkpziRUDetVJQoyn14H8yFmj62dCoB` |
| Executable size | 101,008 bytes |
| Executable SHA-256 | `20e3140d28bc5438f0292983b96a0cbc30896074dcbb63678be3233f01a97890` |
| Deployment verification | Exact bytes verified through finalized RPC state |
| Program-data rent | 513,999,480 lamports, recorded during deployment |
| Funding | 2 Devnet SOL from an existing local wallet; no mainnet SOL spent |
| Storage layout | 360 bytes per forecast, 104 bytes for authority configuration |

Program-data rent is one allocation, not a total expenditure figure or a mainnet
operating-cost quote. See the [public deployment manifest](../infra/solana/devnet.json),
[registry protocol](architecture/devnet-registry-protocol.md), and
[storage and trust boundaries](architecture/storage-and-trust.md).

The local evidence artifacts inspected for this record are
`tmp/registry-deploy/deployment-evidence.json`,
`tmp/registry-deploy/devnet-runtime-test.json`, and
`tmp/registry-deploy/localhost-runtime-test.json`. They contain public results and
transaction identifiers; temporary evidence files are not published web assets.
The recorded Devnet test forecast account is
`6FNX16rFoiJJ4nS4wHnpRhksJeuaWG9Asp4aN5LvtLZX`. It is an isolated verification
record, not public user participation.

## Real transaction checks

The local validator and actual Devnet each exercised the deployed SBF program:

- Registration stores the exact expected OPEN record and immutable identity.
- A reviewed positive trigger permits early LOCKED before the original close.
- Subsequent transactions advance through RESOLVING, PROPOSED and CHALLENGE.
- Duplicate registration, incorrect authority and stale revisions are rejected.
- An early lock without its trigger commitment is rejected.
- Failed operations preserve the accepted state.
- Chain time establishes a real challenge delay and rejects premature finalization.

The Devnet initialization transaction is
`4wB1E9bZuytz777UqAXec8Frz1CA5HHeMudDbPcWiwzhdpJyhjbyYqsEtABiTEiVDoEQPiRDhukJ34KdxnLa3zgn`.
The test registration transaction is
`4akdaWeW4xtEeHDh7aAneXteCVkpj361P2P8KVX27LyjGbLBkBJbmVtER5r9cYiFpxmKVGhu8oYLUxP3ESDjPiDv`.
The resulting chain finalization lower bound was `1789190883000` milliseconds
since the Unix epoch. This records a future eligibility gate; it does not prove
that finalization was subsequently executed.

**The real 48-hour finalization interval has not yet been observed.** Native tests
use controlled clocks for full lifecycle and timing boundaries. Those tests cannot
substitute for an elapsed live challenge, actual dispute contention, or recovery
from a production outage.

## Application and anti-cheat verification

The release baseline passed **555 Python tests, 131 frontend tests, 23 Rust guard
tests and 52 generated schema checks**, with Ruff and strict mypy across 28 source
files. Original versioned contracts remain preserved. Native Rust checks include
authority and owner guards, exact decoding and storage, revision forks, forbidden
transitions, immutable outcomes, disputes, pause recovery, overflow and failed-write
atomicity. Application tests exercise durable delivery, retries, source and
signature boundaries and transactional point accounting.

Two demonstrated cheating paths were reproduced and contained:

1. A receipt submitted when an outcome was already known could be rejected for
   early resolution, yet later receive winnings and perfect reputation through
   ordinary expiry. Immutable timing-review findings now block ordinary lifecycle
   progress, reputation and both legacy and market payouts. Releasing the intake
   notice does not erase that finding. Valid V2 early resolutions still operate.
2. Legacy forecast intake accepted points while a bound source watch had failed,
   never been checked, become stale, or left a pending review without an active
   notice. Database guards now reject these conditions, including races between
   the initial read and committed write.

Regression tests cover snapshot/event rollback, unchanged balances and reserves,
finalization races, retained pending outbox work and exact replay. The new
eligibility checks also passed focused Python `-O` execution. This coverage does
not establish that all coordinated attacks are impossible.

The completed `tmp/registry-final-verification.log` records 555 passing Python
tests, 52 verified schemas, Ruff and strict mypy across 28 source files. Focused
RPC verification passed 21 transport tests, including unsolicited funding of an
otherwise empty system-owned PDA: the adapter treats that account as uninitialized
rather than allowing an attacker to prevent legitimate registration. Eight real
Worker-wrapper tests cover the corrected redirect handling and strict JSON-RPC
response envelope. Transport-error diagnostics also appear in the verification
log; a passing automated suite is not evidence of a successful live RPC connection.

An additional actual SBF/local-validator run in `tmp/registry-prefund-runtime.log`
successfully registered a prefunded forecast PDA, checked exact storage, advanced
through challenge entry and rejected incorrect authority, duplicate registration,
unreviewed early locking, stale revisions and premature finalization. This confirms
the prefunding recovery path beyond mocked transport tests. It remains local
validator evidence, not a newly observed full Devnet finalization.

## Keys and runtime custody

The relayer, cold upgrade/administrator, program and upload-buffer signing roles
have distinct entries in this Mac's Keychain. The helper
[solana_keychain.py](../scripts/solana_keychain.py) verifies public identity against
stored seed material and reports public keys only. Only the hot relayer seed is
provisioned to Workers Secrets. Temporary deployment keypair files are owner-only,
remain under repository `tmp/`, and are removed after use.

The service never asks for a user's wallet seed or private key. Wallet ownership
linking, native MWA use, and the server's registry relayer are separate mechanisms.
Persistence in one Mac's Keychain is not a tested independent recovery backup.

## Confirmed public histories

The public forecast status evidence in `tmp/registry-deploy/public-forecast-status.json`
matches the completed replay in `tmp/registry-public-bootstrap.log`. All three
current local revisions were confirmed from finalized Devnet accounts:

| Forecast | Current / confirmed revision | State | Finalized observation slot |
| --- | --- | --- | --- |
| [iPhone](https://forecast.eastsea.xyz/forecasts/f_IklrwuvCXkq8PVKWnH1oaXjC) | 6 / 6 | CHALLENGE | 496006144 |
| [Windows 12](https://forecast.eastsea.xyz/forecasts/f_NB_bVj_Y7gFrFB7vy61RZFU2) | 2 / 2 | OPEN | 496005443 |
| [M6 Mac](https://forecast.eastsea.xyz/forecasts/f_uZX0iFRUQspTLFrqf2CnOJcV) | 2 / 2 | OPEN | 496005432 |

Their finalized account and final delivered transaction identifiers are:

| Forecast | Account | Transaction |
| --- | --- | --- |
| iPhone | `3ovuSPBt3ns3ZuG51RVVsebkaANFL9YmSNqL69mL1Cum` | `5tywTgFtVKMNpurrUG7pqfYhYo6SAydjLZknHEeGAVcGeiGgC44cZvpsrQEzHhqhjPNn1HxRbRsdM8rr6y6Lbhn` |
| Windows 12 | `6A2UfV2jdPEKe3CAakpzR99J1BUadQXH9r4rQGKbkEcc` | `2ijs9xyH8ZancE628rbP9N7H5Zzm2UNKV754rKo38phQfgcpTm45xDpGg1ND23JSc9qR7SRKLTiKSR7Y3eccsBip` |
| M6 Mac | `9FbxafpvULc5HPb1xE31F4hoxff1avqbhMc4rCvm9ezv` | `2X2ZvTC3L43VreNjW3zMB5QQjqMskmwMxzzuor2DJTyBUsKvH9gdxzFeJnHboG6CBvYTzmcdJn61Cxk7PXJ1pSqe` |

These histories were synchronized once from this Mac using
[sync_devnet_registry.py](../scripts/sync_devnet_registry.py), the same durable
adapter and retained delivery records. They were **not** confirmed by successful
execution of the deployed Cloudflare scheduler. Confirmation describes these
specific revisions and observation slots; it does not cover future changes.

## Hosted-delivery blocker and remaining gates

The official public Devnet RPC returned HTTP 403 from Cloudflare. The tested
OnFinality alternative also failed from Workers, and no working authenticated RPC
credential was available during the audit. Automatic Cloudflare registry delivery
therefore remains blocked. An authenticated provider endpoint must be configured
securely and verified from the actual Worker, followed by a real scheduled update.
The Mac's successful public-RPC access is insufficient evidence of hosted operation.

**Update, 2026-09-17.** The hosted-delivery blocker described above was resolved
for the deployed system after this audit. An authenticated Devnet RPC proxy
(`https://forecast-rpc.eastsea.xyz/rpc`, declared as `SOLANA_RPC_PROXY_URL`) is
deployed and rejects unauthenticated requests, the release configuration sets
`SOLANA_REGISTRY_RELAY_ENABLED=true`, and the live `/api/status` reports
`relayEnabled: true`. The five-minute scheduled sweep has delivered and confirmed
canonical revisions unattended. The findings above stand as the state at
2026-09-10. The deployed health route's `rpcAvailable` value has not been
re-verified since the proxy was installed, and the 48-hour finalization interval
remains unobserved end to end.

The administrative health route checked the hot key with a fixed non-transaction
message in the actual deployed Worker. It returned `signerVerified=true` and
`rpcAvailable=false`. The public API's three current revisions, specification
hashes and audit heads were also independently compared with finalized accounts
through the official RPC. See the [public verification record](research/evidence/devnet-public-verification-2026-09-10.json).
A successful signing test does not prove working automatic chain delivery.

Remaining gates include elapsed live finalization, automatic hosted delivery,
actual native wallet/MWA testing, operational recovery and database restore drills,
independent custody recovery, and broader adversarial/load testing. Fresh-account
and fresh-wallet grant farming remains a Sybil admission policy limitation.
Timing findings are quarantined pending an explicit eligibility adjudication policy;
the system does not silently erase receipts or invent refunds.

`LIVE_MARKETS_ENABLED` remains false. Billing is a nonbillable sandbox, advertising
is not introduced, and there is no automatic revenue claim. Solana Devnet funding
does not pay Cloudflare or AI bills.

The registry is upgradeable and authority-attested. It checks structured commitments
and chain timing; it does not independently prove news truth, AI correctness or
complete off-chain dispute inclusion. A compromised authorized relayer can attest
false content within the program's structural limits, and the cold upgrade authority
can change code. Preserve D1 artifacts and expose these trust boundaries.
