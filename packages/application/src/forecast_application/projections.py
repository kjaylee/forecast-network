"""Read models derive only from persisted observations; empty scores stay null."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from forecast_domain.models import ForecastSpecification, UserForecast

PROFILE_CARD_COMMITMENT_PREFIX = b"forecast-network:sha256:profile-card-json:v1\n"

# One SQLite/D1 statement observes public identity, complete scoring ledger,
# translated titles, newest history, and highlight at a single database snapshot.
PROFILE_CARD_SQL = """
WITH target AS (
 SELECT id,display_name,handle,created_at FROM users WHERE id=?
), finalizations AS (
 SELECT e.forecast_id,MIN(e.created_at) AS finalized_at
 FROM events e JOIN eligible_reputation_scores s ON s.forecast_id=e.forecast_id
 JOIN target u ON u.id=s.user_id
 WHERE json_extract(e.event,'$.command_name')='finalize'
 GROUP BY e.forecast_id
), ledger AS (
 SELECT s.forecast_id,s.outcome AS resolved_outcome,s.correct,s.probability,s.brier_score,
        f.specification_hash,COALESCE(json_extract(t.body,'$.title'),f.title) AS title,
        e.finalized_at
 FROM eligible_reputation_scores s JOIN target u ON u.id=s.user_id
 JOIN forecasts f ON f.id=s.forecast_id
 JOIN finalizations e ON e.forecast_id=s.forecast_id
 LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.language='en'
  AND t.specification_hash=f.specification_hash
 WHERE f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome=s.outcome
), scored AS (
 SELECT *,
   CASE WHEN correct=1 THEN resolved_outcome
        WHEN resolved_outcome='YES' THEN 'NO' ELSE 'YES' END AS outcome,
   CASE WHEN (resolved_outcome='YES' AND correct=1) OR (resolved_outcome='NO' AND correct=0)
        THEN probability ELSE 100-probability END AS confidence,
   (probability-CASE WHEN resolved_outcome='YES' THEN 100 ELSE 0 END)*
   (probability-CASE WHEN resolved_outcome='YES' THEN 100 ELSE 0 END) AS squared_error
 FROM ledger WHERE resolved_outcome IN ('YES','NO') AND correct IN (0,1) AND brier_score IS NOT NULL
), calibration_bins AS (
 SELECT MIN(9,CAST(probability/10 AS INTEGER)) AS bin,
        SUM(probability) AS probability_sum,
        SUM(CASE WHEN resolved_outcome='YES' THEN 100 ELSE 0 END) AS outcome_sum
 FROM scored GROUP BY bin
), newest AS (
 SELECT * FROM scored ORDER BY finalized_at DESC,forecast_id ASC LIMIT 20
), highlight AS (
 SELECT * FROM scored WHERE correct=1
 ORDER BY squared_error ASC,finalized_at DESC,forecast_id ASC LIMIT 1
)
SELECT u.*,
 (SELECT COUNT(*) FROM eligible_user_forecasts v WHERE v.user_id=u.id) AS total_forecasts,
 (SELECT COUNT(*) FROM scored) AS resolved_forecasts,
 (SELECT COALESCE(SUM(correct),0) FROM scored) AS correct_forecasts,
 (SELECT COUNT(*) FROM ledger WHERE resolved_outcome='INVALID') AS invalid_forecasts,
 (SELECT COALESCE(SUM(squared_error),0) FROM scored) AS brier_numerator,
 (SELECT COALESCE(SUM(ABS(probability_sum-outcome_sum)),0) FROM calibration_bins) AS calibration_error_numerator,
 (SELECT json_group_array(json_object('forecastId',forecast_id,'title',title,
   'outcome',outcome,'resolvedOutcome',resolved_outcome,'correct',correct,
   'confidence',confidence,'finalizedAt',finalized_at)) FROM newest) AS history_json,
 (SELECT json_object('forecastId',forecast_id,'title',title,'outcome',outcome,
   'confidence',confidence,'resolvedAt',finalized_at,'specificationHash',specification_hash)
  FROM highlight) AS highlight_json
FROM target u
"""


def profile_card_payload(row: dict[str, Any], as_of: int) -> dict[str, Any]:
    """Generate only the documented public fields; no account secrets or wallet."""
    count = row["resolved_forecasts"]
    history = list(json.loads(row["history_json"] or "[]"))
    for item in history:
        item["correct"] = bool(item["correct"])
    highlight = json.loads(row["highlight_json"]) if row["highlight_json"] else None
    return {
        "schemaVersion": 1, "asOf": as_of,
        "user": {"id": row["id"], "displayName": row["display_name"], "handle": row["handle"],
                 "createdAt": row["created_at"]},
        "metrics": {"totalForecasts": row["total_forecasts"], "resolvedForecasts": count,
            "correctForecasts": row["correct_forecasts"], "invalidForecasts": row["invalid_forecasts"],
            "accuracy": row["correct_forecasts"]*100/count if count else None,
            "brierScore": row["brier_numerator"]/(10000*count) if count else None,
            "calibrationScore": 1-row["calibration_error_numerator"]/(100*count) if count else None},
        "sampleStatus": "new" if count == 0 else "provisional" if count < 10 else "established",
        "history": history, "historyTruncated": count > len(history), "highlight": highlight,
        "methodology": {
            "version": "profile-card-v1",
            "totalForecasts": "Distinct forecasts with an eligible personal forecast, including pending and invalid question results; evidence-voided submissions are excluded.",
            "accuracy": "Correct personal choices divided by all scored, finalized YES/NO forecasts, as a percentage. INVALID is excluded.",
            "brierScore": "Mean squared error of the recorded YES probability against the finalized result. Zero is best; INVALID is excluded.",
            "calibrationScore": "One minus weighted absolute calibration error in ten YES-probability bins. INVALID is excluded.",
            "history": "The newest 20 scored YES/NO results. Outcome and confidence are the user's actual choice; resolvedOutcome is the final result.",
            "highlight": "The correct personal forecast with the lowest Brier error; ties use latest finalization, then forecast ID. This is a selected example.",
            "sampleStatus": "New means no scored YES/NO results; provisional means 1–9; established means at least 10. These are sample sizes, not rankings.",
            "commitment": "An owner-published application snapshot, not an on-chain certificate. Hash the exact retained canonicalJson UTF-8 bytes with the stated prefix.",
        },
        "commitmentProfile": {"algorithm": "SHA-256", "encoding": "UTF-8",
            "prefix": PROFILE_CARD_COMMITMENT_PREFIX.decode("ascii"),
            "canonicalization": "profile-card-json-v1: compact UTF-8 JSON with recursively sorted keys; verify the exact supplied canonicalJson bytes"},
    }


def profile_card_json(payload: dict[str, Any]) -> str:
    """Separate versioned display encoding; domain integer-only rules stay intact."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def display_translation(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("display_translation")
    if not raw:
        return None
    value = dict(json.loads(raw))
    if value.get("specificationHash") != row["specification_hash"] or value.get("language") != "en":
        return None
    return {**value, "translatedAt": row["translated_at"], "translationHash": row["translation_hash"]}


def card(row: dict[str, Any]) -> dict[str, Any]:
    ai = json.loads(row["ai_forecast"]) if row.get("ai_forecast") else None
    translation = display_translation(row)
    if ai and translation and translation["aiRationale"] is not None:
        ai = {**ai, "rationale": translation["aiRationale"], "rationaleLanguage": "en",
              "rationaleAttribution": translation["attribution"]}
    if ai:
        probability = ai.get("probability")
        observed = ai.get("asOf", row["created_at"])
        cutoff = row.get("quality_as_of", row["created_at"])
        valid_ai = (type(probability) in (int, float) and 0 <= probability <= 100
                    and type(observed) is int and 0 <= observed <= cutoff
                    and type(ai.get("provider")) is str and bool(ai["provider"])
                    and type(ai.get("model")) is str and bool(ai["model"]))
        ai = {**ai, "probability": probability if valid_ai else None,
              "count": 1 if valid_ai else 0, "asOf": observed}
    return {
        "id": row["id"], "title": translation["title"] if translation else row["title"],
        "question": translation["question"] if translation else row["question"],
        "translationLanguage": translation["language"] if translation else None,
        "sourceLanguage": translation["sourceLanguage"] if translation else None,
        # Keep containment in the audit trail; terminal result presentation takes
        # precedence once eligibility and resolution gates have completed.
        "participationHold": json.loads(row["participation_hold"])
            if row.get("participation_hold") and row["state"] not in {"FINALIZED", "ARCHIVED"} else None,
        "category": row["category"].lower(), "state": row["state"], "revision": row["revision"],
        "openAt": row["open_at"], "closeAt": row["close_at"], "createdAt": row["created_at"],
        "creator": {"id": row["creator_id"], "displayName": row["creator_name"],
                    "handle": row["creator_handle"]},
        "crowd": {"probability": row["probability"], "count": row["participant_count"]},
        "top": {"probability": row["top_probability"], "count": row["top_count"]},
        "expert": {"probability": row.get("expert_probability"), "count": row.get("expert_count", 0)},
        "cohortMethodology": {"version": "forecast-quality-v2", "asOf": row.get("quality_as_of"),
                              "expertise": "Demonstrated category forecasting record, not professional credentials."},
        "ai": ai or {"probability": None, "provider": None, "model": None, "count": 0},
        "commentCount": row["comment_count"], "shareCount": row["share_count"],
        "specificationHash": row["specification_hash"],
        "chain": {"network": None, "status": "unconnected", "transaction": None},
    }


def specification(spec: ForecastSpecification) -> dict[str, Any]:
    return {
        "canonicalQuestion": spec.canonical_question, "shareTitle": spec.share_title,
        "category": spec.category.value.lower(), "openAt": spec.open_at_ms,
        "closeAt": spec.close_at_ms,
        "rules": [{"clauseId": rule.clause_id, "outcome": rule.outcome.value,
                   "condition": rule.condition} for rule in spec.rules],
        "primarySources": [{"name": source.name, "url": source.url}
                           for source in spec.source_policy.primary_sources],
        "fallbackSources": [{"name": source.name, "url": source.url}
                            for source in spec.source_policy.fallback_sources],
        "invalidationRules": list(spec.invalidation_rules),
        "ambiguityScore": spec.ambiguity_score_bp / 10000,
    }


def submission(value: UserForecast, revision: int) -> dict[str, Any]:
    return {"outcome": value.outcome.value, "confidence": value.confidence,
            "probability": value.confidence if value.outcome.value == "YES" else 100-value.confidence,
            "submittedAt": value.submitted_at_ms, "revision": revision}


def scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [row for row in rows if row["brier_score"] is not None]
    count = len(resolved)
    correct = sum(row["correct"] for row in resolved)
    brier = sum(row["brier_score"] for row in resolved)/count if count else None
    # Reliability uses ten confidence bins, weighted by the number of observations.
    # Small samples are shown as measurements, never promoted to expert status.
    bins: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in resolved:
        bins[min(9, row["probability"] // 10)].append(row)
    calibration_error = sum(
        len(group) * abs(sum(row["probability"]/100 for row in group)/len(group)
                         - sum(row["outcome"] == "YES" for row in group)/len(group))
        for group in bins.values())
    return {"resolvedForecasts": count, "correctForecasts": correct,
            "invalidForecasts": sum(row["outcome"] == "INVALID" for row in rows),
            "accuracy": correct/count*100 if count else None, "brierScore": brier,
            "calibrationScore": 1-calibration_error/count if count else None,
            "consistencyScore": None}


def quality_card_sql(as_of_ms: int, forecast_ids: list[str]) -> str:
    """Bound card reads to selected IDs; qualification excludes each target result.

    SQL performs aggregate work, not one network history read per participant.
    The caller supplies a maximum of 100 IDs after full-inventory ordering.
    """
    if type(as_of_ms) is not int or as_of_ms < 0 or len(forecast_ids) > 100:
        raise ValueError("Invalid quality card scope")
    if any(type(value) is not str or not value or len(value) > 120 for value in forecast_ids):
        raise ValueError("Invalid forecast identifier")
    identifiers = ",".join("'" + value.replace("'", "''") + "'" for value in forecast_ids) or "NULL"
    now = as_of_ms
    return f"""
WITH candidates AS (SELECT * FROM forecasts WHERE id IN ({identifiers})), history AS (
 SELECT *,(probability-CASE outcome WHEN 'YES' THEN 100 ELSE 0 END)*
 (probability-CASE outcome WHEN 'YES' THEN 100 ELSE 0 END) AS loss
 FROM forecast_quality_history WHERE finalized_at<={now} AND eligibility_at<={now}
), samples AS (
 SELECT f.id AS target_id,h.* FROM candidates f JOIN history h ON h.forecast_id<>f.id
), global_scores AS (
 SELECT target_id,user_id,AVG(loss) AS loss FROM samples GROUP BY target_id,user_id HAVING COUNT(*)>=10
), ranked AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY target_id ORDER BY loss,user_id) AS position,
 COUNT(*) OVER(PARTITION BY target_id) AS total FROM global_scores
), top_people AS (SELECT target_id,user_id FROM ranked WHERE position<=(total+9)/10),
 domain_samples AS (SELECT * FROM samples WHERE finalized_at>={max(0, now - 365 * 86400000)}),
 bins AS (
 SELECT target_id,user_id,category,MIN(9,probability/10) AS bin,
 ABS(SUM(probability)-SUM(CASE outcome WHEN 'YES' THEN 100 ELSE 0 END)) AS error
 FROM domain_samples GROUP BY target_id,user_id,category,MIN(9,probability/10)
), calibration AS (
 SELECT target_id,user_id,category,SUM(error) AS error FROM bins GROUP BY target_id,user_id,category
), experts AS (
 SELECT s.target_id,s.user_id,s.category FROM domain_samples s
 JOIN calibration c ON c.target_id=s.target_id AND c.user_id=s.user_id AND c.category=s.category
 GROUP BY s.target_id,s.user_id,s.category HAVING COUNT(*)>=20
 AND COUNT(DISTINCT s.finalized_at/86400000)>=3 AND SUM(s.loss)<=2000*COUNT(*) AND MAX(c.error)<=30*COUNT(*)
), votes AS (
 SELECT v.* FROM eligible_user_forecasts v JOIN candidates f ON f.id=v.forecast_id
 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=f.id
 LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id
 WHERE v.submitted_at<={now} AND (d.id IS NULL OR c.created_at<={now})
), cohorts AS (
 SELECT v.forecast_id,AVG(v.yes_probability) AS probability,COUNT(*) AS participant_count,
 AVG(CASE WHEN t.user_id IS NOT NULL THEN v.yes_probability END) AS top_probability,
 COUNT(t.user_id) AS top_count,
 AVG(CASE WHEN x.user_id IS NOT NULL THEN v.yes_probability END) AS expert_probability,
 COUNT(x.user_id) AS expert_count
 FROM votes v JOIN candidates f ON f.id=v.forecast_id
 LEFT JOIN top_people t ON t.target_id=v.forecast_id AND t.user_id=v.user_id
 LEFT JOIN experts x ON x.target_id=v.forecast_id AND x.user_id=v.user_id AND x.category=f.category
 GROUP BY v.forecast_id
)
SELECT f.*,u.display_name AS creator_name,u.handle AS creator_handle,
 t.body AS display_translation,t.translated_at,t.content_hash AS translation_hash,
 h.body AS participation_hold,c.probability,COALESCE(c.participant_count,0) AS participant_count,
 c.top_probability,COALESCE(c.top_count,0) AS top_count,
 c.expert_probability,COALESCE(c.expert_count,0) AS expert_count,{now} AS quality_as_of,
 (SELECT COUNT(*) FROM comments c WHERE c.forecast_id=f.id AND c.created_at<={now}) AS comment_count
FROM candidates f JOIN users u ON u.id=f.creator_id
LEFT JOIN cohorts c ON c.forecast_id=f.id
LEFT JOIN active_participation_holds h ON h.forecast_id=f.id
LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.language='en' AND t.specification_hash=f.specification_hash
"""
