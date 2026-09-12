"""Persistent nonbillable accounting: full refunds never consume service capital."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path

from forecast_application.database import SQLiteDatabase
from forecast_application.errors import AppError
from forecast_application.service_billing import SandboxServiceBilling

ROOT = Path(__file__).resolve().parents[1]
OWNER = "a" * 64
SCOPE = "b" * 64


class RacingDatabase(SQLiteDatabase):
    def __init__(self, connection):
        super().__init__(connection)
        self.racing = False
        self.arrivals = 0
        self.ready = asyncio.Event()
        self.lose_ack = False

    async def first(self, sql, params=()):
        result = await super().first(sql, params)
        if self.racing and sql.startswith("SELECT * FROM service_billing_sandbox_capital"):
            self.arrivals += 1
            if self.arrivals == 2:
                self.racing = False
                self.ready.set()
            await self.ready.wait()
        return result

    async def batch(self, statements):
        result = await super().batch(statements)
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError("Simulated acknowledgement loss after commit")
        return result


class ServiceBillingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        # Lane is independently deployable; no point or domain schema dependencies.
        self.connection.executescript((ROOT / "apps/web/migrations/0009_service_billing.sql").read_text())
        self.db = RacingDatabase(self.connection)
        self.now = 1_000_000
        self.service = SandboxServiceBilling(self.db, lambda: self.now, enabled=True)
        self.index = 0

    async def asyncTearDown(self):
        self.connection.close()

    def key(self):
        self.index += 1
        return f"sandbox:operation-{self.index}"

    async def quote(self, invoice="sandbox:invoice", **overrides):
        params = {"price_cents": 200, "cost_cap_cents": 115, "max_attempts": 3,
                  "expires_at": self.now + 1000, "refund_until": self.now + 10_000}
        return await self.service.quote(invoice, OWNER, SCOPE, **(params | overrides), key=self.key())

    async def fund(self, amount=115):
        return await self.service.fund_capital(amount, self.key())

    async def accept(self, invoice="sandbox:invoice", reference="sandbox:receipt", key=None):
        return await self.service.accept_sandbox_receipt(invoice, OWNER, reference=reference,
            amount_cents=200, scope_hash=SCOPE, key=key or self.key())

    async def active(self):
        await self.fund()
        await self.quote()
        return await self.accept()

    async def event(self, action, **payload):
        return await self.service.command("sandbox:invoice", OWNER, action, self.key(), **payload)

    async def work(self, cap=40, actual=30):
        await self.event("start_attempt", attemptId="sandbox:attempt", capCents=cap)
        return await self.event("settle_attempt", attemptId="sandbox:attempt", actualCents=actual)

    async def test_disabled_by_default_and_estimate_explicitly_nonbillable(self):
        disabled = SandboxServiceBilling(self.db, lambda: self.now)
        with self.assertRaises(AppError) as error:
            await disabled.fund_capital(115, self.key())
        self.assertEqual(error.exception.code, "billing_sandbox_disabled")
        estimate = disabled.estimate()
        self.assertFalse(estimate["billable"])
        self.assertEqual(estimate["requiredAllocatedAssetsCents"], 315)
        self.assertEqual(estimate["spendableAtPublicationCents"], 0)
        self.assertEqual((await disabled.summary())["availableCapitalCents"], 0)

    async def test_principal_cannot_fund_its_own_failure_envelope(self):
        await self.quote()
        with self.assertRaises(AppError):
            await self.accept()
        invoice = await self.service.invoice("sandbox:invoice", OWNER)
        self.assertEqual(invoice["phase"], "QUOTED")
        self.assertEqual(invoice["assetsCents"], 0)
        await self.fund(114)
        with self.assertRaises(AppError):
            await self.accept()
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 114)

    async def test_accept_funds_p_plus_v_and_publication_releases_nothing(self):
        active = await self.active()
        self.assertEqual((active["assetsCents"], active["principalCents"], active["remainingCostCents"]), (315, 200, 115))
        await self.work()
        published = await self.event("publish", scopeHash=SCOPE)
        self.assertEqual(published["assetsCents"], 285)
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 0)
        with self.assertRaises(AppError):
            await self.event("release")

    async def test_exact_prepublication_refund_retains_spent_operator_loss(self):
        await self.active()
        await self.work()
        requested = await self.event("request_refund")
        self.assertEqual(requested["principalCents"], 200)
        refunded = await self.event("finish_refund")
        self.assertEqual((refunded["assetsCents"], refunded["principalCents"], refunded["remainingCostCents"]), (85, 0, 85))
        closed = await self.event("release")
        self.assertEqual(closed["paymentState"], "SANDBOX_REFUNDED")
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 85)

    async def test_refund_processing_fee_uses_held_operator_envelope(self):
        await self.active()
        await self.work(100, 100)
        await self.event("request_refund")
        with self.assertRaises(AppError):
            await self.event("start_attempt", attemptId="sandbox:unwanted-work", capCents=1)
        await self.event("start_refund_attempt", attemptId="sandbox:refund-fee", capCents=15)
        await self.event("finish_refund")
        await self.event("mark_uncertain", attemptId="sandbox:refund-fee")
        with self.assertRaises(AppError):
            await self.event("release")
        await self.event("settle_attempt", attemptId="sandbox:refund-fee", actualCents=15)
        await self.event("release")
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 0)

    async def test_postpublication_refund_preserves_completion_duties(self):
        await self.active()
        await self.event("publish", scopeHash=SCOPE)
        await self.event("request_refund")
        refunded = await self.event("finish_refund")
        self.assertEqual(refunded["phase"], "PUBLISHED_SERVICING")
        self.assertEqual(refunded["remainingCostCents"], 115)
        with self.assertRaises(AppError):
            await self.event("release")
        await self.work(115, 115)
        await self.event("complete")
        await self.event("release")
        summary = await self.service.summary()
        self.assertEqual((summary["availableCapitalCents"], summary["spentCents"]), (0, 115))

    async def test_completed_service_refund_keeps_principal_until_refund_ack(self):
        await self.active()
        await self.work(115, 115)
        await self.event("publish", scopeHash=SCOPE)
        await self.event("complete")
        pending = await self.event("request_refund")
        self.assertEqual((pending["assetsCents"], pending["principalCents"]), (200, 200))
        with self.assertRaises(AppError):
            await self.event("release")
        refunded = await self.event("finish_refund")
        self.assertEqual(refunded["assetsCents"], 0)

    async def test_earned_only_after_completion_refund_window_and_reconciliation(self):
        await self.active()
        await self.work(115, 115)
        await self.event("publish", scopeHash=SCOPE)
        await self.event("complete")
        with self.assertRaises(AppError):
            await self.event("release")
        self.now += 10_000
        earned = await self.event("release")
        self.assertEqual(earned["paymentState"], "SANDBOX_EARNED")
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 200)
        # 115 original operator capital + 85 net contribution, not 200 profit.
        with self.assertRaises(AppError):
            await self.event("request_refund")

    async def test_uncertain_cost_is_never_released_even_after_refund(self):
        await self.active()
        await self.event("start_attempt", attemptId="sandbox:attempt", capCents=115)
        await self.event("mark_uncertain", attemptId="sandbox:attempt")
        await self.event("request_refund")
        await self.event("finish_refund")
        self.now += 100_000
        with self.assertRaises(AppError):
            await self.event("release")
        invoice = await self.service.invoice("sandbox:invoice", OWNER)
        self.assertEqual(invoice["assetsCents"], 115)
        self.assertEqual(invoice["attempts"]["sandbox:attempt"]["state"], "uncertain")
        await self.event("settle_attempt", attemptId="sandbox:attempt", actualCents=115)
        await self.event("release")
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 0)

    async def test_finite_attempt_and_cost_budgets_include_unknown_bills(self):
        await self.active()
        await self.event("start_attempt", attemptId="sandbox:a", capCents=100)
        await self.event("mark_uncertain", attemptId="sandbox:a")
        with self.assertRaises(AppError):
            await self.event("start_attempt", attemptId="sandbox:b", capCents=16)
        with self.assertRaises(AppError):
            await self.event("settle_attempt", attemptId="sandbox:a", actualCents=101)
        await self.event("settle_attempt", attemptId="sandbox:a", actualCents=0)
        for label in ("b", "c"):
            await self.event("start_attempt", attemptId="sandbox:" + label, capCents=1)
            await self.event("settle_attempt", attemptId="sandbox:" + label, actualCents=0)
        with self.assertRaises(AppError):
            await self.event("start_attempt", attemptId="sandbox:d", capCents=1)

    async def test_pending_attempt_blocks_complete_and_its_reservation_survives(self):
        await self.active()
        await self.event("publish", scopeHash=SCOPE)
        await self.event("start_attempt", attemptId="sandbox:a", capCents=100)
        with self.assertRaises(AppError):
            await self.event("complete")
        self.assertEqual((await self.service.invoice("sandbox:invoice", OWNER))["remainingCostCents"], 115)

    async def test_invoice_payment_scope_owner_expiry_and_amount_guards(self):
        await self.fund()
        await self.quote()
        for updates in ({"owner_hash": "c" * 64}, {"scope_hash": "c" * 64}, {"amount_cents": 199}):
            params = {"owner_hash": OWNER, "scope_hash": SCOPE, "amount_cents": 200,
                      "reference": "sandbox:receipt", "key": self.key()} | updates
            with self.assertRaises(AppError):
                await self.service.accept_sandbox_receipt("sandbox:invoice", **params)
        self.now += 1000
        with self.assertRaises(AppError):
            await self.accept()
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 115)

    async def test_idempotency_is_exact_and_lost_commit_ack_is_recovered(self):
        key = self.key()
        self.db.lose_ack = True
        first = await self.service.fund_capital(115, key)
        self.assertEqual(first, await self.service.fund_capital(115, key))
        with self.assertRaises(AppError):
            await self.service.fund_capital(116, key)
        await self.quote()
        key = self.key()
        self.db.lose_ack = True
        accepted = await self.accept(key=key)
        self.assertEqual(accepted, await self.accept(key=key))
        self.assertEqual((await self.service.summary())["assetsCents"], 315)

    async def test_receipt_cannot_fund_two_invoices(self):
        await self.fund(230)
        await self.quote()
        await self.quote("sandbox:second")
        await self.accept()
        with self.assertRaises(AppError):
            await self.accept("sandbox:second")
        summary = await self.service.summary()
        self.assertEqual((summary["availableCapitalCents"], summary["principalCents"]), (115, 200))
        self.assertEqual((await self.service.invoice("sandbox:second", OWNER))["phase"], "QUOTED")

    async def test_concurrent_admissions_cannot_double_reserve_capital(self):
        await self.fund()
        await self.quote()
        await self.quote("sandbox:second")
        self.db.racing = True
        results = await asyncio.gather(self.accept(), self.accept("sandbox:second", "sandbox:second-receipt"), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, AppError) for result in results), 1)
        summary = await self.service.summary()
        self.assertEqual((summary["availableCapitalCents"], summary["principalCents"], summary["remainingCostCents"]), (0, 200, 115))

    async def test_concurrent_same_invoice_replay_returns_original_once(self):
        await self.fund()
        await self.quote()
        self.db.racing = True
        key = self.key()
        results = await asyncio.gather(self.accept(key=key), self.accept(key=key))
        self.assertEqual(results[0], results[1])
        self.assertEqual((await self.service.summary())["assetsCents"], 315)

    async def test_concurrent_operator_funding_retries_without_lost_credit(self):
        self.db.racing = True
        keys = [self.key(), self.key()]
        results = await asyncio.gather(*(self.service.fund_capital(115, key) for key in keys), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, AppError) for result in results), 1)
        for key in keys:
            await self.service.fund_capital(115, key)
        self.assertEqual((await self.service.summary())["availableCapitalCents"], 230)

    async def test_integer_bounds_real_receipts_and_unsupported_state_rejected(self):
        for invalid in (True, -1, 1.5, "115", 1_000_000_001):
            with self.assertRaises(AppError):
                await self.service.fund_capital(invalid, self.key())
        await self.active()
        for action in ("charge", "real_paid", "refund_sol"):
            with self.assertRaises(AppError):
                await self.event(action)
        with self.assertRaises(AppError):
            await self.service.accept_sandbox_receipt("sandbox:invoice", OWNER, reference="solana:real-tx",
                amount_cents=200, scope_hash=SCOPE, key=self.key())
        with self.assertRaises(AppError):
            await self.event("publish", scopeHash=SCOPE, realPaid=True)

    async def test_sql_protects_audit_receipts_and_nonnegative_liabilities(self):
        await self.active()
        for sql in ("DELETE FROM service_billing_sandbox_audit", "UPDATE service_billing_sandbox_audit SET action='paid'",
                    "DELETE FROM service_billing_sandbox_receipts", "UPDATE service_billing_sandbox_capital SET available_cents=-1",
                    "UPDATE service_billing_sandbox_invoices SET body=json_set(body,'$.assetsCents',199)",
                    "UPDATE service_billing_sandbox_invoices SET body=json_set(body,'$.billable',1)",
                    "UPDATE service_billing_sandbox_invoices SET body=json_remove(body,'$.assetsCents')",
                    "UPDATE service_billing_sandbox_invoices SET body=json_set(body,'$.priceCents',201)"):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.execute(sql)
        self.assertEqual((await self.service.summary())["assetsCents"], 315)

    async def test_invoice_reader_owner_guard_and_no_domain_point_mutations(self):
        self.connection.execute("CREATE TABLE point_accounts (owner TEXT, points INTEGER)")
        self.connection.execute("INSERT INTO point_accounts VALUES ('sentinel',1000)")
        await self.active()
        await self.work()
        await self.event("request_refund")
        await self.event("finish_refund")
        await self.event("release")
        self.assertEqual((await self.db.first("SELECT points FROM point_accounts"))["points"], 1000)
        with self.assertRaises(AppError):
            await self.service.invoice("sandbox:invoice", "c" * 64)
        records = await self.db.all("SELECT result FROM service_billing_sandbox_audit")
        self.assertTrue(all(json.loads(row["result"])["billable"] is False for row in records))

    async def test_failed_commands_leave_entire_accounting_unchanged(self):
        await self.active()
        before = await self.service.invoice("sandbox:invoice", OWNER)
        capital = await self.service.summary()
        for action, payload in (("finish_refund", {}), ("complete", {}), ("publish", {"scopeHash": "c" * 64}),
                                ("release", {}), ("settle_attempt", {"attemptId": "sandbox:unknown", "actualCents": 2})):
            with self.assertRaises(AppError):
                await self.event(action, **payload)
            self.assertEqual(await self.service.invoice("sandbox:invoice", OWNER), before)
            self.assertEqual(await self.service.summary(), capital)
