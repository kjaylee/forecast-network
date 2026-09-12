# Four-Language Interface Verification

September 10, 2026 · API `0.6.0`

The interface supports English (`en`), Korean (`ko`), Japanese (`ja`) and
Traditional Chinese (`zh-Hant`). English remains the first-visit default. An
explicit language choice is saved on the current browser; blocked or unavailable
storage does not prevent using the interface.

## Scope and protected records

The packs cover fixed interface copy, navigation, accessibility labels, account
and recovery screens, wallet ownership steps, point balances and commitments,
errors, dates, numbers, image labels and share captions. Document navigation is
localized, while the article retains an explicit original-language declaration.

Changing language does not translate questions, comments, evidence, immutable
resolution criteria or the wallet ownership message. It does not publish a new
forecast or mint any token. The profile-card snapshot hash and its source content
remain independent of the language used to render the image. AI content translation,
Mobile Wallet Adapter integration and a Solana program are separate work.

## Automated checks

Catalog checks require the same nonempty keys and interpolation parameters in all
four languages, with no duplicate namespaces or embedded HTML. Every literal public
server error code has a localized counterpart. Unknown structured errors use a
localized fallback without exposing an unrecognized raw error.

Locale tests cover English default, supported regional aliases, malformed stored
values, failed storage reads/writes, accurate date/time rendering, numeric request
invariance and HTML escaping. All four locales sign the same ownership-message
bytes. Existing authentication, points, retries, wallet races and card-integrity
tests remain part of the release gate.

Card tests additionally check captured locale during asynchronous rendering,
grapheme-safe CJK wrapping, fixed image dimensions, provisional and empty records,
and unchanged canonical profile hashes. Japanese and Traditional Chinese catalogs
received an independent semantic review of point rules, verification steps and
the distinction between proposed and finalized outcomes.

## Delivery evidence

The integrated source passed **285 Python tests, 94 frontend tests and 40 schema
checks**, plus Ruff and strict mypy. Each locale contains 645 messages with matching
keys and placeholders. The mechanical UI detector reported no findings.

A real isolated Python Worker and local D1 were exercised in Chrome at 320px and
1280px, with 390px profile and dialog checks. All four languages preserved search
text, question drafts, comments, confidence and point amounts, and saved the
selected language across reloads. A French browser still opened the app in English
without a saved preference. No page errors or horizontal overflow were observed.

A real local signup awarded 1,000 points once. The recovery code and saved-code
acknowledgment survived language changes. A 75-point forecast retained its NO
choice and 65% confidence; all language changes preserved 925 available and 75
committed points. An intentionally unavailable compiler response stayed localized
and preserved the draft, without contacting an AI provider.

An isolated Wallet Standard provider signed a real server challenge with a freshly
generated, unfunded Ed25519 key. All four languages displayed the same challenge
bytes; verification awarded 500 points. Profile-card language changes reused the
same retained record and URL without additional publication requests. Invalid
recovery-code errors retained the typed input and remained specific after a
language change. The QA wallet was unlinked and its session logged out.

Document navigation changed language while article text retained its original
language declaration. Original questions, evidence, record hashes and numeric
submission fields were preserved. These tests used synthetic local records, never
public predictions or comments.

Mobile Wallet Adapter and Devnet verification remain separate from the browser
and device checks described below.


## Seeker browser translation finding

Physical-device inspection identified Chrome automatic translation inserting
translated DOM text into question titles while the API still returned the original
English title. The application document now uses `translate="no"` and the
`notranslate` class so the service's language selector controls its own interface
and exact review/signature text. Document navigation has the same protection;
article bodies remain separate. The guard was verified against the active Chrome
translation engine on Seeker before release. No browser-wide settings were changed.


## Production and Seeker evidence

The four packs are deployed at [forecast.eastsea.xyz](https://forecast.eastsea.xyz)
in API `0.6.0`. Clean production browser sessions verified all four selections,
persistence, localized native form validation, unchanged original question titles,
and document article-language boundaries at 320, 390 and 1280 pixels. No page errors
or API mutations were observed during these checks.

The connected physical **Seeker** runs Android 16 and Chrome 152.0.7977.76. Its
1200 × 2670 display at 480 dpi presented the site at 400 × 797 CSS pixels with
device pixel ratio 3. All four languages fit without horizontal overflow and kept
the original English question titles. Trusted touch events navigated between
Explore and Create. Native Android keyboard input reached the question field and
was preserved through a language change; the visual viewport contracted to about
489 CSS pixels while the keyboard was open. Test input was cleared and the initial
English interface restored. No public forecast, comment, wallet signature or
transaction was submitted from the device.

Wallet Standard discovery on the tested Seeker Chrome page returned no compatible
provider. This confirms the remaining need for Mobile Wallet Adapter; it does not
mean the device lacks a native wallet. No Seed Vault connection or Devnet deployment
is claimed by these device checks.
