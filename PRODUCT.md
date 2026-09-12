# Forecast Network product context

<!-- impeccable:product-schema 1 -->

## Authority

The product and architecture handoff (private) remains the product and architecture source of truth. This document records its web-facing context, not a replacement specification.

## Platform

web

## Stack

Delegated implementation choice: semantic HTML, CSS and browser ES modules, served with a same-origin Cloudflare API. No client runtime dependency. This follows the web implementation contract in `docs/architecture/web-api.md`.

## Users

People who want to express a judgment about an objectively resolvable future event, compare their reasoning with other people and AI, and build a durable record of forecasting quality. Creators publish questions and maintain a following.

## Product Purpose

Turn natural-language questions into immutable, reviewable forecast specifications; collect forecasts; resolve them with evidence and community disputes. The global public product name is Forecast Network.

## Operating Context

The primary workflow is mobile-first: discover, choose YES or NO, set confidence, compare, and return for the outcome. The user requested an actual deployed web service, a blueprint and a roadmap.
The deployed API `0.5.0` points update adds an explicit choice between practice and
committing participation points. It does not change forecast confidence.
The deployed `0.6.0` update adds four selectable interface languages: English, Korean,
Japanese and Traditional Chinese. Browser, production and Seeker Chrome layout/input verification are complete.

## Capabilities and Constraints

- Points and reputation are non-purchasable, non-transferable and non-redeemable.
- Participation policy v1 grants 1,000 points once per account and 500 after verified
  wallet ownership, once per account and once per address lifetime. Unlinking,
  relinking, renaming or signing in again does not repeat an award.
- Forecasts use 0 points for practice or an explicit integer commitment of 1–1,000.
  Correct finalized outcomes return 2× the amount including principal; incorrect
  outcomes return 0; INVALID refunds the amount. Before closing, edits reserve or
  release only the difference. Practice remains available at zero available points.
- Points have no economic value, do not weight crowd probabilities or reputation,
  and are excluded from public performance cards. The private profile shows
  available/committed points, onboarding steps and recent ledger entries.
- Publish only objectively resolvable, reviewed specifications. Preserve their original rules.
- Show crowd, top forecasters and AI separately; missing observations remain empty.
- Registration uses a display name and a server-generated recovery code shown once. No wallet barrier.
- Optional Solana wallet linking verifies ownership with a signed message. Wallet connection is independent of program deployment and requests no transactions.
- A Solana verification claim requires an actual confirmed chain record. Unconnected states are explicit.
- Blueprint and roadmap report current implemented status and future work separately.
- English is the first-visit interface default; an explicit saved language preference
  selects `en`, `ko`, `ja` or `zh-Hant`. There is no automatic browser-language detection.
- Language packs translate interface copy, accessible labels, errors and fixed image
  labels. They do not translate user questions, comments, rules, evidence or wallet
  signature bytes, or add an AI translation button. Document navigation is localized;
  articles retain their original language with a notice and language metadata.
- A language switch preserves drafts, recovery and wallet state. Busy operations
  block switching rather than being restarted or canceled by a re-render.
- Profile cards share actual all-time scores and selected finalized calls. Owners
  explicitly create an immutable public record; ongoing choices and wallet/account
  secrets remain excluded. Empty and provisional records cannot imply expertise.

## Evidence on Hand

The handoff, existing domain package, schemas and tests, compression research, and web API contract. No real community participation, accuracy track record, or production chain confirmations may be invented.
The points implementation follows `docs/architecture/participation-points.md`.
Existing profiles receive a one-time migration grant; eligible verified links are
handled once and historical forecasts remain practice. Production migration and
HTTPS checks verified profile grants, wallet grants and no repeat award after
unlink/relink. Stakes, races and all final-result adjustments were verified in
isolated runtimes. The live 48-hour cycle and Solana deployment remain unfinished.

## Product Principles

- A useful judgment comes before onboarding complexity.
- Evidence and dispute gates come before finality.
- Comparison is understandable without financial terminology.
- Errors preserve user intent and expose a clear retry or refresh.

## Implementation assumptions

English remains the default interface and compilation language. The four-language
interface update follows `docs/architecture/localization.md` and does not change
the existing English compilation/preview flow. Published Korean specifications
remain immutable; attributed, hash-bound English display translations remain
separate. No Mobile Wallet Adapter integration or Devnet program is delivered by
localization. The restrained editorial direction remains unchanged; audience
segmentation, brand assets and specialized accessibility accommodations remain open.
