"""Durable use cases backed by the unchanged deterministic domain engine.

All mutations that affect a forecast serialize at its revision. The CAS guard,
snapshot, append-only event, command receipt and read-side effects share one D1
batch, including retry receipts. Provider calls run outside those transactions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from forecast_domain import (
    Command,
    ConcurrencyError,
    DomainError,
    Forecast,
    ForecastSpecification,
    IdempotencyConflict,
    UserForecast,
    apply_command,
    content_hash,
    create_forecast,
    dumps,
    loads,
    to_dict,
)
from forecast_domain.early_resolution import (
    CommandV2,
    ForecastV2,
    LockEarly,
    ProposeEarlyResolution,
    apply_early_command,
    loads_forecast,
)
from forecast_domain.lifecycle import (
    AdjudicateResolution,
    BeginChallenge,
    BeginResolution,
    BeginValidation,
    CommandPayload,
    Escalate,
    Finalize,
    LifecycleState,
    Lock,
    PauseForProviderOutage,
    ProposeResolution,
    Publish,
    ResumeAfterProviderRecovery,
    RetainProposal,
    ReviewDispute,
    SubmitDispute,
    SubmitForecast,
    TransitionResult,
)
from forecast_domain.models import (
    AIProvenance,
    Category,
    Dispute,
    DisputeReview,
    ForecastChoice,
    Resolution,
    ValidationAssessment,
)
from forecast_domain.serialization import COMMITMENT_PREFIX

from . import projections
from .auth import Authentication, public_user, text
from .automation import ForecastAutomation
from .database import Database, Statement
from .display_translations import DisplayTranslations
from .eligibility import ForecastEligibility
from .errors import AppError, conflict, invalid
from .markets import PointMarkets
from .participation_holds import ParticipationHolds, on_hold
from .points import PointsService, reservation_sql, settlement_sql
from .resolution_timing import ResolutionTiming
from .service_billing import SandboxServiceBilling
from .solana_registry import SolanaRegistry, registry_enable_sql, registry_intent_sql
from .sources import Artifact, SourceRejected, SourceUnavailable, validate_public_url

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
CHALLENGE_MS = 48 * HOUR_MS
MAX_ARTIFACT_BYTES = 524288
MAX_DAILY_AI_CALLS = 240
MAX_DAILY_USER_AI_CALLS = 10
EVIDENCE_REWARD_POINTS = 100
AI_WORKFLOW_TIMEOUT_SECONDS = 240
LEASE_MS = 300000
_T = TypeVar("_T")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _key(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,120}", value):
        raise invalid("A request identifier is required. Refresh the page and try again.")
    return value


class Application:
    def __init__(self, db: Database, ai: Any, *, now_ms: Callable[[], int],
                 token_hash: Callable[[str], str], random_token: Callable[[], str],
                 source_watch_enabled: bool = False, live_markets_enabled: bool = False,
                 billing_sandbox_enabled: bool = False, registry: SolanaRegistry | None = None):
        self.db, self.ai, self.now_ms = db, ai, now_ms
        self.registry = registry
        self.auth = Authentication(db, now_ms, token_hash, random_token)
        self.points = PointsService(db)
        self.participation_holds = ParticipationHolds(db, now_ms, random_token)
        self.eligibility = ForecastEligibility(db, now_ms, random_token)
        self.resolution_timing = ResolutionTiming(db, now_ms)
        self.random_token = self.auth.token
        self.display_translations = DisplayTranslations(db, ai, now_ms, self.random_token, self.rate_limit, self._artifact_sql)
        self.automation = ForecastAutomation(self, enabled=source_watch_enabled)
        self.markets = PointMarkets(db, now_ms, self.random_token, live_enabled=live_markets_enabled and self.automation.enabled)
        self.billing = SandboxServiceBilling(db, now_ms, enabled=billing_sandbox_enabled)
        if ai is not None:
            ai.read_artifact = self.read_artifact

    async def register(self, display_name: str) -> dict[str, Any]:
        result = await self.auth.register(display_name)
        return {**result, "points": await self.points.summary(result["user"]["id"])}

    async def login(self, recovery_code: str, context_token: str | None = None) -> dict[str, Any]:
        result = await self.auth.login(recovery_code, context_token)
        return {**result, "points": await self.points.summary(result["user"]["id"])}

    async def logout(self, session_token: str | None, context_token: str | None = None) -> dict[str, bool]:
        return await self.auth.logout(session_token, context_token)

    async def authenticate(self, session_token: str | None, context_token: str | None = None) -> dict[str, Any] | None:
        return await self.auth.authenticate(session_token, context_token)

    async def _user(self, user_id: str) -> dict[str, Any]:
        user = await self.db.first("SELECT * FROM users WHERE id=?", (user_id,))
        if user is None:
            raise AppError(401, "authentication_required", "Please sign in to continue.")
        return user

    async def eligibility_status(self, forecast_id: str, user_id: str | None = None) -> dict[str, Any]:
        result = await self.eligibility.status(forecast_id, user_id)
        if result["status"] == "none":
            timing = await self.resolution_timing.status(forecast_id, user_id)
            if timing["status"] == "review":
                result = {**result, "status": "review", "timingReview": timing}
                if user_id and await self.db.first("SELECT 1 FROM user_forecasts WHERE forecast_id=? AND user_id=?",
                                                  (forecast_id, user_id)):
                    result["personal"] = {"status": "review", "voidedRevisions": [], "effectiveRevision": None,
                                          "refundedPoints": 0, "adjustmentPending": False}
        return result

    async def _forecast(self, forecast_id: str) -> Forecast:
        row = await self.db.first("SELECT snapshot FROM forecasts WHERE id=?", (forecast_id,))
        if row is None:
            raise AppError(404, "forecast_not_found", "Forecast not found.")
        return loads_forecast(row["snapshot"])

    async def _card(self, forecast_id: str) -> dict[str, Any]:
        row = await self.db.first(projections.CARD_SQL + " WHERE f.id=?", (forecast_id,))
        if row is None:
            raise AppError(404, "forecast_not_found", "Forecast not found.")
        return projections.card(row)

    async def update_profile(self, user_id: str, display_name: str) -> dict[str, Any]:
        await self._user(user_id)
        await self.rate_limit("profile:" + user_id, 20, HOUR_MS)
        await self.db.execute("UPDATE users SET display_name=? WHERE id=?",
                              (text(display_name, 40), user_id))
        return {"user": public_user(await self._user(user_id)), "points": await self.points.summary(user_id)}

    async def me(self, user_id: str | None) -> dict[str, Any]:
        if user_id is None:
            return {"user": None, "reputation": None, "myForecasts": [], "activity": [], "points": None}
        user = await self._user(user_id)
        identity = await self.db.first("SELECT address FROM wallet_identities WHERE user_id=? AND status='active' "
                                       "AND converted_at IS NOT NULL", (user_id,))
        rows = await self.db.all(
            projections.CARD_SQL + " WHERE f.id IN (SELECT forecast_id FROM user_forecasts "
            "WHERE user_id=?) ORDER BY f.updated_at DESC LIMIT 100", (user_id,))
        accepted = await self.db.all(
            "SELECT forecast_id,body,revision FROM eligible_user_forecasts WHERE user_id=? "
            "ORDER BY submitted_at DESC LIMIT 1000", (user_id,))
        by_id = {row["forecast_id"]: row for row in accepted}
        positions = await self.points.positions(user_id, [row["id"] for row in rows])
        cards = []
        for row in rows:
            item = projections.card(row)
            choice = by_id.get(row["id"])
            if choice:
                item["myForecast"] = projections.submission(
                    loads(UserForecast, choice["body"]), choice["revision"])
            item["stake"] = positions[row["id"]]
            item["eligibility"] = await self.eligibility_status(row["id"], user_id)
            cards.append(item)
        return {"user": public_user(user), "reputation": await self.reputation(user_id),
                "authentication": {"method": "wallet" if identity else "legacy", "address": identity["address"] if identity else None},
                "myForecasts": cards, "activity": (await self.activity(user_id))["items"],
                "points": await self.points.summary(user_id)}

    async def reputation(self, user_id: str) -> dict[str, Any]:
        # Scores are computed from the latest accepted submission at finalization.
        rows = await self.db.all("SELECT * FROM eligible_reputation_scores WHERE user_id=?", (user_id,))
        result = projections.scores(rows)
        total = await self.db.first("SELECT COUNT(*) AS n FROM eligible_user_forecasts WHERE user_id=?", (user_id,))
        result["totalForecasts"] = total["n"] if total else 0
        categories = sorted({row["category"] for row in rows})
        result["domainScores"] = [{"category": category.lower(), **projections.scores(
            [row for row in rows if row["category"] == category])} for category in categories]
        result.update(totalDisputes=0, resolvedDisputes=0, successfulDisputes=0,
                      disputeAccuracy=None, creatorQuality=None)
        # Adjudication clears active dispute slots for a fresh challenge. Their
        # immutable event-linked artifacts still count in historical reputation.
        disputed = await self.db.all(
            "SELECT DISTINCT a.hash FROM events e JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash') "
            "WHERE json_extract(e.event,'$.command_name')='submit_dispute' "
            "AND json_extract(a.body,'$.disputant_id')=?",
            (user_id,))
        reviews = await self.db.all(
            "SELECT DISTINCT r.hash,r.body FROM events e "
            "JOIN artifacts r ON r.hash=json_extract(e.event,'$.artifact_hash') "
            "JOIN artifacts d ON d.hash=json_extract(r.body,'$.dispute_hash') "
            "WHERE json_extract(e.event,'$.command_name')='review_dispute' "
            "AND json_extract(d.body,'$.disputant_id')=?", (user_id,))
        result["totalDisputes"] = len(disputed)
        result["resolvedDisputes"] = len(reviews)
        result["successfulDisputes"] = sum(
            int(loads(DisputeReview, row["body"]).material_conflict) for row in reviews)
        if result["resolvedDisputes"]:
            result["disputeAccuracy"] = result["successfulDisputes"]/result["resolvedDisputes"]*100
        creator = await self.db.first(
            "SELECT COUNT(*) AS n,SUM(CASE WHEN finalized_outcome='INVALID' THEN 1 ELSE 0 END) AS invalid "
            "FROM forecasts WHERE creator_id=? AND finalized_outcome IS NOT NULL", (user_id,))
        if creator and creator["n"]:
            result["creatorQuality"] = 1-(creator["invalid"] or 0)/creator["n"]
        return result

    async def profile_card(self, user_id: str) -> dict[str, Any]:
        """INTERNAL authenticated-owner snapshot generation, not a public lookup.

        The Worker obtains user_id from the authenticated session. One prepared
        statement captures identity and all ledger-derived fields consistently.
        asOf records when that complete database read finished; it is not a claim
        that the snapshot includes mutations committed after the read began.
        """
        row = await self.db.first(projections.PROFILE_CARD_SQL, (user_id,))
        if row is None:
            raise AppError(404, "profile_not_found", "Profile not found.")
        payload = projections.profile_card_payload(row, self.now_ms())
        canonical = projections.profile_card_json(payload)
        digest = hashlib.sha256(projections.PROFILE_CARD_COMMITMENT_PREFIX+canonical.encode("utf-8")).hexdigest()
        return {**payload, "canonicalJson": canonical, "snapshotHash": digest}

    async def create_profile_card(self, user_id: str) -> dict[str, Any]:
        """Publish only after an authenticated owner's deliberate share action."""
        snapshot = await self.profile_card(user_id)
        canonical = snapshot["canonicalJson"]
        if len(canonical.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise AppError(413, "profile_card_too_large", "The shared profile record exceeds the publication limit.")
        await self.db.execute(
            "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
            (snapshot["snapshotHash"], "profile-share-snapshot", canonical, "application/json", snapshot["asOf"]))
        return await self.get_profile_card(snapshot["snapshotHash"])

    async def get_profile_card(self, snapshot_hash: str) -> dict[str, Any]:
        """Public lookup of deliberately published immutable snapshots only."""
        if type(snapshot_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash):
            raise AppError(404, "profile_card_not_found", "Shared profile record not found.")
        row = await self.db.first("SELECT body,media_type FROM artifacts WHERE hash=? AND kind='profile-share-snapshot'",
                                   (snapshot_hash,))
        if row is None:
            raise AppError(404, "profile_card_not_found", "Shared profile record not found.")
        try:
            canonical = row["body"]
            actual = hashlib.sha256(projections.PROFILE_CARD_COMMITMENT_PREFIX+canonical.encode("utf-8")).hexdigest()
            if actual != snapshot_hash or row["media_type"] != "application/json":
                raise ValueError("Snapshot commitment mismatch")
            payload = json.loads(canonical)
            expected_keys = {"schemaVersion", "asOf", "user", "metrics", "sampleStatus", "history",
                             "historyTruncated", "highlight", "methodology", "commitmentProfile"}
            if type(payload) is not dict or set(payload) != expected_keys or payload["schemaVersion"] != 1 \
                    or payload["methodology"]["version"] != "profile-card-v1" \
                    or payload["commitmentProfile"]["prefix"] != projections.PROFILE_CARD_COMMITMENT_PREFIX.decode("ascii") \
                    or projections.profile_card_json(payload) != canonical:
                raise ValueError("Snapshot encoding mismatch")
        except (TypeError, ValueError, KeyError, UnicodeError) as exc:
            raise AppError(503, "profile_card_integrity_failed", "This shared profile record failed integrity verification.") from exc
        return {**payload, "canonicalJson": canonical, "snapshotHash": snapshot_hash}

    async def list_forecasts(self, user_id: str | None = None, q: str = "", category: str = "",
                             sort: str = "trending", cursor: str | None = None) -> dict[str, Any]:
        if type(q) is not str or len(q) > 200 or type(category) is not str:
            raise invalid()
        sorts = {"trending", "newest", "ending", "ai-gap", "following"}
        if sort not in sorts:
            raise invalid()
        try:
            offset = int(cursor or "0")
        except (TypeError, ValueError) as exc:
            raise invalid() from exc
        if not 0 <= offset <= 100000:
            raise invalid()
        clauses = ["1=1"]
        params: list[Any] = []
        if q.strip():
            clauses.append("(f.question LIKE ? ESCAPE '\\' OR json_extract(t.body,'$.question') LIKE ? ESCAPE '\\' "
                           "OR json_extract(t.body,'$.title') LIKE ? ESCAPE '\\')")
            escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend(["%" + escaped + "%"]*3)
        if category and category.lower() != "all":
            try:
                value = Category(category.upper()).value
            except ValueError as exc:
                raise invalid() from exc
            clauses.append("f.category=?")
            params.append(value)
        if sort == "following":
            clauses.append("f.creator_id IN (SELECT creator_id FROM follows WHERE follower_id=?)")
            params.append(user_id or "")
        order = {
            "trending": "CASE WHEN f.state='OPEN' AND h.id IS NULL THEN 0 ELSE 1 END,"
                        "((participant_count*3+comment_count+MIN(f.share_count,20)+1)*"
                        "(1.0-json_extract(f.snapshot,'$.specification.ambiguity_score_bp')/10000.0)/"
                        "(1.0+MAX(0,?-f.created_at)/86400000.0)) DESC,f.created_at DESC",
            "newest": "f.created_at DESC", "following": "f.created_at DESC",
            "ending": "CASE WHEN f.state='OPEN' AND h.id IS NULL AND f.close_at>? THEN 0 ELSE 1 END,f.close_at ASC",
            "ai-gap": "CASE WHEN probability IS NULL OR f.ai_forecast IS NULL THEN 1 ELSE 0 END,"
                      "ABS(probability-COALESCE(json_extract(f.ai_forecast,'$.probability'),probability)) DESC",
        }[sort]
        if sort in {"ending", "trending"}:
            params.append(self.now_ms())
        # Stable across one UTC day and across page reloads, personalized when signed in.
        day = str(self.now_ms() // DAY_MS)
        rows, count_rows, daily = [result["results"] for result in await self.db.batch((
            (projections.CARD_SQL + " WHERE " + " AND ".join(clauses)
             + " ORDER BY " + order + ",f.id DESC LIMIT 31 OFFSET ?", tuple([*params, offset])),
            ("SELECT COUNT(*) AS total,SUM(CASE WHEN state='OPEN' AND open_at<=? AND close_at>? "
             "AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=forecasts.id) THEN 1 ELSE 0 END) "
             "AS active,(SELECT COUNT(DISTINCT user_id) FROM eligible_user_forecasts) AS participants FROM forecasts",
             (self.now_ms(), self.now_ms())),
            ("SELECT id FROM forecasts WHERE state='OPEN' AND open_at<=? AND close_at>? "
             "AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=forecasts.id) "
             "ORDER BY created_at DESC LIMIT 500", (self.now_ms(), self.now_ms()))))]
        counts = count_rows[0] if count_rows else None
        daily.sort(key=lambda row: hashlib.sha256((day + (user_id or "") + row["id"]).encode()).hexdigest())
        return {"items": [projections.card(row) for row in rows[:30]],
                "nextCursor": str(offset+30) if len(rows) > 30 else None,
                "counts": {"total": counts["total"] if counts else 0,
                           "active": counts["active"] or 0 if counts else 0,
                           "participants": counts["participants"] if counts else 0},
                "dailyIds": [row["id"] for row in daily[:5]]}

    async def read_artifact(self, digest: str) -> str | None:
        row = await self.db.first("SELECT body FROM artifacts WHERE hash=?", (digest,))
        return row["body"] if row else None

    def _artifact_sql(self, artifacts: Sequence[Any]) -> list[Statement]:
        result = []
        total = 0
        for artifact in artifacts:
            encoded = artifact.body.encode("utf-8")
            total += len(encoded)
            if len(encoded) > MAX_ARTIFACT_BYTES or total > 4*MAX_ARTIFACT_BYTES:
                raise AppError(413, "artifact_too_large", "The evidence exceeds the storage limit.")
            hashes = {hashlib.sha256(encoded).hexdigest()}
            if artifact.media_type == "application/json":
                try:
                    hashes.add(content_hash(json.loads(artifact.body)))
                except (ValueError, DomainError) as exc:
                    raise AppError(502, "artifact_invalid", "The AI result did not pass format validation.") from exc
            if artifact.content_hash not in hashes:
                raise AppError(502, "artifact_hash_mismatch", "The evidence did not pass integrity verification.")
            result.append(("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) "
                           "VALUES(?,?,?,?,?)", (artifact.content_hash, artifact.kind, artifact.body,
                                                artifact.media_type, self.now_ms())))
        return result

    def _record_artifact(self, record: Any, kind: str, digest: str | None = None) -> Statement:
        body = dumps(record)
        if len(body.encode()) > MAX_ARTIFACT_BYTES:
            raise AppError(413, "artifact_too_large", "The evidence exceeds the storage limit.")
        return ("INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
                (digest or content_hash(record), kind, body, "application/json", self.now_ms()))

    async def rate_limit(self, scope: str, limit: int, window_ms: int) -> None:
        """Atomic fixed-window counter. The Worker also calls this for IP limits."""
        bucket = self.now_ms() // window_ms
        # RETURNING keeps the increment and the read in one D1 round trip.
        result = await self.db.execute(
            "INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES(?,?,1,?) ON CONFLICT(scope,bucket) "
            "DO UPDATE SET count=count+1 RETURNING count", (scope, bucket, (bucket+1)*window_ms))
        rows = result.get("results") or []
        if rows and rows[0]["count"] > limit:
            raise AppError(429, "rate_limited", "Too many requests. Please try again later.")

    async def _ai_lease(self, owner: str) -> str:
        if self.ai is None:
            raise AppError(503, "ai_unavailable", "AI is not available yet. Please try again later.")
        token, now = self.random_token(), self.now_ms()
        await self.db.execute(
            "INSERT INTO ai_leases(owner,token,expires_at) VALUES(?,?,?) ON CONFLICT(owner) "
            "DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at "
            "WHERE ai_leases.expires_at<=?", (owner, token, now+LEASE_MS, now))
        lease = await self.db.first("SELECT token FROM ai_leases WHERE owner=?", (owner,))
        if lease is None or lease["token"] != token:
            raise AppError(409, "ai_work_in_progress", "Your previous AI request is still being processed.")
        try:
            await self.rate_limit("ai:global", MAX_DAILY_AI_CALLS, DAY_MS)
            if owner.startswith("user:"):
                await self.rate_limit("ai:" + owner, MAX_DAILY_USER_AI_CALLS, DAY_MS)
        except Exception:
            await self._release_ai(owner, token)
            raise
        return token

    async def _release_ai(self, owner: str, token: str) -> None:
        await self.db.execute("DELETE FROM ai_leases WHERE owner=? AND token=?", (owner, token))

    async def _bounded_ai(self, work: Awaitable[_T]) -> _T:
        """The complete workflow finishes before its durable 300-second lease.

        Provider/source timeouts remain nested limits. Cancellation propagates
        through adapters; no result arriving after the workflow cap is accepted.
        The injected wall clock also catches suspension past the lease boundary.
        """
        started = self.now_ms()
        try:
            async with asyncio.timeout(AI_WORKFLOW_TIMEOUT_SECONDS):
                result = await work
            if self.now_ms()-started >= AI_WORKFLOW_TIMEOUT_SECONDS*1000:
                raise TimeoutError("AI workflow exceeded the acceptance deadline")
            return result
        except TimeoutError as exc:
            raise AppError(504, "ai_workflow_timeout", "The AI review timed out. No result was finalized.") from exc

    async def _candidate_forecasts(self, question: str) -> list[Forecast]:
        """Rank across the corpus, then fetch only a bounded UTF-8 context.

        SQL metadata selection avoids loading hundreds of complete snapshots.
        A lexical shortlist is followed by the AI's semantic/rule comparison.
        Exact matches receive priority over recency, including archived items.
        """
        from .ai import MAX_CANDIDATE_CONTEXT_BYTES, MAX_CANDIDATES

        normalized = " ".join(question.casefold().split())
        stopwords = {"will", "the", "before", "after", "this", "that", "with", "and", "for"}
        terms = sorted({word for word in re.findall(r"[a-z0-9]{3,}|[가-힣]{2,}", normalized)
                        if word not in stopwords}, key=lambda word: (-len(word), word))[:10]
        ranking = "CASE WHEN normalized_question=? THEN 1000 ELSE 0 END"
        params: list[Any] = [normalized]
        for term in terms:
            ranking += "+CASE WHEN normalized_question LIKE ? THEN 1 ELSE 0 END"
            params.append("%" + term + "%")
        rows = await self.db.all(
            "SELECT id,length(CAST(json_extract(snapshot,'$.specification') AS BLOB)) AS spec_bytes "
            "FROM forecasts ORDER BY (" + ranking + ") DESC,created_at DESC,id LIMIT 400", tuple(params))
        chosen: list[str] = []
        reserved = 2
        for row in rows:
            # Reserve wrapper keys/IDs and commas in addition to full criteria.
            size = row["spec_bytes"] + 320
            if reserved+size > MAX_CANDIDATE_CONTEXT_BYTES:
                continue
            chosen.append(row["id"])
            reserved += size
            if len(chosen) == MAX_CANDIDATES:
                break
        if not chosen:
            return []
        records = await self.db.all("SELECT id,snapshot FROM forecasts WHERE id IN ("
                                    + ",".join("?" for _ in chosen) + ")", tuple(chosen))
        by_id = {row["id"]: loads_forecast(row["snapshot"]) for row in records}
        result: list[Forecast] = []
        size = 2
        for identifier in chosen:
            forecast = by_id[identifier]
            encoded = _json({"forecast_id": forecast.forecast_id,
                             "specification_hash": forecast.specification_hash,
                             "specification": to_dict(forecast.specification)}).encode("utf-8")
            if size+len(encoded)+1 <= MAX_CANDIDATE_CONTEXT_BYTES:
                result.append(forecast)
                size += len(encoded)+1
        return result

    async def compile_forecast(self, user_id: str, question: str) -> dict[str, Any]:
        await self._user(user_id)
        text(question, 1000, minimum=10)
        if len(question) > 1000:
            raise invalid("The original question must contain at most 1,000 characters.")
        owner = "user:" + user_id
        lease = await self._ai_lease(owner)
        try:
            candidates = await self._candidate_forecasts(question)
            result = await self._bounded_ai(self.ai.compile_question(question, candidates, self.now_ms()))
            result.assessment.require_publishable(result.specification)
            await self.automation.check_creation(result.specification)
            now = self.now_ms()
            if result.specification.close_at_ms <= now:
                raise invalid("The closing time must be in the future.")
            draft_id, expiry = "d_" + self.random_token()[:24], now+HOUR_MS
            statements = self._artifact_sql(result.artifacts)
            statements.extend((self._record_artifact(result.specification, "specification"),
                               self._record_artifact(result.assessment, "validation")))
            statements.append(("INSERT INTO drafts(id,user_id,specification,assessment,ai_forecast,"
                               "created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                               (draft_id, user_id, dumps(result.specification), dumps(result.assessment),
                                _json(result.ai_forecast) if result.ai_forecast else None, now, expiry)))
            await self.db.batch(statements)
            return {"draftId": draft_id, "specification": projections.specification(result.specification),
                    "assessment": {"publishable": True, "explanation": result.assessment.explanation,
                                   "provider": result.assessment.compiler.provider,
                                   "model": result.assessment.compiler.model},
                    "duplicateCandidates": [{"id": item.forecast_id,
                        "similarity": item.similarity_bp/10000, "explanation": item.explanation}
                        for item in result.specification.duplicate_candidates],
                    "aiForecast": result.ai_forecast, "expiresAt": expiry}
        except AppError:
            raise
        except Exception as exc:
            await self._retain_rejected(exc)
            raise self._ai_error(exc) from exc
        finally:
            await self._release_ai(owner, lease)

    @staticmethod
    def _ai_error(exc: Exception) -> AppError:
        from .ai import AIRejected, AIUnavailable
        if isinstance(exc, AIUnavailable):
            source_failure = isinstance(exc.__cause__, SourceUnavailable) or any(
                getattr(artifact, "kind", None) == "source-failure"
                for artifact in getattr(exc, "artifacts", ()))
            if source_failure:
                return AppError(503, "source_temporarily_unavailable",
                    "The official evidence page could not be reached. Try again later or specify an official text page that opens without signing in.")
            return AppError(503, "ai_unavailable", "The AI provider is unavailable. Please try again later.")
        if isinstance(exc, AIRejected):
            code = getattr(exc, "code", "ai_rejected")
            if code in {"compiler_deadline_timezone", "compiler_deadline_invalid",
                        "compiler_deadline_mismatch", "compiler_deadline_range"}:
                return AppError(422, "deadline_clarification_required",
                    "Specify the closing date, time, and time zone. The question and resolution rules must use the same deadline.")
            if code == "question_already_resolved":
                return AppError(422, "question_already_resolved", "Official evidence already answers this question. Choose an unresolved future event.")
            if code == "source_rejected":
                return AppError(502, "source_not_usable",
                    "The selected evidence page could not be read or is unsupported. Specify a shorter official text page that opens without signing in, then request another review.")
            if code in {"ai_output_size", "ai_output_json", "ai_output_type", "ai_output_enum",
                        "ai_output_fields", "ai_output_text_length", "ai_output_range",
                        "ai_output_incomplete", "compiler_domain_validation"}:
                return AppError(502, "ai_response_invalid",
                    "The AI response was incomplete or did not match the required format. Please try again later.")
            if code == "compiler_not_publishable":
                return AppError(422, "specification_needs_review",
                    "The AI review could not approve the resolution criteria. Clarify the subject, the fact to verify, and the deadline before requesting another review.")
            # Unknown codes and mixed legacy rejection cases are deliberately
            # neutral. Never echo exception text or classify them as user error.
            return AppError(502, "ai_review_incomplete",
                            "The AI review could not be completed. Please try again later.")
        return AppError(502, "ai_validation_failed", "The AI result failed validation. No result was finalized.")

    async def _retain_rejected(self, exc: Exception) -> None:
        artifacts = getattr(exc, "artifacts", ())
        if artifacts:
            await self.db.batch(self._artifact_sql(artifacts))

    async def _prior(self, user_id: str, key: str, request: dict[str, Any]) -> dict[str, Any] | None:
        row = await self.db.first("SELECT * FROM operations WHERE user_id=? AND operation_key=?",
                                   (user_id, _key(key)))
        if row and row["request_hash"] != content_hash(request):
            raise AppError(409, "idempotency_conflict", "This request identifier has already been used for different content.")
        return row

    def _operation(self, user_id: str, key: str, request: dict[str, Any],
                   forecast_id: str, result: dict[str, Any]) -> Statement:
        return ("INSERT INTO operations(user_id,operation_key,request_hash,forecast_id,result,created_at) "
                "VALUES(?,?,?,?,?,?)", (user_id, key, content_hash(request), forecast_id,
                                       _json(result), self.now_ms()))

    async def publish_forecast(self, user_id: str, draft_id: str, idempotency_key: str) -> dict[str, Any]:
        await self._user(user_id)
        draft_id = text(draft_id, 128)
        request = {"kind": "publish", "draftId": draft_id}
        prior = await self._prior(user_id, idempotency_key, request)
        if prior:
            return {"forecast": await self._card(prior["forecast_id"])}
        draft = await self.db.first("SELECT * FROM drafts WHERE id=? AND user_id=?", (draft_id, user_id))
        if draft is None:
            raise AppError(404, "draft_not_found", "Draft not found.")
        if draft["published_id"]:
            return {"forecast": await self._card(draft["published_id"])}
        now = self.now_ms()
        if draft["expires_at"] <= now:
            raise AppError(410, "draft_expired", "This draft has expired. Please submit the question for review again.")
        await self.rate_limit("publish:" + user_id, 10, DAY_MS)
        spec = loads(ForecastSpecification, draft["specification"])
        assessment = loads(ValidationAssessment, draft["assessment"])
        await self.automation.check_creation(spec)
        forecast_id = "f_" + self.random_token()[:24]
        forecast = create_forecast(forecast_id=forecast_id, creator_id=user_id,
                                   specification=spec, now_ms=draft["created_at"])
        validation = apply_command(forecast, Command(idempotency_key="begin-validation",
            expected_revision=0, payload=BeginValidation()), now_ms=now)
        published = apply_command(validation.forecast, Command(idempotency_key="publish",
            expected_revision=1, payload=Publish(assessment=assessment)), now_ms=now)
        forecast = published.forecast
        statements: list[Statement] = [
            ("INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,"
             "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,ai_forecast,"
             "mutation_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (forecast_id, user_id, draft_id, dumps(forecast), forecast.revision, forecast.state.value,
              spec.category.value, spec.share_title, spec.canonical_question,
              " ".join(spec.canonical_question.casefold().split()), spec.specification_hash,
              spec.open_at_ms, spec.close_at_ms, now, now, draft["ai_forecast"], idempotency_key)),
            ("UPDATE drafts SET published_id=? WHERE id=? AND user_id=? AND published_id IS NULL",
             (forecast_id, draft_id, user_id)),
        ]
        statements.extend(self._events(validation))
        statements.extend(self._events(published))
        if self.registry is not None:
            statements.append(registry_enable_sql(forecast_id))
        statements.extend((self._operation(user_id, idempotency_key, request, forecast_id, {}),
            ("INSERT OR IGNORE INTO activity(id,user_id,forecast_id,kind,title,body,created_at) "
             "SELECT ?||':'||follower_id,follower_id,?,'creator_published',?,?,? "
             "FROM follows WHERE creator_id=?", ("published:" + forecast_id, forecast_id,
                                                 "New forecast from a creator you follow", spec.share_title, now, user_id))))
        try:
            await self.db.batch(statements)
        except Exception as exc:
            prior = await self._prior(user_id, idempotency_key, request)
            if prior:
                return {"forecast": await self._card(prior["forecast_id"])}
            duplicate = await self.db.first(
                "SELECT id FROM forecasts WHERE specification_hash=? OR (normalized_question=? AND close_at=?)",
                (spec.specification_hash, " ".join(spec.canonical_question.casefold().split()), spec.close_at_ms))
            if duplicate:
                raise AppError(409, "duplicate_forecast", "A forecast with the same resolution criteria already exists. Please join the existing forecast.") from exc
            raise conflict() from exc
        return {"forecast": await self._card(forecast_id)}

    def _events(self, result: TransitionResult) -> list[Statement]:
        statements: list[Statement] = []
        for event in result.events:
            digest = content_hash(event)
            statements.append(("INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
                               (event.forecast_id, event.revision, digest, dumps(event), event.occurred_at_ms)))
            for effect in event.effects:
                statements.append(("INSERT OR IGNORE INTO outbox(id,forecast_id,kind,created_at) VALUES(?,?,?,?)",
                                   (digest + ":" + effect.value, event.forecast_id, effect.value, event.occurred_at_ms)))
        statements.append(("INSERT INTO command_receipts(forecast_id,command_id,receipt) VALUES(?,?,?)",
                           (result.receipt.forecast_id, result.receipt.idempotency_key, dumps(result.receipt))))
        if result.events:
            statements.extend(registry_intent_sql(result.forecast))
        return statements

    async def _mutate(self, forecast: Forecast, payload: CommandPayload, *, key: str,
                      now: int | None = None, extra: Sequence[Statement] = (),
                      job_token: str | None = None, timing_artifacts: Sequence[Artifact] = ()) -> Forecast:
        if isinstance(payload, Finalize) and self.registry is not None:
            if not await self.registry.prepare_finalization(forecast.forecast_id):
                raise AppError(503, "chain_finalization_pending",
                               "The verified Devnet challenge window must finish before finalization.")
            # The scheduler's captured time precedes the awaited chain read.
            # Commit after that attestation, without moving aggregate time back.
            now = max(self.now_ms(), forecast.updated_at_ms, now if now is not None else 0)
        try:
            command_type = CommandV2 if isinstance(payload, (LockEarly, ProposeEarlyResolution)) else Command
            result = apply_early_command(forecast, command_type(idempotency_key=key,
                expected_revision=forecast.revision, payload=payload),
                now_ms=self.now_ms() if now is None else now)
        except (ConcurrencyError, IdempotencyConflict) as exc:
            raise conflict() from exc
        except DomainError as exc:
            raise AppError(409, "transition_rejected", "This request is not allowed in the current state or time window.") from exc
        if isinstance(payload, (ProposeResolution, AdjudicateResolution, Finalize)):
            resolution = result.forecast.resolution
            if resolution is not None:
                await self.resolution_timing.check(forecast, resolution, timing_artifacts)
        changed, guard = result.forecast, self.random_token()
        snapshot = dumps(changed)
        if len(snapshot.encode("utf-8")) > 1048576:
            raise AppError(413, "forecast_too_large", "The forecast record exceeds the safe storage limit.")
        guard_sql = "SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM forecasts WHERE id=? AND revision=?"
        guard_params: tuple[Any, ...] = (guard, forecast.forecast_id, forecast.revision)
        if job_token is not None:
            guard_sql += " AND job_token=? AND job_until>?"
            guard_params += (job_token, self.now_ms())
        statements: list[Statement] = [
            ("INSERT INTO mutation_guards(token,valid) " + guard_sql + ") THEN 1 ELSE 0 END", guard_params),
            ("UPDATE forecasts SET snapshot=?,revision=?,state=?,updated_at=?,challenge_until=?,"
             "finalized_outcome=?,mutation_key=? WHERE id=? AND revision=?",
             (snapshot, changed.revision, changed.state.value, changed.updated_at_ms,
              changed.challenge_until_ms, changed.finalized_outcome.value if changed.finalized_outcome else None,
              key, forecast.forecast_id, forecast.revision)),
        ]
        statements.extend(self._events(result))
        if result.events and result.events[0].artifact_hash:
            artifact_record = changed.pause if isinstance(payload, PauseForProviderOutage) else payload
            if artifact_record is not None and content_hash(artifact_record) == result.events[0].artifact_hash:
                statements.append(self._record_artifact(artifact_record, payload.kind))
        statements.extend(extra)
        statements.append(("DELETE FROM mutation_guards WHERE token=?", (guard,)))
        try:
            await self.db.batch(statements)
        except Exception as exc:
            if "resolution_timing_review" in str(exc):
                raise AppError(409, "resolution_timing_review",
                               "Evidence publication time must be reviewed before rewards or reputation can be credited.") from exc
            if "forecast_eligibility_review" in str(exc):
                raise AppError(409, "early_eligibility_review",
                               "Receipt timing and known-result evidence must be reviewed before resolution or rewards.") from exc
            # D1 and sqlite adapters have different exception types. Re-read the
            # actual revision before classifying a constraint failure as a race.
            current = await self._forecast(forecast.forecast_id)
            if current.revision != forecast.revision or job_token is not None:
                raise conflict() from exc
            # These fixed trigger markers come from our own points migration.
            # Never expose raw database exceptions or partially accept a forecast.
            if "participation_on_hold" in str(exc):
                raise on_hold() from exc
            if "eligibility_account_hold" in str(exc):
                raise AppError(409, "point_correction_pending",
                               "A previous stake correction must finish before you can commit more points.") from exc
            if "points_insufficient_balance" in str(exc):
                raise AppError(409, "insufficient_points", "You do not have enough available points for this stake.") from exc
            if "points_position_conflict" in str(exc):
                raise AppError(409, "stake_conflict", "The stake changed or is already settled. Refresh and try again.") from exc
            if "points_operation_conflict" in str(exc):
                raise AppError(409, "idempotency_conflict", "This stake request identifier was already used.") from exc
            raise
        return changed

    async def _submission_response(self, user_id: str, forecast_id: str,
                                   receipt_result: dict[str, Any]) -> dict[str, Any]:
        eligibility = await self.eligibility_status(forecast_id, user_id)
        if eligibility["status"] != "none":
            effective = await self.db.first("SELECT body,revision FROM eligible_user_forecasts "
                "WHERE forecast_id=? AND user_id=?", (forecast_id, user_id))
            receipt_result = {**receipt_result, "originalReceipt": receipt_result.get("myForecast"),
                "myForecast": projections.submission(loads(UserForecast, effective["body"]), effective["revision"])
                if effective else None}
        return {"forecast": await self._card(forecast_id), **receipt_result,
                "points": await self.points.summary(user_id),
                "eligibility": eligibility,
                "stake": await self.points.position(user_id, forecast_id)}

    async def submit_forecast(self, user_id: str, forecast_id: str, outcome: str, confidence: int,
                              revision: int, idempotency_key: str,
                              stake_points: int | None = None) -> dict[str, Any]:
        await self._user(user_id)
        if type(outcome) is not str or outcome not in {"YES", "NO"} \
                or type(confidence) is not int or not 0 <= confidence <= 100:
            raise invalid()
        if type(revision) is not int or revision < 0:
            raise invalid()
        if stake_points is not None and (type(stake_points) is not int or not 0 <= stake_points <= 1000):
            raise AppError(400, "invalid_stake", "Choose practice with 0 points or a stake from 1 to 1,000 points.")
        request = {"kind": "forecast", "forecastId": forecast_id, "outcome": outcome,
                   "confidence": confidence, "revision": revision}
        if stake_points is not None:
            # Preserve persisted legacy request hashes when the optional field was
            # absent. Explicit practice is a new, distinct request representation.
            request["stakePoints"] = stake_points
        prior = await self._prior(user_id, idempotency_key, request)
        if prior:
            return await self._submission_response(user_id, forecast_id, json.loads(prior["result"]))
        if await self.participation_holds.active(forecast_id):
            raise on_hold()
        # Capture the CAS revision before inspecting a legacy request's hold. A
        # concurrent explicit stake must not turn a previously empty position into
        # an implicitly authorized practice release under a newer snapshot.
        forecast = await self._forecast(forecast_id)
        if forecast.revision != revision:
            raise conflict()
        if stake_points is None:
            position = await self.points.position(user_id, forecast_id)
            if position["status"] == "committed" and position["amount"] > 0:
                raise AppError(409, "stake_required", "This forecast already has a stake. Refresh and explicitly confirm the stake amount.")
        amount = 0 if stake_points is None else stake_points
        await self.rate_limit("forecast:" + user_id, 100, HOUR_MS)
        now = self.now_ms()
        choice = UserForecast(forecaster_id=user_id, forecast_id=forecast_id,
            specification_hash=forecast.specification_hash, outcome=ForecastChoice(outcome),
            confidence=confidence, submitted_at_ms=now)
        probability = confidence if outcome == "YES" else 100-confidence
        response = {"myForecast": projections.submission(choice, revision+1)}
        extra: list[Statement] = [self._record_artifact(choice, "user_forecast"),
            ("INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,"
             "submitted_at,revision,body) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(forecast_id,user_id) "
             "DO UPDATE SET outcome=excluded.outcome,confidence=excluded.confidence,"
             "yes_probability=excluded.yes_probability,submitted_at=excluded.submitted_at,"
             "revision=excluded.revision,body=excluded.body",
             (forecast_id, user_id, outcome, confidence, probability, now, revision+1, dumps(choice))),
            ("INSERT INTO forecast_history(forecast_id,revision,user_id,body,crowd_probability,"
             "participant_count,created_at) SELECT ?,?,?,?,AVG(yes_probability),COUNT(*),? "
             "FROM user_forecasts WHERE forecast_id=?",
             (forecast_id, revision+1, user_id, dumps(choice), now, forecast_id))]
        extra.extend(reservation_sql(user_id, forecast_id, amount, outcome, revision+1,
                                     content_hash({"user": user_id, "key": idempotency_key}), now))
        extra.append(self._operation(user_id, idempotency_key, request, forecast_id, response))
        try:
            await self._mutate(forecast, SubmitForecast(user_forecast=choice),
                key="user:" + content_hash({"user": user_id, "key": idempotency_key}), now=now, extra=extra)
        except Exception as exc:
            # A transport error can arrive after the D1 batch committed. The
            # durable operation receipt takes precedence over error classification.
            try:
                prior = await self._prior(user_id, idempotency_key, request)
            except AppError:
                raise
            except Exception as read_error:
                raise AppError(503, "forecast_storage_unavailable",
                               "The forecast could not be confirmed. Retry with the same request identifier.") from read_error
            if prior:
                return await self._submission_response(user_id, forecast_id, json.loads(prior["result"]))
            if isinstance(exc, AppError):
                raise
            try:
                points = await self.points.summary(user_id)
                position = await self.points.position(user_id, forecast_id)
            except Exception as read_error:
                raise AppError(503, "forecast_storage_unavailable",
                               "The forecast could not be saved. Please try again later.") from read_error
            current_hold = position["amount"] if position["status"] == "committed" else 0
            if amount > points["available"] + current_hold:
                raise AppError(409, "insufficient_points",
                               "You do not have enough available points for this stake.") from exc
            raise AppError(503, "forecast_storage_unavailable",
                           "The forecast could not be saved. Please try again later.") from exc
        return await self._submission_response(user_id, forecast_id, response)

    async def forecast_detail(self, forecast_id: str, user_id: str | None = None) -> dict[str, Any]:
        # One D1 round trip for every independent read; batch() is a single JS promise, which the
        # Workers Python runtime handles safely where concurrent Python tasks do not.
        (snapshot_rows, card_rows, job_rows, events, comments, history, own_rows,
         translation_rows) = [result["results"] for result in await self.db.batch((
            ("SELECT snapshot FROM forecasts WHERE id=?", (forecast_id,)),
            (projections.CARD_SQL + " WHERE f.id=?", (forecast_id,)),
            ("SELECT job_error,retry_at FROM forecasts WHERE id=?", (forecast_id,)),
            ("SELECT event,hash FROM events WHERE forecast_id=? ORDER BY revision DESC LIMIT 500", (forecast_id,)),
            ("SELECT c.*,u.display_name,u.handle FROM comments c JOIN users u ON u.id=c.user_id "
             "WHERE forecast_id=? ORDER BY c.created_at DESC,c.id DESC LIMIT 100", (forecast_id,)),
            ("SELECT crowd_probability,participant_count,created_at FROM forecast_history "
             "WHERE forecast_id=? AND NOT EXISTS (SELECT 1 FROM forecast_eligibility_decisions d "
             "WHERE d.forecast_id=forecast_history.forecast_id AND "
             "(d.event_time_basis='observed_upper_bound' OR forecast_history.created_at>=d.cutoff_at)) "
             "ORDER BY revision DESC LIMIT 300", (forecast_id,)),
            ("SELECT body,revision FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?",
             (forecast_id, user_id or "")),
            ("SELECT body AS display_translation,translated_at,content_hash AS translation_hash,specification_hash "
             "FROM forecast_translations WHERE forecast_id=? AND language='en' "
             "AND specification_hash=(SELECT specification_hash FROM forecasts WHERE id=?)", (forecast_id, forecast_id))))]
        if not snapshot_rows or not card_rows:
            raise AppError(404, "forecast_not_found", "Forecast not found.")
        forecast = loads_forecast(snapshot_rows[0]["snapshot"])
        item = projections.card(card_rows[0])
        job = job_rows[0] if job_rows else None
        own = own_rows[0] if own_rows else None
        translation_row = translation_rows[0] if translation_rows else None
        item.update(specification=projections.specification(forecast.specification),
                    challengeUntil=forecast.challenge_until_ms,
                    finalizedOutcome=forecast.finalized_outcome.value if forecast.finalized_outcome else None,
                    pauseReason="Resolution is paused because all configured AI providers are unavailable."
                    if forecast.pause else job["job_error"] if job else None,
                    retryAt=job["retry_at"] if job and job["retry_at"] else None)
        resolution = None
        if forecast.resolution:
            value = forecast.resolution
            resolution = {"proposedOutcome": value.proposed_outcome.value,
                "confidence": value.confidence_bp/100, "reasonSummary": value.reason_summary,
                "hash": value.resolution_hash, "reviewedAt": value.proposed_at_ms,
                "ruleMatches": list(value.rule_matches), "ruleConflicts": list(value.rule_conflicts),
                "evidence": [{"url": e.url, "hash": e.content_sha256,
                    "snapshotUri": e.snapshot_uri, "collectedAt": e.collected_at_ms} for e in value.evidence],
                "providers": [{"provider": p.provider, "model": p.model,
                    "modelVersion": p.model_version, "task": p.task.value}
                    for p in (value.judge, value.counter_judge)]}
        audit = []
        for event in reversed(events):
            raw = json.loads(event["event"])
            audit.append({"command": raw["command_name"], "oldState": raw["old_state"],
                "newState": raw["new_state"], "at": raw["occurred_at_ms"], "hash": event["hash"],
                "artifactHash": raw["artifact_hash"], "revision": raw["revision"]})
        reviews = {review.dispute_hash: review for review in forecast.dispute_reviews}
        disputes = []
        for dispute in forecast.disputes:
            review = reviews.get(dispute.dispute_hash)
            disputes.append({"id": dispute.dispute_id, "claim": dispute.claim,
                "ruleClauseId": dispute.rule_clause_id, "explanation": dispute.explanation,
                "submittedAt": dispute.submitted_at_ms, "hash": dispute.dispute_hash,
                "evidence": [{"url": e.url, "hash": e.content_sha256} for e in dispute.evidence],
                "review": {"reasonSummary": review.reason_summary,
                    "materialConflict": review.material_conflict,
                    "reviewedAt": review.reviewed_at_ms} if review else None})
        item["earlyResolution"] = self._early_projection(forecast)
        if self.registry is not None:
            item["chain"] = await self.registry.status(forecast_id)
        reports = await self.db.first(
            "SELECT COUNT(*) AS n,(SELECT status FROM evidence_reports WHERE forecast_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1) AS mine "
            "FROM evidence_reports WHERE forecast_id=?", (forecast_id, user_id or "", forecast_id))
        return {"forecast": item, "market": await self.markets.get(forecast_id), "resolution": resolution, "disputes": disputes, "audit": audit,
                "evidenceReports": {"count": reports["n"] if reports else 0, "mine": reports["mine"] if reports else None,
                                    "reward": EVIDENCE_REWARD_POINTS},
                "eligibility": await self.eligibility_status(forecast_id, user_id),
                "points": await self.points.summary(user_id) if user_id else None,
                "stake": await self.points.position(user_id, forecast_id) if user_id else None,
                "displayTranslation": projections.display_translation(translation_row) if translation_row else None,
                "auditTruncated": len(events) == 500, "historyTruncated": len(history) == 300,
                "comments": [self._comment(row) for row in comments],
                "myForecast": projections.submission(loads(UserForecast, own["body"]), own["revision"]) if own else None,
                "history": [{"at": row["created_at"], "probability": row["crowd_probability"],
                             "count": row["participant_count"]} for row in reversed(history)]}

    async def integrity(self, forecast_id: str) -> dict[str, Any]:
        """Public canonical records for local commitment recomputation.

        Read one consistent published snapshot. This is deliberately scoped to
        public specifications/resolutions, never arbitrary private artifact hashes.
        Hash integrity proves these bytes match a commitment, not chain inclusion.
        """
        row = await self.db.first(
            "SELECT snapshot FROM forecasts WHERE id=? "
            "AND json_extract(snapshot,'$.published_at_ms') IS NOT NULL", (forecast_id,))
        if row is None:
            raise AppError(404, "forecast_not_found", "Published forecast not found.")
        forecast = loads_forecast(row["snapshot"])
        resolution = None
        if forecast.resolution is not None:
            resolution = {"canonicalJson": dumps(forecast.resolution),
                          "resolutionHash": forecast.resolution.resolution_hash}
        return {
            "forecastId": forecast.forecast_id,
            "specification": {"canonicalJson": dumps(forecast.specification),
                              "specificationHash": forecast.specification_hash},
            "resolution": resolution, "revision": forecast.revision,
            "auditHead": forecast.audit_head_hash,
            "commitmentProfile": {"algorithm": "SHA-256", "encoding": "UTF-8",
                                  "prefix": COMMITMENT_PREFIX.decode("ascii"),
                                  "canonicalization": "forecast-network-canonical-json-v1"},
            "chain": await self.registry.status(forecast_id) if self.registry is not None else
                {"status": "unconnected", "network": None, "transaction": None},
        }

    async def set_translation(self, forecast_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """ADMIN-CALLER presentation update, independently audited and hash-bound.

        The adapter must authenticate the operator. This deliberately does not
        represent an AI decision or rewrite any published criteria or commitment.
        Repeating identical content preserves its timestamp; corrections append
        a new audit record while replacing only the current display translation.
        """
        expected = {"specificationHash", "title", "question", "rules", "invalidationRules",
                    "aiRationale", "sourceLanguage", "language", "attribution"}
        if type(body) is not dict or set(body) != expected:
            raise invalid("A complete display translation with the documented fields is required.")
        if body["language"] != "en" or body["sourceLanguage"] != "ko" \
                or body["attribution"] != "Forecast editorial translation":
            raise invalid("Use the English editorial translation language and attribution.")
        row = await self.db.first("SELECT snapshot,ai_forecast FROM forecasts WHERE id=? "
            "AND json_extract(snapshot,'$.published_at_ms') IS NOT NULL", (forecast_id,))
        if row is None:
            raise AppError(404, "forecast_not_found", "Published forecast not found.")
        forecast = loads_forecast(row["snapshot"])
        if body["specificationHash"] != forecast.specification_hash:
            raise AppError(409, "translation_specification_mismatch",
                           "This translation does not identify the current published specification.")
        rules = body["rules"]
        if type(rules) is not list or len(rules) != len(forecast.specification.rules) \
                or any(type(rule) is not dict or set(rule) != {"clauseId", "condition"} for rule in rules):
            raise invalid("Translate every published rule without adding fields or changing their order.")
        if [rule["clauseId"] for rule in rules] != list(forecast.specification.clause_ids):
            raise invalid("Rule identifiers and their order must match the published specification.")
        invalidations = body["invalidationRules"]
        if type(invalidations) is not list or len(invalidations) != len(forecast.specification.invalidation_rules):
            raise invalid("Translate every invalidation rule in its original order.")
        ai = json.loads(row["ai_forecast"]) if row["ai_forecast"] else None
        rationale = body["aiRationale"]
        if rationale is not None and (not ai or not ai.get("rationale")):
            raise invalid("An AI rationale translation requires an existing AI rationale.")
        cleaned: dict[str, Any] = {"specificationHash": forecast.specification_hash,
            "title": text(body["title"], 120), "question": text(body["question"], 3000),
            "rules": [{"clauseId": rule["clauseId"], "condition": text(rule["condition"], 16000)} for rule in rules],
            "invalidationRules": [text(value, 8000) for value in invalidations],
            "aiRationale": text(rationale, 8000) if rationale is not None else None,
            "sourceLanguage": "ko", "language": "en", "attribution": "Forecast editorial translation"}
        serialized = _json(cleaned)
        if len(serialized.encode("utf-8")) > 65536:
            raise invalid("The translation exceeds the 64 KiB display limit.")
        translated_text = [cleaned["title"], cleaned["question"],
                           *(rule["condition"] for rule in cleaned["rules"]), *cleaned["invalidationRules"]]
        if rationale is not None:
            translated_text.append(cleaned["aiRationale"])
        if any(re.search(r"[가-힣]", value) or not re.search(r"[A-Za-z]", value) for value in translated_text):
            raise invalid("Provide English display text for every translated field.")
        digest, now, guard = content_hash(cleaned), self.now_ms(), self.random_token()
        audit_id = self.random_token()
        await self.db.batch((
            ("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM forecasts "
             "WHERE id=? AND specification_hash=? AND json_extract(snapshot,'$.published_at_ms') IS NOT NULL) "
             "THEN 1 ELSE 0 END", (guard, forecast_id, forecast.specification_hash)),
            ("INSERT INTO forecast_translation_audit(id,forecast_id,language,specification_hash,"
             "translation_hash,body,actor,attribution,created_at) "
             "SELECT ?,?,'en',?,?,?,'authenticated_admin','Forecast editorial translation',? "
             "WHERE NOT EXISTS(SELECT 1 FROM forecast_translations WHERE forecast_id=? AND language='en' "
             "AND specification_hash=? AND content_hash=?)",
             (audit_id, forecast_id, forecast.specification_hash, digest, serialized, now,
              forecast_id, forecast.specification_hash, digest)),
            ("INSERT INTO forecast_translations(forecast_id,language,specification_hash,source_language,body,"
             "content_hash,attribution,translated_at) VALUES(?,'en',?,'ko',?,?,'Forecast editorial translation',?) "
             "ON CONFLICT(forecast_id,language,specification_hash) DO UPDATE SET body=excluded.body,"
             "content_hash=excluded.content_hash,translated_at=excluded.translated_at "
             "WHERE forecast_translations.content_hash!=excluded.content_hash",
             (forecast_id, forecast.specification_hash, serialized, digest, now)),
            ("DELETE FROM mutation_guards WHERE token=?", (guard,))))
        translation = await self.db.first(
            "SELECT body AS display_translation,translated_at,content_hash AS translation_hash,specification_hash "
            "FROM forecast_translations WHERE forecast_id=? AND language='en' AND specification_hash=?",
            (forecast_id, forecast.specification_hash))
        return {"forecast": await self._card(forecast_id),
                "displayTranslation": projections.display_translation(translation) if translation else None}

    async def submit_dispute(self, user_id: str, forecast_id: str, claim: str, evidence_url: str,
                             rule_clause_id: str, explanation: str, revision: int,
                             idempotency_key: str) -> dict[str, Any]:
        await self._user(user_id)
        claim, explanation = text(claim, 1000), text(explanation, 3000)
        evidence_url, rule_clause_id = text(evidence_url, 2000), text(rule_clause_id, 128)
        if type(revision) is not int or revision < 0:
            raise invalid()
        request = {"kind": "dispute", "forecastId": forecast_id, "claim": claim,
            "evidenceUrl": evidence_url, "ruleClauseId": rule_clause_id,
            "explanation": explanation, "revision": revision}
        prior = await self._prior(user_id, idempotency_key, request)
        if prior:
            return {"forecast": await self._card(forecast_id), **json.loads(prior["result"])}
        forecast = await self._forecast(forecast_id)
        if forecast.revision != revision:
            raise conflict()
        if forecast.state not in {LifecycleState.CHALLENGE, LifecycleState.DISPUTED} \
                or forecast.challenge_until_ms is None or forecast.challenge_until_ms <= self.now_ms():
            raise AppError(409, "challenge_closed", "The challenge window is closed.")
        if rule_clause_id not in forecast.specification.clause_ids:
            raise invalid("Select one of the published resolution rules.")
        await self.rate_limit("dispute:" + user_id, 10, DAY_MS)
        owner = "user:" + user_id
        lease = await self._ai_lease(owner)
        try:
            evidence, artifacts = await self._bounded_ai(self.ai.collect_dispute_evidence(
                forecast.specification, evidence_url, self.now_ms()))
            now = self.now_ms()
            if forecast.resolution is None:
                raise conflict()
            dispute = Dispute(dispute_id="d_" + self.random_token()[:24], disputant_id=user_id,
                forecast_id=forecast_id, specification_hash=forecast.specification_hash,
                resolution_hash=forecast.resolution.resolution_hash, claim=claim, evidence=evidence,
                rule_clause_id=rule_clause_id, explanation=explanation, submitted_at_ms=now)
            response = {"dispute": {"id": dispute.dispute_id, "claim": dispute.claim,
                "submittedAt": now, "hash": dispute.dispute_hash}}
            extra = self._artifact_sql(artifacts)
            extra.extend((self._record_artifact(dispute, "dispute"),
                          self._operation(user_id, idempotency_key, request, forecast_id, response)))
            await self._mutate(forecast, SubmitDispute(dispute=dispute), now=now,
                key="user:" + content_hash({"user": user_id, "key": idempotency_key}), extra=extra)
            return {"forecast": await self._card(forecast_id), **response}
        except AppError:
            prior = await self._prior(user_id, idempotency_key, request)
            if prior:
                return {"forecast": await self._card(forecast_id), **json.loads(prior["result"])}
            raise
        except Exception as exc:
            await self._retain_rejected(exc)
            raise self._ai_error(exc) from exc
        finally:
            await self._release_ai(owner, lease)

    @staticmethod
    def _comment(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "text": row["body"], "createdAt": row["created_at"],
                "user": {"id": row["user_id"], "displayName": row["display_name"], "handle": row["handle"]}}

    async def add_comment(self, user_id: str, forecast_id: str, text_value: str,
                          idempotency_key: str) -> dict[str, Any]:
        user = await self._user(user_id)
        body = text(text_value, 2000)
        request = {"kind": "comment", "forecastId": forecast_id, "text": body}
        prior = await self._prior(user_id, idempotency_key, request)
        if prior:
            return dict(json.loads(prior["result"]))
        await self._forecast(forecast_id)
        await self.rate_limit("comment:" + user_id, 30, HOUR_MS)
        now, cid = self.now_ms(), "c_" + self.random_token()[:24]
        result = {"comment": {"id": cid, "text": body, "createdAt": now, "user": public_user(user)}}
        try:
            await self.db.batch((
                ("INSERT INTO comments(id,forecast_id,user_id,body,created_at) VALUES(?,?,?,?,?)",
                 (cid, forecast_id, user_id, body, now)),
                self._operation(user_id, idempotency_key, request, forecast_id, result)))
        except Exception:
            prior = await self._prior(user_id, idempotency_key, request)
            if prior:
                return dict(json.loads(prior["result"]))
            raise
        return result

    async def record_share(self, forecast_id: str, user_id: str | None = None) -> dict[str, bool]:
        await self._forecast(forecast_id)
        # Anonymous clicks cannot inflate ranking. Only signed-in unique daily
        # share intentions count, and no delivery claim is made.
        if user_id:
            await self._user(user_id)
            await self.db.batch((
                ("UPDATE forecasts SET share_count=share_count+1 WHERE id=? AND NOT EXISTS "
                 "(SELECT 1 FROM share_receipts WHERE forecast_id=? AND actor=? AND bucket=?)",
                 (forecast_id, forecast_id, user_id, self.now_ms()//DAY_MS)),
                ("INSERT OR IGNORE INTO share_receipts(forecast_id,actor,bucket) VALUES(?,?,?)",
                 (forecast_id, user_id, self.now_ms()//DAY_MS))))
        return {"ok": True}

    async def activity(self, user_id: str) -> dict[str, Any]:
        await self._user(user_id)
        rows = await self.db.all("SELECT a.*,json_extract(t.body,'$.title') AS translated_title "
            "FROM activity a LEFT JOIN forecasts f ON f.id=a.forecast_id "
            "LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.specification_hash=f.specification_hash "
            "AND t.language='en' WHERE a.user_id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 100",
                                 (user_id,))
        return {"items": [{"id": row["id"], "forecastId": row["forecast_id"], "kind": row["kind"],
            "title": row["translated_title"] or row["title"] if row["kind"] == "forecast_finalized" else row["title"],
            "body": row["translated_title"] or row["body"] if row["kind"] == "creator_published" else row["body"],
            "createdAt": row["created_at"],
            "readAt": row["read_at"]} for row in rows]}

    async def read_activity(self, user_id: str) -> dict[str, bool]:
        await self._user(user_id)
        await self.db.execute("UPDATE activity SET read_at=? WHERE user_id=? AND read_at IS NULL",
                              (self.now_ms(), user_id))
        return {"ok": True}

    async def follow(self, user_id: str, creator_id: str, following: bool) -> dict[str, bool]:
        await self._user(user_id)
        if type(following) is not bool or user_id == creator_id:
            raise invalid()
        await self._user(creator_id)
        await self.rate_limit("follow:" + user_id, 100, HOUR_MS)
        if following:
            await self.db.execute("INSERT OR IGNORE INTO follows(follower_id,creator_id,created_at) VALUES(?,?,?)",
                                  (user_id, creator_id, self.now_ms()))
        else:
            await self.db.execute("DELETE FROM follows WHERE follower_id=? AND creator_id=?", (user_id, creator_id))
        return {"following": following}

    async def creator(self, creator_id: str, user_id: str | None = None) -> dict[str, Any]:
        user = await self.db.first("SELECT * FROM users WHERE id=?", (creator_id,))
        if not user:
            raise AppError(404, "creator_not_found", "Creator not found.")
        rows = await self.db.all(projections.CARD_SQL + " WHERE f.creator_id=? ORDER BY f.created_at DESC LIMIT 100",
                                 (creator_id,))
        stats = await self.db.first(
            "SELECT COUNT(*) AS created,COUNT(finalized_outcome) AS resolved,"
            "SUM(CASE WHEN finalized_outcome='INVALID' THEN 1 ELSE 0 END) AS invalid,"
            "SUM(CASE WHEN EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=forecasts.id "
            "AND json_extract(e.event,'$.command_name')='submit_dispute') THEN 1 ELSE 0 END) AS disputed "
            "FROM forecasts WHERE creator_id=?", (creator_id,))
        followers = await self.db.first("SELECT COUNT(*) AS n FROM follows WHERE creator_id=?", (creator_id,))
        following = await self.db.first("SELECT 1 AS yes FROM follows WHERE creator_id=? AND follower_id=?",
                                        (creator_id, user_id or ""))
        return {"creator": {**public_user(user), "marketsCreated": stats["created"] if stats else 0,
            "resolvedMarkets": stats["resolved"] if stats else 0, "invalidMarkets": stats["invalid"] or 0 if stats else 0,
            "disputedMarkets": stats["disputed"] or 0 if stats else 0,
            "followerCount": followers["n"] if followers else 0, "reputation": await self.reputation(creator_id)},
            "forecasts": [projections.card(row) for row in rows], "isFollowing": following is not None}

    async def report_evidence(self, user_id: str, forecast_id: str, url: str) -> dict[str, Any]:
        """A forecaster reports an official announcement that may settle an open question.

        The URL must belong to one of the question's published official sources. Supported
        publishers are fetched immediately and enter the same hold-before-review path as the
        automatic watcher; other official sources are recorded for operator review. Reports
        never resolve anything by themselves.
        """
        await self._user(user_id)
        forecast = await self._forecast(forecast_id)
        if forecast.state not in {LifecycleState.OPEN, LifecycleState.LOCKED}:
            raise AppError(409, "evidence_report_closed", "This forecast is no longer accepting evidence reports.")
        if type(url) is not str or len(url) > 2048:
            raise invalid()
        try:
            host = validate_public_url(url, official=True)
        except SourceRejected as exc:
            raise AppError(400, "evidence_report_url", "Report a public https page on one of this question's official sources.") from exc
        allowed = {validate_public_url(source.url, official=True)
                   for source in forecast.specification.source_policy.primary_sources if source.is_official}
        if host not in allowed:
            raise AppError(400, "evidence_report_source", "Only the question's published official sources can be reported.")
        await self.rate_limit("evidence-report:" + user_id, 10, DAY_MS)
        existing = await self.db.first("SELECT id,status FROM evidence_reports WHERE forecast_id=? AND user_id=? AND url=?",
                                       (forecast_id, user_id, url))
        if existing:
            return {"reportId": existing["id"], "status": existing["status"], "duplicate": True}
        report_id, now = "er_" + self.random_token()[:24], self.now_ms()
        await self.db.execute("INSERT INTO evidence_reports(id,forecast_id,user_id,url,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                              (report_id, forecast_id, user_id, url, "received", now, now))
        status = "received"
        observation = None
        watch = self.automation.watch
        if watch is not None:
            # The reported page hangs off the publisher feed the watcher would poll for this
            # source, whether or not a keyword binding exists yet.
            from .automation import publisher_feed_url
            publisher = next(source.url for source in forecast.specification.source_policy.primary_sources
                             if source.is_official and validate_public_url(source.url, official=True) == host)
            index_url = publisher_feed_url(publisher)
            index_id = "publisher-" + hashlib.sha256(index_url.encode()).hexdigest()[:32]
            try:
                await watch.register(index_id, index_url, kind="index")
                observation = await watch.ingest_report(forecast_id, url, index_id=index_id)
            except ValueError:
                observation = None
            if observation is not None:
                held = await self.participation_holds.active(forecast_id)
                status = "unrelated" if observation.get("predatesQuestion") else "held" if held else "unrelated"
                await self.db.execute("UPDATE evidence_reports SET article_id=?,observation_id=?,artifact_hash=?,status=?,updated_at=? WHERE id=?",
                                      ("article-" + hashlib.sha256(url.encode()).hexdigest()[:32], observation["id"],
                                       observation.get("artifactHash"), status, self.now_ms(), report_id))
        return {"reportId": report_id, "status": status, "duplicate": False}

    async def reward_evidence_report(self, forecast_id: str, evidence_hashes: Sequence[str]) -> dict[str, Any] | None:
        """Credit the earliest held report whose retained evidence the accepted trigger cites."""
        if not evidence_hashes:
            return None
        placeholders = ",".join("?" for _ in evidence_hashes)
        report = await self.db.first(
            "SELECT r.* FROM evidence_reports r WHERE r.forecast_id=? AND r.status='held' AND r.artifact_hash IN (" + placeholders + ") "
            "AND NOT EXISTS(SELECT 1 FROM point_evidence_rewards w WHERE w.forecast_id=r.forecast_id) ORDER BY r.created_at,r.id LIMIT 1",
            (forecast_id, *evidence_hashes))
        if report is None:
            return None
        account = await self.db.first("SELECT available,committed FROM point_accounts WHERE user_id=?", (report["user_id"],))
        if account is None:
            return None
        now = self.now_ms()
        await self.db.batch((
            ("UPDATE point_accounts SET available=available+?,updated_at=? WHERE user_id=?", (EVIDENCE_REWARD_POINTS, now, report["user_id"])),
            ("INSERT INTO point_evidence_rewards(id,report_id,user_id,forecast_id,amount,available_after,committed_after,created_at) "
             "VALUES(?,?,?,?,?,?,?,?)", ("pr_" + self.random_token()[:24], report["id"], report["user_id"], forecast_id,
                                       EVIDENCE_REWARD_POINTS, account["available"] + EVIDENCE_REWARD_POINTS, account["committed"], now)),
            ("UPDATE evidence_reports SET status='rewarded',updated_at=? WHERE id=?", (now, report["id"])),
        ))
        return {"reportId": report["id"], "userId": report["user_id"], "amount": EVIDENCE_REWARD_POINTS}

    async def seed(self, question: str, creator_name: str = "Forecast Editorial", *,
                   uncertainty_band: tuple[int, int] | None = (15, 85)) -> dict[str, Any]:
        """Operator-only caller; creates genuine compiler-reviewed questions, no votes.

        Editorial questions must be genuinely open: when the compiler's own forecast
        falls outside the uncertainty band the draft is discarded instead of published,
        so "will there be a new version" style questions never reach the feed.
        """
        creator = await self.db.first("SELECT id FROM users WHERE id='system_editorial'")
        if creator is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                ("system_editorial", text(creator_name, 40), "forecast_editorial",
                 self.auth.token_hash("recovery:" + self.random_token()), self.now_ms()))
            creator = {"id": "system_editorial"}
        seed_key = "seed:" + content_hash({"question": question})
        previous = await self.db.first("SELECT forecast_id FROM operations WHERE user_id=? AND operation_key=?",
                                       (creator["id"], seed_key))
        if previous:
            return {"forecast": await self._card(previous["forecast_id"])}
        draft = await self.compile_forecast(creator["id"], question)
        probability = (draft.get("aiForecast") or {}).get("probability")
        if uncertainty_band is not None and isinstance(probability, (int, float)):
            low, high = uncertainty_band
            if not low <= probability <= high:
                raise AppError(409, "seed_not_uncertain",
                               f"The compiler already expects this outcome ({probability:.0f}% YES); "
                               "editorial questions must be genuinely open.")
        return await self.publish_forecast(creator["id"], draft["draftId"], seed_key)

    async def adjudicate_forecast(self, forecast_id: str, resolution: Resolution,
                                  adjudicator: AIProvenance, artifacts: Sequence[Artifact],
                                  idempotency_key: str, *,
                                  expected_revision: int | None = None) -> dict[str, Any]:
        """Exceptional ADMIN-CALLER operation using supplied, reviewed artifacts.

        The HTTP adapter must authenticate the operator before this method. This
        boundary never manufactures a verdict, modifies provenance, or finalizes.
        A successful adjudication enters PROPOSED; the normal scheduler opens a
        new complete challenge window. Artifact, event and receipt writes share CAS.
        """
        if not isinstance(resolution, Resolution) or not isinstance(adjudicator, AIProvenance):
            raise invalid("A verifiable resolution record and independent adjudication record are required.")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise invalid()
        if len(artifacts) > 32 or any(not isinstance(artifact, Artifact) for artifact in artifacts):
            raise invalid("Check the format and number of evidence artifacts.")
        request = {"kind": "adjudicate", "forecastId": forecast_id,
                   "resolutionHash": resolution.resolution_hash,
                   "adjudicatorHash": content_hash(adjudicator), "revision": expected_revision,
                   "artifacts": [{"hash": artifact.content_hash, "kind": artifact.kind,
                                  "mediaType": artifact.media_type} for artifact in artifacts]}
        operator = "admin:adjudication"
        prior = await self._prior(operator, idempotency_key, request)
        if prior:
            return {"forecast": await self._card(forecast_id), **json.loads(prior["result"])}
        forecast = await self._forecast(forecast_id)
        if expected_revision is not None and forecast.revision != expected_revision:
            raise conflict()
        if forecast.state != LifecycleState.ESCALATED:
            raise AppError(409, "adjudication_not_allowed", "Only forecasts awaiting independent adjudication can be processed.")
        # A prepared operator decision can travel over HTTP without rewriting its
        # hash-bound timestamps. Reject future, stale or backdated decisions.
        decision_at = resolution.proposed_at_ms
        if not forecast.updated_at_ms <= decision_at <= self.now_ms() \
                or self.now_ms()-decision_at > 15*60*1000:
            raise invalid("The independent decision must follow the current record and have been created within the last 15 minutes.")
        extra = self._artifact_sql(artifacts)
        supplied = {artifact.content_hash: artifact.body for artifact in artifacts}
        for evidence in resolution.evidence:
            body = supplied.get(evidence.content_sha256)
            if body is None:
                body = await self.read_artifact(evidence.content_sha256)
            if body is None:
                raise AppError(422, "missing_resolution_artifact", "Every original evidence artifact for the replacement resolution must be retained.")
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != evidence.content_sha256:
                raise AppError(422, "resolution_artifact_mismatch", "The retained resolution evidence does not match its hash.")
        payload = AdjudicateResolution(resolution=resolution, adjudicator=adjudicator)
        # The domain validates source-verification commitments, exact specification
        # and reviewed-dispute bindings, independent provider, and every timestamp.
        command_key = "admin:" + content_hash({"operator": operator, "key": idempotency_key})
        try:
            preview = apply_command(forecast, Command(idempotency_key=command_key,
                expected_revision=forecast.revision, payload=payload), now_ms=decision_at)
        except DomainError as exc:
            raise AppError(422, "adjudication_validation_failed",
                           "The decision failed independence, immutable criteria, or evidence linkage verification.") from exc
        response = {"adjudication": {"resolutionHash": resolution.resolution_hash,
                    "eventHash": preview.receipt.event_hash, "revision": preview.receipt.revision,
                    "acceptedAt": decision_at}}
        extra.extend((self._record_artifact(resolution, "adjudicated_resolution"),
                      self._record_artifact(adjudicator, "independent_adjudicator"),
                      self._operation(operator, idempotency_key, request, forecast_id, response)))
        extra.extend(self._record_artifact(verification, "adjudication_source_verification")
                     for verification in resolution.source_verifications)
        try:
            await self._mutate(forecast, payload, key=command_key, now=decision_at, extra=extra,
                               timing_artifacts=artifacts)
        except AppError:
            prior = await self._prior(operator, idempotency_key, request)
            if prior:
                return {"forecast": await self._card(forecast_id), **json.loads(prior["result"])}
            raise
        return {"forecast": await self._card(forecast_id), **response}

    @staticmethod
    def _early_projection(forecast: Forecast) -> dict[str, Any] | None:
        if not isinstance(forecast, ForecastV2):
            return None
        trigger = forecast.early_trigger
        return {"triggerHash": trigger.trigger_hash, "eventTimeBasis": trigger.event_time_basis,
                "eventAt": trigger.event_at_ms, "observedAt": trigger.observed_at_ms,
                "qualifiedAt": trigger.qualified_at_ms, "originalCloseAt": forecast.specification.close_at_ms,
                "qualification": trigger.qualification, "upgradedAt": forecast.upgraded_at_ms,
                "sources": [{"url": item.url, "sourceId": item.source_id, "evidenceHash": item.evidence_hash}
                            for item in trigger.evidence]}

    async def run_automation(self, *, limit: int = 2) -> dict[str, Any]:
        observation = await self.automation.run(limit=limit)
        eligibility = await self.automation.retry_eligibility()
        due = await self.run_due_jobs(limit=3)
        settled = await self.db.all("SELECT f.id FROM forecasts f JOIN point_markets m ON m.forecast_id=f.id WHERE f.state IN ('FINALIZED','ARCHIVED') AND m.status!='settled' ORDER BY f.updated_at DESC LIMIT 10")
        for row in settled:
            await self.markets.settle(row["id"], limit=25)
        return {"sources": observation, "eligibility": eligibility, "lifecycle": due}

    async def run_due_jobs(self, limit: int = 20) -> dict[str, Any]:
        """Bounded scheduler; exceptions leave durable state recoverable for retry."""
        limit = max(1, min(50, limit))
        now = self.now_ms()
        rows = await self.db.all(
            "SELECT id FROM forecasts WHERE job_until<=? AND retry_at<=? AND "
            "((state='OPEN' AND close_at<=?) OR state IN ('LOCKED','RESOLVING','PROPOSED','DISPUTED','PAUSED') "
            "OR (state='CHALLENGE' AND challenge_until<=?)) ORDER BY close_at,id LIMIT ?",
            (now, now, now, now, limit))
        completed, failures = 0, 0
        for row in rows:
            token = self.random_token()
            await self.db.execute("UPDATE forecasts SET job_token=?,job_until=? WHERE id=? AND job_until<=?",
                                  (token, self.now_ms()+LEASE_MS, row["id"], self.now_ms()))
            claimed = await self.db.first("SELECT job_token FROM forecasts WHERE id=?", (row["id"],))
            if not claimed or claimed["job_token"] != token:
                continue
            try:
                await self._bounded_ai(self._advance_job(row["id"], token))
                completed += 1
                await self.db.execute("UPDATE forecasts SET retry_at=0,failure_count=0,job_error=NULL WHERE id=? AND job_token=?",
                                      (row["id"], token))
            except Exception as exc:
                failures += 1
                await self._retain_rejected(exc)
                from .ai import AIUnavailable
                forecast = await self._forecast(row["id"])
                if isinstance(exc, AIUnavailable) and forecast.state in {
                    LifecycleState.RESOLVING, LifecycleState.PROPOSED, LifecycleState.CHALLENGE,
                    LifecycleState.DISPUTED, LifecycleState.ESCALATED}:
                    providers = tuple(getattr(self.ai, "configured_providers", ()))
                    unavailable = tuple(getattr(exc, "unavailable_providers", ()))
                    if providers and set(unavailable) == set(providers):
                        await self._mutate(forecast, PauseForProviderOutage(
                            configured_providers=providers, unavailable_providers=providers,
                            reason="Resolution is paused because all configured AI providers are unavailable."),
                            key="job:pause:" + str(forecast.revision), job_token=token)
                reason = "Resolution is on hold because evidence or independent review is insufficient. No result will be finalized before another review."
                if isinstance(exc, AppError) and exc.status == 429:
                    reason = "Resolution is on hold because the daily AI limit was reached. Review will resume after the limit resets."
                elif isinstance(exc, AppError) and exc.code == "ai_workflow_timeout":
                    reason = "Resolution is on hold because the AI review timed out. Another review will follow the retry schedule and daily limit."
                elif isinstance(exc, AppError) and exc.code in {"resolution_timing_review", "early_eligibility_review"}:
                    reason = "Evidence publication time and receipt eligibility are being reviewed. No result rewards or reputation will be credited until that review is complete."
                await self.db.execute("UPDATE forecasts SET failure_count=failure_count+1,job_error=?,"
                    "retry_at=?+MIN(21600000,60000*(1<<MIN(failure_count,8))) WHERE id=? AND job_token=?",
                    (reason, self.now_ms(), row["id"], token))
            finally:
                await self.db.execute("UPDATE forecasts SET job_token=NULL,job_until=0 WHERE id=? AND job_token=?",
                                      (row["id"], token))
        processed = await self._process_outbox(limit*3)
        await self.db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        await self.db.execute("DELETE FROM ai_leases WHERE expires_at<=?", (now,))
        await self.db.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now-DAY_MS,))
        return {"processed": completed, "failed": failures, "effects": processed}

    async def _advance_job(self, forecast_id: str, token: str) -> None:
        # At most six transitions in one lease. User disputes remain free to
        # race: the CAS then rejects this job, preserving the submitted dispute.
        for _ in range(6):
            forecast = await self._forecast(forecast_id)
            if await self.db.first("SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?", (forecast_id,)):
                raise AppError(409, "early_eligibility_review",
                               "Receipt timing and known-result evidence must be reviewed before resolution or rewards.")
            key = "job:" + str(forecast.revision)
            payload: CommandPayload
            extra: list[Statement] = []
            timing_artifacts: Sequence[Artifact] = ()
            at = self.now_ms()
            if forecast.state == LifecycleState.OPEN:
                payload = Lock()
            elif forecast.state == LifecycleState.LOCKED:
                payload = BeginResolution()
            elif forecast.state == LifecycleState.RESOLVING:
                owner = "resolution:" + forecast_id
                lease = await self._ai_lease(owner)
                try:
                    result = await self._bounded_ai((self.ai.propose_early_resolution(forecast, self.now_ms()) if isinstance(forecast, ForecastV2) else self.ai.propose_resolution(forecast, self.now_ms())))
                finally:
                    await self._release_ai(owner, lease)
                payload, at = (ProposeEarlyResolution(resolution=result.resolution) if isinstance(forecast, ForecastV2) else ProposeResolution(resolution=result.resolution)), result.resolution.proposed_at_ms
                extra = self._artifact_sql(result.artifacts)
                timing_artifacts = result.artifacts
                extra.append(self._record_artifact(result.resolution, "resolution"))
            elif forecast.state == LifecycleState.PROPOSED:
                payload = BeginChallenge(duration_ms=CHALLENGE_MS)
            elif forecast.state == LifecycleState.CHALLENGE:
                if forecast.challenge_until_ms is None or self.now_ms() < forecast.challenge_until_ms:
                    return
                payload = Finalize()
            elif forecast.state == LifecycleState.DISPUTED:
                reviewed = {review.dispute_hash for review in forecast.dispute_reviews}
                pending = next((d for d in forecast.disputes if d.dispute_hash not in reviewed), None)
                if pending:
                    owner = "review:" + forecast_id
                    lease = await self._ai_lease(owner)
                    try:
                        result = await self._bounded_ai(self.ai.review_dispute(forecast, pending, self.now_ms()))
                    finally:
                        await self._release_ai(owner, lease)
                    payload, at = ReviewDispute(review=result.review), result.review.reviewed_at_ms
                    extra = self._artifact_sql(result.artifacts)
                    extra.append(self._record_artifact(result.review, "dispute_review"))
                elif any(review.material_conflict for review in forecast.dispute_reviews):
                    payload = Escalate()
                else:
                    payload = RetainProposal()
            elif forecast.state == LifecycleState.PAUSED:
                # Recovery must be demonstrated by a successful task, not by a
                # scheduled timer or a provider merely remaining configured.
                await self._recover_job(forecast, token)
                return
            else:
                return
            await self._mutate(forecast, payload, key=key, now=at, extra=extra, job_token=token,
                               timing_artifacts=timing_artifacts)

    async def _recover_job(self, forecast: Forecast, token: str) -> None:
        if forecast.pause is None:
            return
        previous = forecast.pause.previous_state
        # Only tasks that caused an outage in this application are recovered.
        # A hypothetical administrative pause needs explicit operator handling.
        if previous not in {LifecycleState.RESOLVING, LifecycleState.DISPUTED}:
            return
        owner = "recovery:" + forecast.forecast_id
        lease = await self._ai_lease(owner)
        try:
            if previous == LifecycleState.RESOLVING:
                result = await self._bounded_ai((self.ai.propose_early_resolution(forecast, self.now_ms()) if isinstance(forecast, ForecastV2) else self.ai.propose_resolution(forecast, self.now_ms())))
                provider = result.resolution.judge.provider
                payload: CommandPayload = ProposeEarlyResolution(resolution=result.resolution) if isinstance(forecast, ForecastV2) else ProposeResolution(resolution=result.resolution)
                at = result.resolution.proposed_at_ms
                record = result.resolution
            else:
                reviewed = {review.dispute_hash for review in forecast.dispute_reviews}
                pending = next(d for d in forecast.disputes if d.dispute_hash not in reviewed)
                result = await self._bounded_ai(self.ai.review_dispute(forecast, pending, self.now_ms()))
                provider = result.review.independent_judge.provider
                payload = ReviewDispute(review=result.review)
                at = result.review.reviewed_at_ms
                record = result.review
            resumed = await self._mutate(forecast, ResumeAfterProviderRecovery(recovered_provider=provider),
                key="job:resume:" + str(forecast.revision), now=at, job_token=token)
            extra = self._artifact_sql(result.artifacts)
            extra.append(self._record_artifact(record, "provider_recovery_result"))
            await self._mutate(resumed, payload, key="job:recovered:" + str(resumed.revision),
                               now=at, extra=extra, job_token=token, timing_artifacts=result.artifacts)
        finally:
            await self._release_ai(owner, lease)

    async def _process_outbox(self, limit: int) -> int:
        if self.registry is not None:
            await self.db.execute("UPDATE outbox SET status='processed',processed_at=? "
                "WHERE kind='RESOLUTION_COMMITMENT_REQUIRED' AND status='awaiting_adapter' "
                "AND EXISTS (SELECT 1 FROM registry_delivery d JOIN registry_intents i "
                "USING(forecast_id,revision) JOIN forecasts f ON f.id=d.forecast_id "
                "WHERE d.forecast_id=outbox.forecast_id AND d.status='confirmed' "
                "AND d.revision=f.revision AND f.state IN ('FINALIZED','ARCHIVED') "
                "AND json_extract(i.snapshot,'$.audit_head_hash')=json_extract(f.snapshot,'$.audit_head_hash'))",
                (self.now_ms(),))
        rows = await self.db.all("SELECT * FROM outbox WHERE status='pending' AND NOT EXISTS "
                                 "(SELECT 1 FROM forecast_resolution_blockers b WHERE b.forecast_id=outbox.forecast_id) "
                                 "ORDER BY created_at,id LIMIT ?", (limit,))
        processed = 0
        for row in rows:
            now = self.now_ms()
            if row["kind"] == "RESOLUTION_COMMITMENT_REQUIRED":
                await self.db.execute("UPDATE outbox SET status='awaiting_adapter' WHERE id=? AND status='pending'", (row["id"],))
                continue
            statements: list[Statement] = []
            if row["kind"] == "REPUTATION_UPDATE_REQUIRED":
                statements.append((
                    "INSERT OR IGNORE INTO reputation_scores(forecast_id,user_id,category,outcome,probability,"
                    "correct,brier_score,created_at) SELECT f.id,v.user_id,f.category,f.finalized_outcome,"
                    "v.yes_probability,CASE WHEN f.finalized_outcome='INVALID' THEN NULL "
                    "WHEN f.finalized_outcome=v.outcome THEN 1 ELSE 0 END,"
                    "CASE WHEN f.finalized_outcome='INVALID' THEN NULL ELSE "
                    "(v.yes_probability/100.0-CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END)*"
                    "(v.yes_probability/100.0-CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END) END,? "
                    "FROM forecasts f JOIN eligible_user_forecasts v ON v.forecast_id=f.id WHERE f.id=? "
                    "AND f.state IN ('FINALIZED','ARCHIVED') AND EXISTS(SELECT 1 FROM outbox WHERE id=? AND status='pending')",
                    (now, row["forecast_id"], row["id"])))
                statements.extend(settlement_sql(row["forecast_id"], now))
            elif row["kind"] == "RESULT_NOTIFICATION_REQUIRED":
                statements.append((
                    "INSERT OR IGNORE INTO activity(id,user_id,forecast_id,kind,title,body,created_at) "
                    "SELECT ?||':'||v.user_id,v.user_id,f.id,'forecast_finalized',f.title,"
                    "'The forecast was finalized as '||f.finalized_outcome||'.',? "
                    "FROM forecasts f JOIN eligible_user_forecasts v ON v.forecast_id=f.id WHERE f.id=? "
                    "AND f.state IN ('FINALIZED','ARCHIVED') AND EXISTS(SELECT 1 FROM outbox WHERE id=? AND status='pending')",
                    (row["id"], now, row["forecast_id"], row["id"])))
            else:
                continue
            statements.append(("UPDATE outbox SET status='processed',processed_at=? WHERE id=? AND status='pending'",
                               (now, row["id"])))
            await self.db.batch(statements)
            processed += 1
        return processed
