//! The operator's remaining reads and writes: the product analytics window, the participation hold,
//! the sandbox billing ledger, and the AI provider self-test.
//!
//! Four things in here are easy to get subtly wrong and are worth naming.
//!
//!   * **The analytics query is exact.** The reference refuses any parameter it does not know, any
//!     parameter repeated, and any value that is not one to sixteen digits. A port that read the
//!     four values it wanted and ignored the rest would answer a question the reference refused.
//!   * **A missing numeric argument is not a default.** The reference passes `body.get(field)`
//!     straight into the service, so an absent field and a field of the wrong type both arrive as
//!     `None` and are refused by the service's own validator — with its own code. Only the three
//!     fields the *route* defaults (`priceCents`, `costCapCents`, `maxAttempts`) have a fallback,
//!     and it applies when the key is absent, not when it is null.
//!   * **The AI self-test is a status check.** A provider that answers 401 is not a provider that
//!     answers, and the reference raises rather than reporting `ok: false`.
//!   * **The billing switch is off unless it says otherwise.** The sandbox refuses every mutation
//!     with `billing_sandbox_disabled` when it is not switched on, which is what makes the ledger
//!     un-reachable on a deployment that never meant to have one.

use serde_json::{json, Value};
use worker::*;

use crate::admin::refused;
use crate::api_response;
use crate::billing::SandboxServiceBilling;
use crate::db::Database;
use crate::routes::{Context, RouteError};
use crate::wallet_login::truthy;

/// The four parameters the analytics window accepts, and no others.
const ANALYTICS_PARAMETERS: [&str; 4] = ["start", "end", "cohortStart", "cohortEnd"];

const DAY_MS: i64 = 86_400_000;

/// The four windows one analytics read is asked for, once the query has been checked.
#[derive(Debug, PartialEq, Eq)]
struct Windows {
    start: i64,
    end: i64,
    cohort_start: Option<i64>,
    cohort_end: Option<i64>,
}

/// The analytics query, checked and read.
///
/// Pure, so the three refusals can be held to the reference without a request: an unknown
/// parameter, a parameter given twice, and a value that is not one to sixteen digits.
fn windows(query: &[(String, String)], now_ms: i64) -> Result<Windows, ()> {
    if query
        .iter()
        .any(|(name, _)| !ANALYTICS_PARAMETERS.contains(&name.as_str()))
    {
        return Err(());
    }
    if query
        .iter()
        .any(|(name, _)| query.iter().filter(|(other, _)| other == name).count() != 1)
    {
        return Err(());
    }
    if query.iter().any(|(_, value)| !is_milliseconds(value)) {
        return Err(());
    }
    let read = |name: &str| {
        query
            .iter()
            .find(|(candidate, _)| candidate == name)
            .map(|(_, value)| value.parse::<i64>().unwrap_or(0))
    };
    let end = read("end").unwrap_or(now_ms / DAY_MS * DAY_MS);
    Ok(Windows {
        start: read("start").unwrap_or((end - 30 * DAY_MS).max(0)),
        end,
        cohort_start: read("cohortStart"),
        cohort_end: read("cohortEnd"),
    })
}

/// `GET /api/admin/analytics`.
pub async fn analytics_route(context: &Context<'_>, url: &Url) -> Result<Response, RouteError> {
    let query: Vec<(String, String)> = url
        .query_pairs()
        .map(|(name, value)| (name.into_owned(), value.into_owned()))
        .collect();
    let asked = windows(&query, context.now_ms).map_err(|_| refused())?;
    let analytics = crate::analytics::product_analytics(
        &crate::db::D1(context.session),
        context.now_ms,
        asked.start,
        asked.end,
        asked.cohort_start,
        asked.cohort_end,
        "application",
        &[],
    )
    .await
    .map_err(|_| refused())?;
    Ok(api_response(analytics, 200, false)?)
}

/// `[0-9]{1,16}`: what the reference's own pattern allows, and nothing wider.
fn is_milliseconds(value: &str) -> bool {
    !value.is_empty() && value.len() <= 16 && value.bytes().all(|byte| byte.is_ascii_digit())
}

/// `body.get(field)` as the reference passes it: absent is the default, present must be an integer.
///
/// A JSON float or a boolean is not an integer here for the same reason it is not one there —
/// `type(value) is not int` — and `as_i64` says so. The `None` that survives travels into the
/// service, which refuses it with the reference's own code.
fn optional(body: &serde_json::Map<String, Value>, field: &str, default: i64) -> Option<i64> {
    match body.get(field) {
        None => Some(default),
        Some(value) => value.as_i64(),
    }
}

/// `GET /api/admin/forecasts/{id}/participation`.
pub async fn participation_route(context: &Context<'_>, forecast_id: &str) -> Result<Response, RouteError> {
    let status = crate::participation_holds::status(&crate::db::D1(context.session), forecast_id).await?;
    Ok(api_response(status, 200, false)?)
}

/// `POST /api/admin/forecasts/{id}/participation`.
pub async fn change_participation_route(
    context: &Context<'_>,
    forecast_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    let change = crate::participation_holds::change(
        &crate::db::D1(context.session),
        &|| crate::mutate::random_token(),
        context.now_ms,
        forecast_id,
        body,
        None,
    )
    .await?;
    Ok(api_response(change, 200, false)?)
}

/// `GET /api/admin/billing/sandbox`.
pub async fn billing_route(context: &Context<'_>) -> Result<Response, RouteError> {
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let billing = sandbox(&db, &clock, context.env);
    Ok(api_response(billing.summary().await?, 200, false)?)
}

/// `POST /api/admin/billing/sandbox`: the sandbox accounting operations.
///
/// There is deliberately no branch that confirms a real payment. A replay of an unknown action is
/// the only thing left, and it is refused by name.
pub async fn billing_action_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    let db = crate::db::D1(context.session);
    let clock = || context.now_ms;
    let billing = sandbox(&db, &clock, context.env);
    let text = |field: &str| body.get(field).and_then(Value::as_str);
    let action = body.get("action").and_then(Value::as_str);
    let result = match action {
        Some("fund") => {
            billing
                .fund_capital(
                    body.get("amountCents").and_then(Value::as_i64),
                    text("idempotencyKey").unwrap_or(""),
                )
                .await?
        }
        Some("quote") => {
            billing
                .quote(
                    text("invoiceId").unwrap_or(""),
                    text("ownerHash").unwrap_or(""),
                    text("scopeHash").unwrap_or(""),
                    optional(body, "priceCents", 200),
                    optional(body, "costCapCents", 115),
                    optional(body, "maxAttempts", 3),
                    body.get("expiresAt").and_then(Value::as_i64),
                    body.get("refundUntil").and_then(Value::as_i64),
                    text("idempotencyKey").unwrap_or(""),
                )
                .await?
        }
        Some("sandbox_receipt") => {
            billing
                .accept_sandbox_receipt(
                    text("invoiceId").unwrap_or(""),
                    text("ownerHash").unwrap_or(""),
                    text("reference"),
                    body.get("amountCents").and_then(Value::as_i64),
                    text("scopeHash"),
                    text("idempotencyKey").unwrap_or(""),
                )
                .await?
        }
        Some("command") => {
            let payload = match body.get("payload") {
                Some(Value::Object(payload)) => payload.clone(),
                // `isinstance(body.get("payload", {}), dict)`: an absent payload is an empty one,
                // and anything else falls through to the refusal below rather than to `command`.
                None => serde_json::Map::new(),
                Some(_) => {
                    return Err(RouteError::Failed(
                        400,
                        "invalid_input",
                        "Choose a sandbox accounting operation.",
                    ))
                }
            };
            billing
                .command(
                    text("invoiceId").unwrap_or(""),
                    text("ownerHash").unwrap_or(""),
                    text("command").unwrap_or(""),
                    text("idempotencyKey").unwrap_or(""),
                    payload,
                )
                .await?
        }
        _ => {
            return Err(RouteError::Failed(
                400,
                "invalid_input",
                "Choose a sandbox accounting operation.",
            ))
        }
    };
    Ok(api_response(result, 200, false)?)
}

/// The sandbox ledger, switched on only where the environment says so.
///
/// The database and the clock are borrowed from the caller's frame: a `D1` borrows a session that
/// lives in the request, so a helper that built its own could not return this.
fn sandbox<'a>(db: &'a dyn Database, now_ms: &'a dyn Fn() -> i64, env: &Env) -> SandboxServiceBilling<'a> {
    SandboxServiceBilling {
        db,
        now_ms,
        enabled: var(env, "BILLING_SANDBOX_ENABLED").to_lowercase() == "true",
    }
}

/// `GET /api/admin/ai/health`: one fixed question to the configured provider.
///
/// The message is a constant, so this is a liveness probe rather than a signing oracle or a way to
/// spend the operator's AI budget on a chosen question.
pub async fn ai_health_route(context: &Context<'_>) -> Result<Response, RouteError> {
    let relayed = crate::application::gemini_relay(context.env).is_some();
    let url = format!(
        "https://{}/v1beta/models/{}:generateContent",
        crate::application::GEMINI_HOST,
        var(context.env, "GEMINI_MODEL")
    );
    let credential = if relayed {
        "relay".to_string()
    } else {
        context
            .env
            .secret("GEMINI_API_KEY")
            .map(|value| value.to_string())
            .unwrap_or_default()
    };
    let headers = vec![
        ("Content-Type".to_string(), "application/json".to_string()),
        ("x-goog-api-key".to_string(), credential),
    ];
    let body = json!({
        "contents": [{"parts": [{"text": "Reply with the single word OK."}]}],
        "generationConfig": {"maxOutputTokens": 8},
    });
    // A transport failure and an unsuccessful response are the same answer here — the reference
    // raises for both, and the entry turns either into `service_unavailable`.
    let probe = match crate::application::post_json(&url, &headers, &body).await {
        Ok(text) => serde_json::from_str::<Value>(&text).ok().filter(Value::is_object),
        Err(()) => None,
    };
    let Some(probe) = probe else {
        return Err(refused());
    };
    Ok(api_response(
        // `bool(probe.get("candidates"))`: an absent key and an empty list are both "no answer",
        // which is why the check is the language's truthiness rather than a null test.
        json!({"provider": "gemini", "ok": probe.get("candidates").is_some_and(truthy), "proxied": relayed}),
        200,
        false,
    )?)
}

fn var(env: &Env, name: &str) -> String {
    env.var(name).map(|value| value.to_string()).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn query(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(name, value)| (name.to_string(), value.to_string()))
            .collect()
    }

    /// A window the reference would have refused is refused here. Each case is one clause of its
    /// check: the unknown name, the repetition, and the value that is not digits.
    #[test]
    fn an_analytics_window_is_accepted_only_as_the_reference_accepts_one() {
        assert!(windows(&query(&[]), 0).is_ok());
        assert!(windows(
            &query(&[("start", "0"), ("end", "1"), ("cohortStart", "2"), ("cohortEnd", "3")]),
            0
        )
        .is_ok());
        // A sixteenth digit fits; a seventeenth is a value the pattern does not describe.
        assert!(windows(&query(&[("end", "9999999999999999")]), 0).is_ok());
        assert!(windows(&query(&[("end", "99999999999999999")]), 0).is_err());

        assert!(windows(&query(&[("since", "1")]), 0).is_err(), "an unknown parameter");
        assert!(
            windows(&query(&[("start", "1"), ("start", "2")]), 0).is_err(),
            "a repeated one"
        );
        assert!(windows(&query(&[("start", "")]), 0).is_err(), "a blank one");
        assert!(windows(&query(&[("start", "-1")]), 0).is_err(), "a signed one");
        assert!(windows(&query(&[("start", "1.0")]), 0).is_err(), "a fractional one");
    }

    /// The defaults are the reference's own, and they are computed from the *end*, so a window that
    /// names only an end still gets thirty days before it.
    #[test]
    fn the_default_window_is_the_last_thirty_days_from_the_end() {
        let now = 1_700_000_000_000i64;
        let day = now / DAY_MS * DAY_MS;
        assert_eq!(
            windows(&query(&[]), now).unwrap(),
            Windows {
                start: (day - 30 * DAY_MS).max(0),
                end: day,
                cohort_start: None,
                cohort_end: None,
            }
        );
        let asked = windows(&query(&[("end", "1000000000")]), now).unwrap();
        assert_eq!(asked.end, 1_000_000_000);
        // The floor applies to the *default* start: an end three decades before the epoch is a
        // window that starts at it. The reference's `max(0, ...)` is inside the default branch.
        assert_eq!(asked.start, 0);
        assert_eq!(windows(&query(&[("end", "0")]), now).unwrap().start, 0);
        // The cohort defaults are the analytics service's, not the route's: the route passes the
        // absence through, and the service substitutes the activity window.
        assert_eq!(
            windows(&query(&[("cohortStart", "5")]), now).unwrap().cohort_start,
            Some(5)
        );
    }

    /// `body.get(field)` semantics, which are where a default is and is not.
    #[test]
    fn a_missing_number_defaults_but_a_null_one_does_not() {
        let body = json!({"priceCents": null, "costCapCents": 7, "maxAttempts": 1.5, "extra": true});
        let body = body.as_object().unwrap();
        assert_eq!(optional(body, "priceCents", 200), None, "present and null");
        assert_eq!(optional(body, "costCapCents", 115), Some(7));
        assert_eq!(optional(body, "maxAttempts", 3), None, "present and not an integer");
        assert_eq!(optional(body, "extra", 3), None, "a boolean is not an integer");
        assert_eq!(optional(body, "absent", 3), Some(3), "absent takes the default");
    }
}
