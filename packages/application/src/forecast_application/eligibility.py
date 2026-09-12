"""Receipt-level, append-only enforcement of independently reviewed evidence time.

Published instants are inclusive cutoffs. Observation bounds never prove that an
older entry preceded publication. Restoring a pre-cutoff stake may require funds
already spent elsewhere; the account then remains frozen until it can be restored.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from forecast_domain.early_resolution import EarlyResolutionTrigger, loads_forecast
from forecast_domain.lifecycle import Command, CommandReceipt, DomainEvent, SubmitForecast
from forecast_domain.serialization import content_hash, dumps, loads

from .database import Database, Statement
from .errors import AppError

POLICY_VERSION = "evidence-cutoff-v1"
PAGE_SIZE = 100


def receipt_status(submitted_at: int, cutoff_at: int, time_basis: str) -> str:
    """Classify time evidence without interpreting ordinary news as a result."""
    if type(submitted_at) is not int or type(cutoff_at) is not int or min(submitted_at, cutoff_at) < 0:
        raise ValueError("Receipt timestamps must be nonnegative integers")
    if time_basis not in {"published_instant", "observed_upper_bound"}:
        raise ValueError("Unknown evidence time basis")
    return "void" if submitted_at >= cutoff_at else "eligible" if time_basis == "published_instant" else "review"


class ForecastEligibility:
    def __init__(self, db: Database, now_ms: Callable[[], int], random_token: Callable[[], str]):
        self.db, self.now_ms, self.random_token = db, now_ms, random_token

    async def _validate(self, trigger: EarlyResolutionTrigger) -> None:
        if type(trigger) is not EarlyResolutionTrigger:
            raise AppError(400, "invalid_eligibility_trigger", "A validated evidence trigger is required.")
        trigger.validate()
        row = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (trigger.forecast_id,))
        if not row:
            raise AppError(404, "forecast_not_found", "Forecast not found.")
        trigger.validate_for(loads_forecast(row["snapshot"]).specification)
        if trigger.qualified_at_ms > self.now_ms():
            raise AppError(409, "early_trigger_changed", "Evidence review has not completed.")
        for evidence in trigger.evidence:
            retained = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (evidence.content_sha256,))
            if not retained or hashlib.sha256(retained["body"].encode()).hexdigest() != evidence.content_sha256:
                raise AppError(503, "early_evidence_unavailable", "The original official evidence could not be verified.")

    async def _decide(self, trigger: EarlyResolutionTrigger) -> None:
        await self._validate(trigger)
        old = await self.db.first("SELECT id,body FROM forecast_eligibility_decisions WHERE forecast_id=?", (trigger.forecast_id,))
        if old:
            if old["id"] != trigger.trigger_hash or old["body"] != dumps(trigger):
                raise AppError(409, "early_trigger_changed", "This forecast already has a different evidence cutoff.")
            return
        now, guard = self.now_ms(), self.random_token()
        await self.db.batch([
            ("INSERT INTO mutation_guards(token,valid) SELECT ?, ( CASE WHEN NOT EXISTS(SELECT 1 FROM forecasts "
             "WHERE id=? AND state IN ('FINALIZED','ARCHIVED')) AND NOT EXISTS(SELECT 1 FROM reputation_scores WHERE forecast_id=?) "
             "AND NOT EXISTS(SELECT 1 FROM point_ledger WHERE forecast_id=? AND kind='settlement') "
             "AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=? AND id!=?) THEN 1 ELSE 0 END )",
             (guard, trigger.forecast_id, trigger.forecast_id, trigger.forecast_id, trigger.forecast_id, trigger.trigger_hash)),
            ("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'early-resolution-trigger',?,'application/json',?)",
             (trigger.trigger_hash, dumps(trigger), now)),
            ("INSERT OR IGNORE INTO forecast_eligibility_decisions(id,forecast_id,specification_hash,cutoff_at,event_time_basis,created_at,body) "
             "VALUES(?,?,?,?,?,?,?)", (trigger.trigger_hash, trigger.forecast_id, trigger.specification_hash,
                                      trigger.event_at_ms, trigger.event_time_basis, now, dumps(trigger))),
            ("INSERT OR IGNORE INTO forecast_timing_reviews(forecast_id,specification_hash,trigger_hash,event_at,event_time_basis,created_at) "
             "VALUES(?,?,?,?,?,?)", (trigger.forecast_id, trigger.specification_hash, trigger.trigger_hash,
                                    trigger.event_at_ms, trigger.event_time_basis, now)),
            ("DELETE FROM mutation_guards WHERE token=?", (guard,)),
        ])
        actual = await self.db.first("SELECT id FROM forecast_eligibility_decisions WHERE forecast_id=?", (trigger.forecast_id,))
        if not actual or actual["id"] != trigger.trigger_hash:
            raise AppError(409, "early_trigger_changed", "This forecast already has a different evidence cutoff.")

    async def _receipts(self, trigger: EarlyResolutionTrigger) -> bool:
        rows = await self.db.all(
            "SELECT e.revision,e.hash,e.event,e.created_at,c.command_id,c.receipt FROM events e LEFT JOIN command_receipts c "
            "ON c.forecast_id=e.forecast_id AND c.command_id=json_extract(e.event,'$.command_id') "
            "WHERE e.forecast_id=? AND json_extract(e.event,'$.command_name')='submit_forecast' "
            "AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=? AND r.revision=e.revision) "
            "ORDER BY e.revision LIMIT ?", (trigger.forecast_id, trigger.trigger_hash, PAGE_SIZE))
        for row in rows:
            if not row["receipt"]:
                raise AppError(409, "eligibility_history_review", "An accepted receipt is missing; participation remains on hold.")
            receipt, event = loads(CommandReceipt, row["receipt"]), loads(DomainEvent, row["event"])
            choice = receipt.accepted_user_forecast
            if choice is None:
                raise AppError(409, "eligibility_history_review", "Accepted forecast history is incomplete.")
            command = Command(idempotency_key=receipt.idempotency_key, expected_revision=receipt.revision-1,
                              payload=SubmitForecast(user_forecast=choice))
            valid = (event.forecast_id == receipt.forecast_id == choice.forecast_id == trigger.forecast_id
                     and event.specification_hash == choice.specification_hash == trigger.specification_hash
                     and content_hash(event) == row["hash"] == receipt.event_hash
                     and event.revision == receipt.revision == row["revision"]
                     and event.command_id == receipt.idempotency_key == row["command_id"]
                     and receipt.command_hash == content_hash(command)
                     and event.occurred_at_ms == receipt.accepted_at_ms == choice.submitted_at_ms == row["created_at"]
                     and event.artifact_hash == content_hash(choice))
            artifact = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (content_hash(choice),))
            history = await self.db.first("SELECT user_id,body,created_at FROM forecast_history WHERE forecast_id=? AND revision=?",
                                          (trigger.forecast_id, receipt.revision))
            if not valid or not artifact or artifact["body"] != dumps(choice) or not history or (
                    history["user_id"] != choice.forecaster_id or history["body"] != dumps(choice)
                    or history["created_at"] != choice.submitted_at_ms):
                raise AppError(409, "eligibility_history_review", "Accepted forecast history did not pass integrity checks.")
            await self.db.execute(
                "INSERT OR IGNORE INTO forecast_receipt_eligibility(decision_id,forecast_id,user_id,revision,receipt_hash,status,body,submitted_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (trigger.trigger_hash, trigger.forecast_id, choice.forecaster_id, receipt.revision,
                 content_hash(receipt), receipt_status(choice.submitted_at_ms, trigger.event_at_ms, trigger.event_time_basis),
                 dumps(choice), choice.submitted_at_ms))
        return len(rows) < PAGE_SIZE

    async def _adjust(self, trigger: EarlyResolutionTrigger, user: str) -> None:
        if await self.db.first("SELECT id FROM point_eligibility_adjustments WHERE decision_id=? AND user_id=?", (trigger.trigger_hash, user)):
            return
        rows = await self.db.all("SELECT * FROM forecast_receipt_eligibility WHERE decision_id=? AND user_id=? ORDER BY revision",
                                 (trigger.trigger_hash, user))
        raw = await self.db.first("SELECT * FROM user_forecasts WHERE forecast_id=? AND user_id=?", (trigger.forecast_id, user))
        if not rows or not raw or raw["revision"] != rows[-1]["revision"] or raw["body"] != rows[-1]["body"]:
            raise AppError(409, "eligibility_history_review", "The current forecast does not match its accepted history.")
        ledgers = await self.db.all("SELECT l.*,c.receipt AS command_receipt FROM point_ledger l LEFT JOIN command_receipts c "
            "ON c.forecast_id=l.forecast_id AND json_extract(c.receipt,'$.revision')=l.forecast_revision "
            "WHERE l.forecast_id=? AND l.user_id=? AND l.kind='reservation' ORDER BY l.forecast_revision", (trigger.forecast_id, user))
        ledger_by_revision = {row["forecast_revision"]: row for row in ledgers}
        position = await self.db.first("SELECT * FROM point_positions WHERE forecast_id=? AND user_id=?", (trigger.forecast_id, user))
        if len(ledgers) != len(ledger_by_revision) or (position and not ledgers) or (ledgers and not position):
            raise AppError(409, "eligibility_history_review", "The original stake history is incomplete.")
        first_reservation = ledgers[0]["forecast_revision"] if ledgers else None
        if any(item["revision"] not in ledger_by_revision and first_reservation is not None
               and item["revision"] >= first_reservation for item in rows):
            raise AppError(409, "eligibility_history_review", "The original stake history has a missing reservation.")
        if any(revision not in {item["revision"] for item in rows} for revision in ledger_by_revision):
            raise AppError(409, "eligibility_history_review", "A stake reservation has no accepted forecast receipt.")
        old_stake = 0
        for item in rows:
            ledger = ledger_by_revision.get(item["revision"])
            if ledger:
                choice = json.loads(item["body"])
                receipt = loads(CommandReceipt, ledger["command_receipt"]) if ledger["command_receipt"] else None
                expected_request = hashlib.sha256(json.dumps([user, trigger.forecast_id, ledger["stake"], choice["outcome"], item["revision"]],
                                                             separators=(",", ":")).encode()).hexdigest()
                if (receipt is None or receipt.idempotency_key != "user:" + str(ledger["operation_id"])
                        or ledger["id"] != "reservation:" + hashlib.sha256(json.dumps([user, ledger["operation_id"]], separators=(",", ":")).encode()).hexdigest()
                        or ledger["outcome"] != choice["outcome"] or ledger["created_at"] != item["submitted_at"]
                        or ledger["request_hash"] != expected_request or ledger["available_delta"] != old_stake-ledger["stake"]
                        or ledger["committed_delta"] != ledger["stake"]-old_stake):
                    raise AppError(409, "eligibility_history_review", "The original stake receipt did not pass integrity checks.")
                old_stake = ledger["stake"]
        if position and (position["status"] not in {"practice", "committed"} or position["amount"] != old_stake
                         or position["forecast_revision"] != rows[-1]["revision"] or position["outcome"] != raw["outcome"]):
            raise AppError(409, "eligibility_history_review", "The current stake does not match its original receipt.")
        preserved = [row for row in rows if row["status"] in {"eligible", "review"}]
        target = preserved[-1] if preserved else rows[-1]
        target_amount = ledger_by_revision[target["revision"]]["stake"] if preserved and target["revision"] in ledger_by_revision else 0
        account = await self.db.first("SELECT available,committed FROM point_accounts WHERE user_id=?", (user,))
        if not account or account["available"] + old_stake-target_amount < 0:
            return  # Do not write a zero/refund substitute; the unresolved account stays frozen.
        delta = old_stake-target_amount
        await self.db.execute(
            "INSERT OR IGNORE INTO point_eligibility_adjustments(id,decision_id,forecast_id,user_id,old_amount,new_amount,old_revision,new_revision,"
            "outcome,available_delta,committed_delta,available_after,committed_after,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("eligibility:"+content_hash([trigger.trigger_hash, user]), trigger.trigger_hash, trigger.forecast_id, user,
             old_stake, target_amount, position["forecast_revision"] if position else 0, target["revision"],
             json.loads(target["body"])["outcome"], delta, -delta, account["available"]+delta, account["committed"]-delta, self.now_ms()))

    async def apply(self, trigger: EarlyResolutionTrigger) -> dict[str, Any]:
        """Classify at most 100 receipts and adjust 100 accounts per retryable call."""
        try:
            await self._decide(trigger)
        except AppError:
            raise
        except Exception as exc:
            finalized = await self.db.first("SELECT id FROM forecasts WHERE id=? AND (state IN ('FINALIZED','ARCHIVED') "
                "OR EXISTS(SELECT 1 FROM reputation_scores WHERE forecast_id=forecasts.id) "
                "OR EXISTS(SELECT 1 FROM point_ledger WHERE forecast_id=forecasts.id AND kind='settlement'))", (trigger.forecast_id,))
            if finalized:
                raise AppError(409, "eligibility_already_settled", "This forecast already finalized and needs an audited correction review.") from exc
            raise
        if await self.db.first("SELECT decision_id FROM forecast_eligibility_completions WHERE decision_id=?", (trigger.trigger_hash,)):
            return await self.status(trigger.forecast_id)
        if not await self._receipts(trigger):
            return await self.status(trigger.forecast_id)
        users = await self.db.all("SELECT DISTINCT r.user_id FROM forecast_receipt_eligibility r WHERE r.decision_id=? "
            "AND NOT EXISTS(SELECT 1 FROM point_eligibility_adjustments a WHERE a.decision_id=r.decision_id AND a.user_id=r.user_id) LIMIT ?",
            (trigger.trigger_hash, PAGE_SIZE))
        for user in users:
            try:
                await self._adjust(trigger, user["user_id"])
            except Exception:
                if not await self.db.first("SELECT id FROM point_eligibility_adjustments WHERE decision_id=? AND user_id=?",
                                           (trigger.trigger_hash, user["user_id"])):
                    raise
        return await self.status(trigger.forecast_id)

    def completion_sql(self, trigger: EarlyResolutionTrigger) -> Statement:
        return ("INSERT OR IGNORE INTO forecast_eligibility_completions(decision_id,created_at) VALUES(?,?)",
                (trigger.trigger_hash, self.now_ms()))

    async def finish(self, trigger: EarlyResolutionTrigger) -> dict[str, Any]:
        """Run after active-market corrections; database guards reject partial work."""
        await self._validate(trigger)
        decision = await self.db.first("SELECT id FROM forecast_eligibility_decisions WHERE forecast_id=?", (trigger.forecast_id,))
        if not decision or decision["id"] != trigger.trigger_hash:
            raise AppError(409, "early_trigger_changed", "The evidence cutoff changed.")
        state = await self.status(trigger.forecast_id)
        if state["status"] == "complete":
            return state
        try:
            await self.db.execute(*self.completion_sql(trigger))
        except Exception:
            # No completion receipt is written on conflict, ambiguous time, missing
            # stake funds or uncorrected market fills. A later retry can finish.
            return await self.status(trigger.forecast_id)
        return await self.status(trigger.forecast_id)

    async def status(self, forecast_id: str, user_id: str | None = None) -> dict[str, Any]:
        decision = await self.db.first("SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?", (forecast_id,))
        result: dict[str, Any] = {"status": "none", "cutoffAt": None, "timeBasis": None,
            "publishedEvidenceUrl": None, "policyVersion": POLICY_VERSION, "personal": None}
        if not decision:
            return result
        complete = await self.db.first("SELECT decision_id FROM forecast_eligibility_completions WHERE decision_id=?", (decision["id"],))
        review = await self.db.first("SELECT revision FROM forecast_receipt_eligibility WHERE decision_id=? AND status='review' LIMIT 1", (decision["id"],))
        result.update(status="complete" if complete else "review" if review else "pending", cutoffAt=decision["cutoff_at"],
                      timeBasis=decision["event_time_basis"], publishedEvidenceUrl=json.loads(decision["body"])["evidence"][0]["url"])
        if user_id:
            rows = await self.db.all("SELECT revision,status FROM forecast_receipt_eligibility WHERE decision_id=? AND user_id=? ORDER BY revision",
                                     (decision["id"], user_id))
            adjustment = await self.db.first("SELECT available_delta FROM point_eligibility_adjustments WHERE decision_id=? AND user_id=?",
                                             (decision["id"], user_id))
            raw = await self.db.first("SELECT revision FROM user_forecasts WHERE forecast_id=? AND user_id=?", (forecast_id, user_id))
            unclassified = raw is not None and not any(row["revision"] == raw["revision"] for row in rows)
            eligible = [row["revision"] for row in rows if row["status"] == "eligible"]
            voids = [row["revision"] for row in rows if row["status"] == "void"]
            personal = "review" if unclassified or any(row["status"] == "review" for row in rows) else (
                "restored" if eligible and voids else "eligible" if eligible else "void" if voids else "none")
            result["personal"] = {"status": personal, "voidedRevisions": voids,
                "effectiveRevision": eligible[-1] if eligible else None,
                "refundedPoints": max(0, adjustment["available_delta"]) if adjustment else 0,
                "adjustmentPending": unclassified or bool(rows) and not adjustment}
        return result
