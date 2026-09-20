//! Forecast Network edge Worker. Native read paths run here on wasm; every other request is
//! passed untouched to the Python Worker through the `LEGACY` service binding (strangler by
//! route). Response shapes follow `apps/web/src/entry.py`.

use serde_json::{json, Value};
use worker::*;

pub mod ai;
pub mod article;
mod auth;
mod db;
mod detail;
mod discovery;
mod eligibility;
mod forecasts;
pub mod html;
pub mod html_entities;
pub mod html_parse;
mod markets;
mod mutate;
pub mod participation_holds;
mod points;
mod projections;
mod reads;
mod registry;
mod reputation;
pub mod resolution_timing;
pub mod risk_ops;
mod routes;
pub mod source_watch;
pub mod sources;
mod writes;

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
    let headers = Headers::new();
    headers.set("Content-Type", "application/json")?;
    headers.set("Cache-Control", "no-store")?;
    headers.set("X-Content-Type-Options", "nosniff")?;
    headers.set("Referrer-Policy", "strict-origin-when-cross-origin")?;
    headers.set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")?;
    let body = json!({ (if error { "error" } else { "data" }): data });
    Ok(Response::from_bytes(serde_json::to_vec(&body)?)?
        .with_status(status)
        .with_headers(headers))
}

pub fn api_error(status: u16, code: &str, message: &str) -> Result<Response> {
    api_response(json!({"code": code, "message": message}), status, true)
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
    let native_read = method == Method::Get && routes::owns(path);
    let native_write = routes::owns_write(&method, path);
    if !native_read && !native_write {
        return env.service("LEGACY")?.fetch_request(req).await;
    }
    let mut req = req;
    let mut body: Option<serde_json::Map<String, Value>> = None;
    if native_write {
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
        if bytes.len() > MAX_BODY_BYTES {
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
    let outcome = match &body {
        Some(body) => {
            let fingerprint = auth::fingerprint(&env, &req)?;
            match writes::rate_limit(&session, context.now_ms, &format!("http:{fingerprint}"), 180, 3_600_000).await {
                Ok(()) => match auth::user_id(&env, &session, &req, context.now_ms).await {
                    Ok(user) => routes::dispatch_write(&context, &method, path, user.as_deref(), body).await,
                    Err(error) => Err(routes::RouteError::Worker(error)),
                },
                Err(error) => Err(error),
            }
        }
        None => routes::dispatch(&context, path, &req, &url).await,
    };
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
