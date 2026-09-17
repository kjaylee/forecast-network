//! User write routes without AI or chain effects: comments, shares, follows, activity, profile
//! and forecast submissions (`Application` write paths).

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use worker::*;

use forecast_domain::lifecycle::{Payload, Snapshot};
use forecast_domain::models::UserForecast;

use crate::api_response;
use crate::db::{all, batch, first, get, int, text, Row};
use crate::mutate::{canonical_text, hash_of, load_snapshot, mutate, random_token, record_artifact, Mutation, Statement};
use crate::projections::{card, quality_card_sql};
use crate::reads::public_user;
use crate::routes::{Context, RouteError};

pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 86_400_000;
pub const MAX_STAKE: i64 = 1000;
pub const POINTS_POLICY_VERSION: &str = "participation-points-v1";

type Handler = std::result::Result<Response, RouteError>;

pub fn invalid() -> RouteError {
    RouteError::Input
}

pub fn invalid_message(message: &'static str) -> RouteError {
    RouteError::Failed(400, "invalid_input", message)
}

/// `text(value, limit)`: trimmed, bounded, no control characters.
pub fn checked_text(value: &Value, limit: usize) -> std::result::Result<String, RouteError> {
    let text = value.as_str().ok_or_else(invalid)?.trim().to_string();
    let length = text.chars().count();
    if !(1..=limit).contains(&length) || text.chars().any(|c| (c as u32) < 32) {
        return Err(invalid());
    }
    Ok(text)
}

/// `_key`: `[A-Za-z0-9_.:-]{8,120}`.
pub fn idempotency_key(value: &Value) -> std::result::Result<String, RouteError> {
    let key = value.as_str().unwrap_or("");
    let valid = (8..=120).contains(&key.len()) && key.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
    if valid {
        Ok(key.to_string())
    } else {
        Err(invalid_message("A request identifier is required. Refresh the page and try again."))
    }
}

/// Atomic fixed-window counter; the Worker also calls this for IP limits.
pub async fn rate_limit(session: &D1DatabaseSession, now_ms: i64, scope: &str, limit: i64, window_ms: i64) -> std::result::Result<(), RouteError> {
    let bucket = now_ms.div_euclid(window_ms);
    let rows = all(
        session,
        "INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES(?,?,1,?) ON CONFLICT(scope,bucket) DO UPDATE SET count=count+1 RETURNING count",
        &[json!(scope), json!(bucket), json!((bucket + 1) * window_ms)],
    )
    .await?;
    if rows.first().and_then(|r| int(r, "count")).is_some_and(|count| count > limit) {
        return Err(RouteError::Failed(429, "rate_limited", "Too many requests. Please try again later."));
    }
    Ok(())
}

pub async fn user_row(session: &D1DatabaseSession, user_id: &str) -> std::result::Result<Row, RouteError> {
    first(session, "SELECT * FROM users WHERE id=?", &[json!(user_id)])
        .await?
        .ok_or(RouteError::Unauthorized("authentication_required", "Please sign in to continue."))
}

async fn prior(session: &D1DatabaseSession, user_id: &str, key: &str, request: &Value) -> std::result::Result<Option<Row>, RouteError> {
    let row = first(session, "SELECT * FROM operations WHERE user_id=? AND operation_key=?", &[json!(user_id), json!(key)]).await?;
    if let Some(row) = &row {
        if text(row, "request_hash") != Some(hash_of(request)?.as_str()) {
            return Err(RouteError::Failed(409, "idempotency_conflict", "This request identifier has already been used for different content."));
        }
    }
    Ok(row)
}

fn operation(user_id: &str, key: &str, request: &Value, forecast_id: &str, result: &Value, now_ms: i64) -> Result<Statement> {
    Ok((
        "INSERT INTO operations(user_id,operation_key,request_hash,forecast_id,result,created_at) VALUES(?,?,?,?,?,?)".to_string(),
        vec![json!(user_id), json!(key), json!(hash_of(request)?), json!(forecast_id), json!(canonical_text(result)?), json!(now_ms)],
    ))
}

fn prior_result(row: &Row) -> std::result::Result<Value, RouteError> {
    serde_json::from_str(text(row, "result").unwrap_or("{}")).map_err(|e| RouteError::Worker(e.into()))
}

async fn card_of(context: &Context<'_>, forecast_id: &str) -> std::result::Result<Value, RouteError> {
    let sql = format!("{} WHERE f.id=?", quality_card_sql(context.now_ms, &[forecast_id.to_string()]).ok_or_else(invalid)?);
    let row = first(context.session, &sql, &[json!(forecast_id)]).await?.ok_or(RouteError::NotFound("forecast_not_found", "Forecast not found."))?;
    Ok(card(&row))
}

// ---------------------------------------------------------------- comments, shares, follows, activity, profile

pub async fn add_comment(context: &Context<'_>, user_id: &str, forecast_id: &str, body: &Map<String, Value>) -> Handler {
    let session = context.session;
    let user = user_row(session, user_id).await?;
    let text_value = checked_text(body.get("text").unwrap_or(&Value::Null), 2000)?;
    let request = json!({"kind": "comment", "forecastId": forecast_id, "text": text_value});
    let key = idempotency_key(body.get("idempotencyKey").unwrap_or(&json!("")))?;
    if let Some(row) = prior(session, user_id, &key, &request).await? {
        return Ok(api_response(prior_result(&row)?, 200, false)?);
    }
    load_snapshot(session, forecast_id).await?;
    rate_limit(session, context.now_ms, &format!("comment:{user_id}"), 30, HOUR_MS).await?;
    let now = context.now_ms;
    let cid = format!("c_{}", &random_token()[..24]);
    let result = json!({"comment": {"id": cid, "text": text_value, "createdAt": now, "user": public_user(&user)}});
    let statements = vec![
        ("INSERT INTO comments(id,forecast_id,user_id,body,created_at) VALUES(?,?,?,?,?)".to_string(), vec![json!(cid), json!(forecast_id), json!(user_id), json!(text_value), json!(now)]),
        operation(user_id, &key, &request, forecast_id, &result, now)?,
    ];
    if batch(session, statements).await.is_err() {
        if let Some(row) = prior(session, user_id, &key, &request).await? {
            return Ok(api_response(prior_result(&row)?, 200, false)?);
        }
        return Err(RouteError::Worker("comment_storage".into()));
    }
    Ok(api_response(result, 200, false)?)
}

pub async fn record_share(context: &Context<'_>, forecast_id: &str, user_id: Option<&str>) -> Handler {
    let session = context.session;
    load_snapshot(session, forecast_id).await?;
    if let Some(user_id) = user_id {
        user_row(session, user_id).await?;
        let bucket = context.now_ms.div_euclid(DAY_MS);
        batch(
            session,
            vec![
                (
                    "UPDATE forecasts SET share_count=share_count+1 WHERE id=? AND NOT EXISTS (SELECT 1 FROM share_receipts WHERE forecast_id=? AND actor=? AND bucket=?)".to_string(),
                    vec![json!(forecast_id), json!(forecast_id), json!(user_id), json!(bucket)],
                ),
                ("INSERT OR IGNORE INTO share_receipts(forecast_id,actor,bucket) VALUES(?,?,?)".to_string(), vec![json!(forecast_id), json!(user_id), json!(bucket)]),
            ],
        )
        .await?;
    }
    Ok(api_response(json!({"ok": true}), 200, false)?)
}

pub async fn follow(context: &Context<'_>, user_id: &str, creator_id: &str, following: &Value) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    let Some(following) = following.as_bool() else { return Err(invalid()) };
    if user_id == creator_id {
        return Err(invalid());
    }
    user_row(session, creator_id).await?;
    rate_limit(session, context.now_ms, &format!("follow:{user_id}"), 100, HOUR_MS).await?;
    let statement = if following {
        ("INSERT OR IGNORE INTO follows(follower_id,creator_id,created_at) VALUES(?,?,?)".to_string(), vec![json!(user_id), json!(creator_id), json!(context.now_ms)])
    } else {
        ("DELETE FROM follows WHERE follower_id=? AND creator_id=?".to_string(), vec![json!(user_id), json!(creator_id)])
    };
    batch(session, vec![statement]).await?;
    Ok(api_response(json!({"following": following}), 200, false)?)
}

pub async fn read_activity(context: &Context<'_>, user_id: &str) -> Handler {
    user_row(context.session, user_id).await?;
    batch(context.session, vec![("UPDATE activity SET read_at=? WHERE user_id=? AND read_at IS NULL".to_string(), vec![json!(context.now_ms), json!(user_id)])]).await?;
    Ok(api_response(json!({"ok": true}), 200, false)?)
}

pub async fn update_profile(context: &Context<'_>, user_id: &str, display_name: &Value) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    rate_limit(session, context.now_ms, &format!("profile:{user_id}"), 20, HOUR_MS).await?;
    let name = checked_text(display_name, 40)?;
    batch(session, vec![("UPDATE users SET display_name=? WHERE id=?".to_string(), vec![json!(name), json!(user_id)])]).await?;
    let user = user_row(session, user_id).await?;
    let points = crate::points::summary(session, user_id).await?.ok_or(RouteError::Unauthorized("points_account_missing", "Sign in to view your participation points."))?;
    Ok(api_response(json!({"user": public_user(&user), "points": points}), 200, false)?)
}

// ---------------------------------------------------------------- forecast submissions

fn submission_view(choice: &UserForecast, revision: i64) -> Value {
    let yes = choice.outcome == "YES";
    json!({"outcome": choice.outcome, "confidence": choice.confidence, "probability": if yes { choice.confidence } else { 100 - choice.confidence },
           "submittedAt": choice.submitted_at_ms, "revision": revision})
}

fn sha_json(values: &Value) -> String {
    hex::encode(Sha256::digest(serde_json::to_string(values).unwrap_or_default().as_bytes()))
}

/// `points.reservation_sql`: reserve/release an explicit total stake inside the accepted CAS.
pub fn reservation_sql(user_id: &str, forecast_id: &str, amount: i64, outcome: &str, forecast_revision: i64, operation_id: &str, now: i64) -> std::result::Result<Vec<Statement>, RouteError> {
    if !(0..=MAX_STAKE).contains(&amount) {
        return Err(RouteError::Failed(400, "invalid_stake", "Use zero practice points or a whole-number stake from 1 to 1,000."));
    }
    if outcome != "YES" && outcome != "NO" {
        return Err(RouteError::Failed(400, "invalid_stake", "A points stake must have a YES or NO forecast choice."));
    }
    if forecast_revision < 1 || now < 0 {
        return Err(RouteError::Failed(400, "invalid_points_request", "Invalid participation points revision or timestamp."));
    }
    let identity = sha_json(&json!([user_id, operation_id]));
    let ledger_id = format!("reservation:{identity}");
    let request_hash = sha_json(&json!([user_id, forecast_id, amount, outcome, forecast_revision]));
    let guards: Vec<String> = ["operation", "position", "balance"].iter().map(|k| format!("{ledger_id}:{k}")).collect();
    Ok(vec![
        (
            "INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'operation',CASE WHEN NOT EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM point_ledger WHERE id=? AND user_id=? AND kind='reservation' AND request_hash=?) THEN 1 ELSE 0 END".to_string(),
            vec![json!(guards[0]), json!(ledger_id), json!(ledger_id), json!(user_id), json!(request_hash)],
        ),
        (
            "INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'position',CASE WHEN EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM forecasts f LEFT JOIN point_positions p ON p.user_id=? AND p.forecast_id=f.id WHERE f.id=? AND f.state='OPEN' AND f.open_at<=? AND ?<f.close_at AND f.revision=? AND (p.user_id IS NULL OR (p.status IN ('practice','committed') AND p.forecast_revision<?))) THEN 1 ELSE 0 END".to_string(),
            vec![json!(guards[1]), json!(ledger_id), json!(user_id), json!(forecast_id), json!(now), json!(now), json!(forecast_revision), json!(forecast_revision)],
        ),
        (
            "INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'balance',CASE WHEN EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM point_accounts a LEFT JOIN point_positions p ON p.user_id=a.user_id AND p.forecast_id=? WHERE a.user_id=? AND a.available+COALESCE(p.amount,0)-?>=0 AND a.committed+?-COALESCE(p.amount,0)>=0) THEN 1 ELSE 0 END".to_string(),
            vec![json!(guards[2]), json!(ledger_id), json!(forecast_id), json!(user_id), json!(amount), json!(amount)],
        ),
        (
            "INSERT INTO point_ledger(id,user_id,kind,forecast_id,operation_id,request_hash,available_delta,committed_delta,available_after,committed_after,stake,outcome,forecast_revision,policy_version,created_at) SELECT ?,a.user_id,'reservation',?,?,?,COALESCE(p.amount,0)-?,?-COALESCE(p.amount,0),a.available+COALESCE(p.amount,0)-?,a.committed+?-COALESCE(p.amount,0),?,?,?,COALESCE(p.policy_version,?),? FROM point_accounts a LEFT JOIN point_positions p ON p.user_id=a.user_id AND p.forecast_id=? WHERE a.user_id=? AND NOT EXISTS(SELECT 1 FROM point_ledger WHERE id=?)".to_string(),
            vec![
                json!(ledger_id), json!(forecast_id), json!(operation_id), json!(request_hash), json!(amount), json!(amount), json!(amount), json!(amount),
                json!(amount), json!(outcome), json!(forecast_revision), json!(POINTS_POLICY_VERSION), json!(now), json!(forecast_id), json!(user_id), json!(ledger_id),
            ],
        ),
        ("DELETE FROM point_write_guards WHERE id IN (?,?,?)".to_string(), vec![json!(guards[0]), json!(guards[1]), json!(guards[2])]),
    ])
}

async fn submission_response(context: &Context<'_>, user_id: &str, forecast_id: &str, mut receipt: Value) -> Handler {
    let session = context.session;
    let eligibility = crate::eligibility::combined(session, forecast_id, Some(user_id)).await?;
    if eligibility["status"] != "none" {
        let effective = first(session, "SELECT body,revision FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?", &[json!(forecast_id), json!(user_id)]).await?;
        let original = receipt.get("myForecast").cloned().unwrap_or(Value::Null);
        receipt["originalReceipt"] = original;
        receipt["myForecast"] = match effective {
            Some(row) => {
                let choice: UserForecast = serde_json::from_str(text(&row, "body").unwrap_or("")).map_err(|e| RouteError::Worker(e.into()))?;
                submission_view(&choice, int(&row, "revision").unwrap_or(0))
            }
            None => Value::Null,
        };
    }
    let mut data = json!({"forecast": card_of(context, forecast_id).await?});
    if let (Value::Object(target), Value::Object(fields)) = (&mut data, receipt) {
        for (key, value) in fields {
            target.insert(key, value);
        }
    }
    data["points"] = crate::points::summary(session, user_id).await?.ok_or(RouteError::Unauthorized("points_account_missing", "Sign in to view your participation points."))?;
    data["eligibility"] = eligibility;
    data["stake"] = crate::points::position_for(session, user_id, forecast_id).await?;
    Ok(api_response(data, 200, false)?)
}

pub async fn submit_forecast(context: &Context<'_>, user_id: &str, forecast_id: &str, body: &Map<String, Value>) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    let outcome = body.get("outcome").and_then(Value::as_str).filter(|o| matches!(*o, "YES" | "NO")).ok_or_else(invalid)?.to_string();
    let confidence = body.get("confidence").and_then(crate::discovery::integer).filter(|c| (0..=100).contains(c)).ok_or_else(invalid)?;
    let revision = body.get("revision").and_then(crate::discovery::integer).filter(|r| *r >= 0).ok_or_else(invalid)?;
    let stake_points = match body.get("stakePoints") {
        None | Some(Value::Null) => None,
        Some(value) => Some(
            crate::discovery::integer(value)
                .filter(|s| (0..=1000).contains(s))
                .ok_or(RouteError::Failed(400, "invalid_stake", "Choose practice with 0 points or a stake from 1 to 1,000 points."))?,
        ),
    };
    let mut request = json!({"kind": "forecast", "forecastId": forecast_id, "outcome": outcome, "confidence": confidence, "revision": revision});
    if let Some(stake) = stake_points {
        request["stakePoints"] = json!(stake);
    }
    let key = idempotency_key(body.get("idempotencyKey").unwrap_or(&json!("")))?;
    if let Some(row) = prior(session, user_id, &key, &request).await? {
        return submission_response(context, user_id, forecast_id, prior_result(&row)?).await;
    }
    if first(session, "SELECT body FROM active_participation_holds WHERE forecast_id=?", &[json!(forecast_id)]).await?.is_some() {
        return Err(RouteError::Failed(409, "participation_on_hold", "Participation is on hold while newly available evidence is reviewed."));
    }
    let snapshot = load_snapshot(session, forecast_id).await?;
    let forecast = snapshot.base();
    if forecast.revision != revision {
        return Err(crate::mutate::conflict());
    }
    if stake_points.is_none() {
        let position = crate::points::position_for(session, user_id, forecast_id).await?;
        if position["status"] == "committed" && position["amount"].as_i64().unwrap_or(0) > 0 {
            return Err(RouteError::Failed(409, "stake_required", "This forecast already has a stake. Refresh and explicitly confirm the stake amount."));
        }
    }
    let amount = stake_points.unwrap_or(0);
    rate_limit(session, context.now_ms, &format!("forecast:{user_id}"), 100, HOUR_MS).await?;
    let now = context.now_ms;
    let choice = UserForecast {
        schema_version: 1,
        forecaster_id: user_id.to_string(),
        forecast_id: forecast_id.to_string(),
        specification_hash: forecast.specification_hash.clone(),
        outcome: outcome.clone(),
        confidence,
        submitted_at_ms: now,
    };
    let probability = if outcome == "YES" { confidence } else { 100 - confidence };
    let response = json!({"myForecast": submission_view(&choice, revision + 1)});
    let choice_json = canonical_text(&choice)?;
    let mut extra: Vec<Statement> = vec![
        record_artifact(&choice, "user_forecast", None, now)?,
        (
            "INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,revision,body) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(forecast_id,user_id) DO UPDATE SET outcome=excluded.outcome,confidence=excluded.confidence,yes_probability=excluded.yes_probability,submitted_at=excluded.submitted_at,revision=excluded.revision,body=excluded.body".to_string(),
            vec![json!(forecast_id), json!(user_id), json!(outcome), json!(confidence), json!(probability), json!(now), json!(revision + 1), json!(choice_json)],
        ),
        (
            "INSERT INTO forecast_history(forecast_id,revision,user_id,body,crowd_probability,participant_count,created_at) SELECT ?,?,?,?,AVG(yes_probability),COUNT(*),? FROM user_forecasts WHERE forecast_id=?".to_string(),
            vec![json!(forecast_id), json!(revision + 1), json!(user_id), json!(choice_json), json!(now), json!(forecast_id)],
        ),
    ];
    let operation_id = hash_of(&json!({"user": user_id, "key": key}))?;
    extra.extend(reservation_sql(user_id, forecast_id, amount, &outcome, revision + 1, &operation_id, now)?);
    extra.push(operation(user_id, &key, &request, forecast_id, &response, now)?);
    let mutation = Mutation { snapshot: &snapshot, payload: Payload::SubmitForecast { schema_version: 1, user_forecast: choice }, key: format!("user:{operation_id}"), now_ms: now, extra, job_token: None };
    match mutate(session, mutation, now).await {
        Ok(_) => submission_response(context, user_id, forecast_id, response).await,
        Err(error) => {
            // A transport error can arrive after the D1 batch committed; the durable receipt wins.
            let prior_row = match prior(session, user_id, &key, &request).await {
                Ok(row) => row,
                Err(RouteError::Failed(..)) | Err(RouteError::Input) | Err(RouteError::NotFound(..)) | Err(RouteError::Unauthorized(..)) => return Err(error),
                Err(_) => return Err(RouteError::Failed(503, "forecast_storage_unavailable", "The forecast could not be confirmed. Retry with the same request identifier.")),
            };
            if let Some(row) = prior_row {
                return submission_response(context, user_id, forecast_id, prior_result(&row)?).await;
            }
            if !matches!(error, RouteError::Worker(_)) {
                return Err(error);
            }
            let points = crate::points::summary(session, user_id).await.ok().flatten();
            let position = crate::points::position_for(session, user_id, forecast_id).await.ok();
            let (Some(points), Some(position)) = (points, position) else {
                return Err(RouteError::Failed(503, "forecast_storage_unavailable", "The forecast could not be saved. Please try again later."));
            };
            let hold = if position["status"] == "committed" { position["amount"].as_i64().unwrap_or(0) } else { 0 };
            if amount > points["available"].as_i64().unwrap_or(0) + hold {
                return Err(RouteError::Failed(409, "insufficient_points", "You do not have enough available points for this stake."));
            }
            Err(RouteError::Failed(503, "forecast_storage_unavailable", "The forecast could not be saved. Please try again later."))
        }
    }
}

#[allow(dead_code)]
fn _snapshot_kind(snapshot: &Snapshot) -> &'static str {
    match snapshot {
        Snapshot::V1(_) => "v1",
        Snapshot::V2(_) => "v2",
    }
}

#[allow(dead_code)]
fn _row_get(row: &Row) -> &Value {
    get(row, "id")
}
