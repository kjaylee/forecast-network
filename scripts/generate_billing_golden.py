#!/usr/bin/env python3
"""Export the sandbox billing lifecycle, so a Rust port can be held to it.

The module is deliberately nonbillable — no method verifies or accepts a real payment, and
the money it moves is integer cents of hypothetical USD that nobody can spend. That makes
it tempting to treat as bookkeeping. It is not: it is a state machine over two pools of
capital, and the interesting property is that no sequence of events can leave it owing
more than it holds.

So the vector is a sequence, not a snapshot. Fund, quote, accept, reserve, settle,
publish, complete, refund, release — with the replays and the refusals that sit between
them, because a state machine that only reproduces the happy path has not been ported.

Regenerate with `--write`; CI runs `--check`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages/domain/src"), str(ROOT / "packages/application/src"), str(ROOT)]

from forecast_application.database import SQLiteDatabase  # noqa: E402
from forecast_application.errors import AppError  # noqa: E402
from forecast_application.service_billing import SandboxServiceBilling  # noqa: E402
from golden_cli import golden_main  # noqa: E402

GOLDEN = ROOT / "tests/golden/billing-golden.json"
INVOICE = "sandbox:invoice-1"
OWNER = "a" * 64
SCOPE = "b" * 64
NOW = 1_700_000_000_000

TABLES = [
    "service_billing_sandbox_capital", "service_billing_sandbox_invoices",
    "service_billing_sandbox_receipts", "service_billing_sandbox_audit",
]


class Fixture:
    def __init__(self, *, enabled: bool = True) -> None:
        self.connection = sqlite3.connect(":memory:")
        for migration in sorted((ROOT / "apps/web/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())
        self.db = SQLiteDatabase(self.connection)
        self.now, self.counter = NOW, 0
        self.billing = SandboxServiceBilling(self.db, lambda: self.now, enabled=enabled)
        self.calls: list[dict] = []

    def key(self) -> str:
        self.counter += 1
        return "sandbox:key-" + str(self.counter)

    async def call(self, name: str, awaitable, *, expect_refusal: bool = False) -> object:
        try:
            result = await awaitable
        except AppError as exc:
            self.calls.append({"call": name, "error": {"status": exc.status, "code": exc.code, "message": exc.message}})
            if not expect_refusal:
                raise
            return None
        self.calls.append({"call": name, "result": result})
        return result

    async def rows(self) -> dict:
        return {table: [dict(row) for row in await self.db.all(f"SELECT * FROM {table} ORDER BY rowid")] for table in TABLES}


async def build() -> dict:
    fixture = Fixture()
    billing = fixture.billing
    await fixture.call("estimate", _immediate(billing.estimate(200, 115)))
    await fixture.call("summary_initial", billing.summary())

    await fixture.call("fund_capital", billing.fund_capital(1000, fixture.key()))
    # The same key with the same request is the same grant.
    fund_key = fixture.key()
    await fixture.call("fund_capital_key", billing.fund_capital(500, fund_key))
    await fixture.call("fund_capital_replay", billing.fund_capital(500, fund_key))
    # The same key with a different request is a conflict.
    await fixture.call("fund_capital_conflict", billing.fund_capital(600, fund_key), expect_refusal=True)

    quote_key = fixture.key()
    await fixture.call("quote", billing.quote(
        INVOICE, OWNER, SCOPE, price_cents=200, cost_cap_cents=115, max_attempts=5,
        expires_at=NOW + 600_000, refund_until=NOW + 900_000, key=quote_key))
    # Requesting the same invoice again is a conflict, and the replay of the first request is not.
    await fixture.call("quote_replay", billing.quote(
        INVOICE, OWNER, SCOPE, price_cents=200, cost_cap_cents=115, max_attempts=5,
        expires_at=NOW + 600_000, refund_until=NOW + 900_000, key=quote_key))
    await fixture.call("invoice", billing.invoice(INVOICE, OWNER))
    await fixture.call("summary_quoted", billing.summary())

    await fixture.call("accept_receipt", billing.accept_sandbox_receipt(
        INVOICE, OWNER, reference="sandbox:receipt-1", amount_cents=200, scope_hash=SCOPE, key=fixture.key()))

    # The service work: reserve, settle, and a second attempt that is marked uncertain and then
    # settled late — the path where a cost envelope has to hold across an unknown charge.
    await fixture.call("start_attempt", billing.command(INVOICE, OWNER, "start_attempt", fixture.key(),
                                                        attemptId="sandbox:attempt-1", capCents=50))
    await fixture.call("settle_attempt", billing.command(INVOICE, OWNER, "settle_attempt", fixture.key(),
                                                         attemptId="sandbox:attempt-1", actualCents=40))
    await fixture.call("start_attempt_2", billing.command(INVOICE, OWNER, "start_attempt", fixture.key(),
                                                          attemptId="sandbox:attempt-2", capCents=30))
    await fixture.call("mark_uncertain", billing.command(INVOICE, OWNER, "mark_uncertain", fixture.key(),
                                                         attemptId="sandbox:attempt-2"))
    # An uncertain reservation still holds capital, so `complete` cannot pass yet.
    await fixture.call("complete_blocked", billing.command(INVOICE, OWNER, "complete", fixture.key()),
                       expect_refusal=True)
    await fixture.call("settle_late", billing.command(INVOICE, OWNER, "settle_attempt", fixture.key(),
                                                      attemptId="sandbox:attempt-2", actualCents=25))
    # A provider cost above the authorized reservation is refused.
    await fixture.call("start_attempt_3", billing.command(INVOICE, OWNER, "start_attempt", fixture.key(),
                                                          attemptId="sandbox:attempt-3", capCents=10))
    await fixture.call("settle_over_cap", billing.command(INVOICE, OWNER, "settle_attempt", fixture.key(),
                                                          attemptId="sandbox:attempt-3", actualCents=20),
                       expect_refusal=True)
    await fixture.call("settle_attempt_3", billing.command(INVOICE, OWNER, "settle_attempt", fixture.key(),
                                                           attemptId="sandbox:attempt-3", actualCents=10))
    await fixture.call("publish_wrong_scope", billing.command(INVOICE, OWNER, "publish", fixture.key(),
                                                             scopeHash="c" * 64), expect_refusal=True)
    await fixture.call("publish", billing.command(INVOICE, OWNER, "publish", fixture.key(), scopeHash=SCOPE))
    await fixture.call("complete", billing.command(INVOICE, OWNER, "complete", fixture.key()))
    # The refund window: a refund can be requested, and the principal leaves when it is finished.
    await fixture.call("start_refund_before_request", billing.command(
        INVOICE, OWNER, "start_refund_attempt", fixture.key(), attemptId="sandbox:refund-1", capCents=5),
        expect_refusal=True)
    await fixture.call("request_refund", billing.command(INVOICE, OWNER, "request_refund", fixture.key()))
    await fixture.call("start_refund_attempt", billing.command(
        INVOICE, OWNER, "start_refund_attempt", fixture.key(), attemptId="sandbox:refund-1", capCents=5))
    await fixture.call("settle_refund_attempt", billing.command(
        INVOICE, OWNER, "settle_attempt", fixture.key(), attemptId="sandbox:refund-1", actualCents=5))
    await fixture.call("finish_refund", billing.command(INVOICE, OWNER, "finish_refund", fixture.key()))
    await fixture.call("release", billing.command(INVOICE, OWNER, "release", fixture.key()))
    await fixture.call("summary_final", billing.summary())
    await fixture.call("invoice_final", billing.invoice(INVOICE, OWNER))
    # A closed invoice is not active.
    await fixture.call("complete_after_close", billing.command(INVOICE, OWNER, "complete", fixture.key()),
                       expect_refusal=True)

    disabled = Fixture(enabled=False)
    await disabled.call("disabled_fund", disabled.billing.fund_capital(1000, disabled.key()), expect_refusal=True)
    await disabled.call("disabled_summary", disabled.billing.summary())

    return {
        "description": "The sandbox billing state machine: fund, quote, accept, reserve, settle, "
                       "publish, complete, refund, release — with its replays and refusals.",
        "now": NOW,
        "calls": fixture.calls,
        "rows": await fixture.rows(),
        "disabledCalls": disabled.calls,
        "disabledRows": await disabled.rows(),
    }


async def _immediate(value):
    return value


if __name__ == "__main__":
    raise SystemExit(golden_main(build, GOLDEN, description=__doc__))
