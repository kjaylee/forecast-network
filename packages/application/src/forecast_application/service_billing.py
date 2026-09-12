"""Explicitly nonbillable service-reserve sandbox, isolated from forecasting points.

No method verifies or accepts real payments. Caller opt-in enables test accounting
only; production billing requires separate authenticated payment/fulfillment adapters.
Integer cents represent hypothetical USD assets and never user-spendable balances.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from .database import Database, Statement
from .errors import AppError

MAX_CENTS = 1_000_000_000
MAX_ATTEMPTS = 100
MAX_QUOTE_AGE_MS = 3_600_000


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _integer(value: Any, maximum: int = MAX_CENTS, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AppError(400, "billing_input_invalid", "A bounded integer is required.")
    return int(value)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sandbox:[A-Za-z0-9:_-]{1,160}", value):
        raise AppError(400, "billing_sandbox_reference_required", "Use a sandbox-only reference.")
    return value


def _commitment(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise AppError(400, "billing_input_invalid", "A SHA-256 commitment is required.")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AppError(409, "billing_state_conflict", message)


class SandboxServiceBilling:
    """Persistent simulation: principal P plus separately funded cost envelope V.

    All operations require a sandbox idempotency key. Replays return the original
    result; a changed request with the same key fails. Atomic compare-and-swap on
    the shared capital row prevents concurrent invoices reserving the same funds.
    """

    def __init__(self, db: Database, now_ms: Callable[[], int], *, enabled: bool = False):
        self.db, self.now_ms, self.enabled = db, now_ms, enabled

    @staticmethod
    def estimate(price_cents: int = 200, cost_cap_cents: int = 115) -> dict[str, Any]:
        price, cap = _integer(price_cents, minimum=1), _integer(cost_cap_cents, minimum=1)
        return {"mode": "sandbox", "billable": False, "currency": "USD",
                "priceCents": price, "costCapCents": cap,
                "requiredOperatorCapitalCents": cap, "requiredAllocatedAssetsCents": price + cap,
                "maximumEventualContributionCents": price, "contributionAtCostCapCents": price - cap,
                "spendableAtPublicationCents": 0,
                "notice": "Simulation only. No payment is requested or accepted."}

    async def summary(self) -> dict[str, Any]:
        capital = await self.db.first("SELECT * FROM service_billing_sandbox_capital WHERE singleton=1")
        if capital is None:
            raise AppError(503, "billing_unavailable", "Sandbox storage is unavailable.")
        rows = await self.db.all("SELECT body FROM service_billing_sandbox_invoices")
        invoices = [json.loads(row["body"]) for row in rows]
        return {"mode": "sandbox", "billable": False, "enabled": self.enabled,
                "availableCapitalCents": capital["available_cents"], "invoiceCount": len(invoices),
                **{key: sum(invoice[key] for invoice in invoices) for key in
                   ("assetsCents", "principalCents", "remainingCostCents", "spentCents")}}

    async def invoice(self, invoice_id: str, owner_hash: str) -> dict[str, Any]:
        row = await self.db.first("SELECT * FROM service_billing_sandbox_invoices WHERE id=? AND owner_hash=?",
                                  (_identifier(invoice_id), _commitment(owner_hash)))
        if row is None:
            raise AppError(404, "billing_invoice_not_found", "Sandbox invoice not found.")
        result: dict[str, Any] = json.loads(row["body"])
        return result

    async def _replay(self, key: str, request_hash: str) -> dict[str, Any] | None:
        row = await self.db.first("SELECT request_hash,result FROM service_billing_sandbox_audit WHERE operation_key=?", (key,))
        if row is None:
            return None
        _require(row["request_hash"] == request_hash, "Idempotency key was used for a different request.")
        result: dict[str, Any] = json.loads(row["result"])
        return result

    async def _mutate(self, action: str, key: str, payload: dict[str, Any],
                      change: Callable[[dict[str, Any] | None, int], tuple[dict[str, Any], int, list[Statement]]],
                      invoice_id: str | None = None, owner_hash: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise AppError(503, "billing_sandbox_disabled", "Billing is disabled; no payment is accepted.")
        _identifier(key)
        if invoice_id is not None:
            _identifier(invoice_id)
        if owner_hash is not None:
            _commitment(owner_hash)
        request_hash = _hash({"action": action, "invoiceId": invoice_id, "ownerHash": owner_hash, "payload": payload})
        replay = await self._replay(key, request_hash)
        if replay is not None:
            return replay
        capital = await self.db.first("SELECT * FROM service_billing_sandbox_capital WHERE singleton=1")
        if capital is None:
            raise AppError(503, "billing_unavailable", "Sandbox storage is unavailable.")
        existing = (await self.db.first("SELECT * FROM service_billing_sandbox_invoices WHERE id=?", (invoice_id,))
                    if invoice_id is not None else None)
        if action != "quote" and invoice_id is not None:
            if existing is None or existing["owner_hash"] != owner_hash:
                raise AppError(404, "billing_invoice_not_found", "Sandbox invoice not found.")
        # A competing request can commit while the separate snapshot reads await.
        replay = await self._replay(key, request_hash)
        if replay is not None:
            return replay
        before: dict[str, Any] | None = json.loads(existing["body"]) if existing else None
        result, available, extra = change(before, capital["available_cents"])
        _integer(available)
        statements: list[Statement] = [(
            "INSERT INTO service_billing_sandbox_guards SELECT ?,COUNT(*) FROM service_billing_sandbox_capital WHERE singleton=1 AND version=?",
            (key, capital["version"]))]
        if existing is not None:
            statements.append(("INSERT INTO service_billing_sandbox_guards SELECT ?,COUNT(*) FROM service_billing_sandbox_invoices WHERE id=? AND version=?",
                               (key + ":invoice", invoice_id, existing["version"])))
        if invoice_id is not None:
            self._invariant(result)
            if existing is None:
                statements.append(("INSERT INTO service_billing_sandbox_invoices VALUES (?,?,?,?,0)",
                                   (invoice_id, result["ownerHash"], result["scopeHash"], _json(result))))
            else:
                statements.append(("UPDATE service_billing_sandbox_invoices SET body=?,version=version+1 WHERE id=?", (_json(result), invoice_id)))
        statements += extra + [
            ("UPDATE service_billing_sandbox_capital SET available_cents=?,version=version+1 WHERE singleton=1", (available,)),
            ("INSERT INTO service_billing_sandbox_audit VALUES (?,?,?,?,?,?)", (key, request_hash, action, invoice_id, _json(result), self.now_ms())),
            ("DELETE FROM service_billing_sandbox_guards WHERE operation_key IN (?,?)", (key, key + ":invoice"))]
        try:
            await self.db.batch(statements)
        except Exception as error:
            replay = await self._replay(key, request_hash)
            if replay is not None:
                return replay
            raise AppError(409, "billing_state_conflict", "Sandbox accounting changed. Retry with the same key.") from error
        return result

    @staticmethod
    def _invariant(invoice: dict[str, Any]) -> None:
        for field in ("assetsCents", "principalCents", "remainingCostCents", "spentCents"):
            _integer(invoice[field], maximum=MAX_CENTS * 2)
        _require(invoice["mode"] == "sandbox" and invoice["billable"] is False, "Real billing is unavailable.")
        _require(invoice["assetsCents"] >= invoice["principalCents"] + invoice["remainingCostCents"], "Unfunded service liability.")
        held = sum(a["capCents"] for a in invoice["attempts"].values() if a["state"] != "settled")
        _require(held <= invoice["remainingCostCents"], "Provider reservations exceed the cost envelope.")

    async def fund_capital(self, amount_cents: int, key: str) -> dict[str, Any]:
        amount = _integer(amount_cents, minimum=1)
        def change(before: dict[str, Any] | None, available: int) -> tuple[dict[str, Any], int, list[Statement]]:
            return {"mode": "sandbox", "billable": False, "availableCapitalCents": available + amount}, available + amount, []
        return await self._mutate("fund", key, {"amountCents": amount}, change)

    async def quote(self, invoice_id: str, owner_hash: str, scope_hash: str, *, price_cents: int,
                    cost_cap_cents: int, max_attempts: int, expires_at: int, refund_until: int,
                    key: str) -> dict[str, Any]:
        price, cap = _integer(price_cents, minimum=1), _integer(cost_cap_cents, minimum=1)
        attempts = _integer(max_attempts, MAX_ATTEMPTS, minimum=1)
        _commitment(scope_hash)
        _integer(expires_at, 9_000_000_000_000_000)
        _integer(refund_until, 9_000_000_000_000_000)
        payload = {"scopeHash": scope_hash, "priceCents": price, "costCapCents": cap,
                   "maxAttempts": attempts, "expiresAt": expires_at, "refundUntil": refund_until}
        def change(before: dict[str, Any] | None, available: int) -> tuple[dict[str, Any], int, list[Statement]]:
            _require(before is None, "Invoice already exists.")
            _require(self.now_ms() < expires_at <= self.now_ms() + MAX_QUOTE_AGE_MS and refund_until >= expires_at,
                     "Quote expiry or refund window is invalid.")
            return {**payload, "id": invoice_id, "ownerHash": owner_hash, "mode": "sandbox", "billable": False,
                    "currency": "USD", "phase": "QUOTED", "paymentState": "UNFUNDED", "assetsCents": 0,
                    "principalCents": 0, "remainingCostCents": 0, "spentCents": 0, "attempts": {}}, available, []
        return await self._mutate("quote", key, payload, change, invoice_id, owner_hash)

    async def accept_sandbox_receipt(self, invoice_id: str, owner_hash: str, *, reference: str,
                                     amount_cents: int, scope_hash: str, key: str) -> dict[str, Any]:
        _identifier(reference)
        _integer(amount_cents, minimum=1)
        _commitment(scope_hash)
        def change(value: dict[str, Any] | None, available: int) -> tuple[dict[str, Any], int, list[Statement]]:
            if value is None:
                raise AppError(404, "billing_invoice_not_found", "Sandbox invoice not found.")
            _require(value["phase"] == "QUOTED" and self.now_ms() < value["expiresAt"], "Quote expired or already funded.")
            _require(scope_hash == value["scopeHash"] and amount_cents == value["priceCents"], "Receipt does not match the accepted quote.")
            _require(available >= value["costCapCents"], "Separate operator cost capital is exhausted.")
            value.update(phase="ACCEPTED_COMPILING", paymentState="SANDBOX_FUNDED", principalCents=amount_cents,
                         remainingCostCents=value["costCapCents"], assetsCents=amount_cents + value["costCapCents"])
            return value, available - value["costCapCents"], [("INSERT INTO service_billing_sandbox_receipts VALUES (?,?)", (reference, invoice_id))]
        return await self._mutate("accept_sandbox_receipt", key, {"reference": reference, "amountCents": amount_cents, "scopeHash": scope_hash}, change, invoice_id, owner_hash)

    async def command(self, invoice_id: str, owner_hash: str, action: str, key: str,
                      **payload: Any) -> dict[str, Any]:
        """Sandbox event dispatcher. Exact payload keys are required for every action.

        start_attempt(attemptId, capCents), start_refund_attempt(attemptId, capCents),
        mark_uncertain(attemptId),
        settle_attempt(attemptId, actualCents), publish(scopeHash), complete(),
        request_refund(), finish_refund(), release(). No event charges a provider.
        """
        required = {"start_attempt": {"attemptId", "capCents"},
                    "start_refund_attempt": {"attemptId", "capCents"}, "mark_uncertain": {"attemptId"},
                    "settle_attempt": {"attemptId", "actualCents"}, "publish": {"scopeHash"},
                    "complete": set(), "request_refund": set(), "finish_refund": set(), "release": set()}
        if action not in required or set(payload) != required[action]:
            raise AppError(400, "billing_input_invalid", "Unknown sandbox command or fields.")
        if "attemptId" in payload:
            _identifier(payload["attemptId"])
        if "capCents" in payload:
            _integer(payload["capCents"], minimum=1)
        if "actualCents" in payload:
            _integer(payload["actualCents"])
        if "scopeHash" in payload:
            _commitment(payload["scopeHash"])

        def change(value: dict[str, Any] | None, available: int) -> tuple[dict[str, Any], int, list[Statement]]:
            if value is None:
                raise AppError(404, "billing_invoice_not_found", "Sandbox invoice not found.")
            _require(value["phase"] not in {"QUOTED", "CLOSED"}, "Invoice is not active.")
            attempts = value["attempts"]
            pending = any(a["state"] != "settled" for a in attempts.values())
            if action in {"start_attempt", "start_refund_attempt"}:
                if action == "start_refund_attempt":
                    _require(value["paymentState"] == "REFUND_PENDING", "No pending refund-processing duty.")
                else:
                    _require(value["phase"] in {"ACCEPTED_COMPILING", "PUBLISHED_SERVICING"}, "Service work is complete.")
                    _require(value["phase"] == "PUBLISHED_SERVICING" or value["paymentState"] == "SANDBOX_FUNDED", "Unpublished refund has no service work.")
                _require(payload["attemptId"] not in attempts and len(attempts) < value["maxAttempts"], "Attempt limit reached or duplicate attempt.")
                held = sum(a["capCents"] for a in attempts.values() if a["state"] != "settled")
                _require(payload["capCents"] <= value["remainingCostCents"] - held, "Cost envelope exhausted.")
                attempts[payload["attemptId"]] = {"capCents": payload["capCents"], "state": "reserved", "actualCents": None}
            elif action in {"mark_uncertain", "settle_attempt"}:
                attempt = attempts.get(payload["attemptId"])
                _require(attempt is not None and attempt["state"] != "settled", "No outstanding attempt reservation.")
                if action == "mark_uncertain":
                    attempt["state"] = "uncertain"
                else:
                    _require(payload["actualCents"] <= attempt["capCents"], "Provider cost exceeds authorized reservation.")
                    attempt.update(state="settled", actualCents=payload["actualCents"])
                    value["assetsCents"] -= payload["actualCents"]
                    value["remainingCostCents"] -= payload["actualCents"]
                    value["spentCents"] += payload["actualCents"]
            elif action == "publish":
                _require(value["phase"] == "ACCEPTED_COMPILING" and value["paymentState"] == "SANDBOX_FUNDED", "Invoice cannot publish.")
                _require(payload["scopeHash"] == value["scopeHash"], "Creator approval must match the immutable scope.")
                value["phase"] = "PUBLISHED_SERVICING"
            elif action == "complete":
                _require(value["phase"] == "PUBLISHED_SERVICING" and not pending, "Outstanding work or uncertain bills remain.")
                value["phase"] = "COMPLETED_REFUND_WINDOW"
            elif action == "request_refund":
                _require(value["paymentState"] == "SANDBOX_FUNDED", "Refund already requested or paid.")
                value["paymentState"] = "REFUND_PENDING"
                if value["phase"] == "ACCEPTED_COMPILING":
                    value["phase"] = "COMPLETED_REFUND_WINDOW"
            elif action == "finish_refund":
                _require(value["paymentState"] == "REFUND_PENDING", "No pending refund.")
                value["assetsCents"] -= value["principalCents"]
                value["principalCents"] = 0
                value["paymentState"] = "SANDBOX_REFUNDED"
            elif action == "release":
                _require(value["phase"] == "COMPLETED_REFUND_WINDOW" and not pending, "Completion duties or uncertain charges remain.")
                _require(value["paymentState"] == "SANDBOX_REFUNDED" or
                         (value["paymentState"] == "SANDBOX_FUNDED" and self.now_ms() >= value["refundUntil"]),
                         "Refund exposure has not ended.")
                available += value["assetsCents"]
                value["releasedCents"] = value["assetsCents"]
                value.update(assetsCents=0, principalCents=0, remainingCostCents=0, phase="CLOSED")
                if value["paymentState"] == "SANDBOX_FUNDED":
                    value["paymentState"] = "SANDBOX_EARNED"
            return value, available, []
        return await self._mutate(action, key, payload, change, invoice_id, owner_hash)
