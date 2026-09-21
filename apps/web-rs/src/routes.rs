//! Read paths served natively. Each mirrors the Python handler's query and response shape.

use serde_json::{json, Value};
use worker::*;

use forecast_domain::risk_feed::{Record, SignedRiskFeed, SignedRiskFeedV2};

use crate::{api_response, VERSION};

pub const CHALLENGE_HOURS: i64 = 48;

#[derive(Debug)]
pub enum RouteError {
    Invalid,
    Input,
    NotFound(&'static str, &'static str),
    Unauthorized(&'static str, &'static str),
    Failed(u16, &'static str, &'static str),
    Worker(worker::Error),
}

fn identifier(path: &str, prefix: &str, suffix: &str) -> Option<String> {
    let rest = path.strip_prefix(prefix)?.strip_suffix(suffix)?;
    let valid = (1..=128).contains(&rest.len())
        && rest
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
    valid.then(|| rest.to_string())
}

fn profile_card_hash(path: &str) -> Option<&str> {
    let rest = path.strip_prefix("/api/profile-cards/")?;
    (rest.len() == 64 && rest.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))).then_some(rest)
}

fn query_value(url: &Url, name: &str) -> Option<String> {
    url.query_pairs()
        .find(|(k, v)| k == name && !v.is_empty())
        .map(|(_, v)| v.to_string())
}

impl From<worker::Error> for RouteError {
    fn from(error: worker::Error) -> Self {
        RouteError::Worker(error)
    }
}

/// A module with its own coded error reaches the route layer through its status, code and message:
/// the reference's `AppError` is one type, and a port that grew one error type per module still has
/// to answer with the same three fields.
impl From<crate::points::PointsError> for RouteError {
    fn from(error: crate::points::PointsError) -> Self {
        RouteError::Failed(error.status, error.code, error.message)
    }
}

pub struct Context<'a> {
    pub env: &'a Env,
    pub session: &'a D1DatabaseSession,
    pub now_ms: i64,
}

fn feed_id(path: &str, prefix: &str) -> Option<String> {
    let rest = path.strip_prefix(prefix)?;
    let mut chars = rest.chars();
    let valid = matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && rest.len() <= 128
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
    valid.then(|| rest.to_string())
}

/// `/api/forecasts/{id}` with the Python identifier pattern `[A-Za-z0-9_.:-]{1,128}` (no sub-paths).
pub fn forecast_id(path: &str) -> Option<&str> {
    let rest = path.strip_prefix("/api/forecasts/")?;
    let valid = (1..=128).contains(&rest.len())
        && rest
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
    valid.then_some(rest)
}

/// `POST`/`PATCH` paths this Worker answers itself.
pub fn owns_write(method: &Method, path: &str) -> bool {
    match method {
        Method::Patch => path == "/api/me",
        Method::Post => {
            path == "/api/auth/register"
                || path == "/api/auth/login"
                || path == "/api/auth/logout"
                || path.starts_with("/api/auth/wallet/")
                || path == "/api/activity/read"
                || path == "/api/forecasts/compile"
                || path == "/api/forecasts"
                || identifier(path, "/api/creators/", "/follow").is_some()
                || ["/forecast", "/comments", "/share", "/evidence", "/disputes"]
                    .iter()
                    .any(|suffix| identifier(path, "/api/forecasts/", suffix).is_some())
                || identifier(path, "/api/admin/forecasts/", "/adjudicate").is_some()
                || path == "/api/admin/automation/run"
                || path == "/api/admin/sweep"
        }
        _ => false,
    }
}

/// Dispatch a guarded write with its parsed JSON object body.
pub async fn dispatch_write(
    context: &Context<'_>,
    method: &Method,
    path: &str,
    user_id: Option<&str>,
    body: &serde_json::Map<String, Value>,
    req: &Request,
) -> std::result::Result<Response, RouteError> {
    // The administrative paths come first, and before the session is required: they are authorized
    // by the bearer credential the entry already checked, and an operator is not a user. A route
    // that needed both would be a route no operator could reach.
    if path.starts_with("/api/admin/") {
        if let Some(id) = identifier(path, "/api/admin/forecasts/", "/adjudicate") {
            return crate::writes::adjudicate(context, &id, body).await;
        }
        if path == "/api/admin/automation/run" {
            return crate::writes::run_automation(context).await;
        }
        if path == "/api/admin/sweep" {
            return crate::writes::sweep(context).await;
        }
        return Err(RouteError::NotFound("not_found", "This page could not be found."));
    }
    // The routes that *make* a session come before the session requirement, for the obvious
    // reason: a caller with no session is exactly who they are for.
    if path == "/api/auth/register" {
        return crate::writes::register(context, req, body).await;
    }
    if path == "/api/auth/login" {
        return crate::writes::login(context, req, body).await;
    }
    if path == "/api/auth/logout" {
        return crate::writes::logout(context, req).await;
    }
    if path.starts_with("/api/auth/wallet/") {
        return crate::writes::wallet_login(context, req, path, body).await;
    }
    let Some(user_id) = user_id else {
        return Err(RouteError::Unauthorized(
            "authentication_required",
            "Create a profile or sign in to continue.",
        ));
    };
    let null = Value::Null;
    if *method == Method::Patch && path == "/api/me" {
        return crate::writes::update_profile(context, user_id, body.get("displayName").unwrap_or(&json!(""))).await;
    }
    if path == "/api/activity/read" {
        return crate::writes::read_activity(context, user_id).await;
    }
    if path == "/api/forecasts" {
        return crate::writes::publish_forecast(context, user_id, body).await;
    }
    if path == "/api/forecasts/compile" {
        // The per-client bound, applied before the lease: a compiler run costs a model call, and
        // this is the counter that bounds them per address rather than per account.
        let fingerprint = crate::auth::fingerprint(context.env, req)?;
        crate::writes::rate_limit(
            context.session,
            context.now_ms,
            &format!("compile-ip:{fingerprint}"),
            12,
            crate::writes::DAY_MS,
        )
        .await?;
        return crate::writes::compile_forecast(context, user_id, body).await;
    }
    if let Some(creator) = identifier(path, "/api/creators/", "/follow") {
        return crate::writes::follow(context, user_id, &creator, body.get("following").unwrap_or(&null)).await;
    }
    if let Some(id) = identifier(path, "/api/forecasts/", "/forecast") {
        if body.contains_key("stakePoints") {
            let expected = body.get("expectedUserId").and_then(Value::as_str);
            match expected {
                None => {
                    return Err(RouteError::Failed(
                        400,
                        "account_precondition_required",
                        "Reload your profile before changing points or wallet settings.",
                    ))
                }
                Some(expected) if expected != user_id => {
                    return Err(RouteError::Failed(
                        409,
                        "account_changed",
                        "Your signed-in account changed. Reload your profile before continuing.",
                    ))
                }
                _ => {}
            }
            if !body
                .get("stakePoints")
                .is_some_and(|v| v.as_i64().is_some() && !v.is_boolean())
            {
                return Err(RouteError::Invalid);
            }
        }
        return crate::writes::submit_forecast(context, user_id, &id, body).await;
    }
    if let Some(id) = identifier(path, "/api/forecasts/", "/comments") {
        return crate::writes::add_comment(context, user_id, &id, body).await;
    }
    if let Some(id) = identifier(path, "/api/forecasts/", "/share") {
        return crate::writes::record_share(context, &id, Some(user_id)).await;
    }
    if let Some(id) = identifier(path, "/api/forecasts/", "/evidence") {
        return crate::writes::report_evidence(context, user_id, &id, body.get("url").unwrap_or(&null)).await;
    }
    if let Some(id) = identifier(path, "/api/forecasts/", "/disputes") {
        return crate::writes::submit_dispute(context, user_id, &id, body).await;
    }
    Err(RouteError::NotFound("not_found", "This page could not be found."))
}

/// Paths this Worker answers itself; everything else stays with the Python Worker.
/// The administrative reads this Worker serves.
///
/// Separate from `owns` because an administrative path is authorized by a bearer credential rather
/// than by being a public GET, and the entry applies that check to every path this returns.
pub fn owns_admin_read(path: &str) -> bool {
    path == "/api/admin/automation"
}

pub fn owns(path: &str) -> bool {
    matches!(
        path,
        "/api/health"
            | "/api/status"
            | "/api/forecasts"
            | "/api/me"
            | "/api/me/markets"
            | "/api/points"
            | "/api/activity"
            | "/api/wallet"
            | "/api/billing/estimate"
    ) || forecast_id(path).is_some()
        || profile_card_hash(path).is_some()
        || identifier(path, "/api/forecasts/", "/integrity").is_some()
        || identifier(path, "/api/forecasts/", "/market").is_some()
        || identifier(path, "/api/forecasts/", "/translation").is_some()
        || identifier(path, "/api/creators/", "").is_some()
        || path.starts_with("/api/risk/feeds/")
        || path.starts_with("/api/risk/v2/feeds/")
}

pub(crate) fn var(env: &Env, name: &str) -> String {
    env.var(name).map(|v| v.to_string()).unwrap_or_default()
}

pub async fn dispatch(
    context: &Context<'_>,
    path: &str,
    req: &Request,
    url: &Url,
) -> std::result::Result<Response, RouteError> {
    match path {
        "/api/health" => health(context).await,
        "/api/status" => status(context),
        "/api/admin/automation" => automation_status(context).await,
        "/api/forecasts" => {
            let user = crate::auth::user_id(context.env, context.session, req, context.now_ms).await?;
            crate::forecasts::list_forecasts(context, user.as_deref(), &crate::forecasts::list_query(url)).await
        }
        _ if forecast_id(path).is_some() => {
            let user = crate::auth::user_id(context.env, context.session, req, context.now_ms).await?;
            crate::detail::forecast_detail(context, forecast_id(path).expect("matched"), user.as_deref()).await
        }
        "/api/billing/estimate" => crate::reads::billing_estimate(),
        _ if profile_card_hash(path).is_some() => {
            crate::reads::profile_card(context, profile_card_hash(path).expect("matched")).await
        }
        _ if identifier(path, "/api/forecasts/", "/integrity").is_some() => {
            crate::reads::integrity(
                context,
                &identifier(path, "/api/forecasts/", "/integrity").expect("matched"),
            )
            .await
        }
        _ if identifier(path, "/api/forecasts/", "/market").is_some() => {
            let id = identifier(path, "/api/forecasts/", "/market").expect("matched");
            let live = var(context.env, "LIVE_MARKETS_ENABLED") == "true";
            let view = crate::markets::market(context.session, &id, live).await.map_err(|e| {
                if e.to_string().contains("invalid_input") {
                    RouteError::Input
                } else {
                    RouteError::Worker(e)
                }
            })?;
            Ok(crate::api_response(view, 200, false)?)
        }
        _ if identifier(path, "/api/forecasts/", "/translation").is_some() => {
            let id = identifier(path, "/api/forecasts/", "/translation").expect("matched");
            crate::reads::translation(context, &id, query_value(url, "language").as_deref().unwrap_or("")).await
        }
        "/api/me" | "/api/me/markets" | "/api/points" | "/api/activity" | "/api/wallet" => {
            let user = crate::auth::user_id(context.env, context.session, req, context.now_ms).await?;
            match path {
                "/api/me" => crate::reads::me(context, user.as_deref()).await,
                "/api/me/markets" => {
                    crate::reads::my_markets(context, user.as_deref(), query_value(url, "forecastId").as_deref()).await
                }
                _ => {
                    let Some(user) = user.as_deref() else {
                        return Err(RouteError::Unauthorized(
                            "authentication_required",
                            "Create a profile or sign in to continue.",
                        ));
                    };
                    match path {
                        "/api/points" => crate::reads::points(context, user).await,
                        "/api/activity" => crate::reads::activity(context, user).await,
                        _ => crate::reads::wallet(context, user).await,
                    }
                }
            }
        }
        _ if identifier(path, "/api/creators/", "").is_some() => {
            let user = crate::auth::user_id(context.env, context.session, req, context.now_ms).await?;
            crate::reads::creator(
                context,
                &identifier(path, "/api/creators/", "").expect("matched"),
                user.as_deref(),
            )
            .await
        }
        _ if path.starts_with("/api/risk/feeds/") => {
            let id = feed_id(path, "/api/risk/feeds/").ok_or(RouteError::Invalid)?;
            risk_feed(context, &id, false).await
        }
        _ if path.starts_with("/api/risk/v2/feeds/") => {
            let id = feed_id(path, "/api/risk/v2/feeds/").ok_or(RouteError::Invalid)?;
            risk_feed(context, &id, true).await
        }
        _ => Err(RouteError::Invalid),
    }
}

/// `GET /api/admin/automation`: the operator's view of source watching.
async fn automation_status(context: &Context<'_>) -> std::result::Result<Response, RouteError> {
    let enabled = var(context.env, "SOURCE_WATCH_ENABLED") == "true";
    let status = crate::automation::status(&crate::db::D1(context.session), enabled)
        .await
        .map_err(|detail| RouteError::Worker(worker::Error::from(detail)))?;
    Ok(api_response(status, 200, false)?)
}

async fn health(context: &Context<'_>) -> std::result::Result<Response, RouteError> {
    let row: Option<Value> = context.session.prepare("SELECT 1 AS ok").first(None).await?;
    let ok = row.is_some_and(|r| r["ok"] == json!(1));
    Ok(api_response(json!({"ok": ok, "version": VERSION}), 200, false)?)
}

fn status(context: &Context<'_>) -> std::result::Result<Response, RouteError> {
    let env = context.env;
    let mut providers = Vec::new();
    if !var(env, "AI_PROXY_URL").is_empty() && env.secret("AI_PROXY_TOKEN").is_ok() {
        providers.push("gemini");
    }
    if !var(env, "CLOUDFLARE_AI_MODEL").is_empty() {
        providers.push("cloudflare");
    }
    let registry = var(env, "SOLANA_REGISTRY_ENABLED") == "true" && !var(env, "SOLANA_PROGRAM_ID").is_empty();
    Ok(api_response(
        json!({
            "serverTime": context.now_ms, "version": VERSION, "providers": providers, "challengeHours": CHALLENGE_HOURS,
            "chain": {
                "status": if registry { "configured" } else { "unconnected" },
                "network": if registry { Some("devnet") } else { None },
                "programId": if registry { Some(var(env, "SOLANA_PROGRAM_ID")) } else { None },
                "relayEnabled": var(env, "SOLANA_REGISTRY_RELAY_ENABLED").to_lowercase() == "true",
                "transaction": Value::Null,
            },
            "features": {
                "sourceWatch": var(env, "SOURCE_WATCH_ENABLED") == "true",
                "liveMarkets": var(env, "LIVE_MARKETS_ENABLED") == "true",
                "billing": {"billable": false, "mode": "sandbox"},
            },
        }),
        200,
        false,
    )?)
}

#[derive(serde::Deserialize)]
struct EnvelopeRow {
    envelope_json: String,
}

async fn risk_feed(context: &Context<'_>, feed_id: &str, v2: bool) -> std::result::Result<Response, RouteError> {
    let table = if v2 {
        "risk_feed_publications_v2"
    } else {
        "risk_feed_publications"
    };
    let statement = context
        .session
        .prepare(format!(
            "SELECT envelope_json FROM {table} WHERE feed_id=? ORDER BY sequence DESC LIMIT 1"
        ))
        .bind(&[feed_id.into()])?;
    let row: Option<EnvelopeRow> = statement.first(None).await?;
    let now = context.now_ms;
    // The stored envelope was validated at publication; decoding again keeps the contract strict.
    let (envelope, expires) = match row {
        None => (Value::Null, None),
        Some(row) if v2 => {
            let envelope = SignedRiskFeedV2::from_json(&row.envelope_json)
                .map_err(|e| RouteError::Worker(e.to_string().into()))?;
            (
                serde_json::to_value(&envelope).map_err(|e| RouteError::Worker(e.into()))?,
                Some(envelope.payload.expires_at_ms),
            )
        }
        Some(row) => {
            let envelope =
                SignedRiskFeed::from_json(&row.envelope_json).map_err(|e| RouteError::Worker(e.to_string().into()))?;
            (
                serde_json::to_value(&envelope).map_err(|e| RouteError::Worker(e.into()))?,
                Some(envelope.payload.expires_at_ms),
            )
        }
    };
    let status = match expires {
        None => "unavailable",
        Some(expires) if expires > now => "current",
        Some(_) => "stale",
    };
    let mut data = json!({"status": status, "serverTime": now, "envelope": envelope});
    if v2 {
        data["protocol"] = json!("forecast-risk-feed-v2");
    }
    Ok(api_response(data, 200, false)?)
}
