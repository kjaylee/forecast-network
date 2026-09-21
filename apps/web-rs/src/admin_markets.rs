//! The operator's market surface: the treasury, the grant, and the market that a forecast opens.
//!
//! Three routes, and two of them share a subtlety worth stating once. The reference reads its
//! arguments with `body.get(field, default)` and `body.get(field)`, which are not the same call:
//! the first substitutes the default only when the key is *absent*, while a key that is present and
//! null is passed through as `None` and refused by the service's own validator. `defaulted` below
//! is the first, `text` the second, and the difference is visible in which refusal a caller gets.
//!
//! The grants themselves are explicit and administrative: nothing here funds a treasury
//! automatically, and the amount is bounded before any statement runs.

use serde_json::Value;
use worker::*;

use crate::admin::flag;
use crate::api_response;
use crate::db::Database;
use crate::point_markets::PointMarkets;
use crate::routes::{Context, RouteError};

/// `GET /api/admin/markets/treasury`.
pub async fn budget_route(context: &Context<'_>, url: &Url) -> Result<Response, RouteError> {
    // `parse_qs` without `keep_blank_values`, then `[0]`: a blank mode is not a mode, and a
    // repeated one takes its first value.
    let mode = url
        .query_pairs()
        .find(|(name, value)| name == "mode" && !value.is_empty())
        .map(|(_, value)| value.into_owned())
        .unwrap_or_else(|| "shadow".to_string());
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let markets = markets(&db, &clock, &token, context.env);
    Ok(api_response(markets.budget(&mode).await?, 200, false)?)
}

/// `POST /api/admin/markets/treasury`.
pub async fn fund_treasury_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let markets = markets(&db, &clock, &token, context.env);
    // An absent amount is refused by the range check below with the same error the reference's own
    // `type(amount) is not int` produces, so the two cases need no separate branch.
    let amount = body.get("amountPoints").and_then(Value::as_i64).unwrap_or(0);
    let funded = markets
        .fund_treasury(amount, text(body, "idempotencyKey"), defaulted(body, "mode", "shadow"))
        .await?;
    Ok(api_response(funded, 200, false)?)
}

/// `POST /api/admin/forecasts/{id}/market`.
pub async fn create_market_route(
    context: &Context<'_>,
    forecast_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let markets = markets(&db, &clock, &token, context.env);
    // No policy is ever supplied here: the reference's route passes none, so the market opens on
    // the default one and a caller cannot widen the pilot's published limits.
    let created = markets
        .create(
            forecast_id,
            None,
            defaulted(body, "mode", "shadow"),
            text(body, "specificationHash"),
        )
        .await?;
    Ok(api_response(created, 200, false)?)
}

/// The point markets, over the request's own database and clock.
fn markets<'a>(
    db: &'a dyn Database,
    clock: &'a dyn Fn() -> i64,
    token: &'a dyn Fn() -> String,
    env: &Env,
) -> PointMarkets<'a> {
    PointMarkets {
        db,
        clock,
        token,
        // `live_markets_enabled and self.automation.enabled`: a live market needs the market switch
        // *and* the source watcher, because a market on a stale source is a market on nothing.
        live_enabled: flag(env, "LIVE_MARKETS_ENABLED") && flag(env, "SOURCE_WATCH_ENABLED"),
    }
}

/// `body.get(field, default)`: the default applies to an absent key, and a present one is passed
/// through as it is — `""` when it is not a string, which the validator then refuses.
fn defaulted<'a>(body: &'a serde_json::Map<String, Value>, field: &str, default: &'a str) -> &'a str {
    match body.get(field) {
        None => default,
        Some(value) => value.as_str().unwrap_or(""),
    }
}

/// `body.get(field)`, with no default at all.
fn text<'a>(body: &'a serde_json::Map<String, Value>, field: &str) -> &'a str {
    body.get(field).and_then(Value::as_str).unwrap_or("")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// The two readers, and the case that tells them apart: a key that is present and null.
    #[test]
    fn a_default_belongs_to_an_absent_key_and_not_to_a_null_one() {
        let body = json!({"mode": null, "specificationHash": null, "idempotencyKey": "sandbox:k"});
        let body = body.as_object().unwrap();
        assert_eq!(defaulted(body, "mode", "shadow"), "");
        assert_eq!(defaulted(body, "absent", "shadow"), "shadow");
        assert_eq!(text(body, "specificationHash"), "");
        assert_eq!(text(body, "idempotencyKey"), "sandbox:k");
        assert_eq!(text(body, "absent"), "");
    }
}
