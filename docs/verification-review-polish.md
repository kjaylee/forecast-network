# Review repairs and export polish — September 14, 2026

These changes are verified locally. They have not been deployed to the hosted
Worker or installed on a physical Android device in this verification run.

## Correctness repairs

- Comment submissions, paginated feed reads and Seeker verification retain the
  initiating account/session and route generation. Delayed success, error and
  cleanup paths cannot update a replacement screen or account.
- Anonymous comment submission survives the authenticated detail repaint. A
  pre-submission detail response cannot overwrite the confirmed comment, and an
  already rendered replacement form keeps its new draft.
- Seeker checks deduplicate mint candidates and read every required batch of up to
  100 in sequence. No concurrent Python tasks await provider binding promises.
- A complete negative ownership check hides the badge while retaining historical
  evidence and the unique mint claim. Transport errors and incomplete responses
  preserve evidence. Wallet-address guards and revision comparisons reject
  overlapping or obsolete refreshes. There is no passive expiration policy.
- The four interface languages include the changed-verification error.

Migration `0021_seeker_verification_status.sql` adds `invalidated_at` and `revision`.
Apply it before deploying the Worker code that queries those columns. The migration
is additive; historical verification rows retain their existing positive evidence.

## Icons and share cards

The deterministic confidence-ring icon keeps the navy/cobalt identity. Adaptive
layers now use the full 108 dp canvas with a mark sized consistently with legacy
icons; the background covers overscan. Android 13 resources include a monochrome
layer. Favicon geometry comes from the same generator. Existing store, legacy and
splash image outputs remain unchanged.

Profile cards group unscored-state copy and separate the score receipt from the
highlighted forecast. Forecast cards show comparable 0–100 probability tracks,
distinguish missing observations from zero and emphasize the accepted personal
forecast. Snapshot, privacy, locale and export-size contracts remain intact.

## Evidence

- `python3 scripts/check.py --tools`: **697 tests**, **52 schemas**, Ruff and strict
  mypy across **33 source files** passed. The root checker now also parses and
  lints the Cloudflare entry point.
- `node --test apps/web/tests/*.mjs`: **250 tests** passed, including **48** async
  action regressions and **23** card tests. An independent review reran the async
  tests after reproducing and resolving the login/repaint gap.
- `cargo test --workspace --locked`: **23** native registry guard tests passed.
- `python3 scripts/build_web.py`: deployable sources, the pinned mobile-wallet
  bundle and public documents staged successfully under `tmp/web-build/`. The
  staged application, card modules and favicon matched their source hashes.
- Icon checks covered dimensions, alpha, background coverage, circular safe zones,
  density variants, adaptive XML and favicon geometry.
- Before/after browser renders covered **32** demo card cases per round with
  self-hosted Sora loaded, all four interface languages, both profile sizes/themes,
  long content, missing/zero probabilities and empty profiles. No browser errors
  were observed. Demo content was confined to local fixtures.
- The mechanical design scan returned no findings. Root inspection confirmed the
  contact sheets and representative full-size exports.

Local artifacts are under `tmp/review-fixes/`, `tmp/card-polish/` and
`tmp/icon-polish/`; they are excluded from version control. CI now includes separate
frontend, static-analysis and Rust jobs alongside the dependency-free Python matrix.
The GitHub-hosted execution of those new jobs remains unobserved until a push.

This verification does not establish hosted migration execution, physical launcher
appearance, native share-sheet behavior or changes to production finalization and
Devnet relay status.
