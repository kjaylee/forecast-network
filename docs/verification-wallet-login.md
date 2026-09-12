# Wallet login verification — 0.11

## 0.11.1 mobile compatibility correction

A Seeker user reported a signed-message mismatch after approving login. The
previous release had verified the real device's picker, but had not verified its
wallet-approved signature. Its browser fixture covered only combined payloads.

The official Android client documents wallets returning only the 64-byte
signature. A real Ed25519 regression reproduced rejection of that valid format
before the fix. The correction accepts this shape only for the actual local SDK,
and only after verifying the signature against the original requested message.
Malformed payloads, altered messages and account changes still fail closed.
The browser check now covers both formats using a real generated Ed25519 key.
The physical Seeker subsequently confirmed the same response: one 64-byte
signature for a 715-byte original login message. Ed25519 verification over the
original bytes succeeded, the server authenticated the wallet, and the profile
loaded. Reloading preserved the same account and 1,500-point balance. No site
recovery code was required. Device evidence contains lengths and verification
booleans, not the signature, message or credentials.

[Sanitized physical-device verification](research/evidence/seeker-wallet-login-2026-09-10.json)
records this boundary without exposing the user's wallet or account identity.

September 10, 2026. This record distinguishes implemented safeguards, isolated
runtime evidence and remaining physical-device checks.

## Automated checks

- 649 Python tests pass; 52 generated schemas are unchanged; Ruff and strict mypy
  pass across 31 domain/application modules.
- 181 frontend tests pass. Wallet authentication covers delayed bootstrap and
  signature responses, cancellation, repeated clicks, changed accounts, returning
  login after logout, proof mismatch and legacy-code validation.
- 44 backend wallet-login tests cover nonce reuse, wrong signatures, expired
  challenges, explicit migration, identity tombstones, atomic one-time grants,
  quota rollback, old-session revocation and competing login/challenge requests.
- Five HTTP tests exercise the actual Worker routing/cookie helpers against
  transactional SQLite. Tokens stay out of JSON; both cookies are required;
  cross-origin requests and deprecated wallet-link mutations fail closed.
- Independent review reproduced two additional races, both fixed and tested:
  a stale challenge replacing a newer recovery login, and old optional linking
  finishing after logout. Challenge creation now compares the entire captured
  authentication state; wallet-first HTTP requires guarded migration.

## Actual local Cloudflare runtime

An isolated Workerd and D1 database applied migrations through 0016. A browser
generated an ephemeral, nonexportable Ed25519 private key. The actual server
verified the message signature; a mock signature verifier was not used here.

The test observed first login with 1,500 points, returning login with the same
profile and unchanged grant total, Secure/HttpOnly cookie behavior, logout,
invalid-signature rejection, replay rejection and cancellation. No recovery code
or session token was returned in JSON. The UI also completed wallet selection,
message review, signing, profile loading and logout without page errors.

These are isolated test profiles, not public activity. They performed no Solana
transactions and used no production wallet or private key.

## Browser and mobile adapter

The four-language UI passed the wallet-picker, signing, profile-history and
logout/relogin flows at a 400 × 797 viewport with no horizontal overflow or page
errors. These tests used controlled HTTP fixtures, separately from the actual
local Worker test above.

The actual official SDK bundle registered its local Android adapter in Chrome.
Its message-plus-signature normalization, permission modal styling and
cancellation passed with zero CSP violations. The wallet protocol response in
this adapter-specific browser test was controlled; it was not a physical Seeker
signature. Broad inline styles/scripts and external font access were not enabled.

Two builds produced the same 168,916-byte SDK bundle, SHA256
`a6f33fb126e7457205d47cce99d95f7e477c3e442c19f98c277ea0f9950e09e6`.
The installed graph has no reported vulnerabilities with the build's exact
`npm ci --ignore-scripts --omit=peer` configuration. Unused React Native peer
tooling in the full optional graph is not part of the browser installation.

## Remaining boundaries

Physical Seeker wallet login and session restoration were confirmed in 0.11.1
with a user-approved signature. No wallet approval was performed for the user.
The service does not offer wallet rotation, account merging, lost-wallet recovery
or proof of a unique human. Keep `WALLET_LOGIN_REQUIRED=true`; the old optional
linking fallback is disabled in production.

Wallet login uses message verification and works independently of Solana RPC.
Cloudflare's automatic Devnet registry relay remains paused pending a usable
authenticated RPC. This release does not claim to resolve that separate blocker.
