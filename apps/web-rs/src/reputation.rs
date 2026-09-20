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
    // The reference's exported row carries all three timestamps, and this port keeps the record
    // whole even though its own callers read only `finalized_at`. The alternative — dropping the
    // fields and re-deriving them at the export — is how a record quietly stops being the one the
    // reference hands out.
    #[allow(dead_code)]
    pub submitted_at: i64,
    pub finalized_at: i64,
    #[allow(dead_code)]
    pub eligibility_at: i64,
}

impl History {
    /// `eligible_history`'s own row shape. The two extra timestamps are part of the reference's
    /// export, and a port that dropped them would be exporting a different record under the same
    /// name.
    #[allow(dead_code)]
    pub fn to_json(&self) -> Value {
        json!({
            "userId": self.user_id, "forecastId": self.forecast_id, "category": self.category,
            "probabilityBp": self.probability_bp, "outcome": self.outcome,
            "submittedAt": self.submitted_at, "finalizedAt": self.finalized_at,
            "eligibilityAt": self.eligibility_at,
        })
    }
}

/// The reference raises `ValueError` and lets it reach the caller as a server fault: a scoring
/// history that is malformed is not a request to be answered differently, it is a corpus that
/// cannot be scored. A port that quietly skipped the row instead would report a *different*
/// quality score for the same input, which is worse than failing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReputationError(pub String);

impl std::fmt::Display for ReputationError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// `_integer`'s bound check.
///
/// The reference also refuses a value that is not a Python `int` at all, and a Rust `i64` cannot
/// be one — that half of the guard is discharged by the signature rather than by a runtime check,
/// which is why the vector carries such a case and this function does not.
fn checked_bound(value: i64, name: &str, minimum: i64, maximum: Option<i64>) -> Result<i64, ReputationError> {
    if value < minimum || maximum.is_some_and(|bound| value > bound) {
        return Err(ReputationError(format!("Invalid {name}")));
    }
    Ok(value)
}

/// `_identifier` for an argument the signature already says is a string.
fn checked_name(value: &str, name: &str) -> Result<String, ReputationError> {
    if value.is_empty() {
        return Err(ReputationError(format!("Invalid {name}")));
    }
    Ok(value.to_string())
}

/// `_identifier`: a non-empty string, and nothing else.
fn identifier(value: Option<&Value>, name: &str) -> Result<String, ReputationError> {
    match value.and_then(Value::as_str) {
        Some(text) if !text.is_empty() => Ok(text.to_string()),
        _ => Err(ReputationError(format!("Invalid {name}"))),
    }
}

/// `reputation.eligible_history`: fail closed on missing evidence.
///
/// `window_ms` and `exclude_forecast_id` are not optional extras: `qualified_cohorts` uses both,
/// and the exclusion is what keeps a forecast from contributing to the cohort that then scores it.
pub fn eligible_history(
    rows: &[Row],
    as_of_ms: i64,
    window_ms: Option<i64>,
    exclude_forecast_id: Option<&str>,
) -> Result<Vec<History>, ReputationError> {
    checked_bound(as_of_ms, "as_of_ms", 0, None)?;
    if let Some(window) = window_ms {
        checked_bound(window, "window_ms", 1, None)?;
    }
    let mut result = Vec::new();
    let mut seen: Vec<(String, String)> = Vec::new();
    for row in rows {
        // `type(row.get("eligible")) not in (bool, int) or ... != 1`: a JSON true is accepted,
        // because Python's `True == 1`.
        let eligible = matches!(get(row, "eligible"), Value::Bool(true)) || int(row, "eligible") == Some(1);
        if !eligible || !matches!(text(row, "state"), Some("FINALIZED" | "ARCHIVED")) {
            continue;
        }
        let outcome = text(row, "outcome").unwrap_or("");
        if !matches!(outcome, "YES" | "NO") || text(row, "finalized_outcome") != Some(outcome) {
            continue;
        }
        let times: Vec<Option<&Value>> = ["submitted_at", "finalized_at", "eligibility_at"]
            .iter()
            .map(|name| row.get(*name))
            .collect();
        if times
            .iter()
            .any(|value| value.is_none_or(|v| v.as_i64().is_none_or(|n| n < 0 || n > as_of_ms)))
        {
            continue;
        }
        let (submitted, finalized, eligibility) = (
            times[0].unwrap().as_i64().unwrap(),
            times[1].unwrap().as_i64().unwrap(),
            times[2].unwrap().as_i64().unwrap(),
        );
        if submitted > finalized || eligibility < finalized {
            continue;
        }
        if window_ms.is_some_and(|window| finalized < as_of_ms - window) {
            continue;
        }
        if exclude_forecast_id.is_some_and(|excluded| text(row, "forecast_id") == Some(excluded)) {
            continue;
        }
        let Some(probability) = int(row, "probability").filter(|p| (0..=100).contains(p)) else {
            continue;
        };
        // An identifier that is not a non-empty string is a corpus fault, not a row to skip.
        let user = identifier(row.get("user_id"), "user_id")?;
        let forecast = identifier(row.get("forecast_id"), "forecast_id")?;
        let category = identifier(row.get("category"), "category")?.to_lowercase();
        let key = (user.clone(), forecast.clone());
        if seen.contains(&key) {
            return Err(ReputationError(
                "Duplicate scoring history: one eligible result per user and forecast".to_string(),
            ));
        }
        seen.push(key);
        result.push(History {
            user_id: user,
            forecast_id: forecast,
            category,
            probability_bp: probability * 100,
            outcome: outcome.to_string(),
            submitted_at: submitted,
            finalized_at: finalized,
            eligibility_at: eligibility,
        });
    }
    result.sort_by(|a, b| {
        (a.finalized_at, &a.forecast_id, &a.user_id).cmp(&(b.finalized_at, &b.forecast_id, &b.user_id))
    });
    Ok(result)
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

fn qualification(history: &[&History], category: &str, as_of_ms: i64) -> Value {
    let sample: Vec<&History> = history
        .iter()
        .filter(|r| r.category == category && r.finalized_at >= as_of_ms - EXPERT_WINDOW_MS)
        .copied()
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
///
/// `category` narrows the history before anything is measured, and the one-user check is not
/// decoration: every window and every expertise row below is a statement about a single
/// forecaster, and a mixed corpus would report one person's quality under another's name.
pub fn reputation_quality(rows: &[Row], as_of_ms: i64, category: Option<&str>) -> Result<Value, ReputationError> {
    let mut history = eligible_history(rows, as_of_ms, None, None)?;
    let users: std::collections::BTreeSet<&str> = history.iter().map(|r| r.user_id.as_str()).collect();
    if users.len() > 1 {
        return Err(ReputationError(
            "reputation_quality requires one user's history".to_string(),
        ));
    }
    // The requested category is reported even when nothing matches it: the reference builds the
    // expertise list from the *request*, so a category with no eligible history is a row of zeros
    // rather than an absence — and an absence reads as "not asked" to whoever displays it.
    let mut requested: Option<String> = None;
    if let Some(category) = category {
        let category = checked_name(category, "category")?.to_lowercase();
        history.retain(|row| row.category == category);
        requested = Some(category);
    }
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
    let mut categories: Vec<String> = match &requested {
        Some(category) => vec![category.clone()],
        None => {
            let mut seen: Vec<String> = history.iter().map(|r| r.category.clone()).collect();
            seen.sort();
            seen.dedup();
            seen
        }
    };
    categories.dedup();
    let consistency = if enough {
        let max = losses.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let min = losses.iter().cloned().fold(f64::INFINITY, f64::min);
        Some(1.0 - (max - min))
    } else {
        None
    };
    Ok(json!({
        "methodologyVersion": QUALITY_VERSION, "asOf": as_of_ms, "consistencyScore": consistency,
        "consistency": {"status": if enough { "established" } else if !losses.is_empty() { "provisional" } else { "new" },
                        "minimumPerWindow": CONSISTENCY_MIN_PER_WINDOW, "windowDays": 30, "windows": windows,
                        "meaning": "Stability of mean Brier loss across three windows; consistency does not imply accuracy."},
        "expertise": categories.iter().map(|c| qualification(&history.iter().collect::<Vec<_>>(), c, as_of_ms)).collect::<Vec<_>>(),
        "eligibleHistoryCount": history.len(),
    }))
}

/// `reputation.qualified_cohorts`: the two memberships a signed feed may weight.
///
/// The target forecast is excluded from the history that qualifies it. Without that, a
/// forecaster's own submission to the question being aggregated would help decide whether they
/// count as an expert on it — the leak the exclusion exists to close.
///
/// The two cohorts overlap on purpose and are not meant to be summed; the comment on the Python
/// function says so, and the returned ids are application-internal, not addresses.
pub fn qualified_cohorts(
    rows: &[Row],
    forecast_id: &str,
    category: &str,
    as_of_ms: i64,
) -> Result<Value, ReputationError> {
    let category = checked_name(category, "category")?.to_lowercase();
    let history = eligible_history(rows, as_of_ms, None, Some(forecast_id))?;
    // Grouping follows the history's order, which is sorted by (finalizedAt, forecastId, userId) —
    // the same order the reference's `defaultdict` sees.
    let mut grouped: Vec<(String, Vec<&History>)> = Vec::new();
    for record in &history {
        match grouped.iter_mut().find(|(user, _)| *user == record.user_id) {
            Some((_, sample)) => sample.push(record),
            None => grouped.push((record.user_id.clone(), vec![record])),
        }
    }
    let mut qualified: Vec<String> = Vec::new();
    let mut ranked: Vec<(f64, String)> = Vec::new();
    for (user, sample) in &grouped {
        if qualification(sample, &category, as_of_ms)["qualified"] == json!(true) {
            qualified.push(user.clone());
        }
        if sample.len() >= 10 {
            let (_, brier, _) = metrics(sample);
            ranked.push((brier.unwrap_or(0.0), user.clone()));
        }
    }
    // Python sorts `(brierScore, user)` tuples: the score first, then the id as a total tiebreak.
    ranked.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.1.cmp(&b.1))
    });
    // `(len(ranked) + 9) // 10`: the top decile, rounded up, which is Python's expression.
    let cutoff = ranked.len().div_ceil(10);
    let mut top: Vec<String> = ranked[..cutoff].iter().map(|(_, user)| user.clone()).collect();
    top.sort();
    qualified.sort();
    Ok(json!({"top": top, "expert": qualified}))
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
    let quality = reputation_quality(&quality_rows, now_ms, None).map_err(|error| worker::Error::from(error.0))?;
    if let (Value::Object(target), Value::Object(quality)) = (&mut result, quality) {
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

#[cfg(test)]
mod golden_tests {
    use super::*;

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/reputation-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("reputation golden")).expect("json")
    }

    /// The fixture a call names, as the flat rows the projections read.
    fn rows_of(document: &Value, entry: &Value) -> Vec<Row> {
        let name = entry["input"]["rows"].as_str().expect("a call names its fixture");
        document["fixtures"][name]
            .as_array()
            .unwrap_or_else(|| panic!("fixture {name}"))
            .iter()
            .map(|row| row.as_object().cloned().expect("a fixture row is an object"))
            .collect()
    }

    /// `eligible_history`'s result as the reference exports it.
    fn exported(history: &[History]) -> Value {
        Value::Array(history.iter().map(History::to_json).collect())
    }

    /// Check one call against the vector, refusal and all.
    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, ReputationError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap().to_string();
        match produced {
            Ok(value) => {
                assert!(
                    entry["error"].is_null(),
                    "{name}: succeeded where the reference refused"
                );
                assert_eq!(value, entry["result"], "{name}: a different result");
            }
            Err(error) => {
                assert!(
                    !entry["error"].is_null(),
                    "{name}: refused with {error:?} where the reference succeeded"
                );
                assert_eq!(
                    Some(error.0.as_str()),
                    entry["error"]["message"].as_str(),
                    "{name}: a different refusal"
                );
            }
        }
        *index += 1;
    }

    #[test]
    fn the_reference_quality_projections_are_reproduced_call_for_call() {
        let document = golden();
        let now = document["now"].as_i64().unwrap();
        let calls = document["calls"].as_array().expect("calls").clone();
        let mut index = 0usize;
        let argument = |entry: &Value, field: &str| entry["input"].get(field).cloned().unwrap_or(Value::Null);
        let number = |entry: &Value, field: &str| entry["input"].get(field).and_then(Value::as_i64);
        let name = |entry: &Value, field: &str| entry["input"].get(field).and_then(Value::as_str).map(str::to_string);

        for position in 0..calls.len() {
            let entry = calls[position].clone();
            let call = entry["call"].as_str().unwrap();
            let mut rows = rows_of(&document, &entry);
            // Two cases carry a row-level override in the vector: a corpus row whose identifier is
            // not a non-empty string is what the reference refuses. It is named here rather than
            // inferred from the input keys, because `category` is an *argument* to the other two
            // functions and a port that read it as a column would rewrite the corpus.
            let overridden: &[&str] = match call {
                "history:identifier-not-a-string" => &["user_id"],
                "history:identifier-empty" => &["forecast_id"],
                _ => &[],
            };
            for column in overridden {
                if let Some(value) = entry["input"].get(*column) {
                    rows[0].insert((*column).to_string(), value.clone());
                }
            }
            let produced = match entry["kind"].as_str().unwrap() {
                "eligible_history" => {
                    // The reference's `type(as_of_ms) is not int` guard cannot be violated through
                    // this signature — a Rust `i64` is an integer — so the case is asserted to be
                    // a refusal rather than replayed. See the note on `checked_bound`.
                    let Some(as_of) = number(&entry, "as_of_ms")
                        .or_else(|| matches!(argument(&entry, "as_of_ms"), Value::Null).then_some(now))
                    else {
                        assert!(!entry["error"].is_null(), "{call}: the vector expects a refusal");
                        index += 1;
                        continue;
                    };
                    let _ = argument(&entry, "as_of_ms");
                    if !number(&entry, "as_of_ms").is_some() && entry["input"]["as_of_ms"].is_string() {
                        assert!(!entry["error"].is_null(), "{call}: the vector expects a refusal");
                        index += 1;
                        continue;
                    }
                    eligible_history(
                        &rows,
                        as_of,
                        number(&entry, "window_ms"),
                        name(&entry, "exclude_forecast_id").as_deref(),
                    )
                    .map(|history| exported(&history))
                }
                "reputation_quality" => {
                    if !entry["input"]["category"].is_null() && !entry["input"]["category"].is_string() {
                        // The same for `_identifier(category)`: the signature says `&str`.
                        assert!(!entry["error"].is_null(), "{call}: the vector expects a refusal");
                        index += 1;
                        continue;
                    }
                    reputation_quality(
                        &rows,
                        number(&entry, "as_of_ms").unwrap_or(now),
                        name(&entry, "category").as_deref(),
                    )
                }
                "qualified_cohorts" => {
                    if !entry["input"]["category"].is_null() && !entry["input"]["category"].is_string() {
                        assert!(!entry["error"].is_null(), "{call}: the vector expects a refusal");
                        index += 1;
                        continue;
                    }
                    qualified_cohorts(
                        &rows,
                        name(&entry, "forecast_id").unwrap_or_default().as_str(),
                        name(&entry, "category").unwrap_or_default().as_str(),
                        number(&entry, "as_of_ms").unwrap_or(now),
                    )
                }
                other => panic!("{call}: unknown kind {other}"),
            };
            check(&calls, &mut index, produced);
        }

        assert_eq!(index, calls.len(), "every recorded call is replayed");
        // The corpus is built so the top decile is two people: a port that rounded the decile
        // differently would return one or three, and a signature over a different cohort is a
        // different feed.
        assert_eq!(calls[14]["result"]["top"].as_array().unwrap().len(), 2);
        assert_eq!(calls[14]["result"]["expert"].as_array().unwrap().len(), 3);
        assert!(
            calls[19]["result"]["top"].as_array().unwrap().is_empty(),
            "below eleven rankers, nobody"
        );
    }
}
