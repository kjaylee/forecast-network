# Forecast Network web design

This records the implemented web surface in `apps/web/public/`. Product truth is in
the private product handoff; web contracts are in
`docs/architecture/web-api.md`.

## Direction

An editorial social forecast feed. The question has the strongest typographic
weight; the choice follows directly below it. Public evidence and the three
independent probability groups receive separate readable sections.

The working scene is a person checking a few questions on a phone during the day,
then occasionally reading evidence or reviewing their record on a larger screen.
The light canvas and dark text suit that scene. A restrained cobalt accent marks
actions, active navigation, and crowd forecasts. The interface is an operating
surface with actual questions, never a marketing hero or fabricated activity.

The root execution brief selected this direction. The Impeccable direction seed
was `731c4ff4`; the explicit brief superseded the random alternatives.

## Surface composition

- Desktop: 224 px left navigation, central question feed, 248 px right context.
- At 1,000 px: secondary context collapses; the readable feed remains centered.
- At 760 px: a compact brand header and five-tab bottom navigation replace the
  sidebar. Create remains the central action. Safe-area insets protect the bottom
  bar and the body reserves its space.
- Detail, create and profile share a 750 px maximum reading width.
- Forecast rows use dividers and spacing. Bordered surfaces are reserved for
  focused prediction input, the AI specification preview and form controls.
- Documents use the same canvas, ink, accent, reading measure and link treatment;
  their content is generated from authoritative Markdown by the deployment build.

## Four-language interface — 0.6.0 deployed

Provide English, Korean, Japanese and Traditional Chinese choices using their
language names, not flags. English is the first-visit default without automatic
browser-language detection. Reuse the explicit preference in `forecast.locale.v1`
for the application and document navigation. The selected interface sets its `lang`;
articles and user content retain their own known language.

Localize every interface state, including authentication/recovery instructions,
wallet prompts, participation-point rules and errors, image labels, empty states,
accessible names and live announcements. Keep text readable at mobile widths without
shrinking important labels or removing explanatory copy. Use locale-aware number
and date formatting while retaining unambiguous deadline timezones.

Switching language preserves drafts, confidence/stake inputs, recovery-code display,
wallet state and share-preview choices. Block switching during busy operations with
an accessible explanation; never duplicate a submission or start a wallet action.
Do not translate questions, comments, rules, evidence, recovery codes or the exact
wallet message. Existing English display translations remain separately hash-bound.

Document navigation and notices are translated, but article bodies remain English
or their original research language. Give the article its own `lang` and a visible
language notice. Fixed image copy can change language without altering the saved
record, metrics or question text. No AI translation button, new font dependency or
layout redesign is in scope. See the [localization contract](docs/architecture/localization.md).
Four-language browser and image checks passed, including actual Seeker Chrome
touch navigation and native keyboard input. Browser automatic translation is
disabled for the controlled application surface so it cannot override the chosen
pack or exact question/signature display. See [verification](docs/verification-localization.md).

The language picker is a secondary header utility: a 17px outline globe, a compact
EN/KO/JA/繁中 label and a small chevron, with no resting border or raised surface.
Its 44px native-select hit area preserves touch, keyboard and screen-reader behavior;
the open list uses the complete language names. App headers, dialogs and document
navigation share one renderer rather than separate select styling. Hover uses the
existing quiet accent surface; keyboard focus retains the standard cobalt outline.

## Tokens

| Role | Value |
| --- | --- |
| Canvas | `#f7f8fa` |
| Raised form / dialog surface | `#ffffff` |
| Primary ink | `#171c29` |
| Secondary text | `#626a7a` |
| Divider | `#dce1e9` |
| Muted probability track | `#e7eaf0` |
| Action / crowd | `#2456ed` |
| Action hover | `#1743c7` |
| Quiet action surface | `#edf2ff` |
| Error text | `#a42533` |
| Focus surface radius | `14px` |
| Typical control radius | `8–10px` |
| Dialog radius | `18px` |

Use the platform system sans stack for the English-default global interface and
appropriate system CJK fallbacks for Korean, Japanese and Traditional Chinese.
Apple SD Gothic Neo, Noto Sans KR and Malgun Gothic remain Korean fallbacks. No remote font
dependency is needed for the task interface. Weight, measure, spacing and density
create the hierarchy; the wordmark uses a compact lowercase sans treatment.

Page headings are 30 px desktop / 26 px mobile. Question titles are 23 px in
the feed and 32 px desktop / 28 px mobile in detail. Supporting text ranges from
12–15 px. Small metadata is 10–12 px. Letter spacing stays above `-0.04em`
except the short brand wordmark. Probabilities use tabular numerals.

## Components and behavior

- Feed: category, creator, actual deadline, question, crowd probability, separate
  top-forecaster and AI numbers, participation/comment counts and YES/NO actions.
- Empty values display an em dash and a contextual explanation; zero remains 0%.
- Detail: separate comparison columns, one focused prediction form, actual
  history chart, immutable rules, sources, result proposal, evidence, dispute
  form, timeline and comments. Full timestamps show the device timezone; raw
  specification/hash verification is linked from the integrity disclosure.
  English display translations are applied only when their specification hash
  matches, and translated details identify the original Korean source.
  Canonical records, outcome identifiers, timestamps and source URLs stay intact.
- Confidence: 0–100 range control plus a sentence explicitly translating the
  selected outcome into YES probability. No payout or trading vocabulary.
- Authentication: a native modal guides wallet choice, ownership-message review,
  signature and server confirmation. Legacy recovery-code login remains available
  for migration; new profiles receive no recovery code. Wallet signatures and
  recovery secrets never appear in telemetry or public cards.
- Error states retain the form. A stale revision offers an explicit refresh;
  timeouts retry with the same operation identity within the session.
- The profile Wallet section separates provider connection, account choice,
  message review, signing and server verification. Only server-confirmed ownership
  is labeled “Wallet linked”; Devnet deployment status stays separate. Canceled
  requests, incompatible signatures, account changes and expired challenges do not
  establish a verified link. Explicit native forecast attestation uses a separate
  transaction-signing action, described in `docs/android.md`.
- Internal navigation uses browser history and deep links. Documents use ordinary
  full-page navigation. Share opens a locally rendered PNG preview from a freshly
  fetched forecast snapshot. It includes real crowd/top/AI values, the accepted
  personal forecast when present, a timestamp and the deep link. The device share
  sheet receives a PNG File when supported; download and link-copy controls are
  explicit fallbacks. Canceled shares and download dispatch do not increment the
  share count. Missing image values say “No data yet”.

## Profile record sharing

Profile sharing adds two fixed editorial image compositions: 1200×675 wide and
1080×1350 portrait, each in paper and ink. Identity and accuracy lead, with the
correct/scored denominator, Brier/calibration, a labeled standout call and actual
recent results. New profiles have no performance score; provisional samples are
identified. Long names and titles use grapheme-safe fitting and explicit ellipses.
The image footer shows the domain, handle, UTC capture time and record reference.

**Share my record** opens an image preview with size/style choices, PNG saving,
native sharing, a ready-to-post caption and an X composer link. Format changes
reuse one published snapshot and keep keyboard focus on the selected control.
An anonymous record page verifies the retained snapshot before rendering, exposes
its result list, and distinguishes the dated card from current profile statistics.
Synthetic design previews are explicitly watermarked DEMO DATA and are never
seeded into production reputation data.

## Participation points — deployed in API 0.5.0

Keep points subordinate to the question and confidence. The private profile shows
**Available** and **Committed** separately, one-time onboarding steps, and recent
ledger entries. Policy v1 awards 1,000 points for a profile and 500 for verified
wallet ownership, once per account and once per address lifetime. A successful link
does not imply another award when the account or address has already received one.

The forecast form explicitly offers **Practice · 0 points** or a whole-number
commitment from 1 to 1,000. Confidence remains its own probability control. Show
the rule before submission: a correct finalized forecast returns twice the amount
including the original points, an incorrect result returns zero, and INVALID
returns the original amount. For example, committing 100 returns 200 total if
correct, not 300. These are participation points with no monetary value.

Before close, edits reserve or release only the difference; choosing 0 releases
the current commitment and returns to practice. Preserve outcome, confidence and
point input on insufficient-balance errors, refresh the balance, and explain the
rejected request without silently lowering it. Bind actions to the displayed
account so a stale tab cannot spend or release another account's points.

Proposals, challenges and paused resolution do not present points as settled.
Practice remains available at zero balance. Crowd signals and public reputation
cards keep their existing weighting and measures; a grant or points balance never
becomes an expertise badge. Points do not require a Solana transaction. Production
HTTPS checks verified onboarding grants and unlink/relink without another grant.
Browser stake flows and settlement were verified in isolated runtimes; they are
not claims of public production participation or an elapsed 48-hour lifecycle.
The [points verification record](docs/verification-points.md) separates that evidence.

## Accessibility and motion

Semantic landmarks, labeled forms, native dialogs, keyboard focus rings, a skip
link, outcome pressed states, live error/status announcements and a native range
control are included. User text is escaped before HTML insertion; evidence links
accept only HTTP(S) addresses without credentials.

Motion is functional: a short route arrival, selected-control feedback, transform
updates to probability tracks and a restrained toast entrance. The reduced-motion
preference removes animation and smooth scrolling. Dynamic probability transforms
are applied as individual CSS properties, not unsafe inline markup.

## Release verification

The `0.6.0` release passed 285 Python tests, 94 frontend tests and 40 schemas.
Four-language production and actual Seeker Chrome layout/input checks passed; see
[localization verification](docs/verification-localization.md). The following
`0.5.0` baseline coverage remains part of the regression suite.

- Browser modules pass Node syntax validation.
- Sixty-nine boundary tests cover escaping, URL safety, missing and zero data, deadline
  controls, real-history charts, retry keys, stale revisions, network timeouts,
  Korean/grapheme-safe image wrapping, actual-data snapshots, canceled shares and
  explicit timezone labels across calendar-day boundaries, hash-bound display
  translations, Wallet Standard discovery order, account selection, cancellation,
  signed-message integrity and account changes during signing. Delayed wallet
  responses are tested across logout and account changes; stale errors and cleanup
  cannot overwrite another user or clear a newer operation. Connection subscription
  tokens remain independent from selected-account and request generations.
  Points coverage adds explicit practice/stakes, preserved inputs on errors,
  balance displays and account preconditions. The full release also passed 285
  Python tests, 40 schema checks, Ruff and strict mypy.
- The single mechanical design scan found a width-animation warning. Tracks now
  animate their transform, eliminating that layout animation.
- Earlier release evidence included a 92/100 desktop/mobile visual pass and
  browser authentication, forecast submission and PNG export (153 KB). Current
  points UI and isolated runtime evidence are recorded separately; this earlier
  score is not a new production stake-flow measurement.
