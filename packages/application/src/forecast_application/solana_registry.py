"""Durable authority-attested Devnet history, with finalized-account acknowledgement.

Full off-chain records remain in D1. Transport is injected and owns bounded RPC,
transaction signing and fee policy. No RPC acceptance is represented as finality.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from forecast_domain import Command, Forecast, content_hash, create_forecast, dumps, loads
from forecast_domain import lifecycle as domain
from forecast_domain.early_resolution import (
    CommandV2,
    EarlyResolution,
    EarlyResolutionTrigger,
    ForecastV2,
    LockEarly,
    ProposeEarlyResolution,
    apply_early_command,
    loads_forecast,
)
from forecast_domain.models import Dispute, DisputeReview, Resolution, ValidationAssessment

from .database import Database, Statement

DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
CHALLENGE_MS = 48 * 3_600_000
LEASE_MS = 120_000
ZERO = bytes(32)


class RegistryError(ValueError):
    """Fixed operational code, never provider responses or credentials."""


@dataclass(frozen=True)
class RegistryAccount:
    address: bytes
    owner: bytes
    data: bytes
    slot: int
    finalized: bool


class RegistryTransport(Protocol):
    async def genesis_hash(self) -> str: ...
    async def account(self, address: bytes) -> RegistryAccount | None: ...
    async def send(self, instruction: bytes, forecast_address: bytes, *, register: bool) -> str: ...
    async def signature_finalized(self, signature: str) -> bool: ...
    async def finalized_time_ms(self) -> int: ...


def registry_intent_sql(forecast: Forecast) -> list[Statement]:
    """Append after the event INSERT in the same aggregate CAS transaction."""
    if forecast.published_at_ms is None:
        return []
    event = forecast.latest_event
    if event is None or forecast.audit_head_hash != content_hash(event):
        raise RegistryError("invalid_local_event")
    return [("INSERT INTO registry_intents(forecast_id,revision,event_hash,snapshot,created_at) "
             "VALUES(?,?,?,?,?)", (forecast.forecast_id, forecast.revision,
                                  content_hash(event), dumps(forecast), event.occurred_at_ms))]


def registry_enable_sql(forecast_id: str) -> Statement:
    return ("INSERT INTO registry_forecasts(forecast_id) VALUES(?) "
            "ON CONFLICT(forecast_id) DO UPDATE SET enabled=1", (forecast_id,))


async def reserve_daily_spend(db: Database, amount: int, now_ms: int, limit_lamports: int) -> None:
    """Conservatively reserve before signing; uncertain outcomes never refund."""
    if (any(type(value) is not int or value < 0 for value in (amount, now_ms, limit_lamports))
            or amount > limit_lamports or limit_lamports > 9_007_199_254_740_991):
        raise RegistryError("daily_budget_exhausted")
    result = await db.execute("INSERT INTO registry_spend(day,reserved_lamports) VALUES(?,?) "
        "ON CONFLICT(day) DO UPDATE SET reserved_lamports=reserved_lamports+excluded.reserved_lamports "
        "WHERE reserved_lamports<=?", (now_ms//86_400_000, amount, limit_lamports-amount))
    if result["meta"]["changes"] != 1:
        raise RegistryError("daily_budget_exhausted")


def identity_hash(kind: str, value: str) -> bytes:
    return hashlib.sha256(("forecast-network:registry:" + kind + ":v1\n" + value).encode()).digest()


class SolanaRegistry:
    def __init__(self, db: Database, transport: RegistryTransport, *, program_id: bytes,
                 relayer: bytes, now_ms: Callable[[], int], random_token: Callable[[], str]):
        if any(type(key) is not bytes or len(key) != 32 or key == ZERO
               for key in (program_id, relayer)):
            raise RegistryError("invalid_registry_configuration")
        self.db, self.transport = db, transport
        self.program_id, self.relayer = program_id, relayer
        self.now_ms, self.random_token = now_ms, random_token

    async def enable(self, forecast_id: str) -> None:
        await self.backfill(forecast_id)
        await self.db.execute(*registry_enable_sql(forecast_id))

    async def prepare_finalization(self, forecast_id: str) -> bool:
        """Refresh finalized chain evidence immediately before the local CAS."""
        from . import solana_wire as wire
        enabled = await self.db.first("SELECT enabled FROM registry_forecasts WHERE forecast_id=?", (forecast_id,))
        if not enabled or not enabled["enabled"]:
            return True
        await self._configuration()
        row = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (forecast_id,))
        if row is None:
            return False
        forecast = loads_forecast(row["snapshot"])
        if forecast.state != domain.LifecycleState.CHALLENGE:
            return False
        address, _ = wire.forecast_address(self.program_id, identity_hash("forecast", forecast_id))
        account = await self.transport.account(address)
        if account is None:
            return False
        self._checked_account(account, address)
        state = wire.decode_forecast(account.data)
        material = await self._material(forecast)
        if (state.forecast_id_hash != identity_hash("forecast", forecast_id)
                or state.creator_hash != identity_hash("creator", forecast.creator_id)
                or state.specification_hash != bytes.fromhex(forecast.specification_hash)
                or state.open_at_ms != forecast.specification.open_at_ms
                or state.close_at_ms != forecast.specification.close_at_ms
                or any(getattr(state, key) != value for key, value in material.items()
                       if key != "previous_event_hash")):
            return False
        chain_time = await self.transport.finalized_time_ms()
        if type(chain_time) is not int or not 0 <= chain_time <= self.now_ms()+60_000:
            raise RegistryError("invalid_chain_clock")
        await self.db.execute("UPDATE registry_forecasts SET confirmed_revision=?,confirmed_event_hash=?,"
            "confirmed_state=?,chain_deadline=?,chain_time=?,observed_at=? WHERE forecast_id=? AND enabled=1",
            (state.revision, state.event_hash.hex(), state.state, state.chain_finalize_not_before_ms,
             chain_time, self.now_ms(), forecast_id))
        return chain_time >= max(state.chain_finalize_not_before_ms, state.challenge_until_ms)

    async def _artifact(self, digest: str | None, kind: Any, current: Forecast) -> Any:
        if digest is None:
            raise RegistryError("history_artifact_missing")
        row = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (digest,))
        if row:
            try:
                record = loads(kind, row["body"])
            except ValueError:
                if kind is not Resolution:
                    raise RegistryError("history_artifact_invalid") from None
                record = loads(EarlyResolution, row["body"])
        else:
            records = [current.validation_assessment, current.resolution,
                       *current.disputes, *current.dispute_reviews]
            record = next((value for value in records
                           if value is not None and content_hash(value) == digest), None)
        if record is None or content_hash(record) != digest:
            raise RegistryError("history_artifact_missing")
        return record

    async def _payload(self, event: domain.DomainEvent, receipt: domain.CommandReceipt,
                       current: Forecast) -> Any:
        name = event.command_name
        simple: dict[str, Any] = {
            "begin_validation": domain.BeginValidation, "lock": domain.Lock,
            "begin_resolution": domain.BeginResolution, "retain_proposal": domain.RetainProposal,
            "escalate": domain.Escalate, "finalize": domain.Finalize, "archive": domain.Archive,
        }
        if name == "lock" and isinstance(current, ForecastV2) and event.artifact_hash:
            if current.early_trigger.trigger_hash != event.artifact_hash:
                raise RegistryError("history_trigger_mismatch")
            return LockEarly(trigger=current.early_trigger)
        if name in simple:
            return simple[name]()
        if name == "publish":
            return domain.Publish(assessment=await self._artifact(
                event.artifact_hash, ValidationAssessment, current))
        if name == "submit_forecast" and receipt.accepted_user_forecast:
            return domain.SubmitForecast(user_forecast=receipt.accepted_user_forecast)
        if name == "begin_challenge":
            # Legacy records omit the duration. This candidate is accepted only
            # if the original command receipt AND resulting event match exactly.
            return domain.BeginChallenge(duration_ms=CHALLENGE_MS)
        if name == "propose_resolution":
            resolution = await self._artifact(event.artifact_hash, Resolution, current)
            cls = ProposeEarlyResolution if isinstance(resolution, EarlyResolution) else domain.ProposeResolution
            return cls(resolution=resolution)
        if name == "submit_dispute":
            return domain.SubmitDispute(dispute=await self._artifact(event.artifact_hash, Dispute, current))
        if name == "review_dispute":
            return domain.ReviewDispute(review=await self._artifact(event.artifact_hash, DisputeReview, current))
        if name == "pause_for_provider_outage":
            pause = await self._artifact(event.artifact_hash, domain.Pause, current)
            return domain.PauseForProviderOutage(configured_providers=pause.configured_providers,
                unavailable_providers=pause.unavailable_providers, reason=pause.reason)
        if name == "resume_after_provider_recovery":
            return await self._artifact(event.artifact_hash, domain.ResumeAfterProviderRecovery, current)
        if name == "adjudicate_resolution":
            return await self._artifact(event.artifact_hash, domain.AdjudicateResolution, current)
        raise RegistryError("history_command_unrecoverable")

    async def backfill(self, forecast_id: str) -> int:
        """Reconstruct legacy history through the domain; fail without partial writes."""
        row = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (forecast_id,))
        if row is None:
            raise RegistryError("forecast_missing")
        current = loads_forecast(row["snapshot"])
        rows = await self.db.all("SELECT e.*,r.receipt FROM events e LEFT JOIN command_receipts r "
            "ON r.forecast_id=e.forecast_id AND r.command_id=json_extract(e.event,'$.command_id') "
            "WHERE e.forecast_id=? ORDER BY e.revision", (forecast_id,))
        if len(rows) != current.revision:
            raise RegistryError("history_incomplete")
        forecast = create_forecast(forecast_id=current.forecast_id, creator_id=current.creator_id,
            specification=current.specification, now_ms=current.created_at_ms)
        statements: list[Statement] = []
        for row in rows:
            if not row["receipt"]:
                raise RegistryError("history_receipt_missing")
            event = loads(domain.DomainEvent, row["event"])
            receipt = loads(domain.CommandReceipt, row["receipt"])
            payload = await self._payload(event, receipt, current)
            cls = CommandV2 if isinstance(payload, (LockEarly, ProposeEarlyResolution)) else Command
            command = cls(idempotency_key=event.command_id, expected_revision=forecast.revision, payload=payload)
            if receipt.command_hash != content_hash(command):
                raise RegistryError("history_command_mismatch")
            result = apply_early_command(forecast, command, now_ms=event.occurred_at_ms)
            if (result.receipt != receipt or result.events != (event,)
                    or row["hash"] != content_hash(event) or row["revision"] != event.revision):
                raise RegistryError("history_commitment_mismatch")
            forecast = result.forecast
            existing = await self.db.first("SELECT snapshot FROM registry_intents WHERE forecast_id=? AND revision=?",
                                           (forecast_id, forecast.revision))
            if existing:
                if existing["snapshot"] != dumps(forecast):
                    raise RegistryError("history_intent_conflict")
            else:
                statements.extend(registry_intent_sql(forecast))
        if dumps(forecast) != dumps(current):
            raise RegistryError("history_snapshot_mismatch")
        if statements:
            await self.db.batch(statements)
        return len(statements)

    async def _reputation_hash(self, forecast: Forecast) -> bytes:
        if forecast.state not in (domain.LifecycleState.FINALIZED, domain.LifecycleState.ARCHIVED):
            return ZERO
        rows = await self.db.all("SELECT r.receipt,e.event,e.hash,e.revision FROM events e "
            "LEFT JOIN command_receipts r ON r.forecast_id=e.forecast_id "
            "AND r.command_id=json_extract(e.event,'$.command_id') "
            "WHERE e.forecast_id=? AND e.revision<=? ORDER BY e.revision",
            (forecast.forecast_id, forecast.revision))
        submissions = {}
        receipt_records = {}
        previous_hash = None
        for expected_revision, row in enumerate(rows, 1):
            event = loads(domain.DomainEvent, row["event"])
            if (row["revision"] != expected_revision or event.revision != expected_revision
                    or event.forecast_id != forecast.forecast_id or content_hash(event) != row["hash"]
                    or event.previous_event_hash != previous_hash):
                raise RegistryError("reputation_history_mismatch")
            previous_hash = row["hash"]
            if event.command_name != "submit_forecast":
                continue
            if row["receipt"] is None:
                raise RegistryError("reputation_receipt_missing")
            receipt = loads(domain.CommandReceipt, row["receipt"])
            value = receipt.accepted_user_forecast
            if (value is None or receipt.event_hash != row["hash"]
                    or receipt.revision != event.revision or receipt.forecast_id != forecast.forecast_id
                    or receipt.idempotency_key != event.command_id or receipt.accepted_at_ms != event.occurred_at_ms
                    or event.artifact_hash != content_hash(value)):
                raise RegistryError("reputation_receipt_mismatch")
            command = Command(idempotency_key=event.command_id, expected_revision=event.revision-1,
                              payload=domain.SubmitForecast(user_forecast=value))
            if content_hash(command) != receipt.command_hash:
                raise RegistryError("reputation_receipt_mismatch")
            receipt_records[event.revision] = (receipt, value)
            submissions[value.forecaster_id] = value
        if len(rows) != forecast.revision or previous_hash != forecast.audit_head_hash:
            raise RegistryError("reputation_history_incomplete")
        decision = await self.db.first("SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?",
                                       (forecast.forecast_id,))
        if decision:
            from .eligibility import POLICY_VERSION, receipt_status
            trigger = loads(EarlyResolutionTrigger, decision["body"])
            trigger.validate_for(forecast.specification)
            if (trigger.trigger_hash != decision["id"] or trigger.forecast_id != forecast.forecast_id
                    or trigger.event_at_ms != decision["cutoff_at"]
                    or trigger.event_time_basis != decision["event_time_basis"]):
                raise RegistryError("eligibility_commitment_mismatch")
            if not await self.db.first("SELECT 1 FROM forecast_eligibility_completions WHERE decision_id=?",
                                       (decision["id"],)):
                raise RegistryError("eligibility_incomplete")
            classifications = await self.db.all("SELECT * FROM forecast_receipt_eligibility "
                "WHERE decision_id=? ORDER BY revision", (decision["id"],))
            if {item["revision"] for item in classifications} != set(receipt_records):
                raise RegistryError("eligibility_history_incomplete")
            submissions = {}
            commitments = []
            for item in classifications:
                receipt, value = receipt_records[item["revision"]]
                expected = receipt_status(value.submitted_at_ms, trigger.event_at_ms, trigger.event_time_basis)
                if (item["receipt_hash"] != content_hash(receipt) or item["body"] != dumps(value)
                        or item["user_id"] != value.forecaster_id or item["status"] != expected
                        or item["forecast_id"] != forecast.forecast_id or expected == "review"):
                    raise RegistryError("eligibility_receipt_mismatch")
                if expected == "eligible":
                    submissions[value.forecaster_id] = value
                commitments.append({"revision": item["revision"], "receipt_hash": item["receipt_hash"],
                                    "status": expected})
            return bytes.fromhex(content_hash({"schema_version": 2,
                "kind": "eligible_forecast_reputation", "forecast_id": forecast.forecast_id,
                "specification_hash": forecast.specification_hash,
                "resolution_hash": forecast.finalized_resolution_hash, "outcome": forecast.finalized_outcome,
                "eligibility": {"policy_version": POLICY_VERSION, "trigger_hash": trigger.trigger_hash,
                                "receipts": tuple(commitments)},
                "submissions": tuple(submissions[key] for key in sorted(submissions))}))
        return bytes.fromhex(content_hash({"schema_version": 1, "forecast_id": forecast.forecast_id,
            "specification_hash": forecast.specification_hash,
            "resolution_hash": forecast.finalized_resolution_hash,
            "outcome": forecast.finalized_outcome,
            "submissions": tuple(submissions[key] for key in sorted(submissions))}))

    async def _material(self, forecast: Forecast) -> dict[str, Any]:
        event = forecast.latest_event
        if event is None:
            raise RegistryError("invalid_local_event")
        resolution = forecast.resolution
        disputes = {"disputes": forecast.disputes, "reviews": forecast.dispute_reviews}
        return {"revision": forecast.revision, "occurred_at_ms": event.occurred_at_ms,
            "previous_event_hash": bytes.fromhex(event.previous_event_hash) if event.previous_event_hash else ZERO,
            "event_hash": bytes.fromhex(content_hash(event)), "snapshot_hash": bytes.fromhex(event.state_hash),
            "state": list(domain.LifecycleState).index(forecast.state),
            "outcome": {None: 0, "YES": 1, "NO": 2, "INVALID": 3}[
                resolution.proposed_outcome.value if resolution else None],
            "resolution_hash": bytes.fromhex(resolution.resolution_hash) if resolution else ZERO,
            "dispute_hash": bytes.fromhex(content_hash(disputes)) if forecast.disputes else ZERO,
            "reputation_hash": await self._reputation_hash(forecast),
            "trigger_hash": bytes.fromhex(forecast.early_trigger.trigger_hash) if isinstance(forecast, ForecastV2) else ZERO,
            "challenge_until_ms": forecast.challenge_until_ms or 0,
            "pending_disputes": len(forecast.disputes) - len(forecast.dispute_reviews),
            "material_disputes": sum(review.material_conflict for review in forecast.dispute_reviews)}

    def _checked_account(self, account: RegistryAccount, address: bytes) -> None:
        if (account.address != address or account.owner != self.program_id
                or account.finalized is not True or type(account.slot) is not int or account.slot < 0):
            raise RegistryError("chain_account_unverified")

    async def _configuration(self) -> None:
        from . import solana_wire as wire
        if await self.transport.genesis_hash() != DEVNET_GENESIS:
            raise RegistryError("wrong_cluster")
        address, _ = wire.config_address(self.program_id)
        account = await self.transport.account(address)
        if account is None:
            raise RegistryError("registry_uninitialized")
        self._checked_account(account, address)
        config = wire.decode_config(account.data)
        if config.relayer != self.relayer:
            raise RegistryError("relayer_mismatch")
        await self.db.execute("INSERT OR IGNORE INTO registry_deployment(singleton,program_id,genesis_hash) "
                              "VALUES(1,?,?)", (self.program_id.hex(), DEVNET_GENESIS))
        pin = await self.db.first("SELECT program_id,genesis_hash FROM registry_deployment WHERE singleton=1")
        if pin is None or pin["program_id"] != self.program_id.hex() or pin["genesis_hash"] != DEVNET_GENESIS:
            raise RegistryError("registry_deployment_mismatch")

    async def _deliver(self, row: dict[str, Any], token: str) -> bool:
        from . import solana_wire as wire
        forecast = loads_forecast(row["snapshot"])
        material = await self._material(forecast)
        if material["state"] in (10, 11) and await self.db.first(
                "SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (forecast.forecast_id,)):
            raise RegistryError("resolution_eligibility_blocked")
        if row["event_hash"] != material["event_hash"].hex():
            raise RegistryError("local_intent_mismatch")
        id_hash = identity_hash("forecast", forecast.forecast_id)
        creator_hash = identity_hash("creator", forecast.creator_id)
        address, _ = wire.forecast_address(self.program_id, id_hash)
        account = await self.transport.account(address)
        prior = await self.db.first("SELECT MAX(confirmed_slot) AS slot FROM registry_delivery "
                                    "WHERE forecast_id=? AND status='confirmed'", (forecast.forecast_id,))
        minimum_slot = prior["slot"] if prior and prior["slot"] is not None else 0
        if minimum_slot and (account is None or account.slot < minimum_slot):
            await self._release(row, token, "chain_observation_stale", self.now_ms()+30_000)
            return False
        register = forecast.latest_event is not None and forecast.latest_event.command_name == "publish"
        if account:
            self._checked_account(account, address)
            state = wire.decode_forecast(account.data)
            identity = {"forecast_id_hash": id_hash, "creator_hash": creator_hash,
                "specification_hash": bytes.fromhex(forecast.specification_hash),
                "open_at_ms": forecast.specification.open_at_ms, "close_at_ms": forecast.specification.close_at_ms}
            if any(getattr(state, key) != value for key, value in identity.items()):
                raise RegistryError("chain_identity_mismatch")
            if state.revision == forecast.revision:
                expected_pause = list(domain.LifecycleState).index(forecast.pause.previous_state) if forecast.pause else 0
                if state.paused_from != expected_pause:
                    raise RegistryError("chain_pause_mismatch")
                if any(getattr(state, key) != value for key, value in material.items()
                       if key != "previous_event_hash"):
                    raise RegistryError("chain_commitment_mismatch")
                signature = None
                if row["signature"]:
                    try:
                        if await self.transport.signature_finalized(row["signature"]):
                            signature = row["signature"]
                    except Exception:
                        pass  # Exact finalized account is proof; unknown transaction is not.
                result = await self.db.execute("UPDATE registry_delivery SET status='confirmed',confirmed_slot=?,signature=?,"
                    "lease_token=NULL,lease_until=0,error_code=NULL WHERE forecast_id=? AND revision=? AND lease_token=?",
                    (account.slot, signature, forecast.forecast_id, forecast.revision, token))
                return bool(result["meta"]["changes"] == 1)
            if state.revision != forecast.revision - 1 or state.event_hash != material["previous_event_hash"]:
                raise RegistryError("chain_revision_mismatch")
            if material["state"] == 10 and self.now_ms() < state.chain_finalize_not_before_ms:
                await self._release(row, token, "chain_challenge_pending", state.chain_finalize_not_before_ms)
                return False
        elif not register:
            raise RegistryError("chain_predecessor_missing")
        if (row["signature"] and not await self.transport.signature_finalized(row["signature"])
                and self.now_ms() < (row["submitted_at"] or 0) + 600_000):
            await self._release(row, token, "transaction_pending", self.now_ms() + 30_000)
            return False
        if register:
            data = wire.encode_register(forecast_id_hash=id_hash, creator_hash=creator_hash,
                specification_hash=bytes.fromhex(forecast.specification_hash),
                open_at_ms=forecast.specification.open_at_ms, close_at_ms=forecast.specification.close_at_ms,
                **{key: material[key] for key in ("revision", "occurred_at_ms", "event_hash", "snapshot_hash")})
        else:
            data = wire.encode_advance(**material)
        signature = await self.transport.send(data, address, register=register)
        await self.db.execute("UPDATE registry_delivery SET status='submitted',signature=?,submitted_at=?,retry_at=?,"
            "lease_token=NULL,lease_until=0,error_code=NULL WHERE forecast_id=? AND revision=? AND lease_token=?",
            (signature, self.now_ms(), self.now_ms() + 5_000, forecast.forecast_id, forecast.revision, token))
        return False

    async def _release(self, row: dict[str, Any], token: str, error: str, retry_at: int,
                       *, blocked: bool = False) -> None:
        await self.db.execute("UPDATE registry_delivery SET status=?,error_code=?,retry_at=?,"
            "lease_token=NULL,lease_until=0 WHERE forecast_id=? AND revision=? AND lease_token=?",
            ("blocked" if blocked else row["status"], error, retry_at,
             row["forecast_id"], row["revision"], token))

    async def sync(self, limit: int = 8) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise RegistryError("invalid_batch_limit")
        await self._configuration()
        rows = await self.db.all("SELECT i.*,d.status,d.signature,d.submitted_at,d.attempts FROM registry_intents i "
            "JOIN registry_delivery d USING(forecast_id,revision) JOIN registry_forecasts r ON r.forecast_id=i.forecast_id "
            "WHERE r.enabled=1 AND d.status IN ('pending','submitted') "
            "AND d.retry_at<=? AND d.lease_until<=? AND NOT EXISTS (SELECT 1 FROM registry_delivery p "
            "WHERE p.forecast_id=d.forecast_id AND p.revision<d.revision AND p.status!='confirmed') "
            "ORDER BY i.created_at,i.forecast_id,i.revision LIMIT ?", (self.now_ms(), self.now_ms(), limit))
        confirmed = 0
        for row in rows:
            token = self.random_token()
            result = await self.db.execute("UPDATE registry_delivery SET lease_token=?,lease_until=?,attempts=attempts+1 "
                "WHERE forecast_id=? AND revision=? AND lease_until<=? AND status IN ('pending','submitted')",
                (token, self.now_ms()+LEASE_MS, row["forecast_id"], row["revision"], self.now_ms()))
            if not result["meta"]["changes"]:
                continue
            try:
                confirmed += await self._deliver(row, token)
            except RegistryError as exc:
                await self._release(row, token, str(exc), 0, blocked=True)
            except Exception:
                delay = min(3_600_000, 5_000 * 2**min(row["attempts"], 10))
                await self._release(row, token, "transport_unavailable", self.now_ms()+delay,
                                    blocked=row["attempts"] >= 31)
        return {"considered": len(rows), "confirmed": confirmed}

    async def status(self, forecast_id: str) -> dict[str, Any]:
        from . import solana_wire as wire
        enabled, current, confirmed, pending, pin = [
            result["results"][0] if result["results"] else None for result in await self.db.batch((
                ("SELECT enabled FROM registry_forecasts WHERE forecast_id=?", (forecast_id,)),
                ("SELECT revision,state FROM forecasts WHERE id=?", (forecast_id,)),
                ("SELECT revision,confirmed_slot,signature FROM registry_delivery "
                 "WHERE forecast_id=? AND status='confirmed' ORDER BY revision DESC LIMIT 1", (forecast_id,)),
                ("SELECT revision,status,error_code FROM registry_delivery "
                 "WHERE forecast_id=? AND status!='confirmed' ORDER BY revision LIMIT 1", (forecast_id,)),
                ("SELECT program_id,genesis_hash FROM registry_deployment WHERE singleton=1", ())))]
        address, _ = wire.forecast_address(self.program_id, identity_hash("forecast", forecast_id))
        deployment_matches = pin is not None and pin["program_id"] == self.program_id.hex() and pin["genesis_hash"] == DEVNET_GENESIS
        if not deployment_matches:
            confirmed = None
        return {"cluster": "devnet", "programId": wire.base58_encode(self.program_id),
            "account": wire.base58_encode(address), "localRevision": current["revision"] if current else None,
            "localState": current["state"] if current else None,
            "confirmedRevision": confirmed["revision"] if confirmed else None,
            "confirmedSlot": confirmed["confirmed_slot"] if confirmed else None,
            "signature": confirmed["signature"] if confirmed else None,
            "status": "disabled" if not enabled or not enabled["enabled"] else "blocked" if pending and pending["status"] == "blocked" else
                "confirmed" if current and confirmed and current["revision"] == confirmed["revision"] else "pending",
            "pendingReason": pending["error_code"] if pending else None,
            "trust": "authority-attested commitments; upgradeable program"}
