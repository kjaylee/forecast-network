//! The point market's trade surface: a receipt, a quote, and the fill that a quote is confirmed by.
//!
//! Four things here are entry-level rather than service-level, and each is load-bearing:
//!
//!   * **A quote is rate-limited before the account is even considered.** Sixty a minute per
//!     client, because a quote is the cheap half of a trade and the expensive half is the fill.
//!   * **A signed-out quote is a preview.** It binds nothing, and it is the only path here that a
//!     caller without a session may reach — with one exception: a body that carries an
//!     `expectedUserId` while the session is gone is a *changed account*, not a preview, and it is
//!     refused as one. Filling needs a session outright.
//!   * **The fill's minimum is a string on the wire.** `minClaimsAtomic` arrives as up to sixteen
//!     digits of text and is parsed here, so a float that JavaScript would round never becomes a
//!     payout bound. A precise minimum is the caller saying what they will accept; a rounded one
//!     would be this Worker deciding it for them.
//!   * **Every trade path re-states the account it expects.** The quote and the fill both carry
//!     the revision check, so a session that changed mid-trade cannot spend the new account's
//!     points on the old account's intent.

use serde_json::Value;
use worker::*;

use crate::api_response;
use crate::point_markets::PointMarkets;
use crate::routes::{Context, RouteError, RouteError as Error};

/// `MARKET_QUOTE_LIMIT`: sixty quotes per client per minute.
const QUOTE_LIMIT: i64 = 60;
const QUOTE_WINDOW_MS: i64 = 60_000;

/// `GET /api/forecasts/{id}/market/receipt`.
pub async fn receipt_route(
    context: &Context<'_>,
    url: &Url,
    user_id: Option<&str>,
    forecast_id: &str,
) -> Result<Response, RouteError> {
    let Some(user_id) = user_id else {
        return Err(Error::Unauthorized(
            "authentication_required",
            "Sign in to check your market receipt.",
        ));
    };
    // `parse_qs` without `keep_blank_values`: a blank parameter is an absent one, and the reference's
    // own `[""]` default is what the service then refuses.
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let receipt = markets(&db, &clock, &token, context.env)
        .receipt_status(
            user_id,
            forecast_id,
            crate::routes::query_value(url, "quoteId").as_deref().unwrap_or(""),
            crate::routes::query_value(url, "idempotencyKey")
                .as_deref()
                .unwrap_or(""),
        )
        .await?;
    Ok(api_response(receipt, 200, false)?)
}

/// `POST /api/forecasts/{id}/market/quote`.
pub async fn quote_route(
    context: &Context<'_>,
    req: &Request,
    user_id: Option<&str>,
    forecast_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    let fingerprint = crate::auth::fingerprint(context.env, req)?;
    crate::writes::rate_limit(
        context.session,
        context.now_ms,
        &format!("market-quote:{fingerprint}"),
        QUOTE_LIMIT,
        QUOTE_WINDOW_MS,
    )
    .await?;
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let side = body.get("side").and_then(Value::as_str).unwrap_or("");
    // An absent amount is refused by the same bounds check a wrong one is: the service says
    // `type(spend) is not int` and this says the range, and both are the same refusal.
    let spend = body.get("spendPoints").and_then(Value::as_i64).unwrap_or(0);
    let Some(user_id) = user_id else {
        if body.contains_key("expectedUserId") {
            return Err(Error::Failed(
                409,
                "account_changed",
                "Your signed-in account changed. Reload before requesting a quote.",
            ));
        }
        return Ok(api_response(
            markets(&db, &clock, &token, context.env)
                .preview(forecast_id, side, spend)
                .await?,
            200,
            false,
        )?);
    };
    crate::writes::require_expected_user(body, user_id)?;
    let quote = markets(&db, &clock, &token, context.env)
        .quote(user_id, forecast_id, side, spend)
        .await?;
    Ok(api_response(quote, 200, false)?)
}

/// `POST /api/forecasts/{id}/market/fill`.
pub async fn fill_route(
    context: &Context<'_>,
    user_id: Option<&str>,
    forecast_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    let Some(user_id) = user_id else {
        return Err(Error::Unauthorized(
            "authentication_required",
            "Sign in before confirming a market quote.",
        ));
    };
    crate::writes::require_expected_user(body, user_id)?;
    let Some(minimum) = body
        .get("minClaimsAtomic")
        .and_then(Value::as_str)
        .filter(|value| is_atomic(value))
    else {
        return Err(Error::Failed(
            400,
            "market_invalid_request",
            "A precise minimum payout is required.",
        ));
    };
    let minimum = minimum.parse::<i64>().unwrap_or(0);
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let token = || crate::mutate::random_token();
    let accepted = markets(&db, &clock, &token, context.env)
        .accept(
            user_id,
            forecast_id,
            body.get("quoteId").and_then(Value::as_str).unwrap_or(""),
            minimum,
            body.get("idempotencyKey").and_then(Value::as_str).unwrap_or(""),
        )
        .await?;
    Ok(api_response(accepted, 200, false)?)
}

/// `[0-9]{1,16}`: what the reference accepts as a minimum, and nothing wider.
///
/// Sixteen digits is the reference's own bound, and it is a bound rather than a coincidence: the
/// value is parsed into atomic units, and a longer run of digits is a number this application never
/// produces rather than one it should round.
fn is_atomic(value: &str) -> bool {
    !value.is_empty() && value.len() <= 16 && value.bytes().all(|byte| byte.is_ascii_digit())
}

/// The point markets, over collaborators the *caller's* frame owns.
///
/// Nothing here is leaked or owned: a `D1` borrows a session that lives in the request, and a
/// closure that outlived the request would be a per-request allocation the isolate never frees.
fn markets<'a>(
    db: &'a dyn crate::db::Database,
    clock: &'a dyn Fn() -> i64,
    token: &'a dyn Fn() -> String,
    env: &Env,
) -> PointMarkets<'a> {
    PointMarkets {
        db,
        clock,
        token,
        // A live market needs the market switch *and* the source watcher: a market priced off a
        // stale source is a market priced off nothing.
        live_enabled: crate::admin::flag(env, "LIVE_MARKETS_ENABLED")
            && crate::admin::flag(env, "SOURCE_WATCH_ENABLED"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The reference's pattern: one to sixteen digits and nothing else. A leading zero is *not*
    /// among the refusals — `007` is digits, and it parses to the seven it names.
    #[test]
    fn a_minimum_is_one_to_sixteen_digits() {
        assert!(is_atomic("0"));
        assert!(is_atomic("007"));
        assert!(is_atomic("9999999999999999"));
        assert!(!is_atomic(""));
        assert!(!is_atomic("99999999999999999"));
        assert!(!is_atomic("-1"));
        assert!(!is_atomic("1.0"));
        assert!(!is_atomic(" 1"));
    }
}
