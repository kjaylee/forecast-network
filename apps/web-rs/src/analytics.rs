//! Read-only, event-backed product KPIs with explicit UTC denominators and maturity.
//!
//! No wall clock, no click collection, no provider price inference and no financial conversion.
//! The adapter takes one consistent read batch and the aggregation is pure, so a report can be
//! replayed from a retained snapshot — which is the only way a number nobody can reproduce is
//! distinguishable from a number that is wrong.
//!
//! The definitions are the whole content of this module, and three of them are decisions:
//!
//!   - **Identity is merged before anything is counted.** An alias of a participant is that
//!     participant, and excluding one alias excludes the merged identity — including for older
//!     windows, which is why a deletion restates history.
//!   - **Maturity is a status, not a zero.** A retention window that has not matured reports no
//!     value rather than `0`, because "nobody came back" and "it is too early to know" are
//!     different findings and only one of them is about the product.
//!   - **An operating expense does not cease to exist when its operator is excluded** from human
//!     engagement metrics. Population admission and question scope govern expense inclusion;
//!     account deletion never retroactively erases paid costs.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Map, Value};

use forecast_domain::content_hash;

use crate::db::{text, Database};

/// `product-analytics-v1`.
pub const VERSION: &str = "product-analytics-v1";
pub const DAY_MS: i64 = 86_400_000;
pub const MAX_ROWS: usize = 200_000;
const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;
const EXCLUDED_PREFIXES: [&str; 10] = [
    "staff:", "staff_", "test:", "test_", "load:", "load_", "sandbox:", "fixture:", "e2e:", "smoke:",
];
const NON_PARTICIPANT_IDS: [&str; 1] = ["system_editorial"];

fn check(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message.to_string())
    }
}

/// `_window`: a complete UTC-midnight window that ends by `as_of`.
pub fn window(start: i64, end: i64, as_of: i64) -> Result<(), String> {
    check(
        [start, end, as_of]
            .iter()
            .all(|value| (0..=MAX_SAFE_INTEGER).contains(value)),
        "invalid KPI timestamp",
    )?;
    check(
        start < end && end <= as_of,
        "KPI windows must be complete and end by as_of_ms",
    )?;
    check(
        start % DAY_MS == 0 && end % DAY_MS == 0,
        "KPI windows must use UTC midnight boundaries",
    )?;
    check(end - start <= 366 * DAY_MS, "KPI window exceeds 366 days")
}

/// `_ratio`: basis points, with the three statuses that are not a number.
///
/// `no_denominator` and `out_of_range` are not failures: the first says there was nothing to
/// measure, the second says the measurement does not fit in an exact JSON integer. Collapsing
/// either into `0` would report a finding that was never made.
pub fn ratio(numerator: i64, denominator: i64, unit: &str) -> Value {
    let scaled = if denominator != 0 {
        Some((numerator * 10_000 + denominator / 2) / denominator)
    } else {
        None
    };
    let out_of_range = scaled.is_some_and(|value| value > MAX_SAFE_INTEGER);
    let scaled = if out_of_range { None } else { scaled };
    json!({
        "numerator": numerator, "denominator": denominator, "unit": unit,
        "valueBp": if unit == "share" { scaled.into() } else { Value::Null },
        "valueScaled": scaled, "scale": 10_000,
        "status": if out_of_range { "out_of_range" } else if denominator != 0 { "available" } else { "no_denominator" },
    })
}

/// A snapshot row, read through the same helpers the rest of the port uses.
fn field<'a>(row: &'a Map<String, Value>, name: &str) -> Option<&'a Value> {
    row.get(name)
}

fn number(row: &Map<String, Value>, name: &str) -> i64 {
    field(row, name).and_then(Value::as_i64).unwrap_or(0)
}

fn words(row: &Map<String, Value>, name: &str) -> String {
    field(row, name).and_then(Value::as_str).unwrap_or_default().to_string()
}

fn rows(snapshot: &Value, name: &str) -> Vec<Map<String, Value>> {
    snapshot[name]
        .as_array()
        .map(|items| items.iter().filter_map(Value::as_object).cloned().collect())
        .unwrap_or_default()
}

/// `aggregate_product_analytics`.
#[allow(clippy::too_many_arguments)]
pub fn aggregate_product_analytics(
    snapshot: &Value,
    as_of_ms: i64,
    window_start_ms: i64,
    window_end_ms: i64,
    cohort_start_ms: i64,
    cohort_end_ms: i64,
    population_kind: &str,
    excluded_user_ids: &[String],
) -> Result<Value, String> {
    window(window_start_ms, window_end_ms, as_of_ms)?;
    window(cohort_start_ms, cohort_end_ms, as_of_ms)?;
    check(
        ["application", "fixture", "isolated-load"].contains(&population_kind),
        "unknown KPI population",
    )?;

    let aliases: BTreeMap<String, String> = rows(snapshot, "identities")
        .iter()
        .map(|row| (words(row, "alias_user_id"), words(row, "canonical_user_id")))
        .collect();
    let identity = |value: &str| -> Result<String, String> {
        let mut current = value.to_string();
        let mut seen: BTreeSet<String> = BTreeSet::new();
        while let Some(next) = aliases.get(&current) {
            check(
                !seen.contains(&current) && seen.len() < 128,
                "cyclic or excessive analytics identity links",
            )?;
            seen.insert(current.clone());
            current = next.clone();
        }
        Ok(current)
    };
    for alias in aliases.keys() {
        identity(alias)?;
    }

    let user_rows = rows(snapshot, "users");
    let users: BTreeMap<String, Map<String, Value>> =
        user_rows.iter().map(|row| (words(row, "id"), row.clone())).collect();
    let mut excluded: BTreeSet<String> = excluded_user_ids.iter().cloned().collect();
    excluded.extend(NON_PARTICIPANT_IDS.iter().map(|id| (*id).to_string()));
    for row in rows(snapshot, "exclusions") {
        if words(&row, "subject_kind") == "user" {
            excluded.insert(words(&row, "subject_id"));
        }
    }
    excluded.extend(
        users
            .keys()
            .filter(|user| {
                EXCLUDED_PREFIXES
                    .iter()
                    .any(|prefix| user.to_lowercase().starts_with(prefix))
            })
            .cloned(),
    );
    // Deleting or excluding one alias excludes its merged identity, even for older windows.
    let mut resolved_excluded: BTreeSet<String> = BTreeSet::new();
    for user in &excluded {
        resolved_excluded.insert(identity(user)?);
    }
    let excluded = resolved_excluded;

    let excluded_forecasts: BTreeSet<String> = rows(snapshot, "exclusions")
        .iter()
        .filter(|row| words(row, "subject_kind") == "forecast")
        .map(|row| words(row, "subject_id"))
        .collect();
    let mut active_users: BTreeSet<String> = BTreeSet::new();
    for user in users.keys() {
        let canonical = identity(user)?;
        if !excluded.contains(&canonical) && users.contains_key(&canonical) {
            active_users.insert(canonical);
        }
    }
    let mut created: BTreeMap<String, i64> = BTreeMap::new();
    for (user, row) in &users {
        let canonical = identity(user)?;
        if active_users.contains(&canonical) {
            let at = number(row, "created_at");
            created
                .entry(canonical)
                .and_modify(|current| *current = (*current).min(at))
                .or_insert(at);
        }
    }

    let forecasts: BTreeMap<String, Map<String, Value>> = rows(snapshot, "forecasts")
        .into_iter()
        .filter(|row| {
            let id = words(row, "id");
            !excluded_forecasts.contains(&id)
                && !EXCLUDED_PREFIXES
                    .iter()
                    .any(|prefix| id.to_lowercase().starts_with(prefix))
        })
        .map(|row| (words(&row, "id"), row))
        .collect();

    let mut activity: BTreeMap<(String, String, i64), i64> = BTreeMap::new();
    for row in rows(snapshot, "activity") {
        let user = identity(&words(&row, "user_id"))?;
        let forecast = words(&row, "forecast_id");
        if !active_users.contains(&user) || !forecasts.contains_key(&forecast) {
            continue;
        }
        let key = (user, forecast, number(&row, "day_ms"));
        let first = number(&row, "first_at");
        activity
            .entry(key)
            .and_modify(|current| *current = (*current).min(first))
            .or_insert(first);
    }
    // First activation must be derived from allowed forecasts, not an excluded test question.
    let mut firsts: BTreeMap<String, i64> = BTreeMap::new();
    for row in rows(snapshot, "firsts") {
        let user = identity(&words(&row, "user_id"))?;
        if active_users.contains(&user) && forecasts.contains_key(&words(&row, "forecast_id")) {
            let at = number(&row, "first_at");
            firsts
                .entry(user)
                .and_modify(|current| *current = (*current).min(at))
                .or_insert(at);
        }
    }

    let window_activity: BTreeSet<(String, String, i64)> = activity
        .keys()
        .filter(|(_, _, day)| window_start_ms <= *day && *day < window_end_ms)
        .cloned()
        .collect();
    let predictors: BTreeSet<&String> = window_activity.iter().map(|(user, _, _)| user).collect();
    let questions: BTreeSet<&String> = window_activity.iter().map(|(_, forecast, _)| forecast).collect();

    let mut daily = Vec::new();
    let mut day = window_start_ms;
    while day < window_end_ms {
        let entries: BTreeSet<(&String, &String)> = window_activity
            .iter()
            .filter(|(_, _, at)| *at == day)
            .map(|(user, forecast, _)| (user, forecast))
            .collect();
        let distinct_users: BTreeSet<&String> = entries.iter().map(|(user, _)| *user).collect();
        let distinct_questions: BTreeSet<&String> = entries.iter().map(|(_, forecast)| *forecast).collect();
        daily.push(json!({
            "dayStartMs": day, "dayEndMs": day + DAY_MS,
            "activePredictors": distinct_users.len(), "activeQuestions": distinct_questions.len(),
            "distinctPredictorQuestionDays": entries.len(),
        }));
        day += DAY_MS;
    }

    let new_users: BTreeSet<String> = created
        .iter()
        .filter(|(_, at)| window_start_ms <= **at && **at < window_end_ms)
        .map(|(user, _)| user.clone())
        .collect();
    let mature_users: BTreeSet<String> = new_users
        .iter()
        .filter(|user| created[*user] + 7 * DAY_MS <= as_of_ms)
        .cloned()
        .collect();
    let activated: BTreeSet<String> = mature_users
        .iter()
        .filter(|user| {
            firsts
                .get(*user)
                .is_some_and(|first| created[*user] <= *first && *first < created[*user] + 7 * DAY_MS)
        })
        .cloned()
        .collect();
    let new_questions: BTreeSet<String> = forecasts
        .iter()
        .filter(|(_, row)| {
            let at = number(row, "created_at");
            window_start_ms <= at && at < window_end_ms
        })
        .map(|(id, _)| id.clone())
        .collect();
    let mature_questions: BTreeSet<String> = new_questions
        .iter()
        .filter(|id| number(&forecasts[*id], "created_at") + 7 * DAY_MS <= as_of_ms)
        .cloned()
        .collect();
    // External participation: a submission on a question that is not the submitter's own.
    let mut participated: BTreeSet<String> = BTreeSet::new();
    for ((user, forecast, _), at) in &activity {
        if !mature_questions.contains(forecast) {
            continue;
        }
        let row = &forecasts[forecast];
        let owner = identity(&words(row, "creator_id"))?;
        let created_at = number(row, "created_at");
        if user != &owner && created_at <= *at && *at < created_at + 7 * DAY_MS {
            participated.insert(forecast.clone());
        }
    }

    let mut activity_days: BTreeMap<String, BTreeSet<i64>> = BTreeMap::new();
    for (user, _, day) in activity.keys() {
        activity_days.entry(user.clone()).or_default().insert(*day);
    }
    let mut cohorts = Vec::new();
    let mut day = cohort_start_ms;
    while day < cohort_end_ms {
        let members: BTreeSet<String> = firsts
            .iter()
            .filter(|(_, at)| (*at).div_euclid(DAY_MS) * DAY_MS == day)
            .map(|(user, _)| user.clone())
            .collect();
        let mut retention = Map::new();
        for offset in [1i64, 7, 30] {
            let target = day + offset * DAY_MS;
            let mature = target + DAY_MS <= as_of_ms;
            let numerator = if mature {
                members
                    .iter()
                    .filter(|user| activity_days.get(*user).is_some_and(|days| days.contains(&target)))
                    .count() as i64
            } else {
                0
            };
            let mut metric = ratio(numerator, members.len() as i64, "share");
            metric["mature"] = json!(mature);
            metric["targetStartMs"] = json!(target);
            metric["targetEndMs"] = json!(target + DAY_MS);
            if !mature {
                metric["numerator"] = Value::Null;
                metric["valueBp"] = Value::Null;
                metric["valueScaled"] = Value::Null;
                metric["status"] = json!("immature");
            }
            retention.insert(format!("D{offset}"), metric);
        }
        cohorts.push(json!({
            "cohortStartMs": day, "cohortEndMs": day + DAY_MS,
            "activatedUsers": members.len(), "retention": Value::Object(retention),
        }));
        day += DAY_MS;
    }
    let mut retention_totals = Map::new();
    for name in ["D1", "D7", "D30"] {
        let mut numerator = 0i64;
        let mut denominator = 0i64;
        let mut immature_users = 0i64;
        for cohort in &cohorts {
            let metric = &cohort["retention"][name];
            if metric["mature"].as_bool() == Some(true) {
                numerator += metric["numerator"].as_i64().unwrap_or(0);
                denominator += metric["denominator"].as_i64().unwrap_or(0);
            } else {
                immature_users += cohort["activatedUsers"].as_i64().unwrap_or(0);
            }
        }
        let mut total = ratio(numerator, denominator, "share");
        total["immatureUsers"] = json!(immature_users);
        if denominator == 0 && immature_users > 0 {
            total["status"] = json!("immature");
        }
        retention_totals.insert(name.to_string(), total);
    }

    // A user-week is anchored to Monday so the report is comparable across windows.
    let mut weekly: BTreeMap<(String, i64), BTreeSet<i64>> = BTreeMap::new();
    for (user, _, day) in &window_activity {
        let week = (day + 3 * DAY_MS).div_euclid(7 * DAY_MS) * (7 * DAY_MS) - 3 * DAY_MS;
        if window_start_ms <= week && week + 7 * DAY_MS <= window_end_ms {
            weekly.entry((user.clone(), week)).or_default().insert(*day);
        }
    }
    let weekly_total: usize = weekly.values().map(BTreeSet::len).sum();

    let finalized: BTreeSet<String> = forecasts
        .iter()
        .filter(|(_, row)| {
            field(row, "finalized_at")
                .and_then(Value::as_i64)
                .is_some_and(|at| window_start_ms <= at && at < window_end_ms)
        })
        .map(|(id, _)| id.clone())
        .collect();
    let outcome_of = |id: &String| {
        forecasts[id]
            .get("finalized_outcome")
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let known_finalized: BTreeSet<String> = finalized
        .iter()
        .filter(|id| matches!(outcome_of(id).as_deref(), Some("YES") | Some("NO") | Some("INVALID")))
        .cloned()
        .collect();
    let invalid: BTreeSet<String> = finalized
        .iter()
        .filter(|id| outcome_of(id).as_deref() == Some("INVALID"))
        .cloned()
        .collect();
    let mut creator_counts: BTreeMap<String, (i64, i64)> = BTreeMap::new();
    for id in &known_finalized {
        let creator = identity(&words(&forecasts[id], "creator_id"))?;
        if !active_users.contains(&creator) {
            continue;
        }
        let entry = creator_counts.entry(creator).or_insert((0, 0));
        entry.0 += 1;
        if invalid.contains(id) {
            entry.1 += 1;
        }
    }
    let mut creator_quality: Vec<Value> = creator_counts
        .values()
        .map(|(total, bad)| ratio(total - bad, *total, "share"))
        .collect();
    // Ordered by denominator then numerator, and by creator where those tie — the reference
    // iterates a set here, so a tie has no order to reproduce.
    creator_quality.sort_by_key(|value| {
        (
            value["denominator"].as_i64().unwrap_or(0),
            value["numerator"].as_i64().unwrap_or(0),
        )
    });

    let dispute_rows = rows(snapshot, "disputes");
    let mut disputes: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    for row in &dispute_rows {
        let user = words(row, "user_id");
        let admitted = words(row, "command_name") == "submit_dispute"
            && forecasts.contains_key(&words(row, "forecast_id"))
            && users.contains_key(&user)
            && active_users.contains(&identity(&user)?);
        if admitted {
            disputes.insert(words(row, "artifact_hash"), row.clone());
        }
    }
    let mut submitted_disputes: BTreeSet<String> = BTreeSet::new();
    for (key, row) in &disputes {
        let at = number(row, "created_at");
        if window_start_ms <= at && at < window_end_ms {
            submitted_disputes.insert(key.clone());
        }
    }
    let mut reviews: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    for row in &dispute_rows {
        let hash = words(row, "dispute_hash");
        let at = number(row, "created_at");
        let matched = words(row, "command_name") == "review_dispute"
            && disputes.contains_key(&hash)
            && words(row, "forecast_id") == words(&disputes[&hash], "forecast_id")
            && window_start_ms <= at
            && at < window_end_ms;
        if matched {
            reviews.insert(hash, row.clone());
        }
    }
    let material = reviews
        .values()
        .filter(|row| number(row, "material_conflict") == 1)
        .count() as i64;
    let disputed_questions: BTreeSet<String> = known_finalized
        .iter()
        .filter(|id| disputes.values().any(|row| &words(row, "forecast_id") == *id))
        .cloned()
        .collect();

    let clarity: Vec<i64> = new_questions
        .iter()
        .filter_map(|id| field(&forecasts[id], "clarity_bp").and_then(Value::as_i64))
        .collect();

    // An operating expense does not cease to exist when its operator is excluded from human
    // engagement metrics.
    let qualified_costs: Vec<Map<String, Value>> = rows(snapshot, "costs")
        .into_iter()
        .filter(|row| {
            words(row, "population_kind") == population_kind
                && (field(row, "forecast_id").and_then(Value::as_str).is_none()
                    || forecasts.contains_key(&words(row, "forecast_id")))
        })
        .collect();
    let mut costs = Map::new();
    for (kind, unit) in [("provider", "USD_MICRO"), ("chain", "DEVNET_LAMPORT")] {
        let matching: Vec<&Map<String, Value>> = qualified_costs
            .iter()
            .filter(|row| words(row, "source_kind") == kind)
            .collect();
        let subtotal = |status: &str| -> i64 {
            matching
                .iter()
                .filter(|row| words(row, "status") == status)
                .map(|row| number(row, "amount_atomic"))
                .sum()
        };
        let known = subtotal("known");
        let estimated = subtotal("estimated");
        check(
            known.max(estimated) <= MAX_SAFE_INTEGER,
            "cost subtotal exceeds exact JSON integer range",
        )?;
        let count = |status: &str| matching.iter().filter(|row| words(row, "status") == status).count() as i64;
        costs.insert(
            kind.to_string(),
            json!({
                "unit": unit, "knownRecordedSubtotalAtomic": known, "knownOperations": count("known"),
                "unknownRecordedOperations": count("unknown"),
                "estimatedRecordedSubtotalAtomic": estimated, "estimatedOperations": count("estimated"),
                "coverage": "partial", "completeActualTotalAtomic": Value::Null,
                "knownSubtotalPerActivePredictor": ratio(known, predictors.len() as i64, &format!("{unit}_per_predictor")),
                "knownSubtotalPerActiveQuestion": ratio(known, questions.len() as i64, &format!("{unit}_per_question")),
            }),
        );
    }
    let signatures: BTreeSet<String> = rows(snapshot, "deliveries")
        .iter()
        .filter(|row| forecasts.contains_key(&words(row, "forecast_id")))
        .map(|row| words(row, "signature"))
        .collect();
    let known_signatures: BTreeSet<String> = qualified_costs
        .iter()
        .filter(|row| words(row, "source_kind") == "chain" && words(row, "status") == "known")
        .map(|row| words(row, "operation_id"))
        .collect();
    // The chain and provider sides complete the cost picture, and each answers a question the
    // subtotals cannot: how many deliveries have no recorded fee, and how much of the reserve is
    // held across every population rather than this one.
    let chain = costs.get_mut("chain").expect("the chain costs");
    chain["deliverySignaturesWithoutActualFee"] = json!(signatures.difference(&known_signatures).count());
    chain["observedDeliverySignatures"] = json!(signatures.len());
    chain["reservedLamportsAllPopulations"] = json!(snapshot["reserved"][0]["reserved"]);
    let provider = costs.get_mut("provider").expect("the provider costs");
    provider["retainedAiForecastRecordsInWindow"] = json!(new_questions
        .iter()
        .filter(|id| number(&forecasts[*id], "has_ai") != 0)
        .count());
    provider["historicalProviderOperationCount"] = Value::Null;

    let population_kind_value = json!(population_kind);
    let excluded_identities = excluded.iter().filter(|user| users.contains_key(*user)).count();
    let excluded_sorted: Vec<String> = {
        let mut sorted = excluded_user_ids.to_vec();
        sorted.sort();
        sorted
    };
    let input_hash = content_hash(&json!({
        "snapshot": snapshot, "asOfMs": as_of_ms, "window": [window_start_ms, window_end_ms],
        "cohorts": [cohort_start_ms, cohort_end_ms], "populationKind": population_kind,
        "excludedUserIds": excluded_sorted,
    }))
    .map_err(|error| error.to_string())?;

    Ok(json!({
        "formulaVersion": VERSION, "asOfMs": as_of_ms,
        "window": {"startMs": window_start_ms, "endMs": window_end_ms, "timezone": "UTC", "complete": true},
        "population": {
            "kind": population_kind_value,
            "evidenceClass": if population_kind == "application" { "application_database_observations" } else { "fixture_or_load_only" },
            "excludedIdentities": excluded_identities, "deduplicatedIdentities": active_users.len(),
            "exclusionPolicy": "explicit-permanent-rules-and-reserved-id-namespaces",
            "unclassifiedAccounts": active_users.len(), "humanIdentityVerified": false,
        },
        "inputHash": input_hash,
        "activity": {
            "activePredictors": predictors.len(), "activeQuestions": questions.len(),
            "distinctPredictorQuestionDays": window_activity.len(), "daily": daily,
            "openQuestionsAtAsOf": forecasts.values().filter(|row| {
                words(row, "state_at") == "OPEN" && number(row, "on_hold") == 0
                    && number(row, "open_at") <= as_of_ms && as_of_ms < number(row, "close_at")
            }).count(),
        },
        "activationWithin7Days": {
            "numerator": activated.len(), "denominator": mature_users.len(), "unit": "share",
            "valueBp": ratio(activated.len() as i64, mature_users.len() as i64, "share")["valueBp"],
            "valueScaled": ratio(activated.len() as i64, mature_users.len() as i64, "share")["valueScaled"],
            "scale": 10_000,
            "status": ratio(activated.len() as i64, mature_users.len() as i64, "share")["status"],
            "newAccounts": new_users.len(), "immatureAccounts": new_users.len() - mature_users.len(),
        },
        "creationToExternalParticipationWithin7Days": {
            "numerator": participated.len(), "denominator": mature_questions.len(), "unit": "share",
            "valueBp": ratio(participated.len() as i64, mature_questions.len() as i64, "share")["valueBp"],
            "valueScaled": ratio(participated.len() as i64, mature_questions.len() as i64, "share")["valueScaled"],
            "scale": 10_000,
            "status": ratio(participated.len() as i64, mature_questions.len() as i64, "share")["status"],
            "publishedQuestions": new_questions.len(), "immatureQuestions": new_questions.len() - mature_questions.len(),
        },
        "cohortWindow": {"startMs": cohort_start_ms, "endMs": cohort_end_ms, "anchor": "first_eligible_prediction_UTC_day"},
        "cohorts": cohorts, "retention": Value::Object(retention_totals),
        "weeklyActiveDaysPerActiveUserWeek": ratio(weekly_total as i64, weekly.len() as i64, "days_per_active_user_week"),
        "quality": {
            "finalizedQuestions": finalized.len(), "unavailableFinalizedOutcomes": finalized.len() - known_finalized.len(),
            "invalidity": ratio(invalid.len() as i64, known_finalized.len() as i64, "share"),
            "questionValidity": ratio((known_finalized.len() - invalid.len()) as i64, known_finalized.len() as i64, "share"),
            "publishedQuestionClarity": {
                "numerator": clarity.iter().sum::<i64>(), "denominator": 10_000 * clarity.len() as i64,
                "unit": "share", "scale": 10_000,
                "valueBp": ratio(clarity.iter().sum::<i64>(), 10_000 * clarity.len() as i64, "share")["valueBp"],
                "valueScaled": ratio(clarity.iter().sum::<i64>(), 10_000 * clarity.len() as i64, "share")["valueScaled"],
                "status": ratio(clarity.iter().sum::<i64>(), 10_000 * clarity.len() as i64, "share")["status"],
                "measuredQuestions": clarity.len(), "unavailableQuestions": new_questions.len() - clarity.len(),
            },
            "creatorsWithFinalizedResults": creator_quality.len(), "creatorValidityRatios": creator_quality,
        },
        "disputes": {
            "submitted": submitted_disputes.len(), "reviewed": reviews.len(),
            "materialConflictAmongReviewed": ratio(material, reviews.len() as i64, "share"),
            "disputedAmongFinalizedQuestions": ratio(disputed_questions.len() as i64, known_finalized.len() as i64, "share"),
            "questionsWithDisputes": submitted_disputes.iter().filter_map(|key| disputes.get(key)).map(|row| words(row, "forecast_id")).collect::<BTreeSet<String>>().len(),
        },
        "costs": costs,
        "costs": Value::Object(costs),
        "limitations": [
            "Later eligibility decisions, admitted identity links or deletion overlays may restate historical windows.",
            "Identity merges require admitted account evidence; shared IPs are never merged.",
            "No complete provider/chain expense ledger exists; known subtotals are not total cost.",
            "Fixture retention tests do not prove real-user D30 retention.",
        ],
    }))
}

/// The read half: one consistent batch, then the pure aggregation.
#[allow(clippy::too_many_arguments)]
pub async fn product_analytics(
    db: &dyn Database,
    as_of_ms: i64,
    window_start_ms: i64,
    window_end_ms: i64,
    cohort_start_ms: Option<i64>,
    cohort_end_ms: Option<i64>,
    population_kind: &str,
    excluded_user_ids: &[String],
) -> Result<Value, String> {
    let cohort_start = cohort_start_ms.unwrap_or(window_start_ms);
    let cohort_end = cohort_end_ms.unwrap_or(window_end_ms);
    window(window_start_ms, window_end_ms, as_of_ms)?;
    window(cohort_start, cohort_end, as_of_ms)?;
    check(
        ["application", "fixture", "isolated-load"].contains(&population_kind),
        "unknown KPI population",
    )?;
    let activity_start = window_start_ms.min(cohort_start);
    let activity_end = as_of_ms.min((window_end_ms + 7 * DAY_MS).max(cohort_end + 31 * DAY_MS));
    let at = vec![json!(as_of_ms); 5];
    let limit = json!(MAX_ROWS + 1);
    let mut statements: Vec<(String, Vec<Value>)> = Vec::new();
    let mut push = |sql: &str, params: Vec<Value>| statements.push((sql.to_string(), params));

    push(
        "SELECT id,created_at FROM users WHERE created_at<? ORDER BY id LIMIT ?",
        vec![json!(as_of_ms), limit.clone()],
    );
    push(
        "SELECT f.id,f.creator_id,f.created_at,f.open_at,f.close_at,f.finalized_outcome,f.ai_forecast IS NOT NULL AS has_ai,\
         CASE WHEN json_type(f.snapshot,'$.specification.ambiguity_score_bp')='integer' \
         AND json_extract(f.snapshot,'$.specification.ambiguity_score_bp') BETWEEN 0 AND 10000 \
         THEN 10000-json_extract(f.snapshot,'$.specification.ambiguity_score_bp') ELSE NULL END AS clarity_bp,\
         EXISTS(SELECT 1 FROM participation_hold_events h WHERE h.forecast_id=f.id AND h.action='hold' AND h.created_at<? \
         AND NOT EXISTS(SELECT 1 FROM participation_hold_events z WHERE z.forecast_id=h.forecast_id \
         AND z.revision>h.revision AND z.created_at<?)) AS on_hold,\
         COALESCE((SELECT json_extract(e.event,'$.new_state') FROM events e WHERE e.forecast_id=f.id AND e.created_at<? \
         ORDER BY e.revision DESC LIMIT 1),'UNKNOWN') AS state_at,\
         (SELECT MIN(e.created_at) FROM events e WHERE e.forecast_id=f.id AND e.created_at<? \
         AND json_extract(e.event,'$.command_name')='finalize') AS finalized_at \
         FROM forecasts f WHERE f.created_at<? ORDER BY f.id LIMIT ?",
        {
            let mut params = at.clone();
            params.push(limit.clone());
            params
        },
    );
    // `_ELIGIBLE` takes five instants; the sixth is the row bound.
    push(
        &format!(
            "{ELIGIBLE}SELECT user_id,forecast_id,MIN(created_at) AS first_at FROM eligible \
                  GROUP BY user_id,forecast_id ORDER BY user_id,forecast_id LIMIT ?"
        ),
        {
            let mut params = at.clone();
            params.push(limit.clone());
            params
        },
    );
    push(
        &format!("{ELIGIBLE}SELECT user_id,forecast_id,CAST(created_at/86400000 AS INTEGER)*86400000 AS day_ms,\
                  MIN(created_at) AS first_at,COUNT(*) AS submissions FROM eligible WHERE created_at>=? AND created_at<? \
                  GROUP BY user_id,forecast_id,day_ms ORDER BY user_id,forecast_id,day_ms LIMIT ?"),
        {
            let mut params = vec![json!(as_of_ms); 5];
            params.push(json!(activity_start));
            params.push(json!(activity_end));
            params.push(limit.clone());
            params
        },
    );
    push(
        "SELECT e.forecast_id,e.created_at,json_extract(e.event,'$.command_name') AS command_name,a.hash AS artifact_hash,\
         json_extract(a.body,'$.disputant_id') AS user_id,json_extract(a.body,'$.dispute_hash') AS dispute_hash,\
         json_extract(a.body,'$.material_conflict') AS material_conflict \
         FROM events e JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash') \
         WHERE e.created_at<? AND json_extract(e.event,'$.command_name') IN ('submit_dispute','review_dispute') \
         ORDER BY e.created_at,e.revision LIMIT ?",
        vec![json!(as_of_ms), limit.clone()],
    );
    push(
        "SELECT * FROM product_analytics_exclusions ORDER BY subject_kind,subject_id,reason LIMIT ?",
        vec![limit.clone()],
    );
    push(
        "SELECT alias_user_id,canonical_user_id FROM product_analytics_identity_links ORDER BY alias_user_id LIMIT ?",
        vec![limit.clone()],
    );
    push(
        "SELECT c.* FROM product_cost_receipts c WHERE c.occurred_at>=? AND c.occurred_at<? AND c.recorded_at<? \
         AND c.population_kind=? AND NOT EXISTS(SELECT 1 FROM product_cost_receipts later \
         WHERE later.source_kind=c.source_kind AND later.operation_id=c.operation_id AND later.revision>c.revision \
         AND later.recorded_at<?) ORDER BY c.source_kind,c.operation_id LIMIT ?",
        vec![
            json!(window_start_ms),
            json!(window_end_ms),
            json!(as_of_ms),
            json!(population_kind),
            json!(as_of_ms),
            limit.clone(),
        ],
    );
    push(
        "SELECT forecast_id,signature,submitted_at,status FROM registry_delivery \
         WHERE submitted_at>=? AND submitted_at<? AND signature IS NOT NULL ORDER BY signature LIMIT ?",
        vec![json!(window_start_ms), json!(window_end_ms), limit.clone()],
    );
    push(
        "SELECT COALESCE(SUM(reserved_lamports),0) AS reserved FROM registry_spend WHERE day>=? AND day<?",
        vec![json!(window_start_ms / DAY_MS), json!(window_end_ms / DAY_MS)],
    );

    let results = db.batch(&statements).await.map_err(|error| error.to_string())?;
    check(results.len() == statements.len(), "incomplete analytics snapshot")?;
    let names = [
        "users",
        "forecasts",
        "firsts",
        "activity",
        "disputes",
        "exclusions",
        "identities",
        "costs",
        "deliveries",
        "reserved",
    ];
    let mut snapshot = Map::new();
    for (name, result) in names.iter().zip(results.iter()) {
        check(result.len() <= MAX_ROWS, "analytics snapshot exceeds row bound")?;
        snapshot.insert(
            (*name).to_string(),
            Value::Array(result.iter().cloned().map(Value::Object).collect()),
        );
    }
    aggregate_product_analytics(
        &Value::Object(snapshot),
        as_of_ms,
        window_start_ms,
        window_end_ms,
        cohort_start,
        cohort_end,
        population_kind,
        excluded_user_ids,
    )
}

/// `_ELIGIBLE`: every accepted submission remains an immutable event, so the KPI reads the events
/// rather than a latest-choice view — an edit on a later day must not erase an earlier active day.
const ELIGIBLE: &str = "
WITH eligible AS (
 SELECT e.forecast_id,e.revision,e.hash,e.created_at,
        json_extract(a.body,'$.forecaster_id') AS user_id
 FROM events e JOIN forecasts f ON f.id=e.forecast_id
 JOIN artifacts a ON a.hash=json_extract(e.event,'$.artifact_hash')
 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=e.forecast_id AND d.created_at<?
 LEFT JOIN forecast_receipt_eligibility r ON r.forecast_id=e.forecast_id AND r.revision=e.revision
  AND r.decision_id=d.id
 WHERE json_extract(e.event,'$.command_name')='submit_forecast' AND e.created_at<?
 AND json_extract(a.body,'$.forecast_id')=e.forecast_id
 AND json_extract(a.body,'$.specification_hash')=f.specification_hash
 AND json_extract(a.body,'$.submitted_at_ms')=e.created_at
 AND json_extract(a.body,'$.forecaster_id') IS NOT NULL
 AND (d.id IS NULL OR (r.status='eligible' AND r.submitted_at=e.created_at
  AND EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id AND c.created_at<?)))
 AND NOT EXISTS(SELECT 1 FROM participation_hold_events h WHERE h.forecast_id=f.id AND h.action='hold'
  AND h.created_at<? AND NOT EXISTS(SELECT 1 FROM participation_hold_events later WHERE later.forecast_id=h.forecast_id
    AND later.revision>h.revision AND later.created_at<?))
)
";

/// A row read for tests and callers that hold a `Row` rather than a snapshot entry.
pub fn row_text(row: &crate::db::Row, name: &str) -> Option<String> {
    text(row, name).map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/analytics-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("analytics golden")).expect("json")
    }

    #[test]
    fn every_report_matches_the_reference() {
        // The definitions are the content: a port that counted a merged alias twice, or reported
        // an immature retention window as zero, would produce a plausible report that is wrong.
        let document = golden();
        let snapshot = &document["snapshot"];
        let cases = document["cases"].as_array().expect("cases");
        assert_eq!(cases.len(), 3);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let arguments = &case["arguments"];
            let excluded: Vec<String> = arguments["excluded_user_ids"]
                .as_array()
                .map(|ids| ids.iter().filter_map(Value::as_str).map(str::to_string).collect())
                .unwrap_or_default();
            let report = aggregate_product_analytics(
                snapshot,
                arguments["as_of_ms"].as_i64().unwrap(),
                arguments["window_start_ms"].as_i64().unwrap(),
                arguments["window_end_ms"].as_i64().unwrap(),
                arguments["cohort_start_ms"].as_i64().unwrap(),
                arguments["cohort_end_ms"].as_i64().unwrap(),
                case["populationKind"].as_str().unwrap(),
                &excluded,
            )
            .unwrap_or_else(|error| panic!("{name}: {error}"));
            assert_eq!(report, case["report"], "{name}: a different report");
        }
    }

    #[test]
    fn every_window_rule_decides_what_the_reference_decided() {
        let document = golden();
        for entry in document["windows"].as_array().expect("windows") {
            let outcome = window(
                entry["startMs"].as_i64().unwrap(),
                entry["endMs"].as_i64().unwrap(),
                entry["asOfMs"].as_i64().unwrap(),
            );
            match outcome {
                Ok(()) => assert_eq!(entry["accepted"].as_bool(), Some(true), "{entry}"),
                Err(error) => assert_eq!(Some(error.as_str()), entry["error"].as_str(), "{entry}"),
            }
        }
    }

    #[test]
    fn the_read_batch_is_valid_against_the_deployed_schema() {
        // The aggregation can be perfect and still report nothing if a statement in the one read
        // batch does not parse. An empty database is the cheapest way to prove all ten do.
        let db = Sqlite::from_migrations();
        let report = futures_lite::future::block_on(product_analytics(
            &db,
            10 * DAY_MS,
            3 * DAY_MS,
            6 * DAY_MS,
            None,
            None,
            "application",
            &[],
        ))
        .expect("the batch runs against the deployed schema");
        assert_eq!(report["formulaVersion"], VERSION);
        assert_eq!(report["activity"]["activePredictors"], 0);
        assert_eq!(report["retention"]["D1"]["status"], "no_denominator");
    }
}
