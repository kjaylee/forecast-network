# Receipt eligibility verification — API 0.10

The release implements [evidence-cutoff eligibility](architecture/receipt-eligibility.md).
It changes individual participation eligibility without rewriting original
specifications, accepted commands, market fills or published audit history.

## Verification

- 600 Python tests pass, including actual SQLite migration-backed compensation,
  race, scoring, ordinary-resolution and public projection tests.
- 159 frontend tests pass, including four-language notices, frozen/unknown prices,
  read-only lost-ack reconciliation, stale accounts and forged responses.
- 52 generated contracts remain unchanged; Ruff and strict mypy pass.
- Mobile browser checks exercised three application-generated states in four
  languages at 400 × 797 CSS pixels. Normal and doubled notice text had no
  horizontal overflow or runtime errors. The notices preserve the existing status
  panel style. These are isolated fixture checks, not fabricated production trades.

The earlier 23 Rust security tests and actual Devnet registry verification remain
applicable because this release does not change the deployed program. New final
reputation commitments with eligibility decisions use an explicit payload version;
the old format remains for records without a decision.

## Cases proved

A first submission at the precise publication timestamp is void and receives its
remaining committed points back without a winner bonus or reputation score.
NO with 100 points before publication followed by YES with 900 afterward restores
NO with 100 and returns 800. A late withdrawal to zero cannot cancel an earlier
losing prediction. Multiple questions' corrections conserve one account's total.

Compensation is exact-once after lost acknowledgements. SQL rejects partial
classification that would incorrectly restore an earlier, smaller stake, and the
completion barrier independently checks the latest valid target and actual position.
Market suffix refunds preserve earlier claim quantities and sufficient reserves.

Ordinary expiry is also checked: evidence first discovered during resolution with
an earlier or unknown publication time does not silently finalize and pay a late
participant. It stays under timing review. A newer article cannot erase that hold.

## Remaining boundaries

- Delayed semantic timing adjudication is not yet automated. Contextual, date-only,
  unavailable or conflicting timing stays under review rather than being guessed.
- Already settled histories require an audited correction workflow; this release
  does not silently claw back earlier payments or replace committed outcomes.
- One-hour early closure is a separate rule to disclose before participation;
  this release does not impose it retroactively.
- Active paid-value assets do not exist. Active point markets remain disabled in
  production; shadow balances are isolated, and billing remains nonbillable.
- Hosted automatic Solana relay was paused pending authenticated RPC access at the
  time of this verification. Initial public histories and the hosted signing key
  were separately verified.

**Update, 2026-09-17.** The relay is no longer paused: an authenticated Devnet RPC
proxy is deployed and the live `/api/status` reports `relayEnabled: true`. The rest
of this record stands as written.

Evidence files include `tmp/eligibility-ship-check.log`,
`tmp/eligibility-frontend-final.log`, `tmp/eligibility-ui/results.json` and
`.omx/state/receipt-eligibility/ralph-progress.json` in the operator workspace.
