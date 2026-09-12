# Storage and verification boundaries

Forecast uses Cloudflare D1 as its application database. Cloudflare Workers serves
the API and static web assets. The local Mac is the development and deployment
machine; keeping its editor open is not required for the deployed web service.

| Material | Durable location | Purpose |
| --- | --- | --- |
| Accounts, profiles, sessions and linked public wallet addresses | Dedicated D1 database `forecast-network` | Authentication and social identity |
| Published specifications, participation receipts, point ledger and reputation scores | D1 | Complete application records and transactional accounting |
| Retained source excerpts, AI reports, resolutions, disputes and their hashes | D1 artifact records | Evidence and audit reconstruction |
| Original events, command receipts, registry intents and delivery confirmations | D1 | Replay, retry and correspondence with chain state |
| Web documents, JavaScript, CSS and bundled assets | Workers deployment | Public web presentation |
| Specification, state, outcome and evidence/reputation commitments | Solana registry when enabled and finalized | Public integrity commitments |
| Deployment and signing credentials | This Mac's Keychain | Durable local credential storage |
| Credentials needed by the running server | Workers Secrets | Runtime access without exposing secrets to browsers |

The wallet integration requests a signature proving control of a public address.
The service does not receive the user's wallet seed phrase or private key. Its own
Devnet relayer pays registry rent and transaction fees; this does not transfer
users' prediction points or create a redeemable token.

## What Solana establishes

The registry stores 360 bytes per forecast and a 104-byte authority configuration.
Long questions, evidence text, user profiles and private authentication records
remain off-chain. A specification hash alone cannot reconstruct its original text.
Preserving the D1 records therefore remains necessary even after chain anchoring.

The relayer attests to application commitments. The program checks its authority,
immutable specification identity, ordered revisions, legal transitions and an
independent challenge deadline. It does not establish that a news report is true
or that an AI decision is correct. The application independently verifies sources,
keeps the original specification and applies dispute and receipt-eligibility gates.

Program upgrade authority and application administrator share a dedicated cold
Keychain identity; the hot relayer is a separate identity. Only the relayer seed
is provisioned to Workers Secrets. Program and buffer identities also have
separate Keychain entries. The deployment manifest contains public keys only.

## Reading status accurately

Configured infrastructure is different from a confirmed forecast record. The
per-forecast integrity response reports the local revision, confirmed revision,
account address, finalized observation slot and known transaction identifier.
A submission awaiting finality remains pending. An unavailable or lagging RPC
cannot turn an unconfirmed revision into a verified one.

An imported challenge can have a later chain deadline than the original application
deadline. For explicitly enabled forecasts, application finalization must also pass
the chain's verified clock/deadline gate. Neither the original criteria nor an
historical event timestamp is rewritten to speed up confirmation.

## Recovery limits

Keychain persistence is not an off-device backup. A D1 database is not a separately
tested export-and-restore procedure. No independently verified disaster-recovery
backup is claimed by this document. Before broad production admission, establish
and test database restoration and an operator-controlled recovery copy of the
cold signing identity. Never place that copy in the repository or public assets.

Workers' placement hint is not a guarantee of database or user-data residency.
See [deployment operations](../deployment.md) and the
[registry protocol](devnet-registry-protocol.md) for implementation details.
