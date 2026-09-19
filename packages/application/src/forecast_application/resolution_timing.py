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


def _determined(determination: str) -> AppError:
    return AppError(409, "resolution_timing_determined",
                    "The publication-time review for this forecast is closed and determines "
                    f"{determination}. No other result can be finalized from this evidence.")


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
        closure = await self.db.first(
            "SELECT determination FROM resolution_timing_closures WHERE forecast_id=? AND specification_hash=?",
            (forecast.forecast_id, forecast.specification_hash))
        if closure is not None:
            # The review is closed, so the guard has stopped applying everywhere at once --
            # including in the outbox and registry, which read the blocker view rather than
            # asking this method. That is only safe because of the line below: the closure
            # admits its own determination and nothing else, so a reward still cannot be
            # credited from evidence whose publication time could not be placed.
            if resolution.proposed_outcome.value != closure["determination"]:
                raise _determined(closure["determination"])
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

    async def close_indeterminate(self, forecast: Forecast) -> bool:
        """Close a review whose own reason settles the only result the evidence supports.

        Only publication_time_unknown qualifies. The review recorded the evidence as
        authentic and its publication time as absent, and nothing that arrives later
        changes that, so no outcome can be credited from it. This writes down the
        conclusion the review already reached rather than overriding it; the closure is
        bound to that review's proof and to the exact evidence item the review itself
        marked unplaceable, so it cannot be manufactured from an unrelated review.

        Returns whether a closure is in force afterwards, including one written by a
        concurrent sweep.
        """
        specification_hash = forecast.specification_hash
        if await self.db.first("SELECT 1 FROM resolution_timing_closures WHERE forecast_id=? AND specification_hash=?",
                               (forecast.forecast_id, specification_hash)):
            return True
        if await self._completed(forecast.forecast_id, specification_hash):
            return True
        row = await self.db.first(
            "SELECT proof_hash, body FROM resolution_timing_reviews WHERE forecast_id=? AND specification_hash=?"
            " AND reason='publication_time_unknown' ORDER BY created_at,resolution_hash LIMIT 1",
            (forecast.forecast_id, specification_hash))
        if row is None:
            return False
        try:
            evidence = next(item["contentHash"] for item in json.loads(row["body"])["evidence"]
                            if item["reason"] == "publication_time_unknown")
        except (KeyError, TypeError, ValueError, StopIteration):
            # A review whose proof does not carry the reason it is filed under is not
            # something this can close; leave it for a human rather than guess.
            return False
        body = json.dumps({"schemaVersion": 1, "forecastId": forecast.forecast_id,
                           "specificationHash": specification_hash, "reviewProofHash": row["proof_hash"],
                           "determination": "INVALID", "reason": "publication_time_unknown",
                           "evidenceHash": evidence}, sort_keys=True, separators=(",", ":"))
        try:
            await self.db.batch([
                ("INSERT INTO resolution_timing_closures(forecast_id,specification_hash,review_proof_hash,"
                 "determination,reason,evidence_hash,proof_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (forecast.forecast_id, specification_hash, row["proof_hash"], "INVALID",
                  "publication_time_unknown", evidence, hashlib.sha256(body.encode()).hexdigest(), body,
                  self.now_ms()))])
        except Exception:
            # The row is keyed and immutable, so what matters is that it exists, not who
            # wrote it. Anything else -- including a validation trigger refusing the write
            # because the forecast already moved -- has to surface.
            if not await self.db.first("SELECT 1 FROM resolution_timing_closures WHERE forecast_id=? AND specification_hash=?",
                                       (forecast.forecast_id, specification_hash)):
                raise
        return True

    async def status(self, forecast_id: str, user_id: str | None = None) -> dict[str, Any]:
        row = await self.db.first("SELECT * FROM resolution_timing_reviews WHERE forecast_id=? ORDER BY created_at,resolution_hash LIMIT 1",
                                  (forecast_id,))
        if row is None:
            return {"status": "none"}
        complete = await self._completed(forecast_id, row["specification_hash"])
        result = {"status": "complete" if complete else "review", "reason": row["reason"],
                  "candidateCutoffAt": row["candidate_cutoff_at"], "proofHash": row["proof_hash"]}
        closure = await self.db.first(
            "SELECT determination FROM resolution_timing_closures WHERE forecast_id=? AND specification_hash=?",
            (forecast_id, row["specification_hash"]))
        # Additive: callers that only understand "review" keep working, and the closure is
        # reported rather than hidden inside the same word.
        return {**result, "determination": closure["determination"]} if closure is not None else result
