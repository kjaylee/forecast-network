# Profile Record Card Verification

Verified September 10, 2026. The global interface and card copy are English.

## Delivered behavior

The profile's **Share my record** action publishes a dated public snapshot, then
renders a downloadable image from those retained values. Wide images are
1200×675 pixels; portrait images are 1080×1350. Both support paper and ink styles.
Changing style or size reuses the same record and does not create more snapshots.

Cards show the public name/handle, all-time forecast count, accuracy with the
correct/scored denominator, Brier/calibration scores, a selected standout call,
and actual recent results. INVALID results are excluded from scores. New records
have no accuracy score; fewer than ten scored results are labeled provisional.
The standout call is explicitly selected, while the headline statistics cover
all scored results.

PNG saving, device image sharing, caption copying, public-record link copying,
and an X composer link are implemented. The X composer carries text and the record
link; the user must attach the PNG and choose whether to post. No social post was
sent during verification.

## Data and privacy checks

One prepared SQL statement captures identity, the complete scored ledger, the
latest 20 results and highlight. Unit cases verify that a 25-result record keeps
its all-time denominator despite displaying only 20 history entries. Pending
choices, wallet details, sessions and recovery credentials stay out of the snapshot.

The HTTP publication endpoint requires a displayed-account precondition matching
the authenticated account. A stale-tab account mismatch returns 409 before
publication; submitted metrics are rejected with 400. Public retrieval accepts
only the profile snapshot artifact kind and verifies the exact retained bytes.
Later results and profile edits do not rewrite an existing shared snapshot.

## Evidence

- **232 Python tests** and **49 frontend tests** passed, with 40 generated domain
  schemas unchanged, Ruff and strict mypy passing. The new coverage includes 11
  application snapshot cases, three HTTP ownership-precondition cases and 15
  renderer/sharing cases.
- An operational QA account created an actual snapshot over production HTTPS.
  Its empty record rendered without an invented score or achievement.
- The wide PNG exported at **121,545 bytes**; the portrait PNG at **166,946 bytes**.
  Their pixel dimensions matched the selected formats and their record link was
  identical across style/size changes.
- Anonymous retrieval of that record succeeded, and independent SHA-256
  recomputation matched its snapshot identifier. Its public user object contained
  only ID, display name, handle and creation time.
- The desktop share dialog and mobile dialog/public-record page were inspected.
  The 390px mobile page had no horizontal overflow. A profile-header button wrap
  and a clipped wide preview were corrected in the final visual pass.
- Synthetic mature, zero-score, new-profile and long-Unicode design cases were
  rendered locally with an embedded **DEMO DATA** watermark. Those statistics were
  not inserted into production.

The browser's 409 and 400 console messages during negative endpoint probes were
expected test responses. They are separate from normal page rendering errors.

## Boundaries

A card is an application record, not an on-chain certificate or a real-world
identity certification. An exported image can be edited outside the service;
the public record link is how a reader checks its retained source. This feature
does not deploy a Solana program, move funds, publish automatically to X, or claim
support was tested in every social app and wallet extension.

See the [web API contract](architecture/web-api.md), [privacy notice](privacy.md)
and [overall release verification](verification-web.md).
