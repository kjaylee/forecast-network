//! The operator boundary: who may reach `/api/admin/`, and how much they may send.
//!
//! Two credentials, and the difference between them is the point. The **admin** token reaches
//! every administrative route. The **scheduler** token reaches exactly two — the operate tick and
//! the sweep — because a borrowed cron service must be able to trigger work without ever holding
//! the operator's secret. Three properties are load-bearing:
//!
//!   * Both are compared in constant time, and both must be at least 32 characters. A short token
//!     is a token that was never meant to be one.
//!   * An admin request is exempt from the *origin* check that guards ordinary writes, because it
//!     does not come from a browser. That exemption is what the bearer check buys, so it is stated
//!     where the check is rather than left to be inferred from the route table.
//!   * An admin request may send a much larger body — a prepared adjudication carries retained
//!     evidence — and the cap is raised here rather than by widening the general one.

use worker::*;

use crate::routes::RouteError;

/// A credential shorter than this is not a credential. The reference's own floor.
pub const MIN_TOKEN_LENGTH: usize = 32;
pub const MAX_BODY_BYTES: usize = 16 * 1024;
/// `MAX_PROVIDER_BYTES`. What an operator may send, because evidence is large and a verdict is not.
pub const MAX_ADMIN_BODY_BYTES: usize = 512 * 1024;

/// `hmac.compare_digest`, for two byte strings.
///
/// The length is not the secret — the caller's is — so comparing it first is not a leak. What must
/// not leak is *where* two equal-length strings first differ, which is what the accumulating XOR is
/// for: every byte is examined whatever the earlier ones said.
fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0u8;
    for (a, b) in left.iter().zip(right) {
        difference |= a ^ b;
    }
    difference == 0
}

fn secret(env: &Env, name: &str) -> Option<String> {
    let value = env.secret(name).ok().map(|value| value.to_string())?;
    (value.len() >= MIN_TOKEN_LENGTH).then_some(value)
}

/// `is_admin` and the authorization that follows it.
///
/// `scheduler_allowed` is whether this path is one of the two a scheduler credential may reach. It
/// is a parameter rather than a check here, because the set belongs with the route table.
pub fn authorized(env: &Env, req: &Request, scheduler_allowed: bool) -> bool {
    let Ok(Some(supplied)) = req.headers().get("authorization") else {
        return false;
    };
    let supplied = supplied.as_bytes();
    if let Some(admin) = secret(env, "ADMIN_TOKEN") {
        if constant_time_eq(supplied, format!("Bearer {admin}").as_bytes()) {
            return true;
        }
    }
    // Unset means this branch cannot authorize anyone: a missing scheduler token is not an
    // invitation, and a short one is not a token.
    if scheduler_allowed {
        if let Some(scheduler) = secret(env, "SCHEDULER_TOKEN") {
            return constant_time_eq(supplied, format!("Bearer {scheduler}").as_bytes());
        }
    }
    false
}

/// The two paths a scheduler credential may reach, and no others.
pub fn scheduler_may_trigger(path: &str) -> bool {
    path == "/api/admin/risk/v2/operate" || path == "/api/admin/sweep"
}

/// What the entry answers a raised `ValueError` with.
///
/// An operator route that refuses its input does so by raising, and a `ValueError` is not an
/// `AppError` — so it falls through the entry's `except Exception` and becomes this. Every operator
/// route here refuses this way, which is why the shape is one function rather than a literal in
/// each handler.
pub fn refused() -> RouteError {
    crate::routes::RouteError::Failed(503, "service_unavailable", "Please try again shortly.")
}

/// The exact key set a route accepts.
///
/// `set(body) != {...}` in the reference, and the *set* is the check: a body with one right key
/// missing and a wrong one added has the same length and is still refused.
pub fn exact(body: &serde_json::Map<String, serde_json::Value>, keys: &[&str]) -> Result<(), RouteError> {
    let present: Vec<&str> = body.keys().map(String::as_str).collect();
    if present.len() != keys.len() || !keys.iter().all(|key| present.contains(key)) {
        return Err(refused());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The comparison itself, which is the part a route table cannot get wrong on its own.
    #[test]
    fn only_an_exact_match_is_a_match() {
        assert!(constant_time_eq(b"Bearer abc", b"Bearer abc"));
        assert!(!constant_time_eq(b"Bearer abc", b"Bearer abd"));
        assert!(
            !constant_time_eq(b"Bearer abc", b"Bearer abcd"),
            "a prefix is not a match"
        );
        assert!(!constant_time_eq(b"Bearer abcd", b"Bearer abc"), "nor is the reverse");
        assert!(!constant_time_eq(b"", b"x"));
        assert!(constant_time_eq(b"", b""));
    }

    /// The scheduler credential reaches the tick and the sweep, and nothing else. A route table
    /// that widened this would hand a borrowed cron service the operator's reach.
    #[test]
    fn a_scheduler_credential_reaches_exactly_two_paths() {
        assert!(scheduler_may_trigger("/api/admin/sweep"));
        assert!(scheduler_may_trigger("/api/admin/risk/v2/operate"));
        assert!(!scheduler_may_trigger("/api/admin/forecasts/f_1/adjudicate"));
        assert!(!scheduler_may_trigger("/api/admin/automation/run"));
        assert!(!scheduler_may_trigger("/api/admin/risk/v2/health"));
    }
}
