//! User write routes without AI or chain effects: comments, shares, follows, activity, profile
//! and forecast submissions (`Application` write paths).

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use worker::*;

use forecast_domain::lifecycle::{Payload, Snapshot};
use forecast_domain::models::UserForecast;

use crate::api_response;
use crate::db::{all, batch, first, get, int, text, Row};
use crate::mutate::{
    canonical_text, hash_of, load_snapshot, mutate, random_token, record_artifact, Mutation, Statement,
};
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
    let valid = (8..=120).contains(&key.len())
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
    if valid {
        Ok(key.to_string())
    } else {
        Err(invalid_message(
            "A request identifier is required. Refresh the page and try again.",
        ))
    }
}

/// Atomic fixed-window counter; the Worker also calls this for IP limits.
pub async fn rate_limit(
    session: &D1DatabaseSession,
    now_ms: i64,
    scope: &str,
    limit: i64,
    window_ms: i64,
) -> std::result::Result<(), RouteError> {
    let bucket = now_ms.div_euclid(window_ms);
    let rows = all(
        session,
        "INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES(?,?,1,?) ON CONFLICT(scope,bucket) DO UPDATE SET count=count+1 RETURNING count",
        &[json!(scope), json!(bucket), json!((bucket + 1) * window_ms)],
    )
    .await?;
    if rows
        .first()
        .and_then(|r| int(r, "count"))
        .is_some_and(|count| count > limit)
    {
        return Err(RouteError::Failed(
            429,
            "rate_limited",
            "Too many requests. Please try again later.",
        ));
    }
    Ok(())
}

/// `Application._card`: the quality card for one forecast.
// The route that dispatches adjudication is the `Application` composition root, which is not
// ported yet; this path is reachable from its own golden and from nothing else so far.
#[allow(dead_code)]
async fn card_row(
    db: &dyn crate::db::Database,
    forecast_id: &str,
    now_ms: i64,
) -> std::result::Result<Value, RouteError> {
    let sql = format!(
        "{} WHERE f.id=?",
        quality_card_sql(now_ms, &[forecast_id.to_string()]).ok_or(RouteError::Input)?
    );
    let row = db
        .first(&sql, &[json!(forecast_id)])
        .await?
        .ok_or(RouteError::NotFound("forecast_not_found", "Forecast not found."))?;
    Ok(card(&row))
}

/// `Application.adjudicate_forecast`: the exceptional ADMIN-caller path.
///
/// The HTTP adapter authenticates the operator before this is reached. This boundary never
/// manufactures a verdict, modifies provenance, or finalizes: a successful adjudication enters
/// PROPOSED, and the ordinary scheduler then opens a fresh, complete challenge window.
///
/// The idempotency receipt is written *inside* the same batch as the command, so a retry that
/// arrives after a lost response returns the original receipt rather than adjudicating twice.
/// A prepared operator decision, named rather than positional: six of these are strings or records,
/// and a call whose order nobody can check by reading it is a call nobody can review.
pub struct Adjudication<'a> {
    pub forecast_id: &'a str,
    pub resolution: &'a forecast_domain::models::Resolution,
    pub adjudicator: &'a forecast_domain::models::AIProvenance,
    /// `(hash, kind, body, media type)` — the evidence supplied *with* the request.
    pub artifacts: &'a [(String, String, String, String)],
    pub idempotency_key: &'a str,
    pub expected_revision: Option<i64>,
    pub token: &'a dyn Fn() -> String,
}

#[allow(dead_code)]
pub async fn adjudicate_forecast(
    db: &dyn crate::db::Database,
    now_ms: i64,
    adjudication: Adjudication<'_>,
) -> std::result::Result<Value, RouteError> {
    let Adjudication {
        forecast_id,
        resolution,
        adjudicator,
        artifacts,
        idempotency_key,
        expected_revision,
        token,
    } = adjudication;
    if expected_revision.is_some_and(|revision| revision < 0) {
        return Err(invalid());
    }
    if artifacts.len() > 32 {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "Check the format and number of evidence artifacts.",
        ));
    }
    let resolution_hash = resolution.resolution_hash().map_err(|_| invalid())?;
    let request = json!({
        "kind": "adjudicate",
        "forecastId": forecast_id,
        "resolutionHash": resolution_hash,
        "adjudicatorHash": hash_of(adjudicator)?,
        "revision": expected_revision,
        "artifacts": artifacts
            .iter()
            .map(|(hash, kind, _, media)| json!({"hash": hash, "kind": kind, "mediaType": media}))
            .collect::<Vec<_>>(),
    });
    let operator = "admin:adjudication";
    if let Some(prior) = prior_row(db, operator, idempotency_key, &request).await? {
        let mut receipt = prior_result(&prior)?;
        receipt["forecast"] = card_row(db, forecast_id, now_ms).await?;
        return Ok(receipt);
    }
    let snapshot = load_snapshot(db, forecast_id).await?;
    let forecast = snapshot.base().clone();
    if expected_revision.is_some_and(|revision| forecast.revision != revision) {
        return Err(crate::mutate::conflict());
    }
    if forecast.state != "ESCALATED" {
        return Err(RouteError::Failed(
            409,
            "adjudication_not_allowed",
            "Only forecasts awaiting independent adjudication can be processed.",
        ));
    }
    // A prepared operator decision can travel over HTTP without rewriting its hash-bound
    // timestamps. Reject future, stale or backdated decisions.
    let decision_at = resolution.proposed_at_ms;
    if !(forecast.updated_at_ms <= decision_at && decision_at <= now_ms) || now_ms - decision_at > 15 * 60 * 1000 {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "The independent decision must follow the current record and have been created within the last 15 minutes.",
        ));
    }
    let mut extra = crate::source_watch::artifact_sql(artifacts, now_ms)
        .map_err(|refusal| RouteError::Failed(refusal.status(), refusal.code(), refusal.message()))?;
    // Every piece of evidence the replacement resolution cites has to be *retained* — supplied with
    // the request or already in the store — and to hash to what the resolution claims.
    let supplied: std::collections::HashMap<&str, &str> = artifacts
        .iter()
        .map(|(hash, _, body, _)| (hash.as_str(), body.as_str()))
        .collect();
    for evidence in &resolution.evidence {
        let body = match supplied.get(evidence.content_sha256.as_str()) {
            Some(body) => Some((*body).to_string()),
            None => crate::scheduler::read_artifact(db, &evidence.content_sha256)
                .await
                .map_err(|_| {
                    RouteError::Failed(
                        503,
                        "forecast_storage_unavailable",
                        "The evidence store is temporarily unavailable.",
                    )
                })?,
        };
        let Some(body) = body else {
            return Err(RouteError::Failed(
                422,
                "missing_resolution_artifact",
                "Every original evidence artifact for the replacement resolution must be retained.",
            ));
        };
        if crate::source_watch::hash_hex(&body) != evidence.content_sha256 {
            return Err(RouteError::Failed(
                422,
                "resolution_artifact_mismatch",
                "The retained resolution evidence does not match its hash.",
            ));
        }
    }
    let payload = Payload::AdjudicateResolution {
        schema_version: 1,
        resolution: resolution.clone(),
        adjudicator: adjudicator.clone(),
    };
    let command_key = format!(
        "admin:{}",
        forecast_domain::content_hash(&json!({"operator": operator, "key": idempotency_key}))
            .map_err(|error| RouteError::Worker(error.to_string().into()))?
    );
    // The domain validates the source-verification commitments, the exact specification, the
    // reviewed-dispute bindings, the independent provider and every timestamp. A preview is how
    // this boundary learns what the receipt will say without writing anything.
    let preview = forecast_domain::lifecycle::apply_command(
        &snapshot,
        &forecast_domain::lifecycle::Command {
            schema_version: 1,
            idempotency_key: command_key.clone(),
            expected_revision: forecast.revision,
            payload: payload.clone(),
        },
        decision_at,
        None,
    )
    .map_err(|_| {
        RouteError::Failed(
            422,
            "adjudication_validation_failed",
            "The decision failed independence, immutable criteria, or evidence linkage verification.",
        )
    })?;
    let response = json!({
        "adjudication": {
            "resolutionHash": resolution_hash,
            "eventHash": preview.receipt.event_hash,
            "revision": preview.receipt.revision,
            "acceptedAt": decision_at,
        }
    });
    extra.push(record_artifact(resolution, "adjudicated_resolution", None, now_ms)?);
    extra.push(record_artifact(adjudicator, "independent_adjudicator", None, now_ms)?);
    extra.push(operation(
        operator,
        idempotency_key,
        &request,
        forecast_id,
        &response,
        now_ms,
    )?);
    for verification in &resolution.source_verifications {
        extra.push(record_artifact(
            verification,
            "adjudication_source_verification",
            None,
            now_ms,
        )?);
    }
    let timing_artifacts = artifacts
        .iter()
        .map(|(hash, _, body, media)| (hash.clone(), body.clone(), media.clone()))
        .collect();
    let outcome = mutate(
        db,
        Mutation {
            snapshot: &snapshot,
            payload,
            key: command_key,
            now_ms: decision_at,
            extra,
            job_token: None,
            timing_artifacts,
        },
        now_ms,
        token,
        None,
    )
    .await;
    if let Err(error) = outcome {
        // A refusal and a lost race look the same from here, so the receipt is read back before
        // deciding which it was.
        if let Some(prior) = prior_row(db, operator, idempotency_key, &request).await? {
            let mut receipt = prior_result(&prior)?;
            receipt["forecast"] = card_row(db, forecast_id, now_ms).await?;
            return Ok(receipt);
        }
        return Err(error);
    }
    let mut result = response;
    result["forecast"] = card_row(db, forecast_id, now_ms).await?;
    Ok(result)
}

pub async fn user_row(session: &D1DatabaseSession, user_id: &str) -> std::result::Result<Row, RouteError> {
    first(session, "SELECT * FROM users WHERE id=?", &[json!(user_id)])
        .await?
        .ok_or(RouteError::Unauthorized(
            "authentication_required",
            "Please sign in to continue.",
        ))
}

async fn prior(
    session: &D1DatabaseSession,
    user_id: &str,
    key: &str,
    request: &Value,
) -> std::result::Result<Option<Row>, RouteError> {
    let row = first(
        session,
        "SELECT * FROM operations WHERE user_id=? AND operation_key=?",
        &[json!(user_id), json!(key)],
    )
    .await?;
    if let Some(row) = &row {
        if text(row, "request_hash") != Some(hash_of(request)?.as_str()) {
            return Err(RouteError::Failed(
                409,
                "idempotency_conflict",
                "This request identifier has already been used for different content.",
            ));
        }
    }
    Ok(row)
}

/// `_prior`, over the `Database` trait rather than a session: the adjudication path is reachable
/// from tests, and a helper that demanded a D1 session would make the path unreachable there.
#[allow(dead_code)]
async fn prior_row(
    db: &dyn crate::db::Database,
    user_id: &str,
    key: &str,
    request: &Value,
) -> std::result::Result<Option<Row>, RouteError> {
    let row = db
        .first(
            "SELECT * FROM operations WHERE user_id=? AND operation_key=?",
            &[json!(user_id), json!(key)],
        )
        .await?;
    if let Some(row) = &row {
        if text(row, "request_hash") != Some(hash_of(request)?.as_str()) {
            return Err(RouteError::Failed(
                409,
                "idempotency_conflict",
                "This request identifier has already been used for different content.",
            ));
        }
    }
    Ok(row)
}

fn operation(
    user_id: &str,
    key: &str,
    request: &Value,
    forecast_id: &str,
    result: &Value,
    now_ms: i64,
) -> Result<Statement> {
    Ok((
        "INSERT INTO operations(user_id,operation_key,request_hash,forecast_id,result,created_at) VALUES(?,?,?,?,?,?)"
            .to_string(),
        vec![
            json!(user_id),
            json!(key),
            json!(hash_of(request)?),
            json!(forecast_id),
            json!(canonical_text(result)?),
            json!(now_ms),
        ],
    ))
}

fn prior_result(row: &Row) -> std::result::Result<Value, RouteError> {
    serde_json::from_str(text(row, "result").unwrap_or("{}")).map_err(|e| RouteError::Worker(e.into()))
}

async fn card_of(context: &Context<'_>, forecast_id: &str) -> std::result::Result<Value, RouteError> {
    let sql = format!(
        "{} WHERE f.id=?",
        quality_card_sql(context.now_ms, &[forecast_id.to_string()]).ok_or_else(invalid)?
    );
    let row = first(context.session, &sql, &[json!(forecast_id)])
        .await?
        .ok_or(RouteError::NotFound("forecast_not_found", "Forecast not found."))?;
    Ok(card(&row))
}

// ---------------------------------------------------------------- comments, shares, follows, activity, profile

pub async fn add_comment(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    let session = context.session;
    let user = user_row(session, user_id).await?;
    let text_value = checked_text(body.get("text").unwrap_or(&Value::Null), 2000)?;
    let request = json!({"kind": "comment", "forecastId": forecast_id, "text": text_value});
    let key = idempotency_key(body.get("idempotencyKey").unwrap_or(&json!("")))?;
    if let Some(row) = prior(session, user_id, &key, &request).await? {
        return Ok(api_response(prior_result(&row)?, 200, false)?);
    }
    load_snapshot(&crate::db::D1(session), forecast_id).await?;
    rate_limit(session, context.now_ms, &format!("comment:{user_id}"), 30, HOUR_MS).await?;
    let now = context.now_ms;
    let cid = format!("c_{}", &random_token()[..24]);
    let result = json!({"comment": {"id": cid, "text": text_value, "createdAt": now, "user": public_user(&user)}});
    let statements = vec![
        (
            "INSERT INTO comments(id,forecast_id,user_id,body,created_at) VALUES(?,?,?,?,?)".to_string(),
            vec![
                json!(cid),
                json!(forecast_id),
                json!(user_id),
                json!(text_value),
                json!(now),
            ],
        ),
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
    load_snapshot(&crate::db::D1(session), forecast_id).await?;
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
    let Some(following) = following.as_bool() else {
        return Err(invalid());
    };
    if user_id == creator_id {
        return Err(invalid());
    }
    user_row(session, creator_id).await?;
    rate_limit(session, context.now_ms, &format!("follow:{user_id}"), 100, HOUR_MS).await?;
    let statement = if following {
        (
            "INSERT OR IGNORE INTO follows(follower_id,creator_id,created_at) VALUES(?,?,?)".to_string(),
            vec![json!(user_id), json!(creator_id), json!(context.now_ms)],
        )
    } else {
        (
            "DELETE FROM follows WHERE follower_id=? AND creator_id=?".to_string(),
            vec![json!(user_id), json!(creator_id)],
        )
    };
    batch(session, vec![statement]).await?;
    Ok(api_response(json!({"following": following}), 200, false)?)
}

pub async fn read_activity(context: &Context<'_>, user_id: &str) -> Handler {
    user_row(context.session, user_id).await?;
    batch(
        context.session,
        vec![(
            "UPDATE activity SET read_at=? WHERE user_id=? AND read_at IS NULL".to_string(),
            vec![json!(context.now_ms), json!(user_id)],
        )],
    )
    .await?;
    Ok(api_response(json!({"ok": true}), 200, false)?)
}

pub async fn update_profile(context: &Context<'_>, user_id: &str, display_name: &Value) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    rate_limit(session, context.now_ms, &format!("profile:{user_id}"), 20, HOUR_MS).await?;
    let name = checked_text(display_name, 40)?;
    batch(
        session,
        vec![(
            "UPDATE users SET display_name=? WHERE id=?".to_string(),
            vec![json!(name), json!(user_id)],
        )],
    )
    .await?;
    let user = user_row(session, user_id).await?;
    let points = crate::points::summary(&crate::db::D1(session), user_id).await?;
    Ok(api_response(
        json!({"user": public_user(&user), "points": points}),
        200,
        false,
    )?)
}

// ---------------------------------------------------------------- forecast submissions

fn submission_view(choice: &UserForecast, revision: i64) -> Value {
    let yes = choice.outcome == "YES";
    json!({"outcome": choice.outcome, "confidence": choice.confidence, "probability": if yes { choice.confidence } else { 100 - choice.confidence },
           "submittedAt": choice.submitted_at_ms, "revision": revision})
}

fn sha_json(values: &Value) -> String {
    hex::encode(Sha256::digest(
        serde_json::to_string(values).unwrap_or_default().as_bytes(),
    ))
}

/// `points.reservation_sql`: reserve/release an explicit total stake inside the accepted CAS.
pub fn reservation_sql(
    user_id: &str,
    forecast_id: &str,
    amount: i64,
    outcome: &str,
    forecast_revision: i64,
    operation_id: &str,
    now: i64,
) -> std::result::Result<Vec<Statement>, RouteError> {
    if !(0..=MAX_STAKE).contains(&amount) {
        return Err(RouteError::Failed(
            400,
            "invalid_stake",
            "Use zero practice points or a whole-number stake from 1 to 1,000.",
        ));
    }
    if outcome != "YES" && outcome != "NO" {
        return Err(RouteError::Failed(
            400,
            "invalid_stake",
            "A points stake must have a YES or NO forecast choice.",
        ));
    }
    if forecast_revision < 1 || now < 0 {
        return Err(RouteError::Failed(
            400,
            "invalid_points_request",
            "Invalid participation points revision or timestamp.",
        ));
    }
    let identity = sha_json(&json!([user_id, operation_id]));
    let ledger_id = format!("reservation:{identity}");
    let request_hash = sha_json(&json!([user_id, forecast_id, amount, outcome, forecast_revision]));
    let guards: Vec<String> = ["operation", "position", "balance"]
        .iter()
        .map(|k| format!("{ledger_id}:{k}"))
        .collect();
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
    let eligibility = crate::eligibility::combined(&crate::db::D1(session), forecast_id, Some(user_id)).await?;
    if eligibility["status"] != "none" {
        let effective = first(
            session,
            "SELECT body,revision FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?",
            &[json!(forecast_id), json!(user_id)],
        )
        .await?;
        let original = receipt.get("myForecast").cloned().unwrap_or(Value::Null);
        receipt["originalReceipt"] = original;
        receipt["myForecast"] = match effective {
            Some(row) => {
                let choice: UserForecast =
                    serde_json::from_str(text(&row, "body").unwrap_or("")).map_err(|e| RouteError::Worker(e.into()))?;
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
    data["points"] = crate::points::summary(&crate::db::D1(session), user_id).await?;
    data["eligibility"] = eligibility;
    data["stake"] = crate::points::position_for(&crate::db::D1(session), user_id, forecast_id).await?;
    Ok(api_response(data, 200, false)?)
}

pub async fn submit_forecast(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    let outcome = body
        .get("outcome")
        .and_then(Value::as_str)
        .filter(|o| matches!(*o, "YES" | "NO"))
        .ok_or_else(invalid)?
        .to_string();
    let confidence = body
        .get("confidence")
        .and_then(crate::discovery::integer)
        .filter(|c| (0..=100).contains(c))
        .ok_or_else(invalid)?;
    let revision = body
        .get("revision")
        .and_then(crate::discovery::integer)
        .filter(|r| *r >= 0)
        .ok_or_else(invalid)?;
    let stake_points = match body.get("stakePoints") {
        None | Some(Value::Null) => None,
        Some(value) => Some(
            crate::discovery::integer(value)
                .filter(|s| (0..=1000).contains(s))
                .ok_or(RouteError::Failed(
                    400,
                    "invalid_stake",
                    "Choose practice with 0 points or a stake from 1 to 1,000 points.",
                ))?,
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
    if first(
        session,
        "SELECT body FROM active_participation_holds WHERE forecast_id=?",
        &[json!(forecast_id)],
    )
    .await?
    .is_some()
    {
        return Err(RouteError::Failed(
            409,
            "participation_on_hold",
            "Participation is on hold while newly available evidence is reviewed.",
        ));
    }
    let snapshot = load_snapshot(&crate::db::D1(session), forecast_id).await?;
    let forecast = snapshot.base();
    if forecast.revision != revision {
        return Err(crate::mutate::conflict());
    }
    if stake_points.is_none() {
        let position = crate::points::position_for(&crate::db::D1(session), user_id, forecast_id).await?;
        if position["status"] == "committed" && position["amount"].as_i64().unwrap_or(0) > 0 {
            return Err(RouteError::Failed(
                409,
                "stake_required",
                "This forecast already has a stake. Refresh and explicitly confirm the stake amount.",
            ));
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
    extra.extend(reservation_sql(
        user_id,
        forecast_id,
        amount,
        &outcome,
        revision + 1,
        &operation_id,
        now,
    )?);
    extra.push(operation(user_id, &key, &request, forecast_id, &response, now)?);
    let mutation = Mutation {
        snapshot: &snapshot,
        payload: Payload::SubmitForecast {
            schema_version: 1,
            user_forecast: choice,
        },
        key: format!("user:{operation_id}"),
        now_ms: now,
        extra,
        job_token: None,
        // The submission's own evidence is fetched by the resolver, not carried here.
        timing_artifacts: Vec::new(),
    };
    // A submission is not a finalize, so there is no chain gate to consult.
    match mutate(&crate::db::D1(session), mutation, now, &random_token, None).await {
        Ok(_) => submission_response(context, user_id, forecast_id, response).await,
        Err(error) => {
            // A transport error can arrive after the D1 batch committed; the durable receipt wins.
            let prior_row = match prior(session, user_id, &key, &request).await {
                Ok(row) => row,
                Err(RouteError::Failed(..))
                | Err(RouteError::Input)
                | Err(RouteError::NotFound(..))
                | Err(RouteError::Unauthorized(..)) => return Err(error),
                Err(_) => {
                    return Err(RouteError::Failed(
                        503,
                        "forecast_storage_unavailable",
                        "The forecast could not be confirmed. Retry with the same request identifier.",
                    ))
                }
            };
            if let Some(row) = prior_row {
                return submission_response(context, user_id, forecast_id, prior_result(&row)?).await;
            }
            if !matches!(error, RouteError::Worker(_)) {
                return Err(error);
            }
            // A missing account is a refusal now, and this path only wants the summary if there is
            // one: the refusal is what the *route* reports, and swallowing it here is the same
            // `None` the read model used to return.
            let points = crate::points::summary(&crate::db::D1(session), user_id).await.ok();
            let position = crate::points::position_for(&crate::db::D1(session), user_id, forecast_id)
                .await
                .ok();
            let (Some(points), Some(position)) = (points, position) else {
                return Err(RouteError::Failed(
                    503,
                    "forecast_storage_unavailable",
                    "The forecast could not be saved. Please try again later.",
                ));
            };
            let hold = if position["status"] == "committed" {
                position["amount"].as_i64().unwrap_or(0)
            } else {
                0
            };
            if amount > points["available"].as_i64().unwrap_or(0) + hold {
                return Err(RouteError::Failed(
                    409,
                    "insufficient_points",
                    "You do not have enough available points for this stake.",
                ));
            }
            Err(RouteError::Failed(
                503,
                "forecast_storage_unavailable",
                "The forecast could not be saved. Please try again later.",
            ))
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

/// `adjudicate_forecast`, replayed against the reference's own recorded state.
#[cfg(test)]
mod adjudication_tests {
    use super::*;
    use crate::golden::{assert_all_cases_known, assert_case, block, entry, load, static_database, Tokens};

    const REPLAYED: [&str; 7] = [
        "adjudicate:ok",
        "adjudicate:retry",
        "adjudicate:idempotency-conflict",
        "adjudicate:missing-artifact",
        "adjudicate:not-independent",
        "adjudicate:stale-revision",
        "adjudicate:backdated",
    ];

    const NOT_REPLAYED: [(&str, &str); 0] = [];

    #[test]
    fn the_vector_has_no_case_this_replay_silently_skips() {
        assert_all_cases_known(&load("adjudication-golden.json"), &REPLAYED, &NOT_REPLAYED);
    }

    /// The prepared decision, parsed from the vector rather than rebuilt.
    struct Prepared {
        resolution: forecast_domain::models::Resolution,
        adjudicator: forecast_domain::models::AIProvenance,
        artifacts: Vec<(String, String, String, String)>,
        forecast_id: String,
        expected_revision: i64,
        key: String,
    }

    fn prepared(case: &Value, key: &str) -> Prepared {
        let decision = &case["decision"];
        Prepared {
            resolution: serde_json::from_value(decision["resolution"].clone()).expect("resolution"),
            adjudicator: serde_json::from_value(decision["adjudicator"].clone()).expect("adjudicator"),
            artifacts: decision["artifacts"]
                .as_array()
                .expect("artifacts")
                .iter()
                .map(|item| {
                    (
                        item["hash"].as_str().unwrap_or("").to_string(),
                        item["kind"].as_str().unwrap_or("").to_string(),
                        item["body"].as_str().unwrap_or("").to_string(),
                        item["mediaType"].as_str().unwrap_or("").to_string(),
                    )
                })
                .collect(),
            forecast_id: decision["forecastId"].as_str().unwrap_or("").to_string(),
            expected_revision: decision["expectedRevision"].as_i64().unwrap_or(0),
            key: key.to_string(),
        }
    }

    fn refusal(error: &RouteError) -> Value {
        match error {
            RouteError::Failed(status, code, message) => {
                json!({"status": status, "code": code, "message": message})
            }
            RouteError::NotFound(code, message) => json!({"status": 404, "code": code, "message": message}),
            other => json!({"code": format!("{other:?}"), "message": ""}),
        }
    }

    #[test]
    fn the_reference_adjudication_is_reproduced_case_for_case() {
        let document = load("adjudication-golden.json");
        for name in REPLAYED {
            let case = entry(&document, name);
            let db = static_database(&case["initial"]);
            // The instant the case *started* at, not the one it ended at: an action may move the
            // clock part-way through, and the two calls either side of it are at two instants.
            let started_ms = case["nowBefore"].as_i64().unwrap_or(0);
            let now_ms = case["now"].as_i64().unwrap_or(0);
            let tokens = Tokens::new(Tokens::recorded(case));
            let call = |at: i64, prepared: &Prepared, artifacts: &[(String, String, String, String)]| {
                block(adjudicate_forecast(
                    db,
                    at,
                    Adjudication {
                        forecast_id: &prepared.forecast_id,
                        resolution: &prepared.resolution,
                        adjudicator: &prepared.adjudicator,
                        artifacts,
                        idempotency_key: &prepared.key,
                        expected_revision: Some(prepared.expected_revision),
                        token: &|| tokens.next(),
                    },
                ))
            };
            let mut prepared = prepared(case, "operator-valid-key");
            let result = match name {
                "adjudicate:ok" => call(started_ms, &prepared, &prepared.artifacts).map(Some),
                "adjudicate:retry" => {
                    // The same command twice, with a scheduler pass between: the second call is a
                    // retry of the first, and the reference's own `events: 1` is what says so.
                    prepared.key = "operator-retry-key".to_string();
                    let first = call(started_ms, &prepared, &prepared.artifacts);
                    if let Err(error) = &first {
                        panic!("{name}: the first adjudication failed: {}", refusal(error));
                    }
                    let first = first.unwrap_or(Value::Null);
                    let evidence: crate::ai::resolution::EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
                    block(crate::scheduler::run_due_jobs(
                        &crate::scheduler::Scheduler {
                            db,
                            coordinator: &crate::golden::refusing_coordinator(),
                            fetch: &evidence,
                            now_ms,
                            clock: &|| now_ms,
                            daily_limit: 100,
                            registry: None,
                            reader: &crate::golden::Refusing,
                        },
                        5,
                        &mut || tokens.next(),
                    ))
                    .expect("a pass");
                    call(now_ms, &prepared, &prepared.artifacts).map(|retry| {
                        Some(json!({
                            "first": first,
                            "retry": retry,
                            "events": block(crate::db::Database::first(db,
                                "SELECT COUNT(*) AS n FROM events WHERE json_extract(event,'$.command_name')='adjudicate_resolution'",
                                &[],
                            ))
                            .ok()
                            .flatten()
                            .and_then(|row| crate::db::int(&row, "n"))
                            .unwrap_or(0),
                        }))
                    })
                }
                "adjudicate:idempotency-conflict" => {
                    prepared.key = "operator-retry-key".to_string();
                    call(started_ms, &prepared, &[]).map(Some)
                }
                "adjudicate:missing-artifact" => {
                    prepared.key = "operator-missing-key".to_string();
                    call(started_ms, &prepared, &[]).map(Some)
                }
                "adjudicate:not-independent" => {
                    prepared.key = "operator-biased-key".to_string();
                    let judge = prepared.resolution.judge.provider.clone();
                    prepared.adjudicator.provider = judge;
                    call(started_ms, &prepared, &prepared.artifacts).map(Some)
                }
                "adjudicate:stale-revision" => {
                    prepared.key = "operator-stale-key".to_string();
                    prepared.expected_revision -= 1;
                    call(started_ms, &prepared, &prepared.artifacts).map(Some)
                }
                "adjudicate:backdated" => {
                    // The clock moved past the decision rather than the record moving back: the
                    // rejection is about how long ago the decision was prepared.
                    prepared.key = "operator-backdated-key".to_string();
                    call(now_ms, &prepared, &prepared.artifacts).map(Some)
                }
                other => panic!("{other} has no replay"),
            };
            let (result, error) = match result {
                Ok(value) => (value, None),
                Err(error) => (None, Some(refusal(&error))),
            };
            assert_case(name, case, &result, &error, db);
            tokens.assert_drained(name, "");
        }
    }
}
