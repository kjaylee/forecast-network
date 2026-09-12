# Interface Localization

Status: deployed in `0.6.0`. Integration, production and Seeker Chrome layout/input
checks passed. The `0.5.0` evidence remains a separate baseline. See
[localization verification](../verification-localization.md).
This is a display-only update with no new dependencies, database migrations,
or changes to domain contracts. On-demand content translation was added separately
in `0.7.0`; see [its contract](display-translations.md).

## Supported languages and preference

| Stored code | Selector label | Formatting locale |
| --- | --- | --- |
| `en` | English | `en-US` |
| `ko` | 한국어 | `ko-KR` |
| `ja` | 日本語 | `ja-JP` |
| `zh-Hant` | 繁體中文 | `zh-Hant-HK` |

English is the first-visit default. The application does not select a language from
the browser's language or location. An explicit choice is saved in localStorage
under `forecast.locale.v1` and reused by the application and document navigation.
This is a browser preference, not an account setting or authentication credential.
Unavailable storage must not block the interface: the current page can retain its
choice without persistence, and a fresh visit defaults to English.

`normalizeLocale` admits only the four supported results. Recognized regional
English, Korean and Japanese variants normalize to their base code. Traditional
Chinese variants, including Taiwan, Hong Kong and Macau, normalize to `zh-Hant`.
Unsupported or malformed values fall back to English; selecting Traditional Chinese
does not introduce a Simplified Chinese catalog.

## Modules and text contracts

`apps/web/public/i18n.mjs` owns locale initialization, selection, allowlisting,
plain-text interpolation, error translation and shared formatting. Catalog modules
under `apps/web/public/locales/` separate shared/error, interface and card copy;
each provides explicit English, Korean, Japanese and Traditional Chinese entries.
`document-i18n.mjs` applies the same preference to document navigation.

Use stable namespaced keys such as `common.language` and `error.stake_required`.
`t(key, params, locale)` returns plain text with named `{token}` substitutions.
It does not parse HTML, execute expressions, translate arbitrary content, or fetch
model output. Missing keys fall back to English, then to the key for diagnosis;
coverage tests must prevent unresolved keys from reaching production screens.

Insert translations with `textContent` or the appropriate text attribute setter.
If a caller builds an HTML template, escape the complete interpolated result before
insertion. Treat substituted names, API messages and other dynamic values as
untrusted. Catalogs must contain no HTML, event handlers, script fragments or
executable interpolation. A language value must never select an arbitrary module,
URL or expression.

Use `Intl.NumberFormat` and `Intl.DateTimeFormat` for displayed numbers and dates,
retaining explicit timezone context for deadlines. Formatting cannot change a
stored integer, probability, timestamp, submitted numeric input, or canonical JSON.
The language selector updates the interface's `lang` value; each supported interface
is left-to-right. Content with a different known language receives its own `lang`.

## Error coverage

Server error codes remain stable transport data. Known codes map to
`error.<code>` in all four catalogs. English may retain the existing specific
server message. Other locales use the matching catalog text; an unknown server
code gets a safe generic localized failure message, not guessed translated detail.
Client errors can provide a known translation key and plain parameters.

Adding or changing an exposed server/client error requires updating all four
catalogs and their coverage tests. Preserve actionable distinctions: authentication,
account changes, stale revisions, insufficient points, required stake confirmation,
expired wallet challenges, cancellation, source failure and unavailable providers
must not all become an indistinguishable retry prompt. Error translation does not
change HTTP status, retry identity, authorization or error handling.

## Localized surfaces

Coverage includes navigation, discovery, creation and preview, detail, comments and
disputes, profile/activity, authentication and recovery instructions, wallet prompts,
point balances and rules, validation errors, empty/loading states, share captions,
image labels, document navigation, and accessible names and announcements.

The following data is outside the catalogs and remains unchanged:

- Questions, comments, published rules, evidence and source URLs.
- Original compiler input and the existing English compilation/preview contract.
- Canonical outcome and state identifiers, timestamps, hashes and audit records.
- Recovery codes, wallet addresses, challenges, exact signed-message bytes and signatures.
- Point policy values, balances, commitments and reputation/scoring inputs.

Existing separately hash-bound English display translations of historical Korean
specifications keep their current integrity checks. Switching interface language
does not generate another translation or replace either artifact. Explicit content
translation buttons use the separate on-demand translation API.

Share images localize their fixed labels, dates and formatted metrics while keeping
the retained question/result content and actual values. Re-rendering an image in a
different interface language must not republish a record, change its commitment,
add a share event, or imply new results.

## Documents preserve their article language

Document links, navigation, language selector and language notices use the chosen
interface locale. Articles remain in their original language: public product
articles are English, while some research is Korean. Display an explicit language
notice and set the article's own `lang`, independently of the surrounding interface.
Localized navigation must not imply that a whitepaper or policy has been translated.
Changing language does not rewrite Markdown, published specifications or hashes.

## Preserve state while switching

A locale change is not navigation, logout, submission or a wallet operation. Retain
question, comment and dispute drafts; selected outcome, confidence and stake;
recovery-code display and acknowledgment state; current session and profile;
wallet selection, verified link and operation state; and share-preview selection.
Do not persist recovery data alongside the saved language preference.

While submission, authentication, compilation, publication or wallet signing is
busy, block the language switch with an accessible explanation rather than
destroying or replaying the operation. Re-enable it after completion. A safe
re-render preserves meaningful focus and does not repeat network mutations, grants,
debits or idempotency keys. Existing stale-account and wallet-generation guards
continue to apply.

Wallet signing still proves address ownership only. Localization adds no transaction
request, funds movement, Mobile Wallet Adapter integration, or Solana program.
Devnet deployment remains incomplete and participation points retain their existing
non-purchasable, non-transferable, non-redeemable policy.

## Adding a message

1. Choose the owning catalog and a unique namespaced key. Add explicit values to
   `en`, `ko`, `ja`, and `zh-Hant`; do not duplicate an English sentence at call sites.
2. Use the same named tokens in every language. Pass data, not HTML or prebuilt
   fragments, and format displayed numbers/dates through the shared locale helpers.
3. Connect every affected visible and accessible label, including errors and image
   output. Keep user content and signed/canonical data outside the translation call.
4. Extend key/placeholder parity, collision and exposed-error coverage tests. Check
   missing-key fallback, hostile token values and blocked storage behavior.
5. Exercise language changes across drafts, recovery display, busy requests, wallet
   signing, stale-account changes and sharing. Confirm unchanged payloads and hashes.
6. Verify all four languages on mobile and desktop, including long labels, keyboard
   navigation, focus, document-language notices, image layout and persistence after
   reload. Record actual results before marking `0.6.0` deployed and verified.

The repository's normal checks remain applicable. Updated test counts, browser
evidence and production results belong in the release verification record after
integration; the prior release's counts do not certify these language packs.


## Browser-provided translation

The app document is marked `translate="no"` with the `notranslate` class. This
prevents the observed Chrome automatic translation from rewriting question titles
and controlled interface or signature text independently of the selected pack.
This is a display safeguard; canonical records and signature verification remain
server-controlled. Document navigation is similarly marked, while article bodies
retain their original language and remain outside the application translation pack.
