//! Forecast Network edge Worker. Native read paths run here on wasm; every other request is
//! passed untouched to the Python Worker through the `LEGACY` service binding (strangler by
//! route). Response shapes follow `apps/web/src/entry.py`.

use serde_json::{json, Value};
use worker::*;

pub mod adjudication;
pub mod admin;
pub mod admin_markets;
pub mod admin_ops;
pub mod admin_registry;
pub mod admin_risk;
pub mod admin_risk_v1;
pub mod ai;
pub mod analytics;
pub mod application;
pub mod article;
pub mod attestation;
pub mod auth;
pub mod auth_routes;
pub mod automation;
pub mod billing;
mod db;
mod detail;
mod discovery;
pub mod dispute_intake;
pub mod dispute_wire;
mod eligibility;
mod forecasts;
#[cfg(test)]
mod golden;
pub mod html;
pub mod html_entities;
pub mod html_parse;
mod market_trades;
mod markets;
mod mutate;
pub mod operator_routes;
pub mod participation_holds;
pub mod point_markets;
mod points;
pub mod profile_cards;
mod projections;
mod reads;
mod registry;
pub mod registry_chain;
mod reputation;
pub mod resolution_timing;
pub mod risk_feed;
pub mod risk_feed_series;
pub mod risk_feed_v2;
pub mod risk_ops;
pub mod risk_refresh;
mod routes;
pub mod scheduler;
pub mod seeker;
pub mod solana;
pub mod solana_rpc;
pub mod source_watch;
pub mod sources;
pub mod translation_admin;
pub mod translations;
pub mod wallet_login;
pub mod wallets;
pub mod writes;

pub const VERSION: &str = "0.13.0";
pub const BOOKMARK_COOKIE: &str = "__Host-forecast_d1";
pub const MAX_BODY_BYTES: usize = 16 * 1024;

/// Read-replica session anchored at the browser's own bookmark, like the Python entry.
fn bookmark(req: &Request) -> Option<String> {
    let raw = req.headers().get("cookie").ok().flatten()?;
    if raw.len() > 8192 {
        return None;
    }
    raw.split(';').map(str::trim).find_map(|pair| {
        let (name, value) = pair.split_once('=')?;
        (name == BOOKMARK_COOKIE
            && (1..=256).contains(&value.len())
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-')))
        .then(|| value.to_string())
    })
}

pub fn api_response(data: Value, status: u16, error: bool) -> Result<Response> {
    api_response_with(data, status, error, Cookies::None)
}

pub fn api_error(status: u16, code: &str, message: &str) -> Result<Response> {
    api_response(json!({"code": code, "message": message}), status, true)
}

pub const SESSION_COOKIE: &str = "__Host-forecast_session";
pub const AUTH_CONTEXT_COOKIE: &str = "__Host-forecast_auth";
pub const COOKIE_MAX_AGE: i64 = 2_592_000;

/// What a response does to the two cookies, if anything.
///
/// Three states rather than two because they are three *different* responses in the reference: a
/// bootstrapped context and a mutated session may not share one, and a response that carried both
/// would leave the browser holding a context for a session that does not exist yet.
pub enum Cookies<'a> {
    None,
    Session(&'a str),
    ClearSession,
    Context(&'a str),
}

fn cookie_header(cookies: &Cookies<'_>) -> Option<String> {
    let (name, value, age) = match cookies {
        Cookies::None => return None,
        Cookies::Session(value) => (SESSION_COOKIE, (*value).to_string(), COOKIE_MAX_AGE),
        Cookies::ClearSession => (SESSION_COOKIE, String::new(), 0),
        Cookies::Context(value) => (AUTH_CONTEXT_COOKIE, (*value).to_string(), COOKIE_MAX_AGE),
    };
    Some(format!(
        "{name}={value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}"
    ))
}

/// `api_response`, with the cookies it may set.
pub fn api_response_with(data: Value, status: u16, error: bool, cookies: Cookies<'_>) -> Result<Response> {
    let headers = Headers::new();
    headers.set("Content-Type", "application/json")?;
    headers.set("Cache-Control", "no-store")?;
    headers.set("X-Content-Type-Options", "nosniff")?;
    headers.set("Referrer-Policy", "strict-origin-when-cross-origin")?;
    headers.set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")?;
    if let Some(cookie) = cookie_header(&cookies) {
        headers.set("Set-Cookie", &cookie)?;
    }
    let body = json!({ (if error { "error" } else { "data" }): data });
    Ok(Response::from_bytes(serde_json::to_vec(&body)?)?
        .with_status(status)
        .with_headers(headers))
}

fn now_ms() -> i64 {
    Date::now().as_millis() as i64
}

#[event(fetch)]
pub async fn main(req: Request, env: Env, _ctx: Context) -> Result<Response> {
    console_error_panic_hook::set_once();
    let path = req.path();
    let path = path.trim_end_matches('/');
    let path = if path.is_empty() { "/" } else { path };
    if !path.starts_with("/api/") {
        return env.assets("ASSETS")?.fetch_request(req).await;
    }
    let method = req.method();
    let is_admin = path.starts_with("/api/admin/");
    let native_read = method == Method::Get && (routes::owns(path) || routes::owns_admin_read(path));
    let native_write = routes::owns_write(&method, path);
    if !native_read && !native_write {
        // `route_parity.py` proves every route the Python Worker serves is claimed here, so this
        // branch should now be unreachable for a route that exists — and reachable only for a path
        // neither Worker serves. It is left in place as the rollback path, and every request that
        // takes it is logged: those logs are the evidence for retiring the binding, and without
        // them "nothing forwards any more" is a belief rather than an observation.
        console_log!(
            "{}",
            json!({"event": "forwarded_to_legacy", "path": &path[..path.len().min(100)], "method": format!("{method:?}")})
        );
        return env.service("LEGACY")?.fetch_request(req).await;
    }
    let mut req = req;
    // An administrative request is authenticated by a bearer credential, *before* the method is
    // considered: a read an operator may make is no more public than a write, and a check that
    // guarded only writes would leave every administrative GET open.
    if is_admin && !admin::authorized(&env, &req, admin::scheduler_may_trigger(path)) {
        return api_error(403, "forbidden", "You do not have access to this action.");
    }
    let mut body: Option<serde_json::Map<String, Value>> = None;
    if native_write {
        // An administrative request is exempt from the *origin* check that guards browser writes,
        // because it does not come from a browser. That exemption is what the bearer check above
        // buys, and it is stated here rather than left to be inferred from the route table.
        if !is_admin {
            let url = req.url()?;
            let origin = format!("{}://{}", url.scheme(), url.host_str().unwrap_or(""));
            let origin = match url.port() {
                Some(port) => format!("{origin}:{port}"),
                None => origin,
            };
            if req.headers().get("origin")?.as_deref() != Some(origin.as_str())
                || req.headers().get("X-Forecast-Client")?.as_deref() != Some("web")
            {
                return api_error(
                    403,
                    "origin_denied",
                    "Please submit this request from the Forecast website.",
                );
            }
        }
        if !req
            .headers()
            .get("content-type")?
            .unwrap_or_default()
            .to_lowercase()
            .contains("application/json")
        {
            return api_error(415, "json_required", "A JSON request is required.");
        }
        let bytes = req.bytes().await?;
        // A prepared adjudication carries retained evidence, so the operator's cap is the wider
        // one. It is raised *here* rather than by widening the general cap, which every write
        // shares.
        let cap = if is_admin {
            admin::MAX_ADMIN_BODY_BYTES
        } else {
            MAX_BODY_BYTES
        };
        if bytes.len() > cap {
            return api_error(413, "payload_too_large", "The request body exceeds the size limit.");
        }
        match serde_json::from_slice::<Value>(&bytes) {
            Ok(Value::Object(map)) => body = Some(map),
            _ => return api_error(400, "invalid_request", "Check the input format and length."),
        }
    }
    let previous = bookmark(&req);
    let db = env.d1("DB")?;
    let constraint = if native_write {
        "first-primary".to_string()
    } else {
        previous.clone().unwrap_or_else(|| "first-unconstrained".to_string())
    };
    let session = db.with_session(Some(&constraint))?;
    let url = req.url()?;
    let context = routes::Context {
        env: &env,
        session: &session,
        now_ms: now_ms(),
    };
    let opened = db::usage();
    let outcome = match &body {
        Some(body) => {
            // An administrative request is not rate-limited by the *client* bucket: it is one
            // operator, and the bucket exists to bound a crowd.
            if is_admin {
                routes::dispatch_write(&context, &method, path, None, body, &req).await
            } else {
                let fingerprint = auth::fingerprint(&env, &req)?;
                match writes::rate_limit(&session, context.now_ms, &format!("http:{fingerprint}"), 180, 3_600_000).await
                {
                    Ok(()) => match auth::user_id(&env, &session, &req, context.now_ms).await {
                        Ok(user) => routes::dispatch_write(&context, &method, path, user.as_deref(), body, &req).await,
                        Err(error) => Err(routes::RouteError::Worker(error)),
                    },
                    Err(error) => Err(error),
                }
            }
        }
        None => routes::dispatch(&context, path, &req, &url).await,
    };
    // Which request spends the day's D1 rows, named by the request itself.
    //
    // The tick was the obvious suspect and it is not: measured live, one tick reads 51 rows
    // over 20 statements, some 73,000 a day against a budget of 5,000,000 that runs out
    // before lunch. The rest is ordinary traffic, and an average of ~300 rows a statement
    // says at least one route scans. A threshold rather than every request, because a log
    // line per request is itself a cost and a request reading a few rows is not the question.
    let (rows, queries) = db::usage();
    let (rows, queries) = (rows - opened.0, queries - opened.1);
    db::record(path, rows, queries, context.now_ms);
    if rows > ROWS_WORTH_NAMING {
        console_log!(
            "{}",
            json!({"event": "request_rows", "path": &path[..path.len().min(100)],
                   "method": format!("{method:?}"), "rows": rows, "queries": queries})
        );
    }
    let mut response = match outcome {
        Ok(response) => response,
        Err(routes::RouteError::Invalid) => api_error(400, "invalid_request", "Check the input format and length.")?,
        Err(routes::RouteError::Input) => api_error(400, "invalid_input", "Please check your input.")?,
        Err(routes::RouteError::NotFound(code, message)) => api_error(404, code, message)?,
        Err(routes::RouteError::Unauthorized(code, message)) => api_error(401, code, message)?,
        Err(routes::RouteError::Failed(status, code, message)) => api_error(status, code, message)?,
        Err(routes::RouteError::Worker(error)) => {
            console_error!(
                "{}",
                json!({"event": "request_failed", "path": &path[..path.len().min(100)], "errorType": error.to_string()})
            );
            api_error(503, "service_unavailable", "Please try again shortly.")?
        }
    };
    if let Ok(Some(current)) = session.get_bookmark() {
        if previous.as_deref() != Some(current.as_str()) {
            response.headers_mut().append(
                "Set-Cookie",
                &format!("{BOOKMARK_COOKIE}={current}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=86400"),
            )?;
        }
    }
    Ok(response)
}

/// The two jobs the schedule owns, dispatched in-process.
///
/// The reference's `scheduled` handler dispatched through a self service binding, because its
/// background work had to run in the ordinary authenticated fetch path — and that is why a
/// borrowed cron needed the operator's secret, and why a per-minute cron could not survive on
/// Pyodide (an invocation awaiting its own fetch collided with the next request on the isolate).
/// A scheduled event is not an HTTP request: it carries no bearer, needs none, and the work is
/// called here directly against a primary session. Each cron pattern owns one job, as before.
///
/// **The per-minute pattern is not declared in `wrangler.jsonc` as of 2026-09-22, and the arm
/// below is kept for when it is.** A scheduled event on the Free plan is killed at 10 ms of
/// CPU. The tick performed its D1 reads and was killed before publishing, so it spent the
/// day's row budget and delivered nothing: with this cron and the operator host both running,
/// the 5M rows granted at midnight were gone by 06:29Z, against 20:00Z on the day the host was
/// off. An HTTP invocation is allowed more CPU, so the host's tick is the publisher until this
/// one fits the limit. Restoring the cron is how it comes back — the code does not change.
/// A request that reads more rows than this names itself in the log. One D1 page is 4 KB and a
/// lookup on an index reads single digits, so a request over this figure either returned a large
/// page or scanned — and both are worth being able to point at.
const ROWS_WORTH_NAMING: i64 = 500;

#[event(scheduled)]
pub async fn scheduled(event: ScheduledEvent, env: Env, _ctx: ScheduleContext) {
    console_error_panic_hook::set_once();
    let started = now_ms();
    let (name, path) = if event.cron() == "* * * * *" {
        ("scheduled_risk_v2", "/api/admin/risk/v2/operate")
    } else {
        ("scheduled_sweep", "/api/admin/sweep")
    };
    let outcome = async {
        let db = env.d1("DB").map_err(|error| error.to_string())?;
        let session = db
            .with_session(Some("first-primary"))
            .map_err(|error| error.to_string())?;
        let context = routes::Context {
            env: &env,
            session: &session,
            now_ms: now_ms(),
        };
        let body = serde_json::Map::new();
        let response = if path == "/api/admin/sweep" {
            operator_routes::sweep(&context).await
        } else {
            admin_risk::operate_route(&context, &body).await
        };
        match response {
            Ok(response) => Ok(response.status_code()),
            Err(routes::RouteError::Worker(error)) => Err(error.to_string()),
            Err(other) => Err(format!("{other:?}")),
        }
    }
    .await;
    // The same line the reference printed, so the dashboards that read it keep reading it; the
    // duration is new, because the cold start this schedule no longer pays was the point.
    match outcome {
        Ok(status) => console_log!(
            "{}",
            json!({"event": name, "httpStatus": status, "ms": now_ms() - started})
        ),
        Err(error) => console_error!(
            "{}",
            json!({"event": name, "httpStatus": 500, "ms": now_ms() - started, "errorType": error})
        ),
    }
}
