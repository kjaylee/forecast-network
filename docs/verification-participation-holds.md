# Known-outcome participation containment

Date: September 10, 2026. Production API: 0.7.1.

Apple's September9 [official iPhone Duo announcement](https://www.apple.com/newsroom/2026/09/apple-unveils-iphone-duo/)
identifies it as the first foldable iPhone. That is evidence for the existing
question's official-announcement YES clause; retail availability is a different
condition. The question's immutable end-of-year deadline has not been changed.

An authenticated operator applied a `known_outcome_review` participation hold to
`f_IklrwuvCXkq8PVKWnH1oaXjC`. Its evidence link, actor, timestamp, specification hash,
request identity and administrative revision are retained in an immutable audit.
The same request replay produced the original result. A stale release cannot remove
a newer hold. Domain state and original integrity response remain unchanged.

At activation, the question had zero participants. The service now reports two
active questions, and this held question is excluded from daily recommendations.
The published question remains readable with an explicit review notice and evidence
link. No final YES, point settlement or early-resolution claim has been made.

Migration0006 installs transaction-level guards against new forecast writes and
point reservations during an active hold. The application also rejects them before
AI or vote processing. Tests include a hold arriving after a request read OPEN but
before the atomic vote/points commit, practice and positive stakes, increases,
reductions, outcome/confidence edits, original accepted retries and lost commit
acknowledgments. New changes are intentionally blocked even where normal v1 permits
pre-close reductions; existing receipts and amounts remain. That is a temporary
incident containment exception, not a retrospective change to accepted payouts.

Validation:322 Python tests,122 frontend tests,40 schemas, Ruff and strict mypy over
19 modules. Four-language mobile browser checks confirm the review state, official
link and absence of a participation form without horizontal overflow or JavaScript
errors. The connected Seeker also shows the review hold with no participation form.
Anonymous requests cannot access the administrative control.

Current domain guards still prevent Lock/BeginResolution before the published
close. This containment does not implement early resolution. The next P0 design
item is a versioned monotonic-event trigger, reviewed evidence, unchanged dispute
window and explicit treatment of any post-announcement participation. Do not force
that path by changing timestamps, bypassing snapshot validation or faking an outage.
See the pricing and operating design (private).

A regression test advances a held question through its original close, challenge,
finalization and archive. Final/archived public views suppress the pending-review
notice while preserving the administrative audit and original rules.
