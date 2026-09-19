//! Lifecycle persistence (`Application._mutate`): apply one command in the pure domain, then
//! commit snapshot, event, receipt, artifact, registry intent and caller statements in one D1
//! batch guarded by the aggregate revision CAS. Trigger markers from our own migrations map to
//! the same public error codes.

use serde_json::{json, Value};
use worker::*;

use forecast_domain::lifecycle::{apply_command, Command, LifecycleError, Payload, Snapshot, TransitionResult};
use forecast_domain::{canonical_bytes, content_hash};

use crate::db::{batch, first, text};
use crate::routes::RouteError;

pub const MAX_ARTIFACT_BYTES: usize = 262_144;
pub const MAX_SNAPSHOT_BYTES: usize = 1_048_576;

pub type Statement = (String, Vec<Value>);

pub fn canonical_text<T: serde::Serialize>(value: &T) -> Result<String> {
    Ok(String::from_utf8(canonical_bytes(value).map_err(|e| worker::Error::from(e.to_string()))?).unwrap_or_default())
}

pub fn hash_of<T: serde::Serialize>(value: &T) -> Result<String> {
    content_hash(value).map_err(|e| worker::Error::from(e.to_string()))
}

/// `secrets.token_urlsafe(32)`.
pub fn random_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("crypto random");
    base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, bytes)
}

pub fn record_artifact<T: serde::Serialize>(
    record: &T,
    kind: &str,
    digest: Option<&str>,
    now_ms: i64,
) -> std::result::Result<Statement, RouteError> {
    let body = canonical_text(record)?;
    if body.len() > MAX_ARTIFACT_BYTES {
        return Err(RouteError::Failed(
            413,
            "artifact_too_large",
            "The evidence exceeds the storage limit.",
        ));
    }
    let hash = match digest {
        Some(d) => d.to_string(),
        None => hash_of(record)?,
    };
    Ok((
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)".to_string(),
        vec![
            json!(hash),
            json!(kind),
            json!(body),
            json!("application/json"),
            json!(now_ms),
        ],
    ))
}

fn event_statements(result: &TransitionResult) -> Result<Vec<Statement>> {
    let mut statements = Vec::new();
    for event in &result.events {
        let digest = hash_of(event)?;
        statements.push((
            "INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)".to_string(),
            vec![
                json!(event.forecast_id),
                json!(event.revision),
                json!(digest),
                json!(canonical_text(event)?),
                json!(event.occurred_at_ms),
            ],
        ));
        for effect in &event.effects {
            statements.push((
                "INSERT OR IGNORE INTO outbox(id,forecast_id,kind,created_at) VALUES(?,?,?,?)".to_string(),
                vec![
                    json!(format!("{digest}:{effect}")),
                    json!(event.forecast_id),
                    json!(effect),
                    json!(event.occurred_at_ms),
                ],
            ));
        }
    }
    let receipt = &result.receipt;
    statements.push((
        "INSERT INTO command_receipts(forecast_id,command_id,receipt) VALUES(?,?,?)".to_string(),
        vec![
            json!(receipt.forecast_id),
            json!(receipt.idempotency_key),
            json!(canonical_text(receipt)?),
        ],
    ));
    if !result.events.is_empty() {
        let forecast = result.forecast.base();
        if forecast.published_at_ms.is_some() {
            let event = forecast
                .latest_event
                .as_ref()
                .ok_or_else(|| worker::Error::from("invalid_local_event"))?;
            let event_hash = hash_of(event)?;
            if forecast.audit_head_hash.as_deref() != Some(event_hash.as_str()) {
                return Err("invalid_local_event".into());
            }
            statements.push((
                "INSERT INTO registry_intents(forecast_id,revision,event_hash,snapshot,created_at) VALUES(?,?,?,?,?)"
                    .to_string(),
                vec![
                    json!(forecast.forecast_id),
                    json!(forecast.revision),
                    json!(event_hash),
                    json!(canonical_text(&result.forecast)?),
                    json!(event.occurred_at_ms),
                ],
            ));
        }
    }
    Ok(statements)
}

pub struct Mutation<'a> {
    pub snapshot: &'a Snapshot,
    pub payload: Payload,
    pub key: String,
    pub now_ms: i64,
    pub extra: Vec<Statement>,
    pub job_token: Option<String>,
}

fn transition_error() -> RouteError {
    RouteError::Failed(
        409,
        "transition_rejected",
        "This request is not allowed in the current state or time window.",
    )
}

pub fn conflict() -> RouteError {
    RouteError::Failed(
        409,
        "revision_conflict",
        "The forecast has changed. Refresh the page and try again.",
    )
}

pub async fn load_snapshot(
    session: &D1DatabaseSession,
    forecast_id: &str,
) -> std::result::Result<Snapshot, RouteError> {
    let row = first(
        session,
        "SELECT snapshot FROM forecasts WHERE id=?",
        &[json!(forecast_id)],
    )
    .await?;
    let row = row.ok_or(RouteError::NotFound("forecast_not_found", "Forecast not found."))?;
    Snapshot::from_json(text(&row, "snapshot").unwrap_or("")).map_err(|e| RouteError::Worker(e.to_string().into()))
}

/// Apply and persist; returns the new snapshot. Constraint failures are classified like Python.
pub async fn mutate(
    session: &D1DatabaseSession,
    mutation: Mutation<'_>,
    clock_now_ms: i64,
) -> std::result::Result<Snapshot, RouteError> {
    let forecast = mutation.snapshot.base();
    let v2 = matches!(
        &mutation.payload,
        Payload::Lock { trigger: Some(_), .. }
            | Payload::ProposeResolution {
                resolution: forecast_domain::lifecycle::AnyResolution::Early(_),
                ..
            }
    );
    let command = Command {
        schema_version: if v2 { 2 } else { 1 },
        idempotency_key: mutation.key.clone(),
        expected_revision: forecast.revision,
        payload: mutation.payload.clone(),
    };
    let result = match apply_command(mutation.snapshot, &command, mutation.now_ms, None) {
        Ok(result) => result,
        Err(LifecycleError::Concurrency(_)) | Err(LifecycleError::Idempotency(_)) => return Err(conflict()),
        Err(_) => return Err(transition_error()),
    };
    let changed = &result.forecast;
    let snapshot = canonical_text(changed)?;
    if snapshot.len() > MAX_SNAPSHOT_BYTES {
        return Err(RouteError::Failed(
            413,
            "forecast_too_large",
            "The forecast record exceeds the safe storage limit.",
        ));
    }
    let guard = random_token();
    let mut guard_sql = "SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM forecasts WHERE id=? AND revision=?".to_string();
    let mut guard_params = vec![json!(guard), json!(forecast.forecast_id), json!(forecast.revision)];
    if let Some(token) = &mutation.job_token {
        guard_sql.push_str(" AND job_token=? AND job_until>?");
        guard_params.push(json!(token));
        guard_params.push(json!(clock_now_ms));
    }
    let base = changed.base();
    let mut statements: Vec<Statement> = vec![
        (format!("INSERT INTO mutation_guards(token,valid) {guard_sql}) THEN 1 ELSE 0 END"), guard_params),
        (
            "UPDATE forecasts SET snapshot=?,revision=?,state=?,updated_at=?,challenge_until=?,finalized_outcome=?,mutation_key=? WHERE id=? AND revision=?".to_string(),
            vec![
                json!(snapshot), json!(base.revision), json!(base.state), json!(base.updated_at_ms), json!(base.challenge_until_ms),
                json!(base.finalized_outcome), json!(mutation.key), json!(forecast.forecast_id), json!(forecast.revision),
            ],
        ),
    ];
    statements.extend(event_statements(&result)?);
    if let Some(artifact_hash) = result.events.first().and_then(|e| e.artifact_hash.clone()) {
        let (record, kind): (Option<Value>, &str) = match &mutation.payload {
            Payload::PauseForProviderOutage { .. } => (
                base.pause
                    .as_ref()
                    .map(|p| serde_json::to_value(p).unwrap_or(Value::Null)),
                "pause_for_provider_outage",
            ),
            payload => (
                Some(serde_json::to_value(payload).map_err(|e| RouteError::Worker(e.into()))?),
                payload.kind(),
            ),
        };
        if let Some(record) = record {
            if hash_of(&record)? == artifact_hash {
                statements.push(record_artifact(&record, kind, None, clock_now_ms)?);
            }
        }
    }
    statements.extend(mutation.extra);
    statements.push((
        "DELETE FROM mutation_guards WHERE token=?".to_string(),
        vec![json!(guard)],
    ));
    if let Err(error) = batch(session, statements).await {
        let message = error.to_string();
        if message.contains("resolution_timing_closure_mismatch") {
            // A closure has to be the conclusion of the review it names; the schema refuses
            // anything else, and the caller needs to be told why rather than see a 500.
            return Err(RouteError::Failed(
                409,
                "resolution_timing_closure_mismatch",
                "The timing review closure does not match the review it claims to conclude.",
            ));
        }
        if message.contains("resolution_timing_review") {
            return Err(RouteError::Failed(
                409,
                "resolution_timing_review",
                "Evidence publication time must be reviewed before rewards or reputation can be credited.",
            ));
        }
        if message.contains("forecast_eligibility_review") {
            return Err(RouteError::Failed(
                409,
                "early_eligibility_review",
                "Receipt timing and known-result evidence must be reviewed before resolution or rewards.",
            ));
        }
        let current = load_snapshot(session, &forecast.forecast_id).await?;
        if current.base().revision != forecast.revision || mutation.job_token.is_some() {
            return Err(conflict());
        }
        if message.contains("participation_on_hold") {
            return Err(RouteError::Failed(
                409,
                "participation_on_hold",
                "Participation is on hold while newly available evidence is reviewed.",
            ));
        }
        if message.contains("eligibility_account_hold") {
            return Err(RouteError::Failed(
                409,
                "point_correction_pending",
                "A previous stake correction must finish before you can commit more points.",
            ));
        }
        if message.contains("points_insufficient_balance") {
            return Err(RouteError::Failed(
                409,
                "insufficient_points",
                "You do not have enough available points for this stake.",
            ));
        }
        if message.contains("points_position_conflict") {
            return Err(RouteError::Failed(
                409,
                "stake_conflict",
                "The stake changed or is already settled. Refresh and try again.",
            ));
        }
        if message.contains("points_operation_conflict") {
            return Err(RouteError::Failed(
                409,
                "idempotency_conflict",
                "This stake request identifier was already used.",
            ));
        }
        return Err(RouteError::Worker(error));
    }
    Ok(result.forecast)
}
