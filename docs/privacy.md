# Privacy and Data Notice

Updated September 10, 2026

Forecast Network records forecasts and public evidence. The service does not offer
monetary transactions or economic rewards for participation.

## Information we store

- Display name, a randomly assigned account identifier, forecasts, confidence,
  comments, follows, disputes, and activity records.
- The original text submitted for question compilation, the resulting specification,
  and language or translation records needed to explain their relationship.
- Session tokens, browser login contexts and legacy recovery credentials as hashes
  computed with a server secret. New profiles use wallet signatures and receive no
  site recovery code. Registration does not require an email address or password.
- Hashes derived from IP addresses and request counts over a limited period for
  abuse prevention. The application does not store raw IP addresses in its database.

## Participation-point records

API `0.5.0` stores private available and committed balances,
per-forecast commitments, policy versions, onboarding awards and an append-only
ledger of grants, reservations, releases and final-outcome adjustments. The account
identifier, relevant forecast, amounts, timestamps and operation records support
accurate balances, retries and auditability. These records do not represent money.

The authenticated points endpoint exposes only the signed-in account's balance,
onboarding status and recent entries. Public profile performance cards do not
include point balances or private point history. Point amount does not change the
weight of a person's forecast or reputation.

Wallet-award eligibility retains the verified public address and award record so
each account and address can receive that award only once over its lifetime.
Unlinking a wallet does not erase or reset that eligibility history. No private key
or recovery phrase is needed, and point records are not published to Solana. These
additional records are retained for ledger integrity and prevention of repeated
awards. The deployed service's production grant checks and isolated ledger tests
are distinguished in the [points verification record](verification-points.md).

## Wallet sign-in

Browsing is public. Wallet sign-in creates or restores your profile. Existing
guest profiles can import an old site code and explicitly migrate to wallet
authentication. Conversion disables that code and previous sessions.

When you sign in with a wallet, the service stores its public address and challenge
and audit records for verifying and recording ownership. It asks the wallet to sign
a single-use challenge that expires after five minutes. This signature is used to
prove control of the address for sign-in and linking it to your profile.

We do not request or collect wallet private keys or recovery phrases. The sign-in
flow does not submit transactions, move funds, or make claims about your balance.
A linked address may connect your profile to publicly visible blockchain activity;
consider that association before signing in. Wallet ownership verification is
separate from publishing a Forecast record to the Devnet registry. Mobile wallet
authorization is kept in page memory and is not saved in browser localStorage.

## Public information

Choosing **Share my record** creates a public snapshot of your display name,
forecasting statistics, up to 20 finalized personal choices, and a selected
standout call. Pending individual choices, wallet information and recovery data
are excluded. Anyone with its record link can view the published snapshot.
Snapshots retain the name and results recorded at creation, so later profile
edits or new outcomes do not silently change an already-shared card.
Closing the image preview does not withdraw that published record.

PNG images are rendered in your browser. Saving or copying a caption does not
post to a social network. A social platform receives the image or text only when
you choose to share through that platform or a device share sheet.

Published questions, resolution criteria, display names, comments, disputes, and
related evidence may be public. Do not include secrets or another person's private
information in questions, comments, or evidence. Recovery codes, sessions, and
provider API keys are not exposed through public APIs or browser code.

The English-default update compiles multilingual input into an English specification
for review before publication. Existing published specifications retain their
original content and commitment. Separate English display translations carry hash
links to the original, and an original-integrity view makes the authoritative record
available. Translation does not erase the original publication or its audit history.

## AI and infrastructure

Submitted questions, published criteria, and evidence are sent to Google Gemini or
Cloudflare Workers AI for compilation, translation, and evidence analysis. Recovery
codes and session tokens are not sent to models. Service execution and storage use
Cloudflare Workers and D1. Cloudflare may process connection information under its
own policies to provide and secure the service.

## Retention and deletion

Login sessions last up to 30 days; logging out revokes the current session. Temporary
rate-limit records expire through scheduled processing. Public forecasts, evidence,
and audit history needed to verify results are retained to preserve integrity.
Automatic account deletion, wallet rotation and account recovery are not currently
available. Losing access to your login wallet may lose access to the same account;
use your wallet provider's own recovery process. An unconverted guest account
still requires its old site recovery code if its browser session is lost.

A record marked as not connected to the blockchain has not already been published
there. Hashes and transaction records published on-chain in a future integration
may not be removable. We therefore keep original personal information off-chain.
Current wallet linking does not itself publish a Forecast record to Solana.

## Cookies and language preference

The service uses Secure, HttpOnly session and browser-context cookies to keep you signed in. It does
not install advertising tracking cookies. Clearing cookies in your browser requires
you to sign in again.

When you choose an interface language, the browser stores that choice under
`forecast.locale.v1` in local storage. It contains only the language code and is
used across visits on that browser. It is separate from your login cookie and
does not contain a wallet address, recovery code or session token. English is the
default when no supported preference is saved. Clearing site storage removes the
preference. Interface language selection does not send text to an AI provider or
translate your published content.

When operating practices change, this notice and its date will be updated. Current
release boundaries are recorded in the [roadmap](roadmap.md) and
[verification record](verification-web.md).

## Explicit question translation

Pressing Translate sends the selected public forecast’s English display text, rules
and existing AI rationale to the configured AI providers for translation and review.
Your unsubmitted comments, forecast inputs, recovery code and wallet signatures are
not included. Accepted translations and their source relationship are retained in
Cloudflare D1 with generation and review records. Cached copies can be reused by
other readers. Translation request limits use a server-derived IP fingerprint.
