"""Audited operator containment without changing immutable forecast rules or state."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .database import Database
from .errors import AppError, invalid


def on_hold() -> AppError:
    return AppError(409, "participation_on_hold", "Participation is on hold while newly available evidence is reviewed.")


class ParticipationHolds:
    def __init__(self, db: Database, clock: Callable[[], int], token: Callable[[], str]):
        self.db, self.clock, self.token = db, clock, token

    async def active(self, forecast_id: str) -> dict[str, Any] | None:
        row = await self.db.first("SELECT body FROM active_participation_holds WHERE forecast_id=?", (forecast_id,))
        return json.loads(row["body"]) if row else None

    async def status(self, forecast_id: str) -> dict[str, Any]:
        if not await self.db.first("SELECT id FROM forecasts WHERE id=?", (forecast_id,)):
            raise AppError(404, "forecast_not_found", "Forecast not found.")
        rows = await self.db.all("SELECT body FROM participation_hold_events WHERE forecast_id=? ORDER BY revision DESC LIMIT 100", (forecast_id,))
        latest = json.loads(rows[0]["body"]) if rows else None
        return {"revision": latest["revision"] if latest else 0,
                "hold": latest if latest and latest["action"] == "hold" else None,
                "audit": [json.loads(row["body"]) for row in rows], "auditTruncated": len(rows) == 100}

    async def change(self, forecast_id: str, body: dict[str, Any], *, dismissal_review_id: str | None = None) -> dict[str, Any]:
        """Only the authenticated administrative route may call this method."""
        fields = {"action", "expectedRevision", "expectedHoldId", "specificationHash", "reason", "evidenceUrl", "idempotencyKey"}
        if set(body) != fields or body["action"] not in ("hold", "release"):
            raise invalid()
        revision, key, url = body["expectedRevision"], body["idempotencyKey"], body["evidenceUrl"]
        if type(revision) is not int or not 0 <= revision < 9007199254740991:
            raise invalid()
        if type(key) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,120}", key):
            raise invalid()
        if body["reason"] != "known_outcome_review" or type(url) is not str or not 1 <= len(url) <= 2048:
            raise invalid()
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(ord(c) <= 32 for c in url):
                raise ValueError("Invalid evidence URL")
        except ValueError as exc:
            raise invalid() from exc
        request_hash = hashlib.sha256(json.dumps({"forecastId": forecast_id, **body}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async def prior() -> dict[str, Any] | None:
            row = await self.db.first("SELECT request_hash,body FROM participation_hold_events WHERE request_key=?", (key,))
            if row and row["request_hash"] != request_hash:
                raise AppError(409, "idempotency_conflict", "This request identifier was already used.")
            return json.loads(row["body"]) if row else None
        previous = await prior()
        if previous:
            return previous
        forecast = await self.db.first("SELECT specification_hash,state FROM forecasts WHERE id=?", (forecast_id,))
        if not forecast:
            raise AppError(404, "forecast_not_found", "Forecast not found.")
        current = await self.status(forecast_id)
        hold_id = current["hold"]["holdId"] if current["hold"] else None
        if current["revision"] != revision or body["expectedHoldId"] != hold_id or forecast["specification_hash"] != body["specificationHash"]:
            raise AppError(409, "participation_hold_changed", "The participation review changed. Refresh before taking action.")
        if (body["action"] == "hold" and (hold_id is not None or forecast["state"] != "OPEN")) or (body["action"] == "release" and hold_id is None):
            raise AppError(409, "participation_hold_changed", "The participation review changed. Refresh before taking action.")
        dismissal_checks = ""
        dismissal_params: tuple[Any, ...] = ()
        if dismissal_review_id is not None:
            if body["action"] != "release" or not re.fullmatch(r"[0-9a-f]{64}", dismissal_review_id):
                raise invalid()
            dismissal_checks = (
                "AND EXISTS(SELECT 1 FROM official_source_reviews WHERE id=? AND forecast_id=? AND specification_hash=? "
                "AND json_extract(result,'$.accepted')=0 AND json_extract(result,'$.dismissible')=1) "
                "AND NOT EXISTS(SELECT 1 FROM official_source_reviews WHERE forecast_id=? AND specification_hash=? "
                "AND id!=? AND json_extract(result,'$.dismissible') IS NOT 1) "
                "AND NOT EXISTS(SELECT 1 FROM official_watch_sources s JOIN official_watch_bindings b "
                "ON (b.source_id=s.id OR b.source_id=s.parent_id) WHERE b.forecast_id=? AND s.enabled=1 AND s.lease_until>?) "
                "AND EXISTS(SELECT 1 FROM participation_hold_events WHERE id=? AND request_key LIKE 'source-watch:%') ")
            dismissal_params = (dismissal_review_id, forecast_id, body["specificationHash"], forecast_id,
                body["specificationHash"], dismissal_review_id, forecast_id, self.clock(), hold_id)
        event_id, guard = self.token(), self.token()
        result = {"id": event_id, "forecastId": forecast_id, "revision": revision + 1,
                  "action": body["action"], "holdId": event_id if body["action"] == "hold" else hold_id,
                  "specificationHash": body["specificationHash"], "reason": body["reason"],
                  "evidenceUrl": url, "actor": "authenticated_admin", "createdAt": self.clock()}
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            await self.db.batch((
                ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN "
                 "COALESCE((SELECT MAX(revision) FROM participation_hold_events WHERE forecast_id=?),0)=? "
                 "AND EXISTS(SELECT 1 FROM forecasts WHERE id=? AND specification_hash=? AND (?='release' OR state='OPEN')) "
                 + dismissal_checks + "THEN 1 ELSE 0 END", (guard, forecast_id, revision, forecast_id, body["specificationHash"], body["action"], *dismissal_params)),
                ("INSERT INTO participation_hold_events(id,forecast_id,revision,action,hold_id,specification_hash,reason,evidence_url,"
                 "actor,request_key,request_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (event_id, forecast_id, revision+1, body["action"], result["holdId"], body["specificationHash"], body["reason"], url,
                  "authenticated_admin", key, request_hash, serialized, result["createdAt"])),
                ("DELETE FROM mutation_guards WHERE token=?", (guard,))))
        except Exception as exc:
            previous = await prior()
            if previous:
                return previous
            latest = await self.status(forecast_id)
            if latest["revision"] != revision:
                raise AppError(409, "participation_hold_changed", "The participation review changed. Refresh before taking action.") from exc
            raise AppError(503, "forecast_storage_unavailable", "The review could not be confirmed. Retry with the same request identifier.") from exc
        return result
