"""Fail closed when ordinary resolution evidence may predate participation.

Publication metadata is only a conservative admission check, never an attestation
that an article determined the outcome. Ambiguous or older evidence needs a
separate adjudicated cutoff; replacing it with a newer article cannot clear review.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from forecast_domain.early_resolution import EarlyResolution
from forecast_domain.lifecycle import Forecast
from forecast_domain.models import Resolution

from .database import Database, Statement
from .errors import AppError
from .source_watch import article_content
from .sources import Artifact


def _review() -> AppError:
    return AppError(409, "resolution_timing_review",
                    "The publication time of resolution evidence needs review before results or points can be finalized.")


class ResolutionTiming:
    def __init__(self, db: Database, now_ms: Callable[[], int]):
        self.db, self.now_ms = db, now_ms

    async def _completed(self, forecast_id: str, specification_hash: str) -> bool:
        return bool(await self.db.first(
            "SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id "
            "WHERE d.forecast_id=? AND d.specification_hash=?", (forecast_id, specification_hash)))

    async def check(self, forecast: Forecast, resolution: Resolution,
                    artifacts: Sequence[Artifact] = ()) -> None:
        if resolution.forecast_id != forecast.forecast_id or resolution.specification_hash != forecast.specification_hash:
            raise _review()
        resolution.validate_for(forecast.specification)
        if isinstance(resolution, EarlyResolution):
            return
        if await self._completed(forecast.forecast_id, forecast.specification_hash):
            return
        if await self.db.first("SELECT 1 FROM resolution_timing_reviews WHERE forecast_id=? AND specification_hash=?",
                               (forecast.forecast_id, forecast.specification_hash)):
            raise _review()
        latest = await self.db.first(
            "SELECT MAX(at) AS at FROM ("
            "SELECT submitted_at AS at FROM user_forecasts WHERE forecast_id=? UNION ALL "
            "SELECT created_at AS at FROM events WHERE forecast_id=? AND json_extract(event,'$.command_name')='submit_forecast' UNION ALL "
            "SELECT json_extract(receipt,'$.accepted_at_ms') AS at FROM command_receipts WHERE forecast_id=? "
            "AND json_extract(receipt,'$.accepted_user_forecast') IS NOT NULL UNION ALL "
            "SELECT f.created_at AS at FROM market_fills f JOIN point_markets m ON m.forecast_id=f.forecast_id "
            "WHERE f.forecast_id=? AND m.mode='active')", (forecast.forecast_id,)*4)
        if latest is None or latest["at"] is None:
            return
        last_at = latest["at"]
        now = self.now_ms()
        supplied: dict[str, list[Artifact]] = {}
        for artifact in artifacts:
            supplied.setdefault(artifact.content_hash, []).append(artifact)
        retained: list[Statement] = []
        proof: list[dict[str, Any]] = []
        reasons: list[str] = []
        candidates: list[int] = []
        verified = {item.evidence_hash: item.verified for item in resolution.source_verifications}
        for evidence in resolution.evidence:
            rows = supplied.get(evidence.content_sha256, [])
            stored = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (evidence.content_sha256,))
            bodies = [item.body for item in rows] + ([stored["body"]] if stored else [])
            valid = bool(bodies) and all(type(body) is str and hashlib.sha256(body.encode()).hexdigest()
                                        == evidence.content_sha256 for body in bodies)
            publication, precision, at = None, "unknown", None
            if not valid:
                reason = "evidence_unavailable" if not bodies else "evidence_integrity"
            elif not verified.get(evidence.evidence_hash):
                reason = "evidence_unverified"
            else:
                _, publication, precision = article_content(bodies[0])
                if publication is None or precision != "instant":
                    reason = "publication_time_unknown"
                else:
                    at = int(datetime.fromisoformat(publication.replace("Z", "+00:00")).timestamp()*1000)
                    if at < 0 or at > min(now, evidence.collected_at_ms):
                        reason = "publication_time_inconsistent"
                    elif type(last_at) is not int or last_at < 0:
                        reason = "receipt_time_inconsistent"
                    elif at <= last_at:
                        reason = "evidence_may_predate_participation"
                        candidates.append(at)
                    else:
                        reason = "after_last_receipt"
                if rows:
                    retained.append(("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) "
                                     "VALUES(?,'resolution-timing-evidence',?,?,?)",
                                     (evidence.content_sha256, bodies[0], rows[0].media_type, now)))
            proof.append({"contentHash": evidence.content_sha256, "url": evidence.url,
                          "hashVerified": valid, "publication": publication, "precision": precision,
                          "publishedAt": at, "reason": reason})
            if reason != "after_last_receipt":
                reasons.append(reason)
        if not reasons:
            return
        body = json.dumps({"schemaVersion": 1, "forecastId": forecast.forecast_id,
                           "specificationHash": forecast.specification_hash, "resolutionHash": resolution.resolution_hash,
                           "lastReceiptAt": last_at, "evidence": proof}, sort_keys=True, separators=(",", ":"))
        retained.append(("INSERT OR IGNORE INTO resolution_timing_reviews(forecast_id,specification_hash,resolution_hash,"
                         "reason,last_receipt_at,candidate_cutoff_at,proof_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (forecast.forecast_id, forecast.specification_hash, resolution.resolution_hash, reasons[0],
                          last_at, min(candidates) if candidates else None, hashlib.sha256(body.encode()).hexdigest(), body, now)))
        await self.db.batch(retained)
        raise _review()

    async def status(self, forecast_id: str, user_id: str | None = None) -> dict[str, Any]:
        row = await self.db.first("SELECT * FROM resolution_timing_reviews WHERE forecast_id=? ORDER BY created_at,resolution_hash LIMIT 1",
                                  (forecast_id,))
        if row is None:
            return {"status": "none"}
        complete = await self._completed(forecast_id, row["specification_hash"])
        return {"status": "complete" if complete else "review", "reason": row["reason"],
                "candidateCutoffAt": row["candidate_cutoff_at"], "proofHash": row["proof_hash"]}
