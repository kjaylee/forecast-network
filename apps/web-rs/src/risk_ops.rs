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

/// Every channel the feed must account for; a missing mapping stays in the denominator.
pub const CHANNELS: [&str; 12] = [
    "depegRisk1d",
    "depegRisk7d",
    "depegRisk30d",
    "reserveLossRisk",
    "liquidityStressRisk",
    "stableCollateralRisk",
    "btcCrashRisk",
    "ethCrashRisk",
    "solCrashRisk",
    "oracleFailureRisk",
    "bridgeFailureRisk",
    "counterpartyRisk",
];

/// The admitted mapping profiles for a feed; the hash below is computed over exactly these.
pub const PROFILE_SET_SQL: &str =
    "SELECT profile_hash FROM risk_feed_profiles_v2 WHERE feed_id=? ORDER BY profile_hash";

/// Which channels have an admitted definition, as opposed to which are covered right now.
pub const DEFINED_CHANNELS_SQL: &str = "SELECT DISTINCT channel FROM risk_feed_definitions_v2 WHERE feed_id=?";

/// The hash of the profile set, which goes inside the signed payload.
///
/// Sorted here rather than trusted from the caller, so a row order that changed upstream
/// changes nothing: this value is signed, and a different one invalidates every consumer's
/// verification. The Python side gets the same order from `ORDER BY profile_hash`, and
/// SQLite's default text collation is byte-wise, which is what `sort` does.
pub fn profile_set_hash(feed_id: &str, profile_hashes: &[String]) -> Result<String, worker::Error> {
    let mut ordered: Vec<&String> = profile_hashes.iter().collect();
    ordered.sort();
    forecast_domain::canonical::content_hash(&serde_json::json!({
        "feed_id": feed_id,
        "profiles": ordered,
    }))
    .map_err(|error| worker::Error::RustError(error.to_string()))
}

/// The visible status of one channel: covered, unavailable with a reason, or unsupported.
///
/// A channel with no admitted definition is `unsupported` and says so rather than being
/// omitted, because a channel that disappears from the list cannot be noticed as missing.
pub fn coverage_status<'a>(
    channel: &str,
    covered: &'a std::collections::BTreeMap<String, String>,
    withheld: &'a std::collections::BTreeMap<String, String>,
    defined: &std::collections::BTreeSet<String>,
) -> (&'static str, Option<&'a str>, Option<&'a str>) {
    if let Some(binding_id) = covered.get(channel) {
        return ("covered", Some(binding_id.as_str()), None);
    }
    if defined.contains(channel) {
        let reason = withheld
            .get(channel)
            .map(String::as_str)
            .unwrap_or("no operational episode");
        return ("unavailable", None, Some(reason));
    }
    ("unsupported", None, Some("no admitted definition"))
}

/// Whether a computed signal belongs in the published payload.
///
/// Three ways it does not. It may predate the binding's authorization, so it was measured
/// before the feed was allowed to carry it. It may have completed evaluation after now,
/// which is a clock ordering the payload cannot represent. Or the binding may be
/// exact-dated and the estimate is newer than the target start, which means it estimates
/// something other than the window the question asked about.
///
/// These signals are signed, so a filter that admits one the Python side rejects produces
/// an envelope no consumer can verify against the same database.
pub fn signal_is_admissible(
    binding: &RiskFeedBindingV2,
    forecast_as_of_ms: i64,
    evaluation_completed_at_ms: i64,
    now_ms: i64,
) -> bool {
    if binding.authorization_valid_from_ms > forecast_as_of_ms {
        return false;
    }
    if evaluation_completed_at_ms > now_ms {
        return false;
    }
    if binding.mapping_kind == "exact_dated" && forecast_as_of_ms > binding.target_start_ms {
        return false;
    }
    true
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

    /// Generated by `forecast_application.risk_feed_v2.profile_set_hash`. This value is signed,
    /// so a different one makes every consumer's verification fail.
    #[test]
    fn the_profile_set_hash_matches_the_python_implementation() {
        let cases: Vec<(&str, Vec<String>, &str)> = vec![
            (
                "devnet-stable-risk-v2",
                vec![],
                "6bf9a59de3cabb41c6afe1d232a21c3c6b041add5bb33c8316223ab26ea19918",
            ),
            (
                "devnet-stable-risk-v2",
                vec!["a".repeat(64)],
                "d641bd8df8868a81a705318843ced0bab2708fe279735959107afa6a4236d786",
            ),
            (
                "f",
                vec!["b".repeat(64), "a".repeat(64)],
                "374993003c1d319e01abf04bb4368f68dec0fab86c825c84223d171f5b4b2e83",
            ),
            (
                "devnet-stable-risk-v2",
                vec!["0".repeat(64), "f".repeat(64), "1".repeat(64)],
                "2e2b5f8061c011cf52f82555b51afdbc846471741fdbd906e3ffe1d164ee0f0a",
            ),
        ];
        for (feed_id, profiles, expected) in cases {
            assert_eq!(
                profile_set_hash(feed_id, &profiles).expect("a hash"),
                expected,
                "profile_set_hash disagrees with Python for {feed_id} with {} profiles",
                profiles.len()
            );
        }
    }

    #[test]
    fn the_profile_set_hash_does_not_depend_on_the_order_the_rows_arrived_in() {
        let forward = vec!["a".repeat(64), "b".repeat(64), "c".repeat(64)];
        let mut backward = forward.clone();
        backward.reverse();
        assert_eq!(
            profile_set_hash("f", &forward).unwrap(),
            profile_set_hash("f", &backward).unwrap(),
            "the same profile set must sign the same way"
        );
    }

    fn maps(pairs: &[(&str, &str)]) -> std::collections::BTreeMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    fn defined(names: &[&str]) -> std::collections::BTreeSet<String> {
        names.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn a_covered_channel_names_its_binding_and_gives_no_reason() {
        let covered = maps(&[("depegRisk1d", "usdc-depeg-1d-w48-2026-09-17T12:00:00Z")]);
        let withheld = maps(&[("depegRisk1d", "should not be used")]);
        let admitted = defined(&["depegRisk1d"]);
        let (status, binding, reason) = coverage_status("depegRisk1d", &covered, &withheld, &admitted);
        assert_eq!(status, "covered");
        assert_eq!(binding, Some("usdc-depeg-1d-w48-2026-09-17T12:00:00Z"));
        assert_eq!(reason, None, "a covered channel has no reason to give");
    }

    #[test]
    fn an_admitted_but_uncovered_channel_says_why_it_is_missing() {
        let none = maps(&[]);
        let withheld = maps(&[("depegRisk1d", "question not currently eligible")]);
        let admitted = defined(&["depegRisk1d"]);
        let (status, binding, reason) = coverage_status("depegRisk1d", &none, &withheld, &admitted);
        assert_eq!((status, binding), ("unavailable", None));
        assert_eq!(reason, Some("question not currently eligible"));

        let silent = maps(&[]);
        let (_, _, default_reason) = coverage_status("depegRisk1d", &none, &silent, &admitted);
        assert_eq!(default_reason, Some("no operational episode"));
    }

    #[test]
    fn a_channel_with_no_admitted_definition_is_unsupported_not_omitted() {
        // Omitting it would make a missing channel indistinguishable from one that does not exist.
        let none = maps(&[]);
        let admitted = defined(&["depegRisk1d"]);
        let (status, binding, reason) = coverage_status("ethCrashRisk", &none, &none, &admitted);
        assert_eq!((status, binding), ("unsupported", None));
        assert_eq!(reason, Some("no admitted definition"));
    }

    #[test]
    fn a_signal_admitted_by_one_side_must_be_admitted_by_the_other() {
        let now = 2_000_000_i64;
        let mut binding = binding_id("b", "depegRisk1d", 1_000_000);
        binding.authorization_valid_from_ms = 1_500_000;

        assert!(
            signal_is_admissible(&binding, 1_500_000, now, now),
            "authorized and completed"
        );
        assert!(signal_is_admissible(&binding, 1_900_000, now, now));
    }

    #[test]
    fn a_signal_measured_before_the_binding_was_authorized_is_not_admissible() {
        let mut binding = binding_id("b", "depegRisk1d", 1_000_000);
        binding.authorization_valid_from_ms = 1_500_000;
        assert!(
            !signal_is_admissible(&binding, 1_499_999, 2_000_000, 2_000_000),
            "the feed was not allowed to carry this estimate yet"
        );
        assert!(
            signal_is_admissible(&binding, 1_500_000, 2_000_000, 2_000_000),
            "the boundary is inclusive in Python and must be here"
        );
    }

    #[test]
    fn a_signal_that_completes_after_now_is_not_admissible() {
        let binding = binding_id("b", "depegRisk1d", 1_000_000);
        assert!(
            !signal_is_admissible(&binding, 1_000_000, 2_000_001, 2_000_000),
            "evaluation cannot have completed in the future"
        );
        assert!(signal_is_admissible(&binding, 1_000_000, 2_000_000, 2_000_000));
    }

    #[test]
    fn an_exact_dated_binding_refuses_an_estimate_past_its_target_start() {
        let mut binding = binding_id("b", "depegRisk1d", 1_000_000);
        binding.mapping_kind = "exact_dated".to_string();
        assert!(
            signal_is_admissible(&binding, 1_000_000, 2_000_000, 2_000_000),
            "an estimate as of the target start is what the question asked for"
        );
        assert!(
            !signal_is_admissible(&binding, 1_000_001, 2_000_000, 2_000_000),
            "later than the start estimates a different thing"
        );

        binding.mapping_kind = "containing_upper_estimate".to_string();
        assert!(
            signal_is_admissible(&binding, 1_000_001, 2_000_000, 2_000_000),
            "a containing window can carry an estimate from inside it"
        );
    }

    #[test]
    fn every_channel_gets_a_status_so_the_denominator_is_visible() {
        assert_eq!(CHANNELS.len(), 12);
        let admitted = defined(&["depegRisk1d"]);
        let covered = maps(&[("btcCrashRisk", "btc-crash-1d-w48-2026-09-17T12:00:00Z")]);
        let withheld = maps(&[]);
        let statuses: Vec<&str> = CHANNELS
            .iter()
            .map(|channel| coverage_status(channel, &covered, &withheld, &admitted).0)
            .collect();
        assert_eq!(statuses.iter().filter(|s| **s == "covered").count(), 1);
        assert_eq!(statuses.iter().filter(|s| **s == "unavailable").count(), 1);
        assert_eq!(statuses.iter().filter(|s| **s == "unsupported").count(), 10);
    }
}
