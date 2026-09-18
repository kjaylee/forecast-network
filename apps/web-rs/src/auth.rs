//! Session cookie → user id, the same statement `Auth.authenticate` runs.

use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use worker::*;

pub const SESSION_COOKIE: &str = "__Host-forecast_session";
pub const AUTH_CONTEXT_COOKIE: &str = "__Host-forecast_auth";

pub fn cookie(req: &Request, name: &str) -> Option<String> {
    let raw = req.headers().get("cookie").ok().flatten()?;
    if raw.len() > 8192 {
        return None;
    }
    raw.split(';').map(str::trim).find_map(|pair| {
        let (key, value) = pair.split_once('=')?;
        (key == name).then(|| value.trim_matches('"').to_string())
    })
}

pub fn token_hash(secret: &str, token: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac key");
    mac.update(token.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

pub async fn user_id(env: &Env, session: &D1DatabaseSession, req: &Request, now_ms: i64) -> Result<Option<String>> {
    let Some(token) = cookie(req, SESSION_COOKIE) else {
        return Ok(None);
    };
    let context = cookie(req, AUTH_CONTEXT_COOKIE);
    if token.len() > 256 || context.as_ref().is_some_and(|c| c.len() > 256) {
        return Ok(None);
    }
    let secret = env.secret("SESSION_SECRET")?.to_string();
    if secret.len() < 32 {
        return Err("configuration_unavailable".into());
    }
    let context_hash = context.map(|c| token_hash(&secret, &format!("wallet-context:{c}")));
    let statement = session
        .prepare(
            "SELECT u.id FROM sessions s JOIN users u ON u.id=s.user_id \
             LEFT JOIN wallet_login_contexts c ON c.token_hash=s.context_hash \
             WHERE s.token_hash=? AND s.expires_at>? AND ((s.context_hash IS NULL AND NOT EXISTS \
             (SELECT 1 FROM wallet_identities i WHERE i.user_id=u.id AND i.converted_at IS NOT NULL)) \
             OR (s.context_hash=? AND c.epoch=s.context_epoch AND c.revoked_at IS NULL AND c.expires_at>? \
             AND c.active_session_hash=s.token_hash))",
        )
        .bind(&[
            token_hash(&secret, &format!("session:{token}")).into(),
            wasm_bindgen::JsValue::from_f64(now_ms as f64),
            context_hash.map_or(wasm_bindgen::JsValue::NULL, |h| h.into()),
            wasm_bindgen::JsValue::from_f64(now_ms as f64),
        ])?;
    let row: Option<Value> = statement.first(None).await?;
    Ok(row
        .and_then(|r| r["id"].as_str().map(str::to_string))
        .filter(|id| json!(id) != Value::Null))
}

/// HMAC(SESSION_SECRET, client IP) as the anonymous rate-limit identity.
pub fn fingerprint(env: &Env, req: &Request) -> Result<String> {
    let secret = env.secret("SESSION_SECRET")?.to_string();
    let ip = req
        .headers()
        .get("CF-Connecting-IP")?
        .unwrap_or_else(|| "local".to_string());
    Ok(token_hash(&secret, &ip))
}
