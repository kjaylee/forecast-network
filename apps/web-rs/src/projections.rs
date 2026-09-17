//! Read models (`forecast_application.projections`): forecast cards and the bounded quality SQL.

use serde_json::{json, Map, Value};

use crate::discovery::integer;

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
