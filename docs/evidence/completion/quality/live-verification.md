# F05/F09/F11 production read-only verification

Verified production API version **0.12.0** at `https://forecast.eastsea.xyz`. Deployment version **7fa5d458-ab74-4d2a-b28d-e655f2c4577e** is supported by root's `tmp/complete-capabilities-deploy.log` receipt; the public health endpoint independently confirms the application version, not Cloudflare's deployment UUID. Capture timestamps are retained in `live-api.json` and `live-browser.json`.

## Results

- **F05:** actual D1-backed feed returned 7 questions, 5 active. Independently recalculated all 7 quality components and integer weighted scores from the returned input snapshots; independently reproduced the complete unpaginated rank, daily personalized-tie formula for the anonymous scope, category diversity/cold-start selection, and 5 daily IDs. Closed/challenge questions were absent from active recommendations. Live feed ranking disclosures expose measured clarity and honest neutral creator history.
- **F09:** actual detail displayed crowd, top, category experts and AI independently. Representative NASA question: crowd 70% from 1 participant, top unavailable, category experts unavailable, AI 80%. Expert probability is null with count 0 across the live feed. Qualification criteria disclosure opens by keyboard; English/Korean mobile layout is 2×2. No mature experts were invented or claimed.
- **F11:** live public creator profile returns the new methodology, null consistency/calibration, three windows with zero results, no category qualification, and 0 eligible scoring-history rows. The UI accurately renders the new/insufficient-data state. The initial suggestion of a premature creator metric was withdrawn after temporal reconciliation, below. A creator question record and that creator’s personal scored predictions are different populations.

## Temporal reconciliation and correction

**The earlier recommendation to fix a supposedly premature 100% creator metric is retracted.** It was not supported by a coherent snapshot. Root's authoritative D1 evidence in `tmp/complete-capabilities-finalization-current.json` establishes that forecast `f_IklrwuvCXkq8PVKWnH1oaXjC` finalized at **1789428681239**, revision 7, after its challenge began at 1789012665360. That scheduled transition occurred during this multi-request QA. The first feed and later creator response therefore must not be treated as one database snapshot.

A fresh pair of concurrent, cache-busted GETs is retained in `live-temporal-reconciliation.json`. It shows 5 OPEN, 1 CHALLENGE and 1 FINALIZED question; the creator reports one resolved question, zero invalid questions and creator quality 1. A 100% question-validity ratio from that single result is mathematically consistent. It does not imply a mature track record. The creator's personal `eligibleHistoryCount=0`, null consistency and no expertise remain consistent with having no eligible personal predictions.

One distinct observation remains for policy reconciliation: in that fresh feed, `quality.asOf=1789429032837` and `quality.inputs.creatorFinalized=0` even though one item is FINALIZED; the nearby creator response is dated 1789429035738 and reports one resolved question. The new discovery query additionally filters active holds and eligibility completion; the legacy creator ratio does not use the identical population. Root has been asked to inspect those overlays. **This difference alone does not establish an implementation bug or justify a source fix.** Both responses use `Cache-Control: no-store`, and the exact bodies/times are retained. No source changes were made during QA.

## Browser evidence and boundaries

Fresh unauthenticated Chromium contexts exercised 12 scenarios: feed/detail/public creator × desktop 1280×900/mobile 390×844 × English/Korean. Live API responses were used directly, with no fixture injection, cookies, signing keys, login, votes, comments, shares or notifications. A request guard rejected any non-GET/HEAD request; none occurred. No JavaScript page errors or horizontal overflow were observed. Native disclosures were tested with focus and Enter. Screenshot captures include:

- `live-mobile-comparison-ko.png`
- `live-desktop-detail-en.png`
- `live-public-profile-new-en.png`
- `live-mobile-recommendation-en.png`

Visual verdict: **94/pass**, with the temporally reconciled observations and any remaining population-policy question explicitly separate. The first harness attempt incorrectly toggled an already-open disclosure after a language switch; the confirmation respected existing preserved-open behavior and passed. No product edit or additional design iteration was performed.

Representative live URLs:

- `https://forecast.eastsea.xyz/`
- `https://forecast.eastsea.xyz/forecasts/f_Ctsi6HW_e8b443IrxRXe5gsf`
- `https://forecast.eastsea.xyz/creators/system_editorial`

Current live inventory does not establish qualified expert participation, mature consistency, or a provisional nonzero sample. Those states were covered by prior real-SQLite/local-browser tests, not this production run. Authenticated private profile behavior was not exercised. Seven production questions do not prove large-inventory pagination/load capacity. These limitations are not replaced by a claim that all F05/F09/F11 requirements are fully complete.
