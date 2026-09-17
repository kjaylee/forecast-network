//! Risk feed records (v1 frozen, v2 additive) with the Python reference's field constraints
//! and semantic `validate()` rules. Field order matters only for readability; canonical bytes
//! are produced from sorted `serde_json::Value` objects.

use serde::{Deserialize, Serialize};

use crate::errors::{require, Result};
pub use crate::fields::Record;
use crate::fields::{
    check_const, check_enum, check_hash, check_id, check_int, check_range, check_schema_version, check_text, is_asset,
    is_base58_32_44, is_signature, required_option,
};

pub const SIGNATURE_PREFIX: &[u8] = b"forecast-risk-feed-v1:signature:";
pub const SIGNATURE_PREFIX_V2: &[u8] = b"forecast-risk-feed-v2:signature:";
pub const FEED_TTL_MS: i64 = 120_000;
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
pub const SOURCES: [&str; 4] = ["ai", "crowd", "top", "market"];
pub const STATUSES: [&str; 3] = ["new", "provisional", "established"];
pub const CATEGORIES: [&str; 7] = [
    "TECHNOLOGY",
    "CRYPTO",
    "SCIENCE",
    "ENTERTAINMENT",
    "WORLD",
    "SPORTS",
    "OTHER",
];
pub const MAPPING_KINDS: [&str; 2] = ["exact_dated", "containing_upper_estimate"];
pub const COVERAGE_STATUSES: [&str; 4] = ["covered", "unavailable", "saturated", "unsupported"];

pub fn channel_horizon_ms(channel: &str) -> Option<i64> {
    match channel {
        "depegRisk1d" => Some(86_400_000),
        "depegRisk7d" => Some(604_800_000),
        "depegRisk30d" => Some(2_592_000_000),
        _ => None,
    }
}

// ---------------------------------------------------------------- v1 (frozen)

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedBinding {
    pub schema_version: i64,
    pub binding_id: String,
    pub version: String,
    pub forecast_id: String,
    pub specification_hash: String,
    pub channel: String,
    pub horizon_hours: i64,
    pub asset: String,
    pub category: String,
    pub valid_from_ms: i64,
    pub valid_until_ms: i64,
}

impl Record for RiskFeedBinding {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedBinding";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.binding_id"), &self.binding_id)?;
        check_const(&format!("{p}.version"), &self.version, "canonical-risk-binding-v1")?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_enum(&format!("{p}.channel"), &self.channel, &CHANNELS)?;
        check_range(&format!("{p}.horizon_hours"), self.horizon_hours, 1, 8760)?;
        require(is_asset(&self.asset), &format!("{p}.asset: malformed value"))?;
        check_enum(&format!("{p}.category"), &self.category, &CATEGORIES)?;
        check_int(&format!("{p}.valid_from_ms"), self.valid_from_ms)?;
        check_int(&format!("{p}.valid_until_ms"), self.valid_until_ms)?;
        require(self.valid_from_ms < self.valid_until_ms, "binding validity is empty")?;
        let expected = match self.channel.as_str() {
            "depegRisk1d" => Some(24),
            "depegRisk7d" => Some(168),
            "depegRisk30d" => Some(720),
            _ => None,
        };
        require(
            expected.is_none() || expected == Some(self.horizon_hours),
            "channel horizon mismatch",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedSignal {
    pub schema_version: i64,
    pub binding_id: String,
    pub source: String,
    pub probability_bp: i64,
    pub confidence_bp: i64,
    pub sample_count: i64,
    pub units: String,
    pub observed_at_ms: i64,
    pub evidence_hash: String,
    pub dependence_group: String,
    pub calibration_status: String,
}

impl Record for RiskFeedSignal {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedSignal";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.binding_id"), &self.binding_id)?;
        check_enum(&format!("{p}.source"), &self.source, &SOURCES)?;
        check_range(&format!("{p}.probability_bp"), self.probability_bp, 0, 10000)?;
        check_range(&format!("{p}.confidence_bp"), self.confidence_bp, 0, 10000)?;
        check_range(&format!("{p}.sample_count"), self.sample_count, 1, 1_000_000)?;
        check_const(&format!("{p}.units"), &self.units, "basis-points")?;
        check_int(&format!("{p}.observed_at_ms"), self.observed_at_ms)?;
        check_hash(&format!("{p}.evidence_hash"), &self.evidence_hash)?;
        check_hash(&format!("{p}.dependence_group"), &self.dependence_group)?;
        check_enum(&format!("{p}.calibration_status"), &self.calibration_status, &STATUSES)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedPayload {
    pub schema_version: i64,
    pub purpose: String,
    pub genesis_hash: String,
    pub feed_id: String,
    pub sequence: i64,
    pub key_id: String,
    pub issued_at_ms: i64,
    pub expires_at_ms: i64,
    pub bindings: Vec<RiskFeedBinding>,
    pub signals: Vec<RiskFeedSignal>,
    pub weight_set_hash: String,
    pub weight_set_version: String,
}

impl Record for RiskFeedPayload {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedPayload";
        check_schema_version(p, self.schema_version)?;
        check_const(&format!("{p}.purpose"), &self.purpose, "forecast-risk-feed-v1")?;
        require(
            is_base58_32_44(&self.genesis_hash),
            &format!("{p}.genesis_hash: malformed value"),
        )?;
        check_id(&format!("{p}.feed_id"), &self.feed_id)?;
        check_range(&format!("{p}.sequence"), self.sequence, 1, crate::MAX_SAFE_INTEGER)?;
        check_id(&format!("{p}.key_id"), &self.key_id)?;
        check_int(&format!("{p}.issued_at_ms"), self.issued_at_ms)?;
        check_int(&format!("{p}.expires_at_ms"), self.expires_at_ms)?;
        require(
            (1..=12).contains(&self.bindings.len()),
            &format!("{p}.bindings: requires 1..12 entries"),
        )?;
        require(
            (1..=48).contains(&self.signals.len()),
            &format!("{p}.signals: requires 1..48 entries"),
        )?;
        for b in &self.bindings {
            b.validate()?;
        }
        for s in &self.signals {
            s.validate()?;
        }
        check_hash(&format!("{p}.weight_set_hash"), &self.weight_set_hash)?;
        check_id(&format!("{p}.weight_set_version"), &self.weight_set_version)?;
        let lifetime = self.expires_at_ms - self.issued_at_ms;
        require(
            0 < lifetime && lifetime <= FEED_TTL_MS,
            "feed lifetime exceeds 120 seconds",
        )?;
        let ids: Vec<&str> = self.bindings.iter().map(|b| b.binding_id.as_str()).collect();
        let mut sorted_ids = ids.clone();
        sorted_ids.sort();
        sorted_ids.dedup();
        require(sorted_ids.len() == ids.len(), "duplicate binding")?;
        let mut channels: Vec<&str> = self.bindings.iter().map(|b| b.channel.as_str()).collect();
        channels.sort();
        channels.dedup();
        require(channels.len() == self.bindings.len(), "duplicate canonical channel")?;
        require(sorted_ids == ids, "bindings must be sorted")?;
        let identities: Vec<(&str, &str)> = self
            .signals
            .iter()
            .map(|s| (s.binding_id.as_str(), s.source.as_str()))
            .collect();
        let mut sorted = identities.clone();
        sorted.sort();
        sorted.dedup();
        require(
            sorted.len() == identities.len() && sorted == identities,
            "signals must be unique and sorted",
        )?;
        let mut used: Vec<&str> = self.signals.iter().map(|s| s.binding_id.as_str()).collect();
        used.sort();
        used.dedup();
        require(used == sorted_ids, "every signal needs a used binding")?;
        for b in &self.bindings {
            require(
                b.valid_from_ms <= self.issued_at_ms
                    && self.issued_at_ms < self.expires_at_ms
                    && self.expires_at_ms <= b.valid_until_ms,
                "feed outside canonical binding validity",
            )?;
        }
        for s in &self.signals {
            let b = self
                .bindings
                .iter()
                .find(|b| b.binding_id == s.binding_id)
                .expect("checked above");
            require(
                b.valid_from_ms <= s.observed_at_ms && s.observed_at_ms <= self.issued_at_ms,
                "signal outside binding or after issue time",
            )?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedRiskFeed {
    pub schema_version: i64,
    pub payload: RiskFeedPayload,
    pub public_key_hex: String,
    pub signature_hex: String,
}

impl Record for SignedRiskFeed {
    fn validate(&self) -> Result<()> {
        check_schema_version("SignedRiskFeed", self.schema_version)?;
        self.payload.validate()?;
        check_hash("SignedRiskFeed.public_key_hex", &self.public_key_hex)?;
        require(
            is_signature(&self.signature_hex),
            "SignedRiskFeed.signature_hex: malformed value",
        )
    }
}

pub fn signing_bytes(payload: &RiskFeedPayload) -> Result<Vec<u8>> {
    let mut bytes = SIGNATURE_PREFIX.to_vec();
    bytes.extend(payload.canonical()?);
    Ok(bytes)
}

// ---------------------------------------------------------------- v2 (additive)

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CanonicalRiskDefinitionV2 {
    pub schema_version: i64,
    pub definition_id: String,
    pub definition_version: String,
    pub asset: String,
    pub channel: String,
    pub policy_horizon_ms: i64,
    pub predicate: String,
    pub units: String,
    pub threshold: String,
    pub sampling_grid_ms: i64,
    pub witness_semantics: String,
    pub interval_rule: String,
    pub baseline_policy: String,
    pub source_policy: String,
    pub invalid_data_policy: String,
    pub mapping_profile_id: String,
    pub mapping_profile_version: String,
    pub mapping_kind: String,
    pub source_freshness_max_ms: i64,
    pub inference_freshness_max_ms: i64,
    pub clock_skew_max_ms: i64,
    pub calibration_cohort_id: String,
}

impl Record for CanonicalRiskDefinitionV2 {
    fn validate(&self) -> Result<()> {
        let p = "CanonicalRiskDefinitionV2";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.definition_id"), &self.definition_id)?;
        check_id(&format!("{p}.definition_version"), &self.definition_version)?;
        require(is_asset(&self.asset), &format!("{p}.asset: malformed value"))?;
        check_enum(&format!("{p}.channel"), &self.channel, &CHANNELS)?;
        check_range(
            &format!("{p}.policy_horizon_ms"),
            self.policy_horizon_ms,
            1,
            31_536_000_000,
        )?;
        check_text(&format!("{p}.predicate"), &self.predicate, Some(2000))?;
        check_text(&format!("{p}.units"), &self.units, Some(32))?;
        check_text(&format!("{p}.threshold"), &self.threshold, Some(64))?;
        check_range(&format!("{p}.sampling_grid_ms"), self.sampling_grid_ms, 1, 86_400_000)?;
        check_text(&format!("{p}.witness_semantics"), &self.witness_semantics, Some(500))?;
        check_const(&format!("{p}.interval_rule"), &self.interval_rule, "[start,end)")?;
        check_text(&format!("{p}.baseline_policy"), &self.baseline_policy, Some(500))?;
        check_text(&format!("{p}.source_policy"), &self.source_policy, Some(2000))?;
        check_text(
            &format!("{p}.invalid_data_policy"),
            &self.invalid_data_policy,
            Some(500),
        )?;
        check_id(&format!("{p}.mapping_profile_id"), &self.mapping_profile_id)?;
        check_id(&format!("{p}.mapping_profile_version"), &self.mapping_profile_version)?;
        check_enum(&format!("{p}.mapping_kind"), &self.mapping_kind, &MAPPING_KINDS)?;
        check_range(
            &format!("{p}.source_freshness_max_ms"),
            self.source_freshness_max_ms,
            1,
            86_400_000,
        )?;
        check_range(
            &format!("{p}.inference_freshness_max_ms"),
            self.inference_freshness_max_ms,
            1,
            21_600_000,
        )?;
        check_range(&format!("{p}.clock_skew_max_ms"), self.clock_skew_max_ms, 0, 600_000)?;
        check_id(&format!("{p}.calibration_cohort_id"), &self.calibration_cohort_id)?;
        let expected = channel_horizon_ms(&self.channel);
        require(
            expected.is_none() || expected == Some(self.policy_horizon_ms),
            "channel horizon mismatch",
        )?;
        require(
            self.sampling_grid_ms <= self.policy_horizon_ms,
            "sampling grid exceeds policy horizon",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskMappingProfileV2 {
    pub schema_version: i64,
    pub profile_id: String,
    pub profile_version: String,
    pub mapping_kind: String,
    pub predicate_class: String,
    pub max_forecast_age_ms: i64,
    pub max_policy_lag_ms: i64,
    pub source_freshness_max_ms: i64,
    pub clock_skew_max_ms: i64,
    pub review_reference: String,
}

impl Record for RiskMappingProfileV2 {
    fn validate(&self) -> Result<()> {
        let p = "RiskMappingProfileV2";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.profile_id"), &self.profile_id)?;
        check_id(&format!("{p}.profile_version"), &self.profile_version)?;
        check_enum(&format!("{p}.mapping_kind"), &self.mapping_kind, &MAPPING_KINDS)?;
        check_enum(
            &format!("{p}.predicate_class"),
            &self.predicate_class,
            &["monotone_any_event", "exact_target"],
        )?;
        check_range(
            &format!("{p}.max_forecast_age_ms"),
            self.max_forecast_age_ms,
            1,
            21_600_000,
        )?;
        check_range(
            &format!("{p}.max_policy_lag_ms"),
            self.max_policy_lag_ms,
            0,
            31_536_000_000,
        )?;
        check_range(
            &format!("{p}.source_freshness_max_ms"),
            self.source_freshness_max_ms,
            1,
            86_400_000,
        )?;
        check_range(&format!("{p}.clock_skew_max_ms"), self.clock_skew_max_ms, 0, 600_000)?;
        check_text(&format!("{p}.review_reference"), &self.review_reference, Some(500))?;
        if self.mapping_kind == "containing_upper_estimate" {
            require(
                self.predicate_class == "monotone_any_event",
                "containment needs a monotone any-event predicate",
            )?;
            require(
                self.max_policy_lag_ms == 0,
                "containment has no policy lag; coverage is by target interval",
            )
        } else {
            require(
                self.predicate_class == "exact_target",
                "exact dated mapping targets the declared window",
            )
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedSeriesV2 {
    pub schema_version: i64,
    pub series_id: String,
    pub version: String,
    pub feed_id: String,
    pub channel: String,
    pub asset: String,
    pub definition_hash: String,
    pub mapping_profile_hash: String,
    pub mapping_kind: String,
    pub policy_horizon_ms: i64,
    pub window_ms: i64,
    pub cadence_ms: i64,
    pub lead_ms: i64,
    pub question_template: String,
}

impl Record for RiskFeedSeriesV2 {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedSeriesV2";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.series_id"), &self.series_id)?;
        check_const(&format!("{p}.version"), &self.version, "canonical-risk-series-v2")?;
        check_id(&format!("{p}.feed_id"), &self.feed_id)?;
        check_enum(&format!("{p}.channel"), &self.channel, &CHANNELS)?;
        require(is_asset(&self.asset), &format!("{p}.asset: malformed value"))?;
        check_hash(&format!("{p}.definition_hash"), &self.definition_hash)?;
        check_hash(&format!("{p}.mapping_profile_hash"), &self.mapping_profile_hash)?;
        check_enum(&format!("{p}.mapping_kind"), &self.mapping_kind, &MAPPING_KINDS)?;
        check_range(
            &format!("{p}.policy_horizon_ms"),
            self.policy_horizon_ms,
            1,
            31_536_000_000,
        )?;
        check_range(&format!("{p}.window_ms"), self.window_ms, 1, 31_536_000_000)?;
        check_range(&format!("{p}.cadence_ms"), self.cadence_ms, 60_000, 31_536_000_000)?;
        check_range(&format!("{p}.lead_ms"), self.lead_ms, 60_000, 86_400_000)?;
        check_text(&format!("{p}.question_template"), &self.question_template, Some(1000))?;
        let expected = channel_horizon_ms(&self.channel);
        require(
            expected.is_none() || expected == Some(self.policy_horizon_ms),
            "channel horizon mismatch",
        )?;
        if self.mapping_kind == "containing_upper_estimate" {
            require(
                self.window_ms - self.policy_horizon_ms >= self.cadence_ms,
                "containing episodes must overlap by at least one cadence",
            )?;
        } else {
            require(
                self.window_ms == self.policy_horizon_ms,
                "exact dated episodes span one policy horizon",
            )?;
        }
        require(self.cadence_ms <= self.window_ms, "cadence exceeds the episode window")?;
        require(
            self.lead_ms < self.cadence_ms,
            "lead time must be shorter than the cadence",
        )?;
        require(
            self.question_template.contains("{start}") && self.question_template.contains("{end}"),
            "template must place the episode start and end",
        )
    }
}

impl RiskFeedSeriesV2 {
    /// Deterministic template check; the binding's signature still comes from the producer.
    pub fn conforms(&self, binding: &RiskFeedBindingV2) -> bool {
        let reserve = if self.mapping_kind == "containing_upper_estimate" {
            self.policy_horizon_ms
        } else {
            0
        };
        binding.series_id == self.series_id
            && binding.channel == self.channel
            && binding.asset == self.asset
            && binding.definition_hash == self.definition_hash
            && binding.mapping_profile_hash == self.mapping_profile_hash
            && binding.mapping_kind == self.mapping_kind
            && binding.policy_horizon_ms == self.policy_horizon_ms
            && binding.target_end_ms - binding.target_start_ms == self.window_ms
            && binding.target_start_ms % self.cadence_ms == 0
            && binding.operational_valid_from_ms == binding.target_start_ms
            && binding.operational_valid_until_ms == binding.target_end_ms - reserve
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedBindingV2 {
    pub schema_version: i64,
    pub binding_id: String,
    pub version: String,
    pub forecast_id: String,
    pub specification_hash: String,
    pub channel: String,
    pub asset: String,
    pub category: String,
    pub series_id: String,
    pub episode_id: String,
    pub target_start_ms: i64,
    pub target_end_ms: i64,
    pub interval: String,
    pub policy_horizon_ms: i64,
    pub definition_hash: String,
    pub mapping_profile_id: String,
    pub mapping_profile_version: String,
    pub mapping_profile_hash: String,
    pub mapping_kind: String,
    pub question_event_definition_hash: String,
    pub approval_artifact_hash: String,
    pub authorization_valid_from_ms: i64,
    pub authorization_valid_until_ms: i64,
    pub operational_valid_from_ms: i64,
    pub operational_valid_until_ms: i64,
}

impl Record for RiskFeedBindingV2 {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedBindingV2";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.binding_id"), &self.binding_id)?;
        check_const(&format!("{p}.version"), &self.version, "canonical-risk-binding-v2")?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_enum(&format!("{p}.channel"), &self.channel, &CHANNELS)?;
        require(is_asset(&self.asset), &format!("{p}.asset: malformed value"))?;
        check_enum(&format!("{p}.category"), &self.category, &CATEGORIES)?;
        check_id(&format!("{p}.series_id"), &self.series_id)?;
        check_id(&format!("{p}.episode_id"), &self.episode_id)?;
        check_int(&format!("{p}.target_start_ms"), self.target_start_ms)?;
        check_int(&format!("{p}.target_end_ms"), self.target_end_ms)?;
        check_const(&format!("{p}.interval"), &self.interval, "[start,end)")?;
        check_range(
            &format!("{p}.policy_horizon_ms"),
            self.policy_horizon_ms,
            1,
            31_536_000_000,
        )?;
        check_hash(&format!("{p}.definition_hash"), &self.definition_hash)?;
        check_id(&format!("{p}.mapping_profile_id"), &self.mapping_profile_id)?;
        check_id(&format!("{p}.mapping_profile_version"), &self.mapping_profile_version)?;
        check_hash(&format!("{p}.mapping_profile_hash"), &self.mapping_profile_hash)?;
        check_enum(&format!("{p}.mapping_kind"), &self.mapping_kind, &MAPPING_KINDS)?;
        check_hash(
            &format!("{p}.question_event_definition_hash"),
            &self.question_event_definition_hash,
        )?;
        check_hash(&format!("{p}.approval_artifact_hash"), &self.approval_artifact_hash)?;
        for (name, value) in [
            ("authorization_valid_from_ms", self.authorization_valid_from_ms),
            ("authorization_valid_until_ms", self.authorization_valid_until_ms),
            ("operational_valid_from_ms", self.operational_valid_from_ms),
            ("operational_valid_until_ms", self.operational_valid_until_ms),
        ] {
            check_int(&format!("{p}.{name}"), value)?;
        }
        require(self.target_start_ms < self.target_end_ms, "target interval is empty")?;
        let expected = channel_horizon_ms(&self.channel);
        require(
            expected.is_none() || expected == Some(self.policy_horizon_ms),
            "channel horizon mismatch",
        )?;
        require(
            self.authorization_valid_from_ms < self.authorization_valid_until_ms,
            "authorization validity is empty",
        )?;
        require(
            self.operational_valid_from_ms < self.operational_valid_until_ms,
            "operational validity is empty",
        )?;
        require(
            self.authorization_valid_from_ms <= self.operational_valid_from_ms
                && self.operational_valid_until_ms <= self.authorization_valid_until_ms,
            "operational validity outside authorization",
        )?;
        require(
            self.target_start_ms <= self.operational_valid_from_ms,
            "policy use before target start",
        )?;
        if self.mapping_kind == "containing_upper_estimate" {
            require(
                self.operational_valid_until_ms + self.policy_horizon_ms <= self.target_end_ms,
                "policy horizon escapes target end",
            )
        } else {
            require(
                self.target_end_ms - self.target_start_ms == self.policy_horizon_ms,
                "exact dated target must span one policy horizon",
            )?;
            require(
                self.operational_valid_until_ms <= self.target_end_ms,
                "dated forecast used after its target",
            )
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedSignalV2 {
    pub schema_version: i64,
    pub binding_id: String,
    pub source: String,
    pub value_kind: String,
    pub question_probability_bp: i64,
    pub confidence_bp: i64,
    pub sample_count: i64,
    pub units: String,
    pub forecast_as_of_ms: i64,
    pub information_cutoff_ms: i64,
    pub evaluation_started_at_ms: i64,
    pub evaluation_completed_at_ms: i64,
    pub source_capture_started_at_ms: i64,
    pub source_capture_completed_at_ms: i64,
    #[serde(deserialize_with = "required_option")]
    pub source_watermark_ms: Option<i64>,
    pub source_bundle_hash: String,
    pub estimate_hash: String,
    pub coverage_evidence_hash: String,
    pub evidence_hash: String,
    pub dependence_group: String,
    pub estimator_version: String,
    #[serde(deserialize_with = "required_option")]
    pub oldest_member_as_of_ms: Option<i64>,
    #[serde(deserialize_with = "required_option")]
    pub newest_member_as_of_ms: Option<i64>,
    #[serde(deserialize_with = "required_option")]
    pub constituent_dataset_hash: Option<String>,
    pub calibration_status: String,
}

impl Record for RiskFeedSignalV2 {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedSignalV2";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.binding_id"), &self.binding_id)?;
        check_enum(&format!("{p}.source"), &self.source, &SOURCES)?;
        check_const(&format!("{p}.value_kind"), &self.value_kind, "question_probability")?;
        check_range(
            &format!("{p}.question_probability_bp"),
            self.question_probability_bp,
            0,
            10000,
        )?;
        check_range(&format!("{p}.confidence_bp"), self.confidence_bp, 0, 10000)?;
        check_range(&format!("{p}.sample_count"), self.sample_count, 1, 1_000_000)?;
        check_const(&format!("{p}.units"), &self.units, "basis-points")?;
        for (name, value) in [
            ("forecast_as_of_ms", self.forecast_as_of_ms),
            ("information_cutoff_ms", self.information_cutoff_ms),
            ("evaluation_started_at_ms", self.evaluation_started_at_ms),
            ("evaluation_completed_at_ms", self.evaluation_completed_at_ms),
            ("source_capture_started_at_ms", self.source_capture_started_at_ms),
            ("source_capture_completed_at_ms", self.source_capture_completed_at_ms),
        ] {
            check_int(&format!("{p}.{name}"), value)?;
        }
        if let Some(value) = self.source_watermark_ms {
            check_int(&format!("{p}.source_watermark_ms"), value)?;
        }
        for (name, value) in [
            ("source_bundle_hash", &self.source_bundle_hash),
            ("estimate_hash", &self.estimate_hash),
            ("coverage_evidence_hash", &self.coverage_evidence_hash),
            ("evidence_hash", &self.evidence_hash),
            ("dependence_group", &self.dependence_group),
        ] {
            check_hash(&format!("{p}.{name}"), value)?;
        }
        check_id(&format!("{p}.estimator_version"), &self.estimator_version)?;
        if let Some(value) = self.oldest_member_as_of_ms {
            check_int(&format!("{p}.oldest_member_as_of_ms"), value)?;
        }
        if let Some(value) = self.newest_member_as_of_ms {
            check_int(&format!("{p}.newest_member_as_of_ms"), value)?;
        }
        if let Some(value) = &self.constituent_dataset_hash {
            check_hash(&format!("{p}.constituent_dataset_hash"), value)?;
        }
        check_enum(&format!("{p}.calibration_status"), &self.calibration_status, &STATUSES)?;
        require(
            self.information_cutoff_ms <= self.forecast_as_of_ms,
            "information cutoff after forecast as-of",
        )?;
        require(
            self.forecast_as_of_ms <= self.evaluation_completed_at_ms,
            "evaluation completed before its as-of",
        )?;
        require(
            self.evaluation_started_at_ms <= self.evaluation_completed_at_ms,
            "evaluation ends before it starts",
        )?;
        require(
            self.source_capture_started_at_ms <= self.source_capture_completed_at_ms
                && self.source_capture_completed_at_ms <= self.forecast_as_of_ms,
            "source capture outside the forecast information set",
        )?;
        require(
            self.source_watermark_ms.is_none_or(|w| w <= self.forecast_as_of_ms),
            "source event after forecast as-of",
        )?;
        let pool_present = [
            self.oldest_member_as_of_ms.is_some(),
            self.newest_member_as_of_ms.is_some(),
            self.constituent_dataset_hash.is_some(),
        ];
        if self.source == "crowd" || self.source == "top" {
            require(
                pool_present.iter().all(|x| *x),
                "pooled sources declare constituent provenance",
            )?;
        } else {
            require(
                pool_present.iter().all(|x| !*x),
                "single-model sources have no constituent pool",
            )?;
        }
        if let (Some(oldest), Some(newest)) = (self.oldest_member_as_of_ms, self.newest_member_as_of_ms) {
            require(
                oldest <= newest && newest <= self.forecast_as_of_ms,
                "pool members outside the forecast information set",
            )?;
        }
        Ok(())
    }
}

/// A pool is only as fresh as its oldest member; the newest submission never retimes peers.
pub fn freshness_as_of_ms(signal: &RiskFeedSignalV2) -> i64 {
    signal.oldest_member_as_of_ms.unwrap_or(signal.forecast_as_of_ms)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChannelCoverageV2 {
    pub schema_version: i64,
    pub channel: String,
    pub status: String,
    #[serde(deserialize_with = "required_option")]
    pub binding_id: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub reason: Option<String>,
}

impl Record for ChannelCoverageV2 {
    fn validate(&self) -> Result<()> {
        let p = "ChannelCoverageV2";
        check_schema_version(p, self.schema_version)?;
        check_enum(&format!("{p}.channel"), &self.channel, &CHANNELS)?;
        check_enum(&format!("{p}.status"), &self.status, &COVERAGE_STATUSES)?;
        if let Some(value) = &self.binding_id {
            check_id(&format!("{p}.binding_id"), value)?;
        }
        if let Some(value) = &self.reason {
            check_text(&format!("{p}.reason"), value, Some(500))?;
        }
        if self.status == "covered" {
            require(
                self.binding_id.is_some() && self.reason.is_none(),
                "covered channels reference one binding",
            )
        } else {
            require(
                self.binding_id.is_none() && self.reason.is_some(),
                "uncovered channels state a reason",
            )
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskFeedPayloadV2 {
    pub schema_version: i64,
    pub purpose: String,
    pub genesis_hash: String,
    pub feed_id: String,
    pub sequence: i64,
    pub key_id: String,
    pub issued_at_ms: i64,
    pub expires_at_ms: i64,
    pub bindings: Vec<RiskFeedBindingV2>,
    pub signals: Vec<RiskFeedSignalV2>,
    pub channel_coverage: Vec<ChannelCoverageV2>,
    pub profile_set_hash: String,
    pub weight_set_hash: String,
    pub weight_set_version: String,
    pub calibration_cohort_id: String,
}

impl Record for RiskFeedPayloadV2 {
    fn validate(&self) -> Result<()> {
        let p = "RiskFeedPayloadV2";
        check_schema_version(p, self.schema_version)?;
        check_const(&format!("{p}.purpose"), &self.purpose, "forecast-risk-feed-v2")?;
        require(
            is_base58_32_44(&self.genesis_hash),
            &format!("{p}.genesis_hash: malformed value"),
        )?;
        check_id(&format!("{p}.feed_id"), &self.feed_id)?;
        check_range(&format!("{p}.sequence"), self.sequence, 1, crate::MAX_SAFE_INTEGER)?;
        check_id(&format!("{p}.key_id"), &self.key_id)?;
        check_int(&format!("{p}.issued_at_ms"), self.issued_at_ms)?;
        check_int(&format!("{p}.expires_at_ms"), self.expires_at_ms)?;
        require(
            self.bindings.len() <= 12,
            &format!("{p}.bindings: allows at most 12 entries"),
        )?;
        require(
            self.signals.len() <= 48,
            &format!("{p}.signals: allows at most 48 entries"),
        )?;
        require(
            (1..=12).contains(&self.channel_coverage.len()),
            &format!("{p}.channel_coverage: requires 1..12 entries"),
        )?;
        for b in &self.bindings {
            b.validate()?;
        }
        for s in &self.signals {
            s.validate()?;
        }
        for c in &self.channel_coverage {
            c.validate()?;
        }
        check_hash(&format!("{p}.profile_set_hash"), &self.profile_set_hash)?;
        check_hash(&format!("{p}.weight_set_hash"), &self.weight_set_hash)?;
        check_id(&format!("{p}.weight_set_version"), &self.weight_set_version)?;
        check_id(&format!("{p}.calibration_cohort_id"), &self.calibration_cohort_id)?;
        let lifetime = self.expires_at_ms - self.issued_at_ms;
        require(
            0 < lifetime && lifetime <= FEED_TTL_MS,
            "feed lifetime exceeds 120 seconds",
        )?;
        let ids: Vec<&str> = self.bindings.iter().map(|b| b.binding_id.as_str()).collect();
        let mut sorted_ids = ids.clone();
        sorted_ids.sort();
        sorted_ids.dedup();
        require(sorted_ids.len() == ids.len(), "duplicate binding")?;
        let mut channels: Vec<&str> = self.bindings.iter().map(|b| b.channel.as_str()).collect();
        channels.sort();
        channels.dedup();
        require(channels.len() == self.bindings.len(), "duplicate canonical channel")?;
        require(sorted_ids == ids, "bindings must be sorted")?;
        let identities: Vec<(&str, &str)> = self
            .signals
            .iter()
            .map(|s| (s.binding_id.as_str(), s.source.as_str()))
            .collect();
        let mut sorted = identities.clone();
        sorted.sort();
        sorted.dedup();
        require(
            sorted.len() == identities.len() && sorted == identities,
            "signals must be unique and sorted",
        )?;
        let mut used: Vec<&str> = self.signals.iter().map(|s| s.binding_id.as_str()).collect();
        used.sort();
        used.dedup();
        require(used == sorted_ids, "every binding needs a signal and vice versa")?;
        for b in &self.bindings {
            require(
                b.operational_valid_from_ms <= self.issued_at_ms
                    && self.issued_at_ms < self.expires_at_ms
                    && self.expires_at_ms <= b.operational_valid_until_ms,
                "feed outside operational binding validity",
            )?;
        }
        for s in &self.signals {
            let b = self
                .bindings
                .iter()
                .find(|b| b.binding_id == s.binding_id)
                .expect("checked above");
            require(
                b.authorization_valid_from_ms <= s.forecast_as_of_ms
                    && s.evaluation_completed_at_ms <= self.issued_at_ms,
                "signal outside binding authorization or after issue time",
            )?;
            if b.mapping_kind == "exact_dated" {
                require(
                    s.forecast_as_of_ms <= b.target_start_ms,
                    "exact dated estimates need an as-of at or before the target start",
                )?;
            }
        }
        let coverage_channels: Vec<&str> = self.channel_coverage.iter().map(|c| c.channel.as_str()).collect();
        let mut sorted_channels = coverage_channels.clone();
        sorted_channels.sort();
        sorted_channels.dedup();
        require(
            sorted_channels.len() == coverage_channels.len() && sorted_channels == coverage_channels,
            "channel coverage must be unique and sorted",
        )?;
        let mut covered: Vec<(&str, &str)> = self
            .channel_coverage
            .iter()
            .filter(|c| c.status == "covered")
            .map(|c| (c.channel.as_str(), c.binding_id.as_deref().unwrap_or("")))
            .collect();
        covered.sort();
        let mut bound: Vec<(&str, &str)> = self
            .bindings
            .iter()
            .map(|b| (b.channel.as_str(), b.binding_id.as_str()))
            .collect();
        bound.sort();
        require(
            covered == bound,
            "channel coverage must name exactly the bound channels",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedRiskFeedV2 {
    pub schema_version: i64,
    pub payload: RiskFeedPayloadV2,
    pub public_key_hex: String,
    pub signature_hex: String,
}

impl Record for SignedRiskFeedV2 {
    fn validate(&self) -> Result<()> {
        check_schema_version("SignedRiskFeedV2", self.schema_version)?;
        self.payload.validate()?;
        check_hash("SignedRiskFeedV2.public_key_hex", &self.public_key_hex)?;
        require(
            is_signature(&self.signature_hex),
            "SignedRiskFeedV2.signature_hex: malformed value",
        )
    }
}

pub fn signing_bytes_v2(payload: &RiskFeedPayloadV2) -> Result<Vec<u8>> {
    let mut bytes = SIGNATURE_PREFIX_V2.to_vec();
    bytes.extend(payload.canonical()?);
    Ok(bytes)
}
