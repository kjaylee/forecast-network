//! The sign-in surface: the recovery-code account, the session, wallet sign-in and its context,
//! the wallet link/unlink writes and the Seeker attestation. Split from `writes.rs`, whose
//! helpers it still uses.

use serde_json::{json, Map, Value};
use worker::*;

use crate::api_response;
use crate::api_response_with;
use crate::mutate::random_token;
use crate::routes::var;
use crate::routes::{Context, RouteError};
use crate::writes::*;

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
pub(crate) fn session_secret(context: &Context<'_>) -> std::result::Result<String, RouteError> {
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
