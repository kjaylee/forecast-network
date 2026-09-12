# Autonomous runtime verification — 0.8.0

Updated September 10, 2026. Status: deployed and verified on production as0.8.0.
This report does not certify a live final outcome, active
point markets, real payments or a deployed Solana program.

## Evidence collected

| Layer | Result | Scope and limit |
| --- | --- | --- |
| Versioned contracts | 52 generated schemas | Original 40 contracts preserved; new early-trigger and pricing records added |
| Host checks | 487 host tests passed | Full domain/application/tool checks on the integrated source |
| Frontend unit checks | 131 tests passed in the UI lane | Includes account-bound quote validation, expiry, double submission, uncertain retry and translation behavior |
| Isolated browser fixtures | Passed at 400px and 1280px | Four locales, explicit quote/confirmation, input preservation and legacy/active separation; fixture results are not server evidence |
| Actual local Worker/D1 and UI | Passed | Four locales; price 50% → 54.76%; 100-point cost → 190.902828 correct-outcome claims |
| Balance isolation | Passed in actual local runtime | Shadow balance 1,000 → 900; actual participation points unchanged |
| Wallet route | Healthy in local runtime | Existing wallet route continues working; no new ownership or transaction claim |
| Billing sandbox | Passed in actual local Worker/D1 | Uncertain billing reservation retained, full refund and idempotent replay; `billable: false` |
| Real provider early review | Passed locally and in production | Actual Gemini + Cloudflare qualify and propose YES; original deadline/specification preserved; normal 48-hour challenge, no finalization |
| Production deployment | Passed | Migrations0007–0010 applied; source watching enabled, active markets disabled, billing nonbillable; official five-minute schedule verified |

Local evidence files are `tmp/market-runtime-check.log`,
`tmp/billing-runtime-check.log`, `tmp/market-ui-browser-verification.json`, and
`tmp/market-ui-tests.log`. They contain isolated test observations and remain
uncommitted scratch artifacts. The visual verdict is recorded in
`.omx/state/market-ui/ralph-progress.json`; it passed at 93/100.

## Behaviors covered

The market client verifies owner, question, immutable source, pricing policy,
side, amount, expiry and exact atomic claim values before displaying a usable
quote. Repeated confirmation cannot start a second concurrent fill. An uncertain
response keeps its operation identity across navigation within the same browser
session. Stale owner and route results cannot update a different account's view.

Language changes preserve typed amount, side and unrelated comments. They require
a fresh quote rather than silently accepting an older one. English-source
translation controls are hidden in the English interface; Korean, Japanese and
Traditional Chinese retain explicit Translate / Original controls.

Shadow views retain the original legacy commitment form. Active-market views
separate 0-point confidence recording from market orders. The actual runtime
checks exercised the shadow path only and did not debit participation points.

Source observation retains the original closing time and distinguishes a known
publication instant from an observation upper bound. Containment precedes model
review. The early path still requires supported evidence, independent review and
48 hours for challenges; tests do not substitute a production waiting period.

## Production acceptance

Cloudflare serves0.8.0 with official source observation enabled and the existing
five-minute schedule active. Migration0007–0010 applied successfully. Two explicitly
funded shadow previews are available for the Windows12 and M6 questions. Anonymous
100-point quote checks returned190.902828 test-point claims without creating a
user order or spending actual participation points.

The iPhone question's actual official-source review and independent provider checks
moved it into CHALLENGE with proposed YES. The specification's canonical bytes and
original close time remain unchanged. The challenge deadline is 2026-09-12T03:57:45.360000+00:00.
No clock was advanced and no final payout was forced. Actual finalization must be
observed after the full challenge period before it can be claimed as live evidence.

Connected Seeker verification passed across all four languages: native touch
quotes, exact190.902828 preview claims, English translation controls hidden when
appropriate, and the live CHALLENGE display with no final outcome. No real point-
market fill or payment was submitted on the device.

The configured five-minute schedule was read back from Cloudflare. A source poll
was recorded at2026-09-10T04:11:07.259Z, after the manual challenge verification
and without another manual sweep, confirming continued scheduled observation.

Costs are still paid by the operator. The sandbox validates accounting behavior,
not demand, payment collection, profitability or guaranteed autonomous income.

See the [runtime architecture](architecture/autonomous-runtime.md) for interfaces,
flags, scope and migration responsibilities.

Runtime issues found and fixed before rollout: Pyodide/Workers `JsDict` response
wrappers now normalize recursively before strict validation; official-source raw
retention is512KiB while model excerpts remain24KiB; JSON-LD date-only metadata
stays date-only; Microsoft media uploads are excluded from article discovery;
early counter-reviews are concise and bind exact time semantics; clause matches
are constrained to the published ID instead of accepting free-form prose.
