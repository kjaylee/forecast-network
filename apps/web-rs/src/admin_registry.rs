//! The devnet registry's operator surface: whether it can sign and reach the chain, and one
//! bounded pass of its delivery loop.
//!
//! The self-test signs a **fixed** message. That is the whole point of it: an endpoint that signed
//! whatever it was handed would be a signing oracle for whoever holds the operator token.
//!
//! Both routes answer `registry_disabled` when the registry is not switched on — and that check is
//! the *switch*, not the whole configuration. A registry that is switched on but cannot be built is
//! a service fault, not a disabled feature, and the two are told apart here because the reference
//! builds its registry at application construction and would have failed the whole request.

use serde_json::{json, Value};
use worker::*;

use crate::admin::{flag, refused};
use crate::api_response;
use crate::registry_chain::SolanaRegistry;
use crate::routes::{Context, RouteError};

/// `POST /api/admin/registry/run`'s bound: three deliveries per pass, the reference's own.
const SYNC_LIMIT: i64 = 3;

/// `GET /api/admin/registry/health`.
pub async fn health_route(context: &Context<'_>) -> Result<Response, RouteError> {
    require_enabled(context.env)?;
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let parts = crate::application::chain_parts(context.env, &db, &clock).ok_or_else(refused)?;
    let (program, relayer) = addresses(context.env);
    let transport = parts.transport(&program, &relayer).map_err(|_| refused())?;
    // `forecast-network:registry-key-self-test:v1`, and nothing a caller supplies.
    (parts.sign)(b"forecast-network:registry-key-self-test:v1".to_vec())
        .await
        .map_err(|_| refused())?;
    // A refused RPC read is reported rather than raised: the signer having been verified is the
    // other half of the answer, and an operator wants both.
    let rpc_available = transport.genesis_hash().await.is_ok();
    Ok(api_response(
        json!({"signerVerified": true, "rpcAvailable": rpc_available}),
        200,
        false,
    )?)
}

/// `POST /api/admin/registry/run`: enable one forecast if asked, then run one bounded pass.
///
/// A chain fault is a *result* here rather than an error: the pass returns `retry_pending` with the
/// transport's own description, which is what a scheduler reads to decide when to come back. That
/// description is the transport's because the reference stores `str(error)`, and the transport in
/// this port carries that message in its code.
pub async fn run_route(context: &Context<'_>, body: &serde_json::Map<String, Value>) -> Result<Response, RouteError> {
    require_enabled(context.env)?;
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let parts = crate::application::chain_parts(context.env, &db, &clock).ok_or_else(refused)?;
    let (program, relayer) = addresses(context.env);
    let transport = parts.transport(&program, &relayer).map_err(|_| refused())?;
    let registry = SolanaRegistry::new(&db, &transport, &program, &relayer, &clock, &token).map_err(|_| refused())?;
    match body.get("forecastId") {
        None => {}
        Some(value) => {
            // Present and not an identifier is refused; absent is not. The reference's own
            // `if forecast_id is not None` is what makes that distinction, and an `unwrap_or("")`
            // would lose it by turning the absent case into an invalid one.
            let Some(forecast_id) = value.as_str().filter(|id| is_forecast_id(id)) else {
                return Err(refused());
            };
            registry.enable(forecast_id).await.map_err(|_| refused())?;
        }
    }
    match registry.sync(SYNC_LIMIT).await {
        Ok(result) => Ok(api_response(result, 200, false)?),
        Err(error) => Ok(api_response(
            json!({"status": "retry_pending", "reason": error.code}),
            503,
            false,
        )?),
    }
}

/// `[A-Za-z0-9_.:-]{1,128}`, the reference's forecast identity.
fn is_forecast_id(value: &str) -> bool {
    (1..=128).contains(&value.len())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
}

/// The switch, before anything is built.
fn require_enabled(env: &Env) -> Result<(), RouteError> {
    if flag(env, "SOLANA_REGISTRY_ENABLED") {
        return Ok(());
    }
    Err(RouteError::Failed(
        503,
        "registry_disabled",
        "Devnet registry is not enabled.",
    ))
}

/// The program and relayer, as the transport takes them.
fn addresses(env: &Env) -> (Vec<u8>, Vec<u8>) {
    let decode = |name: &str| {
        bs58::decode(env.var(name).map(|value| value.to_string()).unwrap_or_default())
            .into_vec()
            .unwrap_or_default()
    };
    (decode("SOLANA_PROGRAM_ID"), decode("SOLANA_RELAYER"))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The identity the reference's pattern describes, and the two things it refuses: an empty one
    /// and a character outside the set.
    #[test]
    fn a_forecast_identity_is_the_reference_pattern() {
        assert!(is_forecast_id("f_1"));
        assert!(is_forecast_id("f.1:2-3"));
        assert!(is_forecast_id(&"a".repeat(128)));
        assert!(!is_forecast_id(""));
        assert!(!is_forecast_id(&"a".repeat(129)));
        assert!(!is_forecast_id("f/1"));
        assert!(!is_forecast_id("f 1"));
    }
}
