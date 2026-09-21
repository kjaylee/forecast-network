//! User write routes without AI or chain effects: comments, shares, follows, activity, profile
//! and forecast submissions (`Application` write paths).

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use worker::*;

use forecast_domain::lifecycle::{Payload, Snapshot};
use forecast_domain::models::UserForecast;

use crate::api_response;
use crate::api_response_with;
use crate::db::{all, batch, first, get, int, text, Row};
use crate::mutate::{
    canonical_text, hash_of, load_snapshot, mutate, random_token, record_artifact, Mutation, Statement,
};
use crate::projections::{card, quality_card_sql};
use crate::reads::public_user;
use crate::routes::var;
use crate::routes::{Context, RouteError};

pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 86_400_000;
pub const MAX_STAKE: i64 = 1000;
pub const POINTS_POLICY_VERSION: &str = "participation-points-v1";
/// `Application`'s daily AI budget. One call's worth of work per question, bounded per day.
pub const AI_DAILY_LIMIT: i64 = 200;

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
pub(crate) async fn card_row(
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

/// `POST /api/admin/forecasts/{id}/adjudicate`: a prepared operator verdict.
///
/// The route layer authenticates the operator before this is reached; what is checked here is the
/// *shape* of the decision, because a missing revision is not a decision anybody can replay.
/// `revision` is required exactly — a prepared verdict that does not say which revision it was
/// prepared against is a verdict that may be applied to the wrong one.
pub async fn adjudicate(context: &Context<'_>, forecast_id: &str, body: &Map<String, Value>) -> Handler {
    let revision = match body.get("revision").and_then(Value::as_i64) {
        Some(revision) if revision >= 0 => revision,
        _ => return Err(RouteError::Input),
    };
    let resolution: forecast_domain::models::Resolution =
        serde_json::from_value(body.get("resolution").cloned().unwrap_or(Value::Null))
            .map_err(|_| RouteError::Input)?;
    let adjudicator: forecast_domain::models::AIProvenance =
        serde_json::from_value(body.get("adjudicator").cloned().unwrap_or(Value::Null))
            .map_err(|_| RouteError::Input)?;
    let mut artifacts = Vec::new();
    for record in body.get("artifacts").and_then(Value::as_array).unwrap_or(&Vec::new()) {
        let field = |name: &str| record.get(name).and_then(Value::as_str).map(str::to_string);
        let (Some(hash), Some(kind), Some(body_text)) = (field("content_hash"), field("kind"), field("body")) else {
            return Err(RouteError::Input);
        };
        let media = field("media_type").unwrap_or_else(|| "application/json".to_string());
        artifacts.push((hash, kind, body_text, media));
    }
    let key = body.get("idempotencyKey").and_then(Value::as_str).unwrap_or("");
    let db = crate::db::D1(context.session);
    let coordinator = crate::application::coordinator(context.env);
    let evidence = crate::application::evidence_fetcher();
    let collector = crate::application::text_fetcher();
    let application = crate::application::Application {
        db: &db,
        ai: &coordinator,
        evidence: &evidence,
        collector: &collector,
        reader: crate::ai::early::Retained(&db),
        now_ms: context.now_ms,
        token: &random_token,
        daily_limit: AI_DAILY_LIMIT,
        source_watch_enabled: crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
        live_markets_enabled: crate::admin::flag(context.env, "LIVE_MARKETS_ENABLED"),
        // An adjudication enters PROPOSED and never finalizes, so no chain gate is consulted.
        registry: None,
    };
    let result = application
        .adjudicate_forecast(Adjudication {
            forecast_id,
            resolution: &resolution,
            adjudicator: &adjudicator,
            artifacts: &artifacts,
            idempotency_key: key,
            expected_revision: Some(revision),
            token: &random_token,
        })
        .await?;
    Ok(api_response(result, 200, false)?)
}

/// The `Authentication` service, over the session secret the Worker holds.
/// `POST /api/auth/register`: the recovery-code account, when wallet sign-in is not required.
///
/// The switch is a *deployment* decision and it is checked before anything else: an installation
/// that requires wallet sign-in must not leave a second way in open.
pub async fn register(context: &Context<'_>, req: &Request, body: &Map<String, Value>) -> Handler {
    if crate::admin::switch(context.env, "WALLET_LOGIN_REQUIRED", true) {
        return Err(RouteError::Failed(
            409,
            "wallet_login_required",
            "Connect and sign with your wallet to create a profile.",
        ));
    }
    let fingerprint = crate::auth::fingerprint(context.env, req)?;
    rate_limit(
        context.session,
        context.now_ms,
        &format!("register:{fingerprint}"),
        5,
        DAY_MS,
    )
    .await?;
    let secret = session_secret(context)?;
    let hash = |token: &str| crate::auth::token_hash(&secret, token);
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let authentication = crate::auth::Authentication {
        db: &db,
        now_ms: &now,
        token_hash: &hash,
        random_token: &random_token,
    };
    let mut result = authentication
        .register(body.get("displayName"))
        .await
        .map_err(refused_from_auth)?;
    let user_id = result["user"]["id"].as_str().unwrap_or("").to_string();
    // The points summary is part of the same answer, and it is read *after* the account exists.
    result["points"] = crate::points::summary(&db, &user_id)
        .await
        .map_err(refused_from_points)?;
    let token = take_session_token(&mut result);
    Ok(api_response_with(result, 201, false, crate::Cookies::Session(&token))?)
}

/// `POST /api/auth/login`: import an old profile with its recovery code.
pub async fn login(context: &Context<'_>, req: &Request, body: &Map<String, Value>) -> Handler {
    let fingerprint = crate::auth::fingerprint(context.env, req)?;
    rate_limit(
        context.session,
        context.now_ms,
        &format!("login:{fingerprint}"),
        20,
        HOUR_MS,
    )
    .await?;
    // A recovery code is imported *into* a wallet context, so one has to exist: without it the
    // account would be created outside the context the caller is signing in from.
    let Some(context_token) = crate::auth::cookie(req, crate::AUTH_CONTEXT_COOKIE) else {
        return Err(RouteError::Failed(
            409,
            "wallet_context_required",
            "Start sign-in again before importing your old profile.",
        ));
    };
    let secret = session_secret(context)?;
    let hash = |token: &str| crate::auth::token_hash(&secret, token);
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let authentication = crate::auth::Authentication {
        db: &db,
        now_ms: &now,
        token_hash: &hash,
        random_token: &random_token,
    };
    let mut result = authentication
        .login(body.get("recoveryCode"), Some(&json!(context_token)))
        .await
        .map_err(refused_from_auth)?;
    let user_id = result["user"]["id"].as_str().unwrap_or("").to_string();
    result["points"] = crate::points::summary(&db, &user_id)
        .await
        .map_err(refused_from_points)?;
    let token = take_session_token(&mut result);
    Ok(api_response_with(result, 200, false, crate::Cookies::Session(&token))?)
}

/// `POST /api/auth/logout`.
pub async fn logout(context: &Context<'_>, req: &Request) -> Handler {
    let secret = session_secret(context)?;
    let hash = |token: &str| crate::auth::token_hash(&secret, token);
    let session_token = crate::auth::cookie(req, crate::SESSION_COOKIE);
    let context_token = crate::auth::cookie(req, crate::AUTH_CONTEXT_COOKIE);
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let authentication = crate::auth::Authentication {
        db: &db,
        now_ms: &now,
        token_hash: &hash,
        random_token: &random_token,
    };
    let result = authentication
        .logout(session_token.as_deref(), context_token.as_deref())
        .await
        .map_err(refused_from_auth)?;
    Ok(api_response_with(result, 200, false, crate::Cookies::ClearSession)?)
}

/// The session secret, with the reference's own floor. A short one is a misconfiguration, and it
/// is refused rather than used: every token hash would be weaker for it.
fn session_secret(context: &Context<'_>) -> std::result::Result<String, RouteError> {
    let secret = var(context.env, "SESSION_SECRET");
    let secret = if secret.is_empty() {
        context
            .env
            .secret("SESSION_SECRET")
            .ok()
            .map(|value| value.to_string())
            .unwrap_or_default()
    } else {
        secret
    };
    if secret.len() < 32 {
        return Err(RouteError::Failed(
            503,
            "configuration_unavailable",
            "Service configuration is temporarily unavailable.",
        ));
    }
    Ok(secret)
}

/// `sessionToken` is the cookie, not part of the body: it is removed from the answer so a caller
/// never sees the credential in a payload it might log.
fn take_session_token(result: &mut Value) -> String {
    result
        .as_object_mut()
        .and_then(|object| object.remove("sessionToken"))
        .and_then(|token| token.as_str().map(str::to_string))
        .unwrap_or_default()
}

fn refused_from_auth(error: crate::auth::AuthError) -> RouteError {
    RouteError::Failed(error.status, error.code, error.message)
}

fn refused_from_points(error: crate::points::PointsError) -> RouteError {
    RouteError::Failed(error.status, error.code, error.message)
}

/// The wallet sign-in routes: one service, reached four ways.
///
/// Two of them refuse a body outright. A bootstrap that accepted client identity, or a cancellation
/// that did, would be a way to influence a flow whose whole point is that the client supplies only
/// a wallet and a signature — and only at the two moments the server asks for them.
pub async fn wallet_login(context: &Context<'_>, req: &Request, path: &str, body: &Map<String, Value>) -> Handler {
    let fingerprint = crate::auth::fingerprint(context.env, req)?;
    // The reference puts no counter on cancellation, because a cancellation a rate limit refused
    // would leave a half-finished sign-in the caller cannot clear.
    if let Some(limit) = wallet_rate_limit(path) {
        rate_limit(
            context.session,
            context.now_ms,
            &format!("{}:{fingerprint}", wallet_scope(path)),
            limit,
            HOUR_MS,
        )
        .await?;
    }
    let origin = origin_of(req)?;
    let context_token = crate::auth::cookie(req, crate::AUTH_CONTEXT_COOKIE);
    let session_token = crate::auth::cookie(req, crate::SESSION_COOKIE);
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let secret = session_secret(context)?;
    let hash = |token: &str| crate::auth::token_hash(&secret, token);
    let verifier = crate::application::signature_verifier();
    let points = ProductionPoints {
        session: context.session,
    };
    let hook = RegisterHook {
        fingerprint: fingerprint.clone(),
        session: context.session,
        now_ms: context.now_ms,
    };
    let login = crate::wallet_login::WalletLogin {
        db: &db,
        now_ms: &now,
        token_hash: &hash,
        random_token: &random_token,
        verify_signature: &verifier,
        points: &points,
        origin,
        on_create: Some(&hook),
    };
    let context_value = context_token.as_ref().map(|token| json!(token));
    match path {
        "/api/auth/wallet/context" => {
            if !body.is_empty() {
                return Err(RouteError::Failed(
                    400,
                    "invalid_input",
                    "Context bootstrap does not accept client identity.",
                ));
            }
            let mut result = login
                .context(context_value.as_ref())
                .await
                .map_err(refused_from_wallet)?;
            let token = take_context_token(&mut result);
            Ok(api_response_with(result, 200, false, crate::Cookies::Context(&token))?)
        }
        "/api/auth/wallet/challenge" => {
            let result = login
                .challenge(
                    context_value.as_ref(),
                    &Value::Object(body.clone()),
                    session_token.as_deref(),
                )
                .await
                .map_err(refused_from_wallet)?;
            Ok(api_response(result, 200, false)?)
        }
        "/api/auth/wallet/verify" => {
            let mut result = login
                .verify(
                    context_value.as_ref(),
                    &Value::Object(body.clone()),
                    session_token.as_deref(),
                )
                .await
                .map_err(refused_from_wallet)?;
            let token = take_session_token(&mut result);
            Ok(api_response_with(result, 200, false, crate::Cookies::Session(&token))?)
        }
        _ => {
            if !body.is_empty() {
                return Err(RouteError::Failed(
                    400,
                    "invalid_input",
                    "Cancellation does not accept client identity.",
                ));
            }
            let result = login
                .cancel(context_value.as_ref())
                .await
                .map_err(refused_from_wallet)?;
            Ok(api_response(result, 200, false)?)
        }
    }
}

/// `PointsService.summary`, over the session the request was served with.
struct ProductionPoints<'a> {
    session: &'a D1DatabaseSession,
}

impl crate::wallets::PointsSummary for ProductionPoints<'_> {
    fn summary<'a>(&'a self, user_id: String) -> crate::wallets::BorrowedFuture<'a, Result<Value, ()>> {
        Box::pin(async move {
            crate::points::summary(&crate::db::D1(self.session), &user_id)
                .await
                .map_err(|_| ())
        })
    }
}

/// The `wallet-register` bound, applied where the reference applies it: *after* ownership is proven,
/// which is the only moment at which this is a registration rather than an attempt.
struct RegisterHook<'a> {
    fingerprint: String,
    session: &'a D1DatabaseSession,
    now_ms: i64,
}

impl crate::wallets::CreateHook for RegisterHook<'_> {
    fn created<'a>(&'a self) -> crate::wallets::BorrowedFuture<'a, Result<(), ()>> {
        Box::pin(async move {
            rate_limit(
                self.session,
                self.now_ms,
                &format!("wallet-register:{}", self.fingerprint),
                5,
                DAY_MS,
            )
            .await
            .map_err(|_| ())
        })
    }
}

/// The rate-limit scope and its bound, per phase. Named rather than positional because two integers
/// that look alike are exactly how a bound ends up on the wrong route.
fn wallet_scope(path: &str) -> &'static str {
    match path {
        "/api/auth/wallet/context" => "wallet-context",
        "/api/auth/wallet/challenge" => "wallet-login-challenge",
        _ => "wallet-login-verify",
    }
}

fn wallet_rate_limit(path: &str) -> Option<i64> {
    match path {
        "/api/auth/wallet/context" => Some(60),
        "/api/auth/wallet/challenge" => Some(30),
        "/api/auth/wallet/verify" => Some(40),
        _ => None,
    }
}

/// The origin, which the sign-in message quotes verbatim: a wallet signs the exact origin it was
/// shown, so a loose spelling here would compare two different sites.
fn origin_of(req: &Request) -> std::result::Result<String, RouteError> {
    let url = req.url()?;
    Ok(match url.port() {
        Some(port) => format!("{}://{}:{port}", url.scheme(), url.host_str().unwrap_or("")),
        None => format!("{}://{}", url.scheme(), url.host_str().unwrap_or("")),
    })
}

/// `contextToken` is the cookie, not part of the body — the same rule as the session token.
fn take_context_token(result: &mut Value) -> String {
    result
        .as_object_mut()
        .and_then(|object| object.remove("contextToken"))
        .and_then(|token| token.as_str().map(str::to_string))
        .unwrap_or_default()
}

fn refused_from_wallet(error: crate::wallets::WalletError) -> RouteError {
    RouteError::Failed(error.status, error.code, error.message)
}

/// `POST /api/forecasts/compile`: turn a question into a reviewable draft.
///
/// The order is the reference's and every step is load-bearing. The question is bounded *before*
/// the lease, so a malformed one costs nothing; the lease comes before the model, so one account
/// cannot have two compilations in flight; the duplicate candidates are read after the model has
/// seen them but before anything is written; and the draft is written in one batch with the
/// specification and assessment it names, so a draft cannot exist whose own record disagrees with
/// it. A closing time in the past is refused here rather than stored, because a draft that cannot
/// be published is not a draft.
pub async fn compile_forecast(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    user_row(context.session, user_id).await?;
    let question = crate::auth::checked_text(body.get("question"), 1000, 10)
        .map_err(|_| RouteError::Failed(400, "invalid_input", "Please check your input."))?;
    if question.chars().count() > 1000 {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "The original question must contain at most 1,000 characters.",
        ));
    }
    let db = crate::db::D1(context.session);
    let owner = format!("user:{user_id}");
    let lease = random_token();
    crate::scheduler::ai_lease(&db, &owner, &lease, context.now_ms, AI_DAILY_LIMIT)
        .await
        .map_err(|code| RouteError::Failed(429, "ai_unavailable", Box::leak(code.into_boxed_str())))?;
    let outcome = compile(context.env, &db, user_id, &question, context.now_ms, false).await;
    crate::scheduler::release_ai(&db, &owner, &lease).await;
    let result = outcome?;
    Ok(api_response(result, 200, false)?)
}

async fn compile(
    env: &Env,
    db: &crate::db::D1<'_>,
    user_id: &str,
    question: &str,
    now_ms: i64,
    canonical_series: bool,
) -> std::result::Result<Value, RouteError> {
    let candidates = crate::ai::compiler_wire::candidate_forecasts(db, question)
        .await
        .map_err(RouteError::Worker)?;
    let coordinator = crate::application::coordinator(env);
    let evidence = crate::application::evidence_fetcher();
    let compiled = crate::ai::compile::compile_question(
        &coordinator,
        &evidence,
        question,
        &candidates,
        now_ms,
        // Only an operator-declared canonical series may treat a shifted explicit measurement
        // interval as a distinct contract; a question a person wrote never does, which is why this
        // is a parameter with one call site that sets it.
        canonical_series,
    )
    .await
    .map_err(|error| {
        let mapped = crate::ai::error::ai_error(&error, false);
        RouteError::Failed(mapped.status, mapped.code, mapped.message)
    })?;
    // The freshness gate runs before the draft exists: a question whose event a retained article
    // already establishes is refused, not stored and refused later.
    let reader = crate::ai::early::Retained(db);
    let collector = crate::application::text_fetcher();
    let automation = crate::automation::Automation::new(
        db,
        Some(&collector),
        Some(&coordinator),
        &reader,
        now_ms,
        &random_token,
        crate::admin::flag(env, "SOURCE_WATCH_ENABLED"),
    );
    automation
        .check_creation(&compiled.specification)
        .await
        .map_err(|error| {
            RouteError::Failed(
                400,
                "source_temporarily_unavailable",
                Box::leak(error.message().into_boxed_str()),
            )
        })?;
    if compiled.specification.close_at_ms <= now_ms {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "The closing time must be in the future.",
        ));
    }
    let draft_id = format!("d_{}", &random_token()[..24]);
    let expiry = now_ms + HOUR_MS;
    let specification = crate::mutate::canonical_text(&compiled.specification)?;
    let assessment = crate::mutate::canonical_text(&compiled.assessment)?;
    let mut statements = crate::source_watch::artifact_sql(&retained_from(&compiled.artifacts), now_ms)
        .map_err(|refusal| RouteError::Failed(refusal.status(), refusal.code(), refusal.message()))?;
    statements.push(record_artifact(&compiled.specification, "specification", None, now_ms)?);
    statements.push(record_artifact(&compiled.assessment, "validation", None, now_ms)?);
    statements.push((
        "INSERT INTO drafts(id,user_id,specification,assessment,ai_forecast,created_at,expires_at) VALUES(?,?,?,?,?,?,?)"
            .to_string(),
        vec![
            json!(draft_id),
            json!(user_id),
            json!(specification),
            json!(assessment),
            match &compiled.ai_forecast {
                Some(forecast) => json!(crate::source_watch::compact(forecast)),
                None => Value::Null,
            },
            json!(now_ms),
            json!(expiry),
        ],
    ));
    crate::db::Database::batch(db, &statements)
        .await
        .map_err(RouteError::Worker)?;
    Ok(json!({
        "draftId": draft_id,
        "specification": crate::projections::specification(&serde_json::to_value(&compiled.specification).unwrap_or(Value::Null)),
        "assessment": {
            "publishable": true,
            "explanation": compiled.assessment.explanation,
            "provider": compiled.assessment.compiler.provider,
            "model": compiled.assessment.compiler.model,
        },
        "duplicateCandidates": compiled.specification.duplicate_candidates.iter().map(|item| json!({
            "id": item.forecast_id,
            "similarity": item.similarity_bp as f64 / 10000.0,
            "explanation": item.explanation,
        })).collect::<Vec<_>>(),
        "aiForecast": compiled.ai_forecast,
        "expiresAt": expiry,
    }))
}

/// The retained bytes a compiled artifact names, in the shape `artifact_sql` takes.
fn retained_from(artifacts: &[crate::ai::coordinator::Artifact]) -> Vec<crate::source_watch::Retained> {
    artifacts
        .iter()
        .map(|artifact| {
            (
                artifact.hash.clone(),
                artifact.kind.to_string(),
                artifact.body.clone(),
                "application/json".to_string(),
            )
        })
        .collect()
}

/// `POST /api/forecasts`: publish a draft.
///
/// The forecast is created and carried through *two* domain commands in memory before anything is
/// written — `BeginValidation` then `Publish` — because the record that is stored has to be the one
/// the domain would have produced, and both transitions are what make it that. The draft is marked
/// published in the same batch, under a `published_id IS NULL` guard, which is what makes two
/// simultaneous publishes of one draft produce one forecast rather than two.
///
/// A failure is read back before it is reported: a lost response and a lost race look the same from
/// here, so the receipt is consulted first, then the duplicate criteria, and only then is the
/// caller told the request conflicted.
pub async fn publish_forecast(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    user_row(context.session, user_id).await?;
    let draft_id = crate::auth::checked_text(body.get("draftId"), 128, 1)
        .map_err(|_| RouteError::Failed(400, "invalid_input", "Please check your input."))?;
    let key = body.get("idempotencyKey").and_then(Value::as_str).unwrap_or("");
    let db = crate::db::D1(context.session);
    let result = publish(context, &db, user_id, &draft_id, key).await?;
    Ok(api_response(result, 201, false)?)
}

/// The publication itself, without the route's body parsing: an operator seed publishes through the
/// same path a person does, and it must not go round it.
async fn publish(
    context: &Context<'_>,
    db: &crate::db::D1<'_>,
    user_id: &str,
    draft_id: &str,
    key: &str,
) -> std::result::Result<Value, RouteError> {
    let request = json!({"kind": "publish", "draftId": draft_id});
    if let Some(prior) = prior_row(db, user_id, key, &request).await? {
        let forecast_id = text(&prior, "forecast_id").unwrap_or("").to_string();
        return Ok(json!({"forecast": card_row(db, &forecast_id, context.now_ms).await?}));
    }
    let draft = crate::db::Database::first(
        db,
        "SELECT * FROM drafts WHERE id=? AND user_id=?",
        &[json!(draft_id), json!(user_id)],
    )
    .await?
    .ok_or(RouteError::NotFound("draft_not_found", "Draft not found."))?;
    if let Some(published) = text(&draft, "published_id") {
        return Ok(json!({"forecast": card_row(db, published, context.now_ms).await?}));
    }
    let now = context.now_ms;
    if int(&draft, "expires_at").unwrap_or(0) <= now {
        return Err(RouteError::Failed(
            410,
            "draft_expired",
            "This draft has expired. Please submit the question for review again.",
        ));
    }
    rate_limit(context.session, now, &format!("publish:{user_id}"), 10, DAY_MS).await?;
    let specification: forecast_domain::models::ForecastSpecification =
        serde_json::from_str(text(&draft, "specification").unwrap_or("")).map_err(|_| invalid())?;
    let assessment: forecast_domain::models::ValidationAssessment =
        serde_json::from_str(text(&draft, "assessment").unwrap_or("")).map_err(|_| invalid())?;
    // The freshness gate again, at the moment of publication: a draft made an hour ago may name an
    // event a retained article has since established.
    let coordinator = crate::application::coordinator(context.env);
    let collector = crate::application::text_fetcher();
    let reader = crate::ai::early::Retained(db);
    crate::automation::Automation::new(
        db,
        Some(&collector),
        Some(&coordinator),
        &reader,
        now,
        &random_token,
        crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
    )
    .check_creation(&specification)
    .await
    .map_err(|error| {
        RouteError::Failed(
            400,
            "source_temporarily_unavailable",
            Box::leak(error.message().into_boxed_str()),
        )
    })?;
    let forecast_id = format!("f_{}", &random_token()[..24]);
    let created = forecast_domain::lifecycle::create_forecast(
        &forecast_id,
        user_id,
        specification.clone(),
        int(&draft, "created_at").unwrap_or(now),
    )
    .map_err(|_| invalid())?;
    let snapshot = forecast_domain::lifecycle::Snapshot::V1(created);
    let validated = forecast_domain::lifecycle::apply_command(
        &snapshot,
        &forecast_domain::lifecycle::Command {
            schema_version: 1,
            idempotency_key: "begin-validation".to_string(),
            expected_revision: 0,
            payload: Payload::BeginValidation { schema_version: 1 },
        },
        now,
        None,
    )
    .map_err(|_| invalid())?;
    let published = forecast_domain::lifecycle::apply_command(
        &validated.forecast,
        &forecast_domain::lifecycle::Command {
            schema_version: 1,
            idempotency_key: "publish".to_string(),
            expected_revision: 1,
            payload: Payload::Publish {
                schema_version: 1,
                assessment: assessment.clone(),
            },
        },
        now,
        None,
    )
    .map_err(|_| invalid())?;
    let forecast = published.forecast.base().clone();
    let mut statements: Vec<Statement> = vec![
        (
            "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,             normalized_question,specification_hash,open_at,close_at,created_at,updated_at,ai_forecast,mutation_key)              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                .to_string(),
            vec![
                json!(forecast_id),
                json!(user_id),
                json!(draft_id),
                json!(crate::mutate::canonical_text(&published.forecast)?),
                json!(forecast.revision),
                json!(forecast.state),
                json!(specification.category),
                json!(specification.share_title),
                json!(specification.canonical_question),
                json!(crate::automation::casefold(&specification.canonical_question)
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")),
                json!(specification.specification_hash().map_err(|_| invalid())?),
                json!(specification.open_at_ms),
                json!(specification.close_at_ms),
                json!(now),
                json!(now),
                draft.get("ai_forecast").cloned().unwrap_or(Value::Null),
                json!(key),
            ],
        ),
        (
            "UPDATE drafts SET published_id=? WHERE id=? AND user_id=? AND published_id IS NULL".to_string(),
            vec![json!(forecast_id), json!(draft_id), json!(user_id)],
        ),
    ];
    statements.extend(crate::mutate::event_statements(&validated)?);
    statements.extend(crate::mutate::event_statements(&published)?);
    if crate::admin::flag(context.env, "SOLANA_REGISTRY_ENABLED") {
        statements.push(crate::registry_chain::registry_enable_sql(&forecast_id));
    }
    statements.push(operation(user_id, key, &request, &forecast_id, &json!({}), now)?);
    // The title is the fixed phrase and the *body* is the share title — read the SELECT against the
    // column list, because the two are in the opposite order to what the names suggest.
    statements.push((
        "INSERT OR IGNORE INTO activity(id,user_id,forecast_id,kind,title,body,created_at) \
         SELECT ?||':'||follower_id,follower_id,?,'creator_published',?,?,? FROM follows WHERE creator_id=?"
            .to_string(),
        vec![
            json!(format!("published:{forecast_id}")),
            json!(forecast_id),
            json!("New forecast from a creator you follow"),
            json!(specification.share_title),
            json!(now),
            json!(user_id),
        ],
    ));
    if crate::db::Database::batch(db, &statements).await.is_err() {
        if let Some(prior) = prior_row(db, user_id, key, &request).await? {
            let forecast_id = text(&prior, "forecast_id").unwrap_or("").to_string();
            return Ok(json!({"forecast": card_row(db, &forecast_id, context.now_ms).await?}));
        }
        let duplicate = crate::db::Database::first(
            db,
            "SELECT id FROM forecasts WHERE specification_hash=? OR (normalized_question=? AND close_at=?)",
            &[
                json!(specification.specification_hash().map_err(|_| invalid())?),
                json!(crate::automation::casefold(&specification.canonical_question)
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")),
                json!(specification.close_at_ms),
            ],
        )
        .await?;
        if duplicate.is_some() {
            return Err(RouteError::Failed(
                409,
                "duplicate_forecast",
                "A forecast with the same resolution criteria already exists. Please join the existing forecast.",
            ));
        }
        return Err(crate::mutate::conflict());
    }
    Ok(json!({"forecast": card_row(db, &forecast_id, context.now_ms).await?}))
}

/// `require_expected_user`: the displayed account is a precondition, not a decoration.
///
/// A profile can change in another tab, so an action that moves points or wallet settings carries
/// the account the caller believed they were acting as. A missing precondition is the caller's to
/// fix; a mismatched one is a different account entirely, and those are two different answers.
pub(crate) fn require_expected_user(body: &Map<String, Value>, user_id: &str) -> std::result::Result<(), RouteError> {
    let Some(expected) = body.get("expectedUserId").and_then(Value::as_str) else {
        return Err(RouteError::Failed(
            400,
            "account_precondition_required",
            "Reload your profile before changing points or wallet settings.",
        ));
    };
    if expected != user_id {
        return Err(RouteError::Failed(
            409,
            "account_changed",
            "Your signed-in account changed. Reload your profile before continuing.",
        ));
    }
    Ok(())
}

/// The three wallet-management routes: challenge, link and unlink.
///
/// All three are gated by the wallet-login switch and by the displayed-account precondition, and
/// both gates are checked before the limiter: an installation that has moved to wallet sign-in must
/// not leave the migration routes open, and a stale tab must not spend a caller's daily allowance.
pub async fn wallet(
    context: &Context<'_>,
    req: &Request,
    path: &str,
    user_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    if crate::admin::switch(context.env, "WALLET_LOGIN_REQUIRED", true) {
        return Err(RouteError::Failed(
            409,
            "wallet_migration_required",
            "Use wallet sign-in to migrate your existing profile.",
        ));
    }
    require_expected_user(body, user_id)?;
    let (scope, limit) = match path {
        "/api/wallet/challenge" => ("wallet-challenge", 20),
        "/api/wallet/link" => ("wallet-link", 40),
        _ => ("wallet-unlink", 10),
    };
    rate_limit(
        context.session,
        context.now_ms,
        &format!("{scope}:{user_id}"),
        limit,
        HOUR_MS,
    )
    .await?;
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let verifier = crate::application::signature_verifier();
    let points = ProductionPoints {
        session: context.session,
    };
    let service = crate::wallets::WalletService {
        db: &db,
        now_ms: &now,
        random_token: &random_token,
        verify_signature: &verifier,
        points: &points,
        origin: origin_of(req)?,
    };
    let result = match path {
        "/api/wallet/challenge" => {
            service
                .challenge(user_id, body.get("address").and_then(Value::as_str).unwrap_or(""))
                .await
        }
        "/api/wallet/link" => {
            // The precondition is the *route's*, not the service's: the body it forwards carries
            // only what the wallet protocol is about.
            let wallet_body: Map<String, Value> = body
                .iter()
                .filter(|(key, _)| key.as_str() != "expectedUserId")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            service.link(user_id, &Value::Object(wallet_body)).await
        }
        _ => service.unlink(user_id).await,
    }
    .map_err(refused_from_wallet)?;
    Ok(api_response(result, 200, false)?)
}

/// `POST /api/seeker/verify`: prove a Seeker device from its own token.
pub async fn seeker_verify(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    require_expected_user(body, user_id)?;
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let rpc = seeker_rpc(context.env);
    let limiter = SeekerLimit {
        session: context.session,
        now_ms: context.now_ms,
    };
    let verification = crate::seeker::SeekerVerification {
        db: &db,
        rpc: rpc.as_deref(),
        now_ms: &now,
        rate_limit: &limiter,
    };
    let result = verification
        .verify(user_id)
        .await
        .map_err(|error| RouteError::Failed(error.status, error.code, error.message))?;
    Ok(api_response(result, 200, false)?)
}

/// `mainnet_rpc`: a keyed endpoint first, then the public failover list from the vars.
fn seeker_rpc(env: &Env) -> Option<Box<crate::seeker::Rpc>> {
    let keyed = env
        .secret("SOLANA_MAINNET_RPC_KEYED")
        .ok()
        .map(|value| value.to_string());
    let public = var(env, "SOLANA_MAINNET_RPC");
    let mut urls: Vec<String> = Vec::new();
    if let Some(keyed) = keyed {
        if !keyed.is_empty() {
            urls.push(keyed);
        }
    }
    urls.extend(
        public
            .split(',')
            .map(str::trim)
            .filter(|url| !url.is_empty())
            .map(str::to_string),
    );
    if urls.is_empty() {
        return None;
    }
    let rpc: crate::seeker::Rpc = Box::new(move |method: String, params: Vec<Value>| {
        let urls = urls.clone();
        Box::pin(async move { rpc_call(&urls, &method, &params).await })
    });
    Some(Box::new(rpc))
}

/// A JSON-RPC call with failover, for the one reader that talks to a chain this Worker does not own.
async fn rpc_call(urls: &[String], method: &str, params: &[Value]) -> Result<Value, ()> {
    for url in urls {
        let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
        let headers = vec![("Content-Type".to_string(), "application/json".to_string())];
        let Ok(text) = crate::application::post_json(url, &headers, &body).await else {
            continue;
        };
        if let Ok(reply) = serde_json::from_str::<Value>(&text) {
            if let Some(result) = reply.get("result") {
                return Ok(result.clone());
            }
        }
    }
    Err(())
}

struct SeekerLimit<'a> {
    session: &'a D1DatabaseSession,
    now_ms: i64,
}

impl crate::seeker::RateLimit for SeekerLimit<'_> {
    fn check<'a>(
        &'a self,
        scope: String,
        limit: i64,
        window_ms: i64,
    ) -> crate::seeker::BorrowedFuture<'a, Result<(), ()>> {
        Box::pin(async move {
            rate_limit(self.session, self.now_ms, &scope, limit, window_ms)
                .await
                .map_err(|_| ())
        })
    }
}

/// `POST /api/me/share-card`: publish the profile card, so nothing is public until it is shared.
pub async fn share_card(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    // The body's key set is checked exactly: a publication that accepted anything else would be a
    // way to hand this route fields it does not read.
    let exact = body.len() == 1 && body.contains_key("expectedUserId");
    if !exact || !body["expectedUserId"].is_string() {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "Profile publication requires the displayed account precondition.",
        ));
    }
    if body["expectedUserId"].as_str() != Some(user_id) {
        return Err(RouteError::Failed(
            409,
            "profile_owner_changed",
            "Your signed-in profile changed. Reload your profile before sharing.",
        ));
    }
    rate_limit(
        context.session,
        context.now_ms,
        &format!("profile-card:{user_id}"),
        10,
        HOUR_MS,
    )
    .await?;
    let db = crate::db::D1(context.session);
    let result = crate::profile_cards::create(&db, user_id, context.now_ms)
        .await
        .map_err(|error| RouteError::Failed(error.status, error.code, error.message))?;
    Ok(api_response(result, 201, false)?)
}

/// `POST /api/admin/seed`: create a genuine, compiler-reviewed question with no votes.
///
/// Editorial questions must be genuinely *open*: when the compiler's own forecast falls outside the
/// uncertainty band the draft is discarded rather than published, so a question like "will there be
/// a new version" never reaches the feed looking like a market. That check is the whole reason this
/// is not merely compile-then-publish.
///
/// The seed key is derived from the question, so seeding the same question twice returns the first
/// one: the operations row is the record that it was already asked.
pub async fn seed(
    context: &Context<'_>,
    question: &str,
    creator_name: &str,
    uncertainty_band: Option<(i64, i64)>,
    canonical_risk: bool,
) -> std::result::Result<Value, RouteError> {
    let db = crate::db::D1(context.session);
    let editorial = crate::db::Database::first(&db, "SELECT id FROM users WHERE id='system_editorial'", &[]).await?;
    if editorial.is_none() {
        let secret = session_secret(context)?;
        let recovery = crate::auth::token_hash(&secret, &format!("recovery:{}", random_token()));
        crate::db::Database::execute(
            &db,
            "INSERT OR IGNORE INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
            &[
                json!("system_editorial"),
                json!(creator_name.chars().take(40).collect::<String>()),
                json!("forecast_editorial"),
                json!(recovery),
                json!(context.now_ms),
            ],
        )
        .await?;
    }
    let seed_key = format!(
        "seed:{}",
        forecast_domain::content_hash(&json!({"question": question}))
            .map_err(|error| RouteError::Worker(error.to_string().into()))?
    );
    let previous = crate::db::Database::first(
        &db,
        "SELECT forecast_id FROM operations WHERE user_id=? AND operation_key=?",
        &[json!("system_editorial"), json!(seed_key)],
    )
    .await?;
    if let Some(previous) = previous {
        let forecast_id = text(&previous, "forecast_id").unwrap_or("").to_string();
        return Ok(json!({"forecast": card_row(&db, &forecast_id, context.now_ms).await?}));
    }
    let draft = compile(
        context.env,
        &db,
        "system_editorial",
        question,
        context.now_ms,
        canonical_risk,
    )
    .await?;
    if let Some((low, high)) = uncertainty_band {
        if let Some(probability) = draft.get("aiForecast").and_then(|forecast| forecast.get("probability")) {
            if let Some(probability) = probability.as_f64() {
                if !(low as f64..=high as f64).contains(&probability) {
                    return Err(RouteError::Failed(
                        409,
                        "seed_not_uncertain",
                        Box::leak(
                            format!(
                                "The compiler already expects this outcome ({probability:.0}% YES); editorial questions must be genuinely open."
                            )
                            .into_boxed_str(),
                        ),
                    ));
                }
            }
        }
    }
    let draft_id = draft["draftId"].as_str().unwrap_or("");
    publish(context, &db, "system_editorial", draft_id, &seed_key).await
}

/// `POST /api/forecasts/{id}/attest/{prepare|confirm}`: a phone signs a Devnet memo.
///
/// `prepare` returns an *incomplete* transaction — one real signature and one zeroed slot — which is
/// the whole contract: the relayer pays the fee and the wallet fills the slot, so neither side can
/// produce the other's signature. `confirm` is what makes the record final, and it is separate
/// because a phone that never came back must not leave a half-signed transaction looking sent.
pub async fn attest(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    action: &str,
    body: &Map<String, Value>,
) -> Handler {
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let signer = crate::application::relayer_signer(context.env);
    let relayer = relayer_public_key(context.env);
    let limiter = AttestationLimit {
        session: context.session,
        now_ms: context.now_ms,
    };
    // The relayer is only available when both its address and its seed are deployed: an address
    // with no seed cannot sign, and a seed with no address cannot be checked against.
    let attestations = crate::attestation::Attestations {
        db: &db,
        relayer,
        sign: signer.as_deref(),
        now_ms: &now,
        random_token: &random_token,
        rate_limit: &limiter,
    };
    let payload = Value::Object(body.clone());
    if action == "prepare" {
        let result = attestations
            .prepare(user_id, forecast_id, &payload)
            .await
            .map_err(refused_from_attestation)?;
        return Ok(api_response(result, 201, false)?);
    }
    let result = attestations
        .confirm(user_id, forecast_id, &payload)
        .await
        .map_err(refused_from_attestation)?;
    Ok(api_response(result, 200, false)?)
}

/// `relayer_public_key`: the hot relayer's address, only when its seed is deployed too.
fn relayer_public_key(env: &Env) -> Option<[u8; 32]> {
    let address = var(env, "SOLANA_RELAYER");
    let seed = env.secret("SOLANA_RELAYER_SEED").ok().map(|value| value.to_string())?;
    if address.is_empty() || seed.is_empty() {
        return None;
    }
    let decoded = bs58::decode(address).into_vec().ok()?;
    <[u8; 32]>::try_from(decoded.as_slice()).ok()
}

/// The `attest` limiter, over the session the request was served with.
struct AttestationLimit<'a> {
    session: &'a D1DatabaseSession,
    now_ms: i64,
}

impl crate::attestation::RateLimit for AttestationLimit<'_> {
    fn check<'a>(
        &'a self,
        scope: String,
        limit: i64,
        window_ms: i64,
    ) -> crate::attestation::BorrowedFuture<'a, Result<(), ()>> {
        Box::pin(async move {
            rate_limit(self.session, self.now_ms, &scope, limit, window_ms)
                .await
                .map_err(|_| ())
        })
    }
}

fn refused_from_attestation(error: crate::attestation::AttestationError) -> RouteError {
    RouteError::Failed(error.status, error.code, error.message)
}

/// `POST /api/forecasts/{id}/disputes`: a participant challenges the proposed resolution.
///
/// The evidence is *collected here*, under the application's own AI lease, and only then is the
/// dispute built: a dispute whose evidence could not be retained is not a dispute anybody can
/// review, and recording one would put an unreviewable claim in the record. The two failures are
/// told apart because the adjudication shows a different thing for each.
pub async fn submit_dispute(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    user_row(context.session, user_id).await?;
    let invalid = || RouteError::Failed(400, "invalid_input", "Please check your input.");
    let claim = crate::auth::checked_text(body.get("claim"), 1000, 1).map_err(|_| invalid())?;
    let explanation = crate::auth::checked_text(body.get("explanation"), 3000, 1).map_err(|_| invalid())?;
    let evidence_url = crate::auth::checked_text(body.get("evidenceUrl"), 2000, 1).map_err(|_| invalid())?;
    let rule_clause_id = crate::auth::checked_text(body.get("ruleClauseId"), 128, 1).map_err(|_| invalid())?;
    let revision = match body.get("revision").and_then(Value::as_i64) {
        Some(revision) if revision >= 0 => revision,
        _ => return Err(crate::writes::invalid()),
    };
    let key = body.get("idempotencyKey").and_then(Value::as_str).unwrap_or("");
    let request = json!({
        "kind": "dispute", "forecastId": forecast_id, "claim": claim,
        "evidenceUrl": evidence_url, "ruleClauseId": rule_clause_id,
        "explanation": explanation, "revision": revision,
    });
    let db = crate::db::D1(context.session);
    if let Some(prior) = prior_row(&db, user_id, key, &request).await? {
        let mut receipt = prior_result(&prior)?;
        receipt["forecast"] = card_row(&db, forecast_id, context.now_ms).await?;
        return Ok(api_response(receipt, 200, false)?);
    }
    let snapshot = load_snapshot(&db, forecast_id).await?;
    let forecast = snapshot.base().clone();
    if forecast.revision != revision {
        return Err(crate::mutate::conflict());
    }
    // A dispute is a challenge, so it may only be filed inside the challenge window — and the
    // window is the *record's* own, not a fresh one.
    if !matches!(forecast.state.as_str(), "CHALLENGE" | "DISPUTED")
        || forecast.challenge_until_ms.is_none_or(|until| until <= context.now_ms)
    {
        return Err(RouteError::Failed(
            409,
            "challenge_closed",
            "The challenge window is closed.",
        ));
    }
    if !forecast.specification.clause_ids().contains(&rule_clause_id.as_str()) {
        return Err(invalid());
    }
    rate_limit(
        context.session,
        context.now_ms,
        &format!("dispute:{user_id}"),
        10,
        DAY_MS,
    )
    .await?;
    let owner = format!("user:{user_id}");
    let lease = random_token();
    crate::scheduler::ai_lease(&db, &owner, &lease, context.now_ms, AI_DAILY_LIMIT)
        .await
        .map_err(|code| RouteError::Failed(503, "ai_unavailable", Box::leak(code.into_boxed_str())))?;
    // The collector owns the transport: `DisputeCollector`'s future is `'static`, so the closure
    // cannot borrow one that lives in this frame.
    let fetch = std::rc::Rc::new(crate::application::text_fetcher());
    let collector: Box<crate::ai::dispute::DisputeCollector> = Box::new(
        move |specification: &forecast_domain::models::ForecastSpecification, url: String, now_ms: i64| {
            let fetch = fetch.clone();
            let specification = specification.clone();
            Box::pin(async move {
                crate::sources::collect_dispute(|target| fetch(target, Vec::new()), &specification, &url, now_ms).await
            })
        },
    );
    let started = context.now_ms;
    let outcome = crate::ai::dispute::collect_dispute_evidence(
        &collector,
        &forecast.specification,
        &evidence_url,
        context.now_ms,
    )
    .await;
    crate::scheduler::release_ai(&db, &owner, &lease).await;
    let (evidence, artifact) = outcome.map_err(|error| {
        let mapped = crate::ai::error::ai_error(
            &error,
            matches!(error, crate::ai::coordinator::CoordinatorError::Unavailable { .. }),
        );
        RouteError::Failed(mapped.status, mapped.code, mapped.message)
    })?;
    let _ = started;
    let now = context.now_ms;
    let Some(resolution) = forecast.resolution.as_ref() else {
        return Err(crate::mutate::conflict());
    };
    let dispute = forecast_domain::models::Dispute {
        schema_version: 1,
        dispute_id: format!("d_{}", &random_token()[..24]),
        disputant_id: user_id.to_string(),
        forecast_id: forecast_id.to_string(),
        specification_hash: forecast.specification_hash.clone(),
        resolution_hash: resolution.resolution_hash().map_err(|_| invalid())?,
        claim,
        evidence: vec![evidence],
        rule_clause_id,
        explanation,
        submitted_at_ms: now,
    };
    let response = json!({"dispute": {"id": dispute.dispute_id, "claim": dispute.claim,
                                      "submittedAt": now, "hash": dispute.dispute_hash().map_err(|_| invalid())?}});
    let retained = vec![(
        artifact.hash.clone(),
        artifact.kind.to_string(),
        artifact.body.clone(),
        "application/json".to_string(),
    )];
    let mut extra = crate::source_watch::artifact_sql(&retained, now)
        .map_err(|refusal| RouteError::Failed(refusal.status(), refusal.code(), refusal.message()))?;
    extra.push(record_artifact(&dispute, "dispute", None, now)?);
    let operation_key = format!(
        "user:{}",
        forecast_domain::content_hash(&json!({"user": user_id, "key": key}))
            .map_err(|error| RouteError::Worker(error.to_string().into()))?
    );
    extra.push(operation(user_id, key, &request, forecast_id, &response, now)?);
    mutate(
        &db,
        Mutation {
            snapshot: &snapshot,
            payload: Payload::SubmitDispute {
                schema_version: 1,
                dispute,
            },
            key: operation_key,
            now_ms: now,
            extra,
            job_token: None,
            timing_artifacts: Vec::new(),
        },
        now,
        &random_token,
        None,
    )
    .await?;
    let mut result = response;
    result["forecast"] = card_row(&db, forecast_id, context.now_ms).await?;
    Ok(api_response(result, 200, false)?)
}

/// `POST /api/admin/automation/run`: one automation pass, on demand.
///
/// This is the path that *needs* the chain adapter: `run_automation` sweeps the lifecycle, and a
/// finalize consults the chain before it commits. Wiring it without an adapter would finalize
/// locally and differ from the reference in exactly the way that is hardest to notice.
pub async fn run_automation(context: &Context<'_>) -> Handler {
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let parts = crate::application::chain_parts(context.env, &db, &now);
    let program = bs58::decode(var(context.env, "SOLANA_PROGRAM_ID"))
        .into_vec()
        .unwrap_or_default();
    let relayer = bs58::decode(var(context.env, "SOLANA_RELAYER"))
        .into_vec()
        .unwrap_or_default();
    let transport = match &parts {
        Some(parts) => Some(
            parts
                .transport(&program, &relayer)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.0)))?,
        ),
        None => None,
    };
    let registry = match (&transport, &parts) {
        (Some(transport), Some(_)) => Some(
            crate::registry_chain::SolanaRegistry::new(&db, transport, &program, &relayer, &now, &random_token)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.code)))?,
        ),
        _ => None,
    };
    let coordinator = crate::application::coordinator(context.env);
    let evidence = crate::application::evidence_fetcher();
    let collector = crate::application::text_fetcher();
    let application = crate::application::Application {
        db: &db,
        ai: &coordinator,
        evidence: &evidence,
        collector: &collector,
        reader: crate::ai::early::Retained(&db),
        now_ms: context.now_ms,
        token: &random_token,
        daily_limit: AI_DAILY_LIMIT,
        source_watch_enabled: crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
        live_markets_enabled: crate::admin::flag(context.env, "LIVE_MARKETS_ENABLED"),
        registry: registry
            .as_ref()
            .map(|registry| registry as &dyn crate::mutate::FinalizationGate),
    };
    let result = application
        .run_automation(1)
        .await
        .map_err(|detail| RouteError::Worker(worker::Error::from(detail)))?;
    Ok(api_response(result, 200, false)?)
}

/// `POST /api/admin/sweep`: the five-minute pass, and the chain's delivery half.
///
/// Two jobs in one call, and the order is the reference's: the local sweep first, then whatever the
/// chain owes. The phase timings are returned because this route was once failing with a platform
/// error and nothing recorded where the time went — the failure was being attributed to whatever
/// seemed likeliest, which is the same reason the operate tick got them.
pub async fn sweep(context: &Context<'_>) -> Handler {
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let started = clock_ms();
    let parts = crate::application::chain_parts(context.env, &db, &now);
    let program = bs58::decode(var(context.env, "SOLANA_PROGRAM_ID"))
        .into_vec()
        .unwrap_or_default();
    let relayer = bs58::decode(var(context.env, "SOLANA_RELAYER"))
        .into_vec()
        .unwrap_or_default();
    let transport = match &parts {
        Some(parts) => Some(
            parts
                .transport(&program, &relayer)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.0)))?,
        ),
        None => None,
    };
    let registry = match (&transport, &parts) {
        (Some(transport), Some(_)) => Some(
            crate::registry_chain::SolanaRegistry::new(&db, transport, &program, &relayer, &now, &random_token)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.code)))?,
        ),
        _ => None,
    };
    let coordinator = crate::application::coordinator(context.env);
    let evidence = crate::application::evidence_fetcher();
    let collector = crate::application::text_fetcher();
    let application = crate::application::Application {
        db: &db,
        ai: &coordinator,
        evidence: &evidence,
        collector: &collector,
        reader: crate::ai::early::Retained(&db),
        now_ms: context.now_ms,
        token: &random_token,
        daily_limit: AI_DAILY_LIMIT,
        source_watch_enabled: crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
        live_markets_enabled: crate::admin::flag(context.env, "LIVE_MARKETS_ENABLED"),
        registry: registry
            .as_ref()
            .map(|registry| registry as &dyn crate::mutate::FinalizationGate),
    };
    // Four source polls per five-minute sweep keeps every watched publisher and article current;
    // one per tick starved the market source gate.
    let mut result = application
        .run_automation(4)
        .await
        .map_err(|detail| RouteError::Worker(worker::Error::from(detail)))?;
    let automation_ms = clock_ms() - started;
    if let Some(registry) = &registry {
        if crate::admin::flag(context.env, "SOLANA_REGISTRY_RELAY_ENABLED") {
            let registry_started = clock_ms();
            // A delivery that fails is a retry, not a failed sweep: the local half has already
            // completed, and reporting the whole pass as failed would re-run it.
            result["registry"] = match registry.sync(3).await {
                Ok(value) => value,
                Err(_) => json!({"status": "retry_pending"}),
            };
            result["phaseMs"]["registry"] = json!(clock_ms() - registry_started);
        } else {
            result["registry"] = json!({"status": "relay_paused"});
        }
    }
    result["phaseMs"]["automation"] = json!(automation_ms);
    result["phaseMs"]["total"] = json!(clock_ms() - started);
    Ok(api_response(result, 200, false)?)
}

/// A monotonic-enough millisecond clock for the phase timings. It is the wall clock, which is what
/// this runtime offers; a phase that takes a negative number of milliseconds would mean the clock
/// moved, and the reference's `time.monotonic` is the only thing that would have hidden it.
fn clock_ms() -> i64 {
    #[cfg(target_arch = "wasm32")]
    {
        worker::js_sys::Date::now() as i64
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        0
    }
}

/// `POST /api/forecasts/{id}/evidence`: a forecaster reports an official announcement.
///
/// The application is assembled here rather than held on the route context, because every transport
/// it needs is created from bindings the context does not carry — and because a request that does
/// not use them should not build them.
pub async fn report_evidence(context: &Context<'_>, user_id: &str, forecast_id: &str, url: &Value) -> Handler {
    let Some(url) = url.as_str() else {
        return Err(RouteError::Failed(400, "invalid_input", "Please check your input."));
    };
    let db = crate::db::D1(context.session);
    let coordinator = crate::application::coordinator(context.env);
    let evidence = crate::application::evidence_fetcher();
    let collector = crate::application::text_fetcher();
    let application = crate::application::Application {
        db: &db,
        ai: &coordinator,
        evidence: &evidence,
        collector: &collector,
        reader: crate::ai::early::Retained(&db),
        now_ms: context.now_ms,
        token: &random_token,
        daily_limit: AI_DAILY_LIMIT,
        source_watch_enabled: crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
        live_markets_enabled: crate::admin::flag(context.env, "LIVE_MARKETS_ENABLED"),
        // A report never commits anything to a chain, so no adapter is consulted on this path.
        registry: None,
    };
    let report = application.report_evidence(user_id, forecast_id, url).await?;
    Ok(api_response(report, 200, false)?)
}

/// `report_evidence`'s refusals, as the route layer reports them.
///
/// The codes are the reference's own and a *closed* set — every one of them is raised in that
/// function and nowhere else — so each is mapped to a static rather than carried through as text.
/// A route error wants `&'static str`, and the alternative to naming them here is naming them
/// nowhere.
impl From<crate::source_watch::WatchError> for RouteError {
    fn from(error: crate::source_watch::WatchError) -> Self {
        match error {
            // The refusal's own status and text are the reference's and are repeated below rather
            // than carried: a route error wants statics, and these are a closed set that belongs to
            // one function. They are repeated *exactly*, which is what makes the repetition safe to
            // check by reading.
            crate::source_watch::WatchError::Refused { code, .. } => match code.as_str() {
                "authentication_required" => {
                    RouteError::Unauthorized("authentication_required", "Please sign in to continue.")
                }
                "evidence_report_closed" => RouteError::Failed(
                    409,
                    "evidence_report_closed",
                    "This forecast is no longer accepting evidence reports.",
                ),
                "evidence_report_url" => RouteError::Failed(
                    400,
                    "evidence_report_url",
                    "Report a public https page on one of this question's official sources.",
                ),
                "evidence_report_source" => RouteError::Failed(
                    400,
                    "evidence_report_source",
                    "Only the question's published official sources can be reported.",
                ),
                "rate_limited" => RouteError::Failed(429, "rate_limited", "Too many requests. Please try again later."),
                _ => RouteError::Failed(400, "invalid_input", "Please check your input."),
            },
            // A watcher that cannot fetch is not a bad request; it is an outage of a dependency.
            other => RouteError::Worker(worker::Error::from(other.message())),
        }
    }
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
