# Participation Points Verification

September 10, 2026 · Policy `participation-points-v1`

The [participation-points contract](architecture/participation-points.md) defines
the starting grants, optional commitments and settlement rules. These points have
no economic value and require no Solana transaction.

## Deployed release

API `0.5.0` and migration 0004 are deployed at
[forecast.eastsea.xyz](https://forecast.eastsea.xyz). Automated checks passed:
**285 Python tests, 69 frontend tests, 40 schema checks, Ruff and strict mypy**.

All five existing production profiles received their 1,000-point grant. A new
private QA profile received 1,000 on signup, followed by 500 after an actual
Ed25519 ownership proof over HTTPS. Unlinking and relinking did not grant again;
the profile retained exactly one ledger entry for each milestone. The test wallet
was unlinked and the session logged out. The signer was unfunded and issued no
transaction. Anonymous balance requests returned 401.

Production balances reconcile with their ledger entries. The three editorial
questions and zero human forecasts were preserved: production QA made no public
prediction, comment or test-question writes. Reservations and settlement were
exercised in isolated local Worker and remote D1 environments as detailed below.

## Database and application checks

Regression tests cover one-time profile and verified-wallet grants, existing-user
backfill, wallet-address reuse, atomic rollback, exact retries, stale account tabs,
legacy practice requests, concurrent commitments to different questions and
competing updates to one question. The immutable ledger reconstructs available and
committed balances. Stakes do not change crowd averages or reputation scores.

Settlement tests use the real domain transitions, evidence commitments and challenge
rules. Proposals do not pay out. Finalized YES/NO outcomes use the latest committed
choice; INVALID returns the original amount. Repeated outbox processing cannot
credit an already settled position. Tests also reject a future-dated finalization
event and a mismatched accepted forecast revision.

Migration 0004 was applied successfully by pinned Wrangler 4.130.0 against an
isolated local D1 database, after migrations 0001–0003. Remote D1 testing exposed
an additional parser restriction on unparenthesized `CASE` expressions inside
triggers. Guards now use conditional `SELECT RAISE ... WHERE`; the remaining value
expressions are parenthesized. The earlier applied migrations were not edited.
The failed production attempt rolled back completely before application deployment.
[Upstream D1 trigger parser issue](https://github.com/cloudflare/workers-sdk/issues/4727).

A disposable remote D1 database accepted all four complete migration files. The
same domain fixtures and ledger queries then verified a 1,000-point signup, three
100-point commitments, correct/incorrect/INVALID returns of 200/0/100, and a repeated
settlement with no additional credit. These tests used isolated records, not public
user forecasts. Local statement-splitter and remote-parser regressions preserve the
required parentheses and whitespace without weakening the validation rules.

## Actual local Worker requests

The deployed Python runtime was exercised through local HTTP with isolated D1 data,
synthetic historical evidence and locally generated, unfunded Ed25519 signers.
These records were never copied into the public service.

| Scenario | Observed result |
| --- | --- |
| Create profile | 1,000 available, zero committed |
| Concurrent 700-point commitments to two questions | One succeeds; the other returns `insufficient_points`; 300 available and 700 held |
| Retry the successful request | No second debit |
| Reduce commitment from 700 to 300 | 700 available and 300 held; latest outcome retained |
| Omit amount while a positive commitment exists | `stake_required`; commitment preserved |
| Submit from a stale account tab | `account_changed`; no mutation |
| Explicit zero-point practice | Full release of the previous commitment |
| Verify wallet signature | 500 additional points |
| Unlink and verify the same address again | No second grant |
| Link the rewarded address to another account | Ownership link succeeds; no wallet grant |
| Finalized correct / incorrect / INVALID, each with 100 held | 200 / 0 / 100 returned |
| Repeat settlement sweep | Zero further effects; balances and ledger row count unchanged |
| Invoke the actual local scheduled handler twice | Both return 200; the authenticated self-service sweep succeeds and the ledger is unchanged |

The historical settlement fixture used a controlled clock and genuine lifecycle
commands to place the entire challenge period in the past. It demonstrates runtime
settlement behavior; it is not evidence of 48 hours of elapsed production operation.

Local development removes the production custom-domain route and uses a matching
localhost origin. Production wallet origin validation continues to require HTTPS;
the local test does not weaken it.

The scheduled check exposed a prior mismatch with the Python WorkerEntrypoint SDK:
bindings live on the initialized instance, and wrapped service fetches accept
Python keyword options. The handler now uses that contract. A regression constructs
the entrypoint and exercises its bound method, rejects positional shadow bindings,
and preserves failure reporting without exposing the operator secret.

## Browser checks

Real local browser requests verified a 1,000-point signup, explicit 50 → 100 → 0
commitment changes, and a separately held 65% confidence value. Empty and over-limit
amounts were blocked without writes. Wallet signing raised the account to 1,500
exactly once, with matching onboarding steps and ledger history.

The profile and forecast form fit 320px, 390px and 1440px viewports without horizontal
overflow. The English points panel distinguishes available, committed and total
balances. The profile record export remained 1200 × 675 and excluded private wallet
details and point balances. No browser console errors were observed in these flows. A final production review
also found a shrinking Search button at narrow widths; its scoped layout now keeps
the label on one line while the input takes the remaining space.

## Boundaries

The checks do not establish unique-person identity, abuse resistance under load,
support for every wallet extension, or an external security certification. Wallet
ownership is address control. No points can be purchased, transferred or redeemed.
Production verification must avoid synthetic public predictions or inflated human
participation statistics.
