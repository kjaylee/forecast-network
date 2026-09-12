# Wallet authentication

Release 0.11 uses wallet ownership as the primary login credential. Browsing is
public. Creating a profile requires an Ed25519 message signature; it does not
require SOL, an RPC request or a transaction. A returning address loads the same
profile, point ledger and forecast history. New profiles receive no site recovery
code. The service never asks for a wallet seed phrase or private key.

A valid name supplied by the selected wallet account can suggest the initial
profile display name. Existing profile names and migrated guest names are kept.
The optional wallet label is editable presentation data, not verified `.skr`
ownership or proof of a Seeker device. Mainnet domain ownership resolution is a
separate identity integration; it must not be inferred from a label's suffix.

## HTTP and identity boundaries

1. Same-origin POST bootstraps a random browser context in a Secure, HttpOnly,
   SameSite=Strict `__Host-forecast_auth` cookie. Only expiry appears in JSON.
2. The server creates a five-minute nonce binding the address, origin, Devnet
   chain, purpose, mode and keyed profile commitment. The message explicitly
   covers both sign-in and profile linking. The unsigned client chooses no owner.
3. The wallet signs the retained message. Workerd verifies Ed25519 over those
   exact bytes. No transaction-signing method is called.
4. One D1 batch rechecks the context, latest challenge, expiration, identity and
   migration session; consumes the nonce; creates or restores the profile;
   preserves one-time point grants; and issues the session cookie. An old or
   canceled proof cannot issue a session, even if signature verification finishes
   later. Concurrent signups cannot create two owners for one address.

Sessions require both cookies and the context's current active session. Issuing
a challenge compares the captured context epoch, active session and latest
challenge. New login or cancellation invalidates stale responses. Logout revokes
the relevant contexts and sessions server-side. A late Set-Cookie may replace a
cookie, but cannot make its invalidated session authenticate.

New-account grants retain the existing 1,000 profile and 500 wallet point policy.
They remain nonpurchasable, nontransferable and nonredeemable. New wallet profiles
are limited to five per IP-derived fingerprint per day after proof verification;
challenge and verification requests have additional hourly limits. A wallet is
not proof of a unique human: these controls do not establish Sybil resistance.

## Existing profiles

An old site recovery code can import an unconverted account. This route accepts
only the original URL-safe code format, never a phrase. Newly issued import
sessions use the same browser context and stale-response guards as wallet login.
Older, already issued guest sessions remain usable until conversion or expiry.

Migration is explicit: the authenticated guest signs for their displayed profile
ID. The wallet cannot already belong to another profile. Conversion preserves
history, disables the old recovery code and revokes previous sessions atomically.
Former optional link/unlink HTTP mutations are disabled in wallet-first mode;
they cannot bypass the migration session checks. Login-wallet replacement and
removal require a future separately verified ownership-change flow.

Historical unlinked addresses retain tombstones and award history. They cannot
silently reclaim an account or earn a fresh wallet grant. There is no automatic
account merge, wallet-loss recovery or wallet rotation in this release. A wallet
owner must keep access through their wallet provider's own recovery mechanisms.

## Seeker and mobile wallets

The pinned official Mobile Wallet Adapter Wallet Standard SDK 0.6.0 registers
only the local Android flow. Desktop browsers keep Wallet Standard discovery.
Authorization tokens remain in page memory. The app configures Devnet and no
remote relay. It does not save wallet authorization in localStorage.

The SDK normally returns message bytes followed by a 64-byte signature. Only an
actual registered SDK class instance receives this normalization, with exact
prefix and suffix comparisons. Release 0.11.1 also handles the signature-only
compatibility case documented in the official Android client: both response
fields must contain the same exact 64 bytes, and WebCrypto must verify that
signature over the exact requested message before accepting the original bytes.
Account and expiry guards run again after verification. Other providers must
return the original message unchanged. Server verification remains authoritative
and always uses the retained original bytes and address-derived public key.

Reference: [official Android client's detached-message compatibility handling](https://github.com/solana-mobile/mobile-wallet-adapter/blob/main/android/clientlib/src/main/java/com/solana/mobilewalletadapter/clientlib/protocol/MobileWalletAdapterClient.java#L927).

The reproducible browser bundle uses esbuild 0.28.1. The build removes the SDK's
Google Fonts insertion and preserves its system font fallbacks. CSP permits only
the specific localhost wallet connection and exact SDK style hashes. Broad
inline-script or inline-style permission is not enabled. Installation omits
unused React Native peers; the actual installed graph is audited separately from
the larger lockfile's optional peers.

## Verification boundaries

Unit and transport tests cover wrong proofs, replay, expiration, cancellation,
concurrent signups, migration, quota rollback, immutable identity, delayed
cookies and interleaved account changes. Actual local Workerd/D1 and browser
checks are recorded separately from production and physical-device approval in
the release verification record. Browser emulation cannot prove Seeker biometric
approval or compatibility with every wallet application.
