//! The first slice of the risk producer port: choosing which bindings to publish.
//!
//! `forecast_application.risk_feed_v2` decides this, and the whole reason to move it
//! here is measured — 96% of a slow operation tick is spent outside the application,
//! in Pyodide startup, so no amount of Python optimisation helps.
//!
//! This is the part that can be ported and proven without an AI call: the queries, the
//! eligibility filter and the ordering. The ordering is where a port diverges silently —
//! a different tie order publishes a different envelope — so the SQL is asserted against
//! the Python literal and the rules are asserted against cases, rather than being
//! reviewed and hoped for.

use forecast_domain::risk_feed::{RiskFeedBindingV2, RiskMappingProfileV2};

/// Capacity guard, matching `operational_bindings_v2`.
pub const MAX_BINDINGS: usize = 64;

/// Bindings whose operational validity contains now; authorization-only bindings stay out.
/// Asserted equal to the Python literal in the tests.
pub const OPERATIONAL_SQL: &str = "SELECT binding_json FROM risk_feed_bindings_v2 b WHERE feed_id=? \
AND json_extract(binding_json,'$.operational_valid_from_ms')<=? \
AND json_extract(binding_json,'$.operational_valid_until_ms')>? \
AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id) \
ORDER BY binding_id LIMIT 65";

/// Bindings operational now or within half a forecast age, joined to their profile, whose
/// clock-backed estimate may be missing or aging. Candidates only; the clock decides.
pub const STALE_CANDIDATE_SQL: &str =
    "SELECT binding_json,profile_json FROM risk_feed_bindings_v2 b JOIN risk_feed_profiles_v2 p \
ON p.profile_hash=json_extract(b.binding_json,'$.mapping_profile_hash') WHERE b.feed_id=? \
AND json_extract(b.binding_json,'$.operational_valid_until_ms')>? \
AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id) \
ORDER BY json_extract(b.binding_json,'$.operational_valid_from_ms') LIMIT 64";

/// The retained clock artifact for a forecast, used to decide whether its estimate is aging.
pub const LATEST_CLOCK_SQL: &str = "SELECT json_extract(a.body,'$.forecast_as_of_ms') AS as_of FROM forecasts f \
JOIN risk_prediction_clocks_v2 c ON c.estimate_artifact_hash=json_extract(f.ai_forecast,'$.artifactHash') \
JOIN artifacts a ON a.hash=c.clock_artifact_hash WHERE f.id=?";

/// A half-life: how long an estimate may go before it is worth refreshing.
pub fn half_age(profile: &RiskMappingProfileV2) -> i64 {
    profile.max_forecast_age_ms / 2
}

/// Whether this binding is a candidate for a refresh at all, before the clock is read.
///
/// Two bindings are not: one whose operational window has not opened early enough for a
/// fresh estimate to be usable in it, and an exact-dated one whose target start has passed,
/// where a later estimate could never be as of the start.
pub fn worth_clock_check(binding: &RiskFeedBindingV2, profile: &RiskMappingProfileV2, now_ms: i64) -> bool {
    if binding.operational_valid_from_ms - half_age(profile) > now_ms {
        return false;
    }
    if binding.mapping_kind == "exact_dated" && now_ms > binding.target_start_ms {
        return false;
    }
    true
}

/// Whether the retained estimate is missing or has reached half its permitted age. A missing
/// clock or one that is not an integer is stale rather than fresh: absent evidence is not
/// evidence of freshness.
pub fn clock_is_stale(profile: &RiskMappingProfileV2, as_of_ms: Option<i64>, now_ms: i64) -> bool {
    match as_of_ms {
        None => true,
        Some(as_of) => now_ms - as_of >= half_age(profile),
    }
}

/// Deterministic per-channel order: newest target start first, then identity.
///
/// Publication takes the first episode with eligible estimates; probability values never
/// influence which episode is selected. A different order here publishes a different
/// envelope from the same database.
pub fn order_operational(bindings: &mut [RiskFeedBindingV2]) {
    bindings.sort_by(|a, b| {
        a.channel
            .cmp(&b.channel)
            .then(b.target_start_ms.cmp(&a.target_start_ms))
            .then(a.binding_id.cmp(&b.binding_id))
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    /// The port is only trustworthy while the SQL is the same SQL. Read from the Python
    /// source rather than copied, so a change there fails here instead of drifting silently.
    fn python_source() -> String {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../packages/application/src/forecast_application/risk_feed_v2.py");
        std::fs::read_to_string(path).expect("the Python module this is a port of")
    }

    /// Python writes these as adjacent string literals, so the source carries quote
    /// characters the statement does not, and the two languages wrap lines differently.
    /// Both are noise; the statement itself must be identical. None of these queries
    /// contains a double quote, so removing them cannot hide a real difference.
    fn normalize(sql: &str) -> String {
        sql.replace('"', " ").split_whitespace().collect::<Vec<_>>().join(" ")
    }

    fn binding_id(id: &str, channel: &str, target_start_ms: i64) -> RiskFeedBindingV2 {
        let mut binding: RiskFeedBindingV2 = serde_json::from_value(serde_json::json!({
            "schema_version": 1, "binding_id": id, "version": "canonical-risk-binding-v2",
            "forecast_id": "f_x", "specification_hash": "a".repeat(64), "channel": channel,
            "asset": "USDC", "category": "CRYPTO", "series_id": "s", "episode_id": "e",
            "target_start_ms": target_start_ms, "target_end_ms": target_start_ms + 86400000,
            "interval": "[start,end)", "policy_horizon_ms": 86400000,
            "definition_hash": "b".repeat(64), "mapping_profile_id": "p", "mapping_profile_version": "v1",
            "mapping_profile_hash": "c".repeat(64), "mapping_kind": "containing_upper_estimate",
            "question_event_definition_hash": "d".repeat(64), "approval_artifact_hash": "e".repeat(64),
            "authorization_valid_from_ms": 0, "authorization_valid_until_ms": 1,
            "operational_valid_from_ms": 0, "operational_valid_until_ms": 1,
        }))
        .expect("a canonical binding");
        binding.binding_id = id.to_string();
        binding
    }

    fn profile(max_forecast_age_ms: i64) -> RiskMappingProfileV2 {
        serde_json::from_value(serde_json::json!({
            "schema_version": 1, "profile_id": "p", "profile_version": "v1",
            "mapping_kind": "containing_upper_estimate", "predicate_class": "monotone",
            "max_forecast_age_ms": max_forecast_age_ms, "max_policy_lag_ms": 1,
            "source_freshness_max_ms": 1, "clock_skew_max_ms": 1, "review_reference": "r",
        }))
        .expect("a canonical profile")
    }

    #[test]
    fn the_queries_are_the_ones_the_python_worker_runs() {
        let python = normalize(&python_source());
        for (name, sql) in [
            ("OPERATIONAL_SQL", OPERATIONAL_SQL),
            ("STALE_CANDIDATE_SQL", STALE_CANDIDATE_SQL),
            ("LATEST_CLOCK_SQL", LATEST_CLOCK_SQL),
        ] {
            let stripped = normalize(sql);
            assert!(
                python.contains(&stripped),
                "{name} has drifted from the Python it is a port of:\n{stripped}"
            );
        }
    }

    #[test]
    fn a_binding_is_ordered_by_channel_then_newest_target_then_identity() {
        let mut bindings = vec![
            binding_id("b", "depegRisk1d", 100),
            binding_id("a", "depegRisk1d", 100),
            binding_id("c", "depegRisk1d", 300),
            binding_id("d", "btcCrashRisk", 200),
        ];
        order_operational(&mut bindings);
        let order: Vec<&str> = bindings.iter().map(|b| b.binding_id.as_str()).collect();
        assert_eq!(
            order,
            ["d", "c", "a", "b"],
            "btc first, then newest target, then identity"
        );
    }

    #[test]
    fn the_order_does_not_depend_on_the_input_order() {
        let mut forward = vec![
            binding_id("a", "x", 1),
            binding_id("b", "x", 2),
            binding_id("c", "y", 3),
        ];
        let mut backward = forward.clone();
        backward.reverse();
        order_operational(&mut forward);
        order_operational(&mut backward);
        assert_eq!(forward, backward, "the same rows must publish the same envelope");
    }

    #[test]
    fn a_window_that_has_not_opened_early_enough_is_not_a_candidate() {
        let profile = profile(1000);
        let mut binding = binding_id("b", "c", 0);
        binding.operational_valid_from_ms = 10_000;
        // Half an age before the window opens is the last moment a refresh is useful.
        assert!(
            !worth_clock_check(&binding, &profile, 9_000),
            "too early to be worth a refresh"
        );
        assert!(
            worth_clock_check(&binding, &profile, 9_500),
            "at the boundary it is worth checking"
        );
        assert!(worth_clock_check(&binding, &profile, 10_000));
    }

    #[test]
    fn an_exact_dated_binding_past_its_start_is_not_a_candidate() {
        let profile = profile(1000);
        let mut binding = binding_id("b", "c", 5_000);
        binding.mapping_kind = "exact_dated".to_string();
        assert!(
            worth_clock_check(&binding, &profile, 5_000),
            "at the start a refresh still helps"
        );
        assert!(
            !worth_clock_check(&binding, &profile, 5_001),
            "after the start no later estimate could be as of it"
        );
        binding.mapping_kind = "containing_upper_estimate".to_string();
        assert!(
            worth_clock_check(&binding, &profile, 5_001),
            "a containing window can still be refreshed after its start"
        );
    }

    #[test]
    fn a_missing_clock_is_stale_rather_than_fresh() {
        let profile = profile(1000);
        assert!(
            clock_is_stale(&profile, None, 1_000_000),
            "absent evidence is not evidence of freshness"
        );
        assert!(
            !clock_is_stale(&profile, Some(1_000_000), 1_000_499),
            "inside half an age it is fresh"
        );
        assert!(
            clock_is_stale(&profile, Some(1_000_000), 1_000_500),
            "at half an age it needs refreshing"
        );
        assert!(clock_is_stale(&profile, Some(1_000_000), 1_001_000));
    }
}
