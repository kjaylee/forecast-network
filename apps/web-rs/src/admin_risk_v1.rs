//! The frozen v1 risk registry's operator routes: the artifact admission, the binding gate and the
//! feed publication that the v2 registry was built beside rather than on top of.
//!
//! Its refusals answer exactly as the v2 ones do, for the same reason: `require(...)` raises a
//! `ValidationError`, which is not an `AppError`, so the entry's `except Exception` turns it into
//! `service_unavailable`. The one refusal that keeps its own code is the missing relayer, and here
//! the entry raises it *in the route* rather than inside the publish — so a v1 publication checks
//! for its signer before it reads anything, where a v2 one cannot.
//!
//! `refresh_bound_prediction` — the v1 refresh, which is the one v1 route needing the chain — lives
//! in `admin_risk` beside the v2 refresh it shares a seam with.

use serde_json::{json, Value};
use worker::*;

use crate::admin::{exact, refused};
use crate::api_response;
use crate::db::Database;
use crate::risk_feed::{approve_binding, publish_feed, revoke_binding};
use crate::routes::{Context, RouteError};

/// The operator's recorded name, spelled the same as the v2 module's.
const ACTOR: &str = crate::admin_risk::ACTOR;

/// `FEED_WINDOW_MS`: six hours, the reference's own argument to `publish_feed`.
const FEED_WINDOW_MS: i64 = 21_600_000;

/// `MAX_WEIGHT_BYTES`: the reference's cap on a retained weight document.
const MAX_WEIGHT_BYTES: usize = 65_536;

/// The version string a weight document must name. The consumer validates its own record; this only
/// says which shape this Worker agreed to retain.
const WEIGHT_VERSION: &str = "source-calibration-v1";

/// `POST /api/admin/risk/seed`.
pub async fn seed_route(context: &Context<'_>, body: &serde_json::Map<String, Value>) -> Result<Response, RouteError> {
    exact(body, &["question"])?;
    let Some(question) = body["question"].as_str() else {
        return Err(refused());
    };
    // Rare tail events are useful risk questions even below the ordinary editorial uncertainty band.
    // Compiler, validation, publication and later explicit binding approval still apply.
    let result = crate::operator_routes::seed(context, question, "Forecast Editorial", None, true).await?;
    Ok(api_response(result, 201, false)?)
}

/// `POST /api/admin/risk/weights`: admitted immutable artifact admission.
///
/// Not a claim of calibrated performance. The bytes are stored under their own commitment and then
/// read back and compared, so an insert that silently changed the body could not be reported as a
/// success — which is the whole reason this route returns a hash at all.
pub async fn retain_weights_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["document"])?;
    let Some(document) = body.get("document").filter(|value| value.is_object()) else {
        return Err(refused());
    };
    if document.get("version").and_then(Value::as_str) != Some(WEIGHT_VERSION) {
        return Err(refused());
    }
    let digest = forecast_domain::content_hash(document).map_err(|_| refused())?;
    let canonical =
        String::from_utf8(forecast_domain::canonical_bytes(document).map_err(|_| refused())?).unwrap_or_default();
    if canonical.len() > MAX_WEIGHT_BYTES {
        return Err(refused());
    }
    let db = crate::db::D1(context.session);
    db.execute(
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
        &[
            json!(digest),
            json!("risk-weight-set"),
            json!(canonical),
            json!("application/json"),
            json!(context.now_ms),
        ],
    )
    .await?;
    let retained = db
        .first("SELECT kind,body FROM artifacts WHERE hash=?", &[json!(digest)])
        .await?;
    let stored = retained.as_ref().is_some_and(|row| {
        crate::db::text(row, "kind") == Some("risk-weight-set")
            && crate::db::text(row, "body") == Some(canonical.as_str())
    });
    if !stored {
        return Err(refused());
    }
    Ok(api_response(
        json!({"status": "stored", "hash": digest, "version": document["version"]}),
        201,
        false,
    )?)
}

/// `POST /api/admin/risk/bindings`.
pub async fn approve_binding_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["feedId", "binding"])?;
    let binding: forecast_domain::risk_feed::RiskFeedBinding =
        serde_json::from_value(body["binding"].clone()).map_err(|_| refused())?;
    let binding_id = binding.binding_id.clone();
    approve_binding(
        &crate::db::D1(context.session),
        body["feedId"].as_str().unwrap_or(""),
        &binding,
        ACTOR,
        context.now_ms,
    )
    .await
    .map_err(|_| refused())?;
    Ok(api_response(
        json!({"status": "approved", "bindingId": binding_id}),
        201,
        false,
    )?)
}

/// `POST /api/admin/risk/bindings/{id}/revoke`.
pub async fn revoke_binding_route(
    context: &Context<'_>,
    binding_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["reason"])?;
    let Some(reason) = body["reason"].as_str() else {
        return Err(refused());
    };
    revoke_binding(
        &crate::db::D1(context.session),
        binding_id,
        ACTOR,
        context.now_ms,
        reason,
    )
    .await
    .map_err(|_| refused())?;
    Ok(api_response(
        json!({"status": "revoked", "bindingId": binding_id}),
        200,
        false,
    )?)
}

/// `POST /api/admin/risk/feeds/{id}/publish`.
///
/// The weight set must be one this application admitted: the retained body is read back and both
/// its commitment and its named version are checked, so a caller cannot cite a version the stored
/// bytes do not carry.
pub async fn publish_feed_route(
    context: &Context<'_>,
    feed_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["weightSetHash", "weightSetVersion"])?;
    let hash = body["weightSetHash"].as_str().unwrap_or("");
    let version = body["weightSetVersion"].as_str().unwrap_or("");
    let db = crate::db::D1(context.session);
    let row = db
        .first(
            "SELECT body FROM artifacts WHERE hash=? AND kind='risk-weight-set'",
            &[json!(hash)],
        )
        .await?;
    let admitted = row
        .as_ref()
        .and_then(|row| crate::db::text(row, "body"))
        .and_then(|body| serde_json::from_str::<Value>(body).ok())
        .filter(|value| {
            forecast_domain::content_hash(value).is_ok_and(|digest| digest == hash)
                && value.get("version").and_then(Value::as_str) == Some(version)
        });
    if admitted.is_none() {
        return Err(refused());
    }
    // The signer is checked here rather than inside the publish, which is the reference's shape: a
    // publication with no relayer is refused before the feed is read, not after.
    let Some(public_key) = crate::application::relayer_public_key(context.env) else {
        return Err(RouteError::Failed(
            503,
            "risk_signer_unavailable",
            "Risk feed signing is not configured.",
        ));
    };
    let identity = crate::application::Identity::of(&public_key);
    let signer = crate::application::relayer_signer(context.env).ok_or(RouteError::Failed(
        503,
        "risk_signer_unavailable",
        "Risk feed signing is not configured.",
    ))?;
    // The v1 publish takes a `'static` future, so the adapter owns the signer rather than borrowing
    // it, and the signature widens from the fixed 64 bytes to a slice.
    let adapted = move |message: Vec<u8>| -> crate::wallets::BoxFuture<Result<Vec<u8>, ()>> {
        let signature = signer(message);
        Box::pin(async move { signature.await.map(|value| value.to_vec()) })
    };
    let envelope = publish_feed(
        &db,
        feed_id,
        crate::admin_risk::FEED_GENESIS,
        &identity.key_id,
        &identity.public_key_hex,
        &adapted,
        context.now_ms,
        hash,
        version,
        FEED_WINDOW_MS,
    )
    .await
    .map_err(|_| refused())?;
    Ok(api_response(
        json!({"status": "published", "envelope": serde_json::to_value(&envelope).map_err(|error| RouteError::Worker(error.into()))?}),
        201,
        false,
    )?)
}
