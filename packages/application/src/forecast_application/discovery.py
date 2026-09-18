"""Pure and independently reproducible quality ranking; no unbounded popularity weight."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

DAY_MS = 86_400_000
DISCOVERY_VERSION = "forecast-discovery-v2"
WEIGHTS_BP = {"clarity": 4000, "creator": 2000, "adjudication": 1500,
              "engagement": 1500, "freshness": 1000}


def _count(row: Mapping[str, Any], name: str) -> int:
    value = row.get(name, 0)
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid {name}")
    return value


def _clarity(row: Mapping[str, Any]) -> int:
    ambiguity = row.get("ambiguity_score_bp")
    if ambiguity is None:
        snapshot = row.get("snapshot")
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if isinstance(snapshot, Mapping):
            ambiguity = snapshot.get("specification", {}).get("ambiguity_score_bp")
    if type(ambiguity) is not int or not 0 <= ambiguity <= 10000:
        raise ValueError("Missing or invalid measured ambiguity_score_bp")
    return 10000 - ambiguity


def score_forecast(row: Mapping[str, Any], *, as_of_ms: int) -> dict[str, Any]:
    """Input joined counts must reflect eligible, finalized history at as_of_ms.

    creator_finalized_count includes INVALID; creator_invalid_count is its INVALID
    subset. creator_reviewed_disputes/material_disputes are event-linked reviews of
    the creator's questions, never the creator's own unresolved dispute submissions.
    Missing quality samples use a stated neutral prior, not an invented good score.
    """
    if type(as_of_ms) is not int or as_of_ms < 0:
        raise ValueError("Invalid as_of_ms")
    created = _count(row, "created_at")
    opened = _count(row, "open_at")
    closed = _count(row, "close_at")
    count = _count(row, "creator_finalized_count")
    invalid = _count(row, "creator_invalid_count")
    reviewed = _count(row, "creator_reviewed_disputes")
    material = _count(row, "creator_material_disputes")
    if invalid > count or material > reviewed:
        raise ValueError("Quality numerator exceeds sample count")
    clarity = _clarity(row)
    participants = min(50, _count(row, "participant_count"))
    comments = min(20, _count(row, "comment_count"))
    shares = min(20, _count(row, "share_count"))
    age_days = max(0, as_of_ms // DAY_MS - created // DAY_MS)
    components = {"clarity": clarity, "creator": (count - invalid + 2) * 10000 // (count + 4),
                  "adjudication": (reviewed - material + 2) * 10000 // (reviewed + 4),
                  "engagement": (participants * 3 + comments + shares) * 10000 // 190,
                  "freshness": 10000 // (1 + age_days)}
    active = (row.get("state") == "OPEN" and created <= as_of_ms and opened <= as_of_ms < closed
              and not row.get("participation_hold") and type(row.get("eligible")) in (bool, int) and row.get("eligible") == 1)
    inputs = {"ambiguityScoreBp": 10000 - clarity, "creatorFinalized": count,
              "creatorInvalid": invalid, "reviewedDisputes": reviewed, "materialDisputes": material,
              "participantsCapped": participants, "commentsCapped": comments, "sharesCapped": shares,
              "ageDays": age_days}
    return {"version": DISCOVERY_VERSION, "asOf": as_of_ms, "active": active,
            "scoreBp": sum(components[key] * weight for key, weight in WEIGHTS_BP.items()) // 10000,
            "inputs": inputs, "componentsBp": components, "weightsBp": dict(WEIGHTS_BP),
            "creatorSampleStatus": "new" if count == 0 else "provisional" if count < 5 else "established",
            "reasons": ["Measured question clarity", "Finalized creator outcomes with a neutral four-result prior",
                        "Resolved material disputes with a neutral four-review prior",
                        "Participation, comments and shares capped at 15% of total score", "UTC-day freshness"]}


def rank_forecasts(rows: Iterable[Mapping[str, Any]], *, as_of_ms: int,
                   user_id: str | None = None) -> list[dict[str, Any]]:
    """Rank the entire candidate set before pagination, preserving input columns."""
    result = []
    seen: set[str] = set()
    for row in rows:
        identifier = row.get("id")
        if type(identifier) is not str or not identifier or identifier in seen:
            raise ValueError("Missing or duplicate forecast ID")
        seen.add(identifier)
        score = score_forecast(row, as_of_ms=as_of_ms)
        if score["active"]:
            result.append({**row, "quality": score, "discoveryTie": tie_break(identifier, as_of_ms, user_id)})
    return sorted(result, key=lambda row: (-row["quality"]["scoreBp"], row["discoveryTie"], row["id"]))


def recommendations(rows: Iterable[Mapping[str, Any]], *, as_of_ms: int,
                    user_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """Reserve every fifth slot for a clear cold-start question when available.

    Prefer categories with fewer than two picks while any remain. Within each
    selection pool use the exact quality/tie ordering, never hidden randomness.
    """
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("Invalid recommendation limit")
    pool = rank_forecasts(rows, as_of_ms=as_of_ms, user_id=user_id)
    selected: list[dict[str, Any]] = []
    categories: Counter[str] = Counter()
    while pool and len(selected) < limit:
        reason = "quality"
        choices = pool
        if (len(selected) + 1) % 5 == 0:
            cold = [row for row in pool if row["quality"]["inputs"]["creatorFinalized"] < 5
                    and row["quality"]["componentsBp"]["clarity"] >= 7000]
            if cold:
                choices = cold
                reason = "clear-cold-start"
        diverse = [row for row in choices if categories[str(row.get("category", "")).lower()] < 2]
        if diverse:
            choices = diverse
        choice = choices[0]
        categories[str(choice.get("category", "")).lower()] += 1
        selected.append({**choice, "recommendationReason": reason})
        pool.remove(choice)
    return selected


def _tie_coefficients(as_of_ms: int, user_id: str | None) -> list[int]:
    # ensure_ascii=False is load-bearing, not cosmetic. The default escapes non-ASCII as
    # \uXXXX, and the Rust edge serializes the same seed as raw UTF-8, so a non-ASCII user
    # id produced different coefficients on each side — and a different SQL expression, since
    # the coefficients are interpolated into it. Account ids are `u_` plus base64url and so
    # always ASCII, which is why this was never seen; it is a landmine, not a live fault.
    # Raw UTF-8 on both sides removes the question rather than answering it twice.
    seed = json.dumps([DISCOVERY_VERSION, as_of_ms // DAY_MS, user_id or ""],
                      separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(seed.encode()).digest()
    return [byte + 1 for byte in digest]


def tie_break(identifier: str, as_of_ms: int, user_id: str | None = None) -> int:
    """Portable SQL/Python daily tie key; final ID breaks collisions."""
    return sum(ord(char) * weight for char, weight in zip(identifier[:32], _tie_coefficients(as_of_ms, user_id), strict=False))


def tie_sql(as_of_ms: int, user_id: str | None = None, *, column: str = "f.id") -> str:
    if column not in ("f.id", "id"):
        raise ValueError("Unsupported tie column")
    return "(" + "+".join(f"COALESCE(unicode(substr({column},{index + 1},1)),0)*{weight}"
                          for index, weight in enumerate(_tie_coefficients(as_of_ms, user_id))) + ")"


def candidate_sql(as_of_ms: int, user_id: str | None = None) -> str:
    """Full-inventory scoring in SQL; callers append filters/order before LIMIT.

    All joins aggregate once; rows sent to Python are the requested page or the
    sufficient per-category daily candidate set, never all participant histories.
    """
    if type(as_of_ms) is not int or as_of_ms < 0:
        raise ValueError("Invalid as_of_ms")
    now = as_of_ms
    tie = tie_sql(now, user_id)
    return f"""
WITH finalized AS (
 SELECT f.creator_id,COUNT(*) AS n,SUM(f.finalized_outcome='INVALID') AS invalid
 FROM forecasts f JOIN forecast_quality_finalizations z ON z.forecast_id=f.id
 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=f.id
 LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id
 WHERE f.state IN ('FINALIZED','ARCHIVED') AND z.finalized_at<={now}
 AND (d.id IS NULL OR c.created_at<={now})
 AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)
 GROUP BY f.creator_id
), reviews AS (
 SELECT creator_id,COUNT(*) AS n,SUM(material) AS material FROM (
 SELECT DISTINCT f.creator_id,a.hash,json_extract(a.body,'$.material_conflict') AS material
 FROM events e JOIN forecasts f ON f.id=e.forecast_id
 JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash')
 WHERE json_extract(e.event,'$.command_name')='review_dispute' AND e.created_at<={now}
 AND a.created_at<={now} AND json_extract(a.body,'$.evidence_validated')=1
 ) GROUP BY creator_id
), votes AS (
 SELECT v.forecast_id,COUNT(*) AS n,AVG(v.yes_probability) AS probability
 FROM eligible_user_forecasts v LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=v.forecast_id
 LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id
 WHERE v.submitted_at<={now} AND (d.id IS NULL OR c.created_at<={now}) GROUP BY v.forecast_id
), comment_counts AS (
 SELECT forecast_id,COUNT(*) AS n FROM comments WHERE created_at<={now} GROUP BY forecast_id
), raw AS (
 SELECT f.*,t.body AS display_translation,t.translated_at,t.content_hash AS translation_hash,
 h.body AS participation_hold,COALESCE(v.n,0) AS participant_count,v.probability,
 COALESCE(comment_counts.n,0) AS comment_count,
 COALESCE(z.n,0) AS creator_finalized_count,COALESCE(z.invalid,0) AS creator_invalid_count,
 COALESCE(r.n,0) AS creator_reviewed_disputes,COALESCE(r.material,0) AS creator_material_disputes,
 10000-json_extract(f.snapshot,'$.specification.ambiguity_score_bp') AS clarity,
 CAST(MAX(0,{now}//86400000-f.created_at/86400000) AS INTEGER) AS age_days,
 CASE WHEN h.id IS NULL AND (d.id IS NULL OR c.created_at<={now}) THEN 1 ELSE 0 END AS eligible,
 {tie} AS discovery_tie
 FROM forecasts f LEFT JOIN finalized z ON z.creator_id=f.creator_id
 LEFT JOIN reviews r ON r.creator_id=f.creator_id LEFT JOIN votes v ON v.forecast_id=f.id
 LEFT JOIN comment_counts ON comment_counts.forecast_id=f.id LEFT JOIN active_participation_holds h ON h.forecast_id=f.id
 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=f.id
 LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id
 LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.language='en' AND t.specification_hash=f.specification_hash
), scored AS (
 SELECT raw.*,CAST((4000*clarity
 +2000*CAST((creator_finalized_count-creator_invalid_count+2)*10000/(creator_finalized_count+4) AS INTEGER)
 +1500*CAST((creator_reviewed_disputes-creator_material_disputes+2)*10000/(creator_reviewed_disputes+4) AS INTEGER)
 +1500*CAST((MIN(participant_count,50)*3+MIN(comment_count,20)+MIN(share_count,20))*10000/190 AS INTEGER)
 +1000*CAST(10000/(1+age_days) AS INTEGER))/10000 AS INTEGER) AS quality_score,
 CASE WHEN eligible=1 AND state='OPEN' AND created_at<={now} AND open_at<={now} AND close_at>{now}
 THEN 1 ELSE 0 END AS active_quality FROM raw
)
SELECT f.* FROM scored f
""".replace(f"{now}//86400000", str(now // DAY_MS))
