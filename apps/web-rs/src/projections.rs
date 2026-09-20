//! Read models (`forecast_application.projections`): forecast cards and the bounded quality SQL.

use serde_json::{json, Map, Value};

use crate::db::{get, int, text, Row};
use crate::discovery::integer;
use crate::reads::PROFILE_CARD_PREFIX;

pub fn display_translation(row: &Map<String, Value>) -> Option<Value> {
    let raw = row.get("display_translation")?.as_str().filter(|s| !s.is_empty())?;
    let value: Value = serde_json::from_str(raw).ok()?;
    if value["specificationHash"] != row["specification_hash"] || value["language"] != json!("en") {
        return None;
    }
    let mut merged = value.as_object()?.clone();
    merged.insert(
        "translatedAt".to_string(),
        row.get("translated_at").cloned().unwrap_or(Value::Null),
    );
    merged.insert(
        "translationHash".to_string(),
        row.get("translation_hash").cloned().unwrap_or(Value::Null),
    );
    Some(Value::Object(merged))
}

fn number_in_range(value: &Value, low: f64, high: f64) -> bool {
    value.as_f64().is_some_and(|f| (low..=high).contains(&f))
}

pub fn card(row: &Map<String, Value>) -> Value {
    let get = |name: &str| row.get(name).cloned().unwrap_or(Value::Null);
    let mut ai: Option<Value> = row
        .get("ai_forecast")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .and_then(|s| serde_json::from_str(s).ok());
    let translation = display_translation(row);
    if let (Some(Value::Object(current)), Some(translation)) = (&ai, &translation) {
        if !translation["aiRationale"].is_null() {
            let mut merged = current.clone();
            merged.insert("rationale".to_string(), translation["aiRationale"].clone());
            merged.insert("rationaleLanguage".to_string(), json!("en"));
            merged.insert("rationaleAttribution".to_string(), translation["attribution"].clone());
            ai = Some(Value::Object(merged));
        }
    }
    if let Some(Value::Object(current)) = &ai {
        let probability = current.get("probability").cloned().unwrap_or(Value::Null);
        let observed = current.get("asOf").cloned().unwrap_or_else(|| get("created_at"));
        let cutoff = row.get("quality_as_of").cloned().unwrap_or_else(|| get("created_at"));
        let valid = number_in_range(&probability, 0.0, 100.0)
            && integer(&observed).is_some_and(|o| o >= 0 && integer(&cutoff).is_some_and(|c| o <= c))
            && current
                .get("provider")
                .and_then(Value::as_str)
                .is_some_and(|p| !p.is_empty())
            && current
                .get("model")
                .and_then(Value::as_str)
                .is_some_and(|m| !m.is_empty());
        let mut merged = current.clone();
        merged.insert("probability".to_string(), if valid { probability } else { Value::Null });
        merged.insert("count".to_string(), json!(if valid { 1 } else { 0 }));
        merged.insert("asOf".to_string(), observed);
        ai = Some(Value::Object(merged));
    }
    let state = get("state");
    let terminal = state == json!("FINALIZED") || state == json!("ARCHIVED");
    let hold = row
        .get("participation_hold")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && !terminal)
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or(Value::Null);
    let translated =
        |field: &str, fallback: &str| translation.as_ref().map_or_else(|| get(fallback), |t| t[field].clone());
    json!({
        "id": get("id"), "title": translated("title", "title"), "question": translated("question", "question"),
        "translationLanguage": translation.as_ref().map_or(Value::Null, |t| t["language"].clone()),
        "sourceLanguage": translation.as_ref().map_or(Value::Null, |t| t["sourceLanguage"].clone()),
        "participationHold": hold,
        "category": get("category").as_str().unwrap_or("").to_lowercase(), "state": state, "revision": get("revision"),
        "openAt": get("open_at"), "closeAt": get("close_at"), "createdAt": get("created_at"),
        "creator": {"id": get("creator_id"), "displayName": get("creator_name"), "handle": get("creator_handle")},
        "crowd": {"probability": get("probability"), "count": get("participant_count")},
        "top": {"probability": get("top_probability"), "count": get("top_count")},
        "expert": {"probability": get("expert_probability"), "count": row.get("expert_count").cloned().unwrap_or(json!(0))},
        "cohortMethodology": {"version": "forecast-quality-v2", "asOf": get("quality_as_of"),
                              "expertise": "Demonstrated category forecasting record, not professional credentials."},
        "ai": ai.unwrap_or_else(|| json!({"probability": null, "provider": null, "model": null, "count": 0})),
        "commentCount": get("comment_count"), "shareCount": get("share_count"),
        "specificationHash": get("specification_hash"),
        "chain": {"network": null, "status": "unconnected", "transaction": null},
    })
}

/// Bound card reads to selected IDs; qualification excludes each target result.
pub fn quality_card_sql(as_of_ms: i64, forecast_ids: &[String]) -> Option<String> {
    if as_of_ms < 0 || forecast_ids.len() > 100 || forecast_ids.iter().any(|v| v.is_empty() || v.len() > 120) {
        return None;
    }
    let identifiers = if forecast_ids.is_empty() {
        "NULL".to_string()
    } else {
        forecast_ids
            .iter()
            .map(|v| format!("'{}'", v.replace('\'', "''")))
            .collect::<Vec<_>>()
            .join(",")
    };
    let now = as_of_ms;
    let year_ago = (now - 365 * 86_400_000).max(0);
    Some(format!(
        "
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
 domain_samples AS (SELECT * FROM samples WHERE finalized_at>={year_ago}),
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
"
    ))
}

// ---------------------------------------------------------------- profile cards

/// `PROFILE_CARD_SQL`. One statement observes the identity, the whole scoring ledger, the
/// translated titles, the newest history and the highlight at a single database snapshot — which is
/// what makes `asOf` a statement about one read rather than about a sequence of them.
/// Nothing calls the card yet: `profile_cards::snapshot` is the caller, and the publish route is
/// part of the write phase this Worker still forwards to the Python Worker.
#[allow(dead_code)]
pub const PROFILE_CARD_SQL: &str = concat!(
    "\nWITH target AS (\n",
    " SELECT id,display_name,handle,created_at FROM users WHERE id=?\n",
    "), finalizations AS (\n",
    " SELECT e.forecast_id,MIN(e.created_at) AS finalized_at\n",
    " FROM events e JOIN eligible_reputation_scores s ON s.forecast_id=e.forecast_id\n",
    " JOIN target u ON u.id=s.user_id\n",
    " WHERE json_extract(e.event,'$.command_name')='finalize'\n",
    " GROUP BY e.forecast_id\n",
    "), ledger AS (\n",
    " SELECT s.forecast_id,s.outcome AS resolved_outcome,s.correct,s.probability,s.brier_score,\n",
    "        f.specification_hash,COALESCE(json_extract(t.body,'$.title'),f.title) AS title,\n",
    "        e.finalized_at\n",
    " FROM eligible_reputation_scores s JOIN target u ON u.id=s.user_id\n",
    " JOIN forecasts f ON f.id=s.forecast_id\n",
    " JOIN finalizations e ON e.forecast_id=s.forecast_id\n",
    " LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.language='en'\n",
    "  AND t.specification_hash=f.specification_hash\n",
    " WHERE f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome=s.outcome\n",
    "), scored AS (\n",
    " SELECT *,\n",
    "   CASE WHEN correct=1 THEN resolved_outcome\n",
    "        WHEN resolved_outcome='YES' THEN 'NO' ELSE 'YES' END AS outcome,\n",
    "   CASE WHEN (resolved_outcome='YES' AND correct=1) OR (resolved_outcome='NO' AND correct=0)\n",
    "        THEN probability ELSE 100-probability END AS confidence,\n",
    "   (probability-CASE WHEN resolved_outcome='YES' THEN 100 ELSE 0 END)*\n",
    "   (probability-CASE WHEN resolved_outcome='YES' THEN 100 ELSE 0 END) AS squared_error\n",
    " FROM ledger WHERE resolved_outcome IN ('YES','NO') AND correct IN (0,1) AND brier_score IS NOT NULL\n",
    "), calibration_bins AS (\n",
    " SELECT MIN(9,CAST(probability/10 AS INTEGER)) AS bin,\n",
    "        SUM(probability) AS probability_sum,\n",
    "        SUM(CASE WHEN resolved_outcome='YES' THEN 100 ELSE 0 END) AS outcome_sum\n",
    " FROM scored GROUP BY bin\n",
    "), newest AS (\n",
    " SELECT * FROM scored ORDER BY finalized_at DESC,forecast_id ASC LIMIT 20\n",
    "), highlight AS (\n",
    " SELECT * FROM scored WHERE correct=1\n",
    " ORDER BY squared_error ASC,finalized_at DESC,forecast_id ASC LIMIT 1\n",
    ")\n",
    "SELECT u.*,\n",
    " (SELECT COUNT(*) FROM eligible_user_forecasts v WHERE v.user_id=u.id) AS total_forecasts,\n",
    " (SELECT COUNT(*) FROM scored) AS resolved_forecasts,\n",
    " (SELECT COALESCE(SUM(correct),0) FROM scored) AS correct_forecasts,\n",
    " (SELECT COUNT(*) FROM ledger WHERE resolved_outcome='INVALID') AS invalid_forecasts,\n",
    " (SELECT COALESCE(SUM(squared_error),0) FROM scored) AS brier_numerator,\n",
    " (SELECT COALESCE(SUM(ABS(probability_sum-outcome_sum)),0) FROM calibration_bins) AS calibration_error_numerator,\n",
    " (SELECT json_group_array(json_object('forecastId',forecast_id,'title',title,\n",
    "   'outcome',outcome,'resolvedOutcome',resolved_outcome,'correct',correct,\n",
    "   'confidence',confidence,'finalizedAt',finalized_at)) FROM newest) AS history_json,\n",
    " (SELECT json_object('forecastId',forecast_id,'title',title,'outcome',outcome,\n",
    "   'confidence',confidence,'resolvedAt',finalized_at,'specificationHash',specification_hash)\n",
    "  FROM highlight) AS highlight_json\n",
    "FROM target u\n",
);

/// `profile_card_payload`. Only the documented public fields — no account secret, no wallet.
#[allow(dead_code)]
pub fn profile_card_payload(row: &Row, as_of: i64) -> Value {
    let count = int(row, "resolved_forecasts").unwrap_or(0);
    let mut history: Vec<Value> = serde_json::from_str(text(row, "history_json").unwrap_or("[]")).unwrap_or_default();
    for item in &mut history {
        // SQLite reports the flag as `0`/`1`; the card reports it as a boolean, and the canonical
        // encoding of `true` is not the encoding of `1`.
        item["correct"] = json!(item["correct"].as_i64().unwrap_or(0) != 0);
    }
    let highlight: Value = text(row, "highlight_json")
        .filter(|text| !text.is_empty())
        .and_then(|text| serde_json::from_str(text).ok())
        .unwrap_or(Value::Null);
    let correct = int(row, "correct_forecasts").unwrap_or(0);
    let brier = int(row, "brier_numerator").unwrap_or(0);
    let calibration = int(row, "calibration_error_numerator").unwrap_or(0);
    json!({
        "schemaVersion": 1, "asOf": as_of,
        "user": {"id": get(row, "id"), "displayName": get(row, "display_name"),
                 "handle": get(row, "handle"), "createdAt": get(row, "created_at")},
        "metrics": {
            "totalForecasts": get(row, "total_forecasts"), "resolvedForecasts": count,
            "correctForecasts": correct, "invalidForecasts": get(row, "invalid_forecasts"),
            // Integer division first, then the float: Python computes `correct*100/count` as a
            // float, and a port that divided in integer arithmetic would report `0` for a perfect
            // score below 100.
            "accuracy": if count != 0 { Some(correct as f64 * 100.0 / count as f64) } else { None },
            "brierScore": if count != 0 { Some(brier as f64 / (10000.0 * count as f64)) } else { None },
            "calibrationScore": if count != 0 { Some(1.0 - calibration as f64 / (100.0 * count as f64)) } else { None },
        },
        "sampleStatus": if count == 0 { "new" } else if count < 10 { "provisional" } else { "established" },
        "history": history,
        "historyTruncated": count > history.len() as i64,
        "highlight": highlight,
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
            "prefix": PROFILE_CARD_PREFIX,
            "canonicalization": "profile-card-json-v1: compact UTF-8 JSON with recursively sorted keys; verify the exact supplied canonicalJson bytes"},
    })
}

/// `profile_card_json`: the versioned display encoding, which is the *commitment* rule — non-ASCII
/// raw, keys sorted, compact. `forecast_domain`'s integer-only rules do not apply to a payload whose
/// accuracy is a float.
#[allow(dead_code)]
pub fn profile_card_json(payload: &Value) -> String {
    crate::source_watch::compact(payload)
}
