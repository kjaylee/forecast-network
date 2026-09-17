//! Pure, reproducible quality ranking (`forecast_application.discovery`), SQL and scoring
//! verbatim so the edge and the Python Worker rank identically.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub const DAY_MS: i64 = 86_400_000;
pub const DISCOVERY_VERSION: &str = "forecast-discovery-v2";
pub const WEIGHTS_BP: [(&str, i64); 5] = [
    ("clarity", 4000),
    ("creator", 2000),
    ("adjudication", 1500),
    ("engagement", 1500),
    ("freshness", 1000),
];

#[derive(Debug)]
pub struct Invalid(pub String);

fn count(row: &Map<String, Value>, name: &str) -> Result<i64, Invalid> {
    match row.get(name) {
        None | Some(Value::Null) => Ok(0),
        Some(value) => integer(value)
            .filter(|v| *v >= 0)
            .ok_or_else(|| Invalid(format!("Invalid {name}"))),
    }
}

/// D1 hands integral numbers as floats through JavaScript; treat integral values as integers.
pub fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n.as_i64().or_else(|| {
            n.as_f64()
                .filter(|f| f.fract() == 0.0 && f.abs() < 9.0e15)
                .map(|f| f as i64)
        }),
        _ => None,
    }
}

fn clarity(row: &Map<String, Value>) -> Result<i64, Invalid> {
    let mut ambiguity = row.get("ambiguity_score_bp").and_then(integer);
    if ambiguity.is_none() {
        let snapshot = match row.get("snapshot") {
            Some(Value::String(text)) => serde_json::from_str::<Value>(text).ok(),
            Some(other) => Some(other.clone()),
            None => None,
        };
        ambiguity = snapshot
            .as_ref()
            .and_then(|s| integer(&s["specification"]["ambiguity_score_bp"]));
    }
    match ambiguity {
        Some(value) if (0..=10000).contains(&value) => Ok(10000 - value),
        _ => Err(Invalid("Missing or invalid measured ambiguity_score_bp".to_string())),
    }
}

pub fn score_forecast(row: &Map<String, Value>, as_of_ms: i64) -> Result<Value, Invalid> {
    let created = count(row, "created_at")?;
    let opened = count(row, "open_at")?;
    let closed = count(row, "close_at")?;
    let finalized = count(row, "creator_finalized_count")?;
    let invalid = count(row, "creator_invalid_count")?;
    let reviewed = count(row, "creator_reviewed_disputes")?;
    let material = count(row, "creator_material_disputes")?;
    if invalid > finalized || material > reviewed {
        return Err(Invalid("Quality numerator exceeds sample count".to_string()));
    }
    let clarity = clarity(row)?;
    let participants = count(row, "participant_count")?.min(50);
    let comments = count(row, "comment_count")?.min(20);
    let shares = count(row, "share_count")?.min(20);
    let age_days = (as_of_ms.div_euclid(DAY_MS) - created.div_euclid(DAY_MS)).max(0);
    let components: BTreeMap<&str, i64> = BTreeMap::from([
        ("clarity", clarity),
        ("creator", (finalized - invalid + 2) * 10000 / (finalized + 4)),
        ("adjudication", (reviewed - material + 2) * 10000 / (reviewed + 4)),
        ("engagement", (participants * 3 + comments + shares) * 10000 / 190),
        ("freshness", 10000 / (1 + age_days)),
    ]);
    let hold = row
        .get("participation_hold")
        .is_some_and(|v| !v.is_null() && v != &json!(""));
    let eligible = row.get("eligible").and_then(integer) == Some(1) || row.get("eligible") == Some(&Value::Bool(true));
    let active = row.get("state") == Some(&json!("OPEN"))
        && created <= as_of_ms
        && opened <= as_of_ms
        && as_of_ms < closed
        && !hold
        && eligible;
    let score: i64 = WEIGHTS_BP
        .iter()
        .map(|(key, weight)| components[key] * weight)
        .sum::<i64>()
        / 10000;
    Ok(json!({
        "version": DISCOVERY_VERSION, "asOf": as_of_ms, "active": active, "scoreBp": score,
        "inputs": {
            "ambiguityScoreBp": 10000 - clarity, "creatorFinalized": finalized, "creatorInvalid": invalid,
            "reviewedDisputes": reviewed, "materialDisputes": material, "participantsCapped": participants,
            "commentsCapped": comments, "sharesCapped": shares, "ageDays": age_days,
        },
        "componentsBp": components,
        "weightsBp": WEIGHTS_BP.iter().map(|(k, v)| (k.to_string(), json!(v))).collect::<Map<String, Value>>(),
        "creatorSampleStatus": if finalized == 0 { "new" } else if finalized < 5 { "provisional" } else { "established" },
        "reasons": [
            "Measured question clarity", "Finalized creator outcomes with a neutral four-result prior",
            "Resolved material disputes with a neutral four-review prior",
            "Participation, comments and shares capped at 15% of total score", "UTC-day freshness",
        ],
    }))
}

fn tie_coefficients(as_of_ms: i64, user_id: Option<&str>) -> Vec<i64> {
    let seed = serde_json::to_string(&json!([
        DISCOVERY_VERSION,
        as_of_ms.div_euclid(DAY_MS),
        user_id.unwrap_or("")
    ]))
    .expect("seed");
    Sha256::digest(seed.as_bytes())
        .iter()
        .map(|b| i64::from(*b) + 1)
        .collect()
}

/// Portable SQL/Python daily tie key; final ID breaks collisions.
pub fn tie_break(identifier: &str, as_of_ms: i64, user_id: Option<&str>) -> i64 {
    identifier
        .chars()
        .take(32)
        .zip(tie_coefficients(as_of_ms, user_id))
        .map(|(c, weight)| i64::from(u32::from(c)) * weight)
        .sum()
}

pub fn tie_sql(as_of_ms: i64, user_id: Option<&str>, column: &str) -> String {
    let terms: Vec<String> = tie_coefficients(as_of_ms, user_id)
        .iter()
        .enumerate()
        .map(|(index, weight)| format!("COALESCE(unicode(substr({column},{},1)),0)*{weight}", index + 1))
        .collect();
    format!("({})", terms.join("+"))
}

/// Full-inventory scoring in SQL; callers append filters/order before LIMIT.
pub fn candidate_sql(as_of_ms: i64, user_id: Option<&str>) -> String {
    let now = as_of_ms;
    let day = now.div_euclid(DAY_MS);
    let tie = tie_sql(now, user_id, "f.id");
    format!(
        "
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
 CAST(MAX(0,{day}-f.created_at/86400000) AS INTEGER) AS age_days,
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
"
    )
}

/// Rank the entire candidate set before pagination, preserving input columns.
pub fn rank_forecasts(
    rows: &[Map<String, Value>],
    as_of_ms: i64,
    user_id: Option<&str>,
) -> Result<Vec<Map<String, Value>>, Invalid> {
    let mut result = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for row in rows {
        let identifier = row
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string);
        let Some(identifier) = identifier.filter(|id| !seen.contains(id)) else {
            return Err(Invalid("Missing or duplicate forecast ID".to_string()));
        };
        seen.push(identifier.clone());
        let score = score_forecast(row, as_of_ms)?;
        if score["active"] == Value::Bool(true) {
            let mut ranked = row.clone();
            ranked.insert("quality".to_string(), score);
            ranked.insert(
                "discoveryTie".to_string(),
                json!(tie_break(&identifier, as_of_ms, user_id)),
            );
            result.push(ranked);
        }
    }
    result.sort_by(|a, b| {
        let key = |r: &Map<String, Value>| {
            (
                -r["quality"]["scoreBp"].as_i64().unwrap_or(0),
                r["discoveryTie"].as_i64().unwrap_or(0),
                r["id"].as_str().unwrap_or("").to_string(),
            )
        };
        key(a).cmp(&key(b))
    });
    Ok(result)
}

/// Reserve every fifth slot for a clear cold-start question when available; prefer categories
/// with fewer than two picks; exact quality/tie ordering within each pool.
pub fn recommendations(
    rows: &[Map<String, Value>],
    as_of_ms: i64,
    user_id: Option<&str>,
    limit: usize,
) -> Result<Vec<Map<String, Value>>, Invalid> {
    let mut pool = rank_forecasts(rows, as_of_ms, user_id)?;
    let mut selected: Vec<Map<String, Value>> = Vec::new();
    let mut categories: BTreeMap<String, usize> = BTreeMap::new();
    let category = |row: &Map<String, Value>| {
        row.get("category")
            .map(|c| c.as_str().map_or_else(|| c.to_string(), str::to_string))
            .unwrap_or_default()
            .to_lowercase()
    };
    while !pool.is_empty() && selected.len() < limit {
        let mut reason = "quality";
        let mut choices: Vec<usize> = (0..pool.len()).collect();
        if (selected.len() + 1).is_multiple_of(5) {
            let cold: Vec<usize> = choices
                .iter()
                .copied()
                .filter(|i| {
                    pool[*i]["quality"]["inputs"]["creatorFinalized"].as_i64().unwrap_or(0) < 5
                        && pool[*i]["quality"]["componentsBp"]["clarity"].as_i64().unwrap_or(0) >= 7000
                })
                .collect();
            if !cold.is_empty() {
                choices = cold;
                reason = "clear-cold-start";
            }
        }
        let diverse: Vec<usize> = choices
            .iter()
            .copied()
            .filter(|i| categories.get(&category(&pool[*i])).copied().unwrap_or(0) < 2)
            .collect();
        if !diverse.is_empty() {
            choices = diverse;
        }
        let index = choices[0];
        let mut choice = pool.remove(index);
        *categories.entry(category(&choice)).or_default() += 1;
        choice.insert("recommendationReason".to_string(), json!(reason));
        selected.push(choice);
    }
    Ok(selected)
}
