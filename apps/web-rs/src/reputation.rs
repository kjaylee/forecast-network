//! Reputation read models (`projections.scores`, `reputation.reputation_quality`,
//! `Application.reputation`) with Python float evaluation order preserved.

use std::collections::BTreeMap;

use num_bigint::BigInt;
use num_rational::BigRational;
use num_traits::ToPrimitive;
use serde_json::{json, Value};
use worker::*;

use crate::db::{all, first, get, int, text, Row};

pub const DAY_MS: i64 = 86_400_000;
pub const QUALITY_VERSION: &str = "forecast-quality-v2";
pub const EXPERT_WINDOW_MS: i64 = 365 * DAY_MS;
pub const CONSISTENCY_WINDOW_MS: i64 = 30 * DAY_MS;
pub const EXPERT_MIN_SAMPLES: usize = 20;
pub const EXPERT_MIN_DAYS: usize = 3;
pub const CONSISTENCY_MIN_PER_WINDOW: usize = 5;

fn number(value: &Value) -> f64 {
    value.as_f64().unwrap_or(0.0)
}

/// `projections.scores`: accuracy, Brier and ten-bin reliability over resolved rows.
pub fn scores(rows: &[&Row]) -> Value {
    let resolved: Vec<&Row> = rows
        .iter()
        .copied()
        .filter(|r| !get(r, "brier_score").is_null())
        .collect();
    let count = resolved.len();
    let correct: f64 = resolved.iter().map(|r| number(get(r, "correct"))).sum();
    let correct_int: i64 = resolved.iter().map(|r| int(r, "correct").unwrap_or(0)).sum();
    let brier = if count > 0 {
        Some(resolved.iter().map(|r| number(get(r, "brier_score"))).sum::<f64>() / count as f64)
    } else {
        None
    };
    let mut bins: BTreeMap<i64, Vec<&Row>> = BTreeMap::new();
    let mut order: Vec<i64> = Vec::new();
    for row in &resolved {
        let bin = (int(row, "probability").unwrap_or(0) / 10).min(9);
        if !bins.contains_key(&bin) {
            order.push(bin);
        }
        bins.entry(bin).or_default().push(row);
    }
    let mut calibration_error = 0.0f64;
    for bin in order {
        let group = &bins[&bin];
        let n = group.len() as f64;
        let mean_probability: f64 = group.iter().map(|r| number(get(r, "probability")) / 100.0).sum::<f64>() / n;
        let mean_outcome: f64 = group.iter().filter(|r| get(r, "outcome") == &json!("YES")).count() as f64 / n;
        calibration_error += n * (mean_probability - mean_outcome).abs();
    }
    let invalid = rows.iter().filter(|r| get(r, "outcome") == &json!("INVALID")).count();
    json!({
        "resolvedForecasts": count, "correctForecasts": correct_int, "invalidForecasts": invalid,
        "accuracy": if count > 0 { Some(correct / count as f64 * 100.0) } else { None },
        "brierScore": brier,
        "calibrationScore": if count > 0 { Some(1.0 - calibration_error / count as f64) } else { None },
        "consistencyScore": Value::Null,
    })
}

#[derive(Debug, Clone)]
pub struct History {
    pub user_id: String,
    pub forecast_id: String,
    pub category: String,
    pub probability_bp: i64,
    pub outcome: String,
    pub finalized_at: i64,
}

/// `reputation.eligible_history`: fail closed on missing evidence.
pub fn eligible_history(rows: &[Row], as_of_ms: i64) -> Vec<History> {
    let mut result = Vec::new();
    let mut seen: Vec<(String, String)> = Vec::new();
    for row in rows {
        if int(row, "eligible") != Some(1) || !matches!(text(row, "state"), Some("FINALIZED" | "ARCHIVED")) {
            continue;
        }
        let outcome = text(row, "outcome").unwrap_or("");
        if !matches!(outcome, "YES" | "NO") || text(row, "finalized_outcome") != Some(outcome) {
            continue;
        }
        let times: Vec<Option<i64>> = ["submitted_at", "finalized_at", "eligibility_at"]
            .iter()
            .map(|n| int(row, n))
            .collect();
        if times.iter().any(|t| t.is_none_or(|v| v < 0 || v > as_of_ms)) {
            continue;
        }
        let (submitted, finalized, eligibility) = (times[0].unwrap(), times[1].unwrap(), times[2].unwrap());
        if submitted > finalized || eligibility < finalized {
            continue;
        }
        let Some(probability) = int(row, "probability").filter(|p| (0..=100).contains(p)) else {
            continue;
        };
        let user = text(row, "user_id").unwrap_or("").to_string();
        let forecast = text(row, "forecast_id").unwrap_or("").to_string();
        let category = text(row, "category").unwrap_or("").to_lowercase();
        let key = (user.clone(), forecast.clone());
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        result.push(History {
            user_id: user,
            forecast_id: forecast,
            category,
            probability_bp: probability * 100,
            outcome: outcome.to_string(),
            finalized_at: finalized,
        });
    }
    result.sort_by(|a, b| {
        (a.finalized_at, &a.forecast_id, &a.user_id).cmp(&(b.finalized_at, &b.forecast_id, &b.user_id))
    });
    result
}

fn fraction_float(numerator: i64, denominator: i64) -> f64 {
    BigRational::new(BigInt::from(numerator), BigInt::from(denominator))
        .to_f64()
        .unwrap_or(0.0)
}

fn metrics(history: &[&History]) -> (usize, Option<f64>, Option<f64>) {
    let count = history.len();
    if count == 0 {
        return (0, None, None);
    }
    let mut loss: i64 = 0;
    let mut bins: BTreeMap<i64, (i64, i64)> = BTreeMap::new();
    for row in history {
        let actual = if row.outcome == "YES" { 10000 } else { 0 };
        loss += (row.probability_bp - actual).pow(2);
        let bucket = bins.entry((row.probability_bp / 1000).min(9)).or_default();
        bucket.0 += row.probability_bp;
        bucket.1 += actual;
    }
    let error: i64 = bins.values().map(|(p, y)| (p - y).abs()).sum();
    let brier = fraction_float(loss, 100_000_000 * count as i64);
    let calibration = BigRational::from_integer(BigInt::from(1))
        - BigRational::new(BigInt::from(error), BigInt::from(10000 * count as i64));
    (count, Some(brier), Some(calibration.to_f64().unwrap_or(0.0)))
}

fn qualification(history: &[History], category: &str, as_of_ms: i64) -> Value {
    let sample: Vec<&History> = history
        .iter()
        .filter(|r| r.category == category && r.finalized_at >= as_of_ms - EXPERT_WINDOW_MS)
        .collect();
    let (count, brier, calibration) = metrics(&sample);
    let mut days: Vec<i64> = sample.iter().map(|r| r.finalized_at.div_euclid(DAY_MS)).collect();
    days.sort();
    days.dedup();
    let enough = count >= EXPERT_MIN_SAMPLES && days.len() >= EXPERT_MIN_DAYS;
    let qualified = enough && brier.is_some_and(|b| b <= 0.20) && calibration.is_some_and(|c| c >= 0.70);
    let mut value = json!({
        "category": category, "count": count, "brierScore": brier, "calibrationScore": calibration,
        "distinctFinalizationDays": days.len(),
        "status": if qualified { "qualified" } else if enough { "not-qualified" } else if !sample.is_empty() { "provisional" } else { "new" },
        "qualified": qualified, "windowStart": (as_of_ms - EXPERT_WINDOW_MS).max(0),
        "minimumSamples": EXPERT_MIN_SAMPLES, "minimumDays": EXPERT_MIN_DAYS, "maximumBrier": 0.20, "minimumCalibration": 0.70,
    });
    value["category"] = json!(category);
    value
}

/// `reputation.reputation_quality` for one user.
pub fn reputation_quality(rows: &[Row], as_of_ms: i64) -> Value {
    let history = eligible_history(rows, as_of_ms);
    let mut windows = Vec::new();
    let mut losses: Vec<f64> = Vec::new();
    let mut enough = true;
    for offset in [3i64, 2, 1] {
        let start = as_of_ms - offset * CONSISTENCY_WINDOW_MS;
        let end = start + CONSISTENCY_WINDOW_MS;
        let sample: Vec<&History> = history
            .iter()
            .filter(|r| start <= r.finalized_at && (r.finalized_at < end || (offset == 1 && r.finalized_at == end)))
            .collect();
        let (count, brier, calibration) = metrics(&sample);
        enough &= count >= CONSISTENCY_MIN_PER_WINDOW;
        if let Some(b) = brier {
            losses.push(b);
        }
        windows.push(json!({"start": start.max(0), "end": end.max(0), "count": count, "brierScore": brier, "calibrationScore": calibration}));
    }
    let mut categories: Vec<String> = history.iter().map(|r| r.category.clone()).collect();
    categories.sort();
    categories.dedup();
    let consistency = if enough {
        let max = losses.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let min = losses.iter().cloned().fold(f64::INFINITY, f64::min);
        Some(1.0 - (max - min))
    } else {
        None
    };
    json!({
        "methodologyVersion": QUALITY_VERSION, "asOf": as_of_ms, "consistencyScore": consistency,
        "consistency": {"status": if enough { "established" } else if !losses.is_empty() { "provisional" } else { "new" },
                        "minimumPerWindow": CONSISTENCY_MIN_PER_WINDOW, "windowDays": 30, "windows": windows,
                        "meaning": "Stability of mean Brier loss across three windows; consistency does not imply accuracy."},
        "expertise": categories.iter().map(|c| qualification(&history, c, as_of_ms)).collect::<Vec<_>>(),
        "eligibleHistoryCount": history.len(),
    })
}

/// `Application.reputation`.
pub async fn reputation(session: &D1DatabaseSession, user_id: &str, now_ms: i64) -> Result<Value> {
    let rows = all(
        session,
        "SELECT * FROM eligible_reputation_scores WHERE user_id=?",
        &[json!(user_id)],
    )
    .await?;
    let refs: Vec<&Row> = rows.iter().collect();
    let mut result = scores(&refs);
    let quality_rows = all(
        session,
        "SELECT * FROM forecast_quality_history WHERE user_id=? AND finalized_at<=? AND eligibility_at<=?",
        &[json!(user_id), json!(now_ms), json!(now_ms)],
    )
    .await?;
    if let (Value::Object(target), Value::Object(quality)) = (&mut result, reputation_quality(&quality_rows, now_ms)) {
        for (key, value) in quality {
            target.insert(key, value);
        }
    }
    let total = first(
        session,
        "SELECT COUNT(*) AS n FROM eligible_user_forecasts WHERE user_id=?",
        &[json!(user_id)],
    )
    .await?;
    result["totalForecasts"] = total.as_ref().map_or(json!(0), |t| get(t, "n").clone());
    let mut categories: Vec<String> = rows
        .iter()
        .filter_map(|r| text(r, "category").map(str::to_string))
        .collect();
    categories.sort();
    categories.dedup();
    let mut domain = Vec::new();
    for category in &categories {
        let subset: Vec<&Row> = rows.iter().filter(|r| text(r, "category") == Some(category)).collect();
        let mut entry = scores(&subset);
        entry["category"] = json!(category.to_lowercase());
        domain.push(entry);
    }
    result["domainScores"] = json!(domain);
    let disputed = all(
        session,
        "SELECT DISTINCT a.hash FROM events e JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash') \
         WHERE json_extract(e.event,'$.command_name')='submit_dispute' AND json_extract(a.body,'$.disputant_id')=?",
        &[json!(user_id)],
    )
    .await?;
    let reviews = all(
        session,
        "SELECT DISTINCT r.hash,r.body FROM events e JOIN artifacts r ON r.hash=json_extract(e.event,'$.artifact_hash') \
         JOIN artifacts d ON d.hash=json_extract(r.body,'$.dispute_hash') \
         WHERE json_extract(e.event,'$.command_name')='review_dispute' AND json_extract(d.body,'$.disputant_id')=?",
        &[json!(user_id)],
    )
    .await?;
    let successful = reviews
        .iter()
        .filter(|r| {
            serde_json::from_str::<Value>(text(r, "body").unwrap_or("{}"))
                .ok()
                .is_some_and(|b| b["material_conflict"] == json!(true))
        })
        .count();
    result["totalDisputes"] = json!(disputed.len());
    result["resolvedDisputes"] = json!(reviews.len());
    result["successfulDisputes"] = json!(successful);
    result["disputeAccuracy"] = if reviews.is_empty() {
        Value::Null
    } else {
        json!(successful as f64 / reviews.len() as f64 * 100.0)
    };
    let creator = first(
        session,
        "SELECT COUNT(*) AS n,SUM(CASE WHEN finalized_outcome='INVALID' THEN 1 ELSE 0 END) AS invalid FROM forecasts WHERE creator_id=? AND finalized_outcome IS NOT NULL",
        &[json!(user_id)],
    )
    .await?;
    result["creatorQuality"] = match creator.as_ref().and_then(|c| int(c, "n")).filter(|n| *n > 0) {
        Some(n) => json!(1.0 - creator.as_ref().and_then(|c| int(c, "invalid")).unwrap_or(0) as f64 / n as f64),
        None => Value::Null,
    };
    Ok(result)
}
