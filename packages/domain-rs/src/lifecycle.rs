#![allow(clippy::large_enum_variant, clippy::too_many_arguments)]
//! Pure, immutable forecasting lifecycle (`forecast_domain.lifecycle` + `early_resolution`).
//! Never reads a clock or performs an effect; the adapter persists aggregate, event and receipt
//! atomically with a revision CAS. Snapshots are v1 (`Forecast`) or v2 (`ForecastV2`, early
//! positive resolution); both decode strictly and hash exactly like the Python records.

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::errors::{require, Result, ValidationError};
use crate::fields::{
    check_hash, check_id, check_int, check_range, check_schema_version, check_text, required_option, Record,
};
use crate::models::{
    counter_judge_input_hash, counter_judge_output_hash, resolution_input_hash, to_value, url_hostname, AIProvenance,
    Dispute, DisputeReview, EvidenceSnapshot, ForecastSpecification, Resolution, SourceVerification, UserForecast,
    ValidationAssessment,
};
use crate::{content_hash, MAX_SAFE_INTEGER};

pub const MAX_ACTIVE_DISPUTES: usize = 256;
pub const STATES: [&str; 12] = [
    "DRAFT",
    "VALIDATING",
    "OPEN",
    "LOCKED",
    "RESOLVING",
    "PROPOSED",
    "CHALLENGE",
    "DISPUTED",
    "ESCALATED",
    "PAUSED",
    "FINALIZED",
    "ARCHIVED",
];
pub const EFFECTS: [&str; 3] = [
    "REPUTATION_UPDATE_REQUIRED",
    "RESULT_NOTIFICATION_REQUIRED",
    "RESOLUTION_COMMITMENT_REQUIRED",
];
const PAUSABLE: [&str; 5] = ["RESOLVING", "PROPOSED", "CHALLENGE", "DISPUTED", "ESCALATED"];

/// Lifecycle failure classes (`TransitionError`, `ConcurrencyError`, `IdempotencyConflict`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LifecycleError {
    Validation(String),
    Transition(String),
    Concurrency(String),
    Idempotency(String),
}

impl std::fmt::Display for LifecycleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LifecycleError::Validation(m)
            | LifecycleError::Transition(m)
            | LifecycleError::Concurrency(m)
            | LifecycleError::Idempotency(m) => write!(f, "{m}"),
        }
    }
}

impl From<ValidationError> for LifecycleError {
    fn from(error: ValidationError) -> Self {
        LifecycleError::Validation(error.0)
    }
}

pub type Transition<T> = std::result::Result<T, LifecycleError>;

fn guard(condition: bool, message: &str) -> Transition<()> {
    if condition {
        Ok(())
    } else {
        Err(LifecycleError::Transition(message.to_string()))
    }
}

fn digest(value: &str, name: &str) -> Result<()> {
    require(
        value.len() == 64 && value.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')),
        &format!("{name} must be a lowercase SHA-256 digest"),
    )
}

fn allowed_states(command: &str) -> Option<&'static [&'static str]> {
    Some(match command {
        "edit_specification" | "begin_validation" => &["DRAFT"],
        "reject_validation" | "publish" => &["VALIDATING"],
        "lock" | "submit_forecast" => &["OPEN"],
        "begin_resolution" => &["LOCKED"],
        "propose_resolution" => &["RESOLVING"],
        "begin_challenge" => &["PROPOSED"],
        "submit_dispute" => &["CHALLENGE", "DISPUTED"],
        "review_dispute" | "retain_proposal" | "escalate" => &["DISPUTED"],
        "adjudicate_resolution" => &["ESCALATED"],
        "finalize" => &["CHALLENGE"],
        "archive" => &["FINALIZED"],
        "pause_for_provider_outage" => &PAUSABLE,
        "resume_after_provider_recovery" => &["PAUSED"],
        _ => return None,
    })
}

fn target_states(command: &str) -> Option<&'static [&'static str]> {
    Some(match command {
        "edit_specification" | "reject_validation" => &["DRAFT"],
        "begin_validation" => &["VALIDATING"],
        "publish" | "submit_forecast" => &["OPEN"],
        "lock" => &["LOCKED"],
        "begin_resolution" => &["RESOLVING"],
        "propose_resolution" | "adjudicate_resolution" => &["PROPOSED"],
        "begin_challenge" | "retain_proposal" => &["CHALLENGE"],
        "submit_dispute" | "review_dispute" => &["DISPUTED"],
        "escalate" => &["ESCALATED"],
        "finalize" => &["FINALIZED"],
        "archive" => &["ARCHIVED"],
        "pause_for_provider_outage" => &["PAUSED"],
        "resume_after_provider_recovery" => &PAUSABLE,
        _ => return None,
    })
}

// ---------------------------------------------------------------- pause and events

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pause {
    pub schema_version: i64,
    pub previous_state: String,
    pub paused_at_ms: i64,
    pub configured_providers: Vec<String>,
    pub unavailable_providers: Vec<String>,
    pub reason: String,
}

fn check_providers(path: &str, providers: &[String]) -> Result<()> {
    require(
        (1..=32).contains(&providers.len()),
        &format!("{path}: requires 1..32 entries"),
    )?;
    for (index, item) in providers.iter().enumerate() {
        check_text(&format!("{path}[{index}]"), item, None)?;
    }
    require(
        providers.iter().enumerate().all(|(i, p)| !providers[..i].contains(p)),
        &format!("{path}: duplicate entries"),
    )
}

impl Record for Pause {
    fn validate(&self) -> Result<()> {
        let p = "Pause";
        check_schema_version(p, self.schema_version)?;
        require(
            STATES.contains(&self.previous_state.as_str()),
            &format!("{p}.previous_state: expected one of {STATES:?}"),
        )?;
        check_int(&format!("{p}.paused_at_ms"), self.paused_at_ms)?;
        check_providers(&format!("{p}.configured_providers"), &self.configured_providers)?;
        check_providers(&format!("{p}.unavailable_providers"), &self.unavailable_providers)?;
        check_text(&format!("{p}.reason"), &self.reason, None)?;
        require(
            PAUSABLE.contains(&self.previous_state.as_str()),
            "state cannot pause for provider failure",
        )?;
        require(
            !self.configured_providers.is_empty(),
            "configured providers must not be empty",
        )?;
        let mut configured = self.configured_providers.clone();
        configured.sort();
        let mut unavailable = self.unavailable_providers.clone();
        unavailable.sort();
        require(
            configured == unavailable,
            "provider outage requires every configured provider to be unavailable",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DomainEvent {
    pub schema_version: i64,
    pub forecast_id: String,
    pub command_id: String,
    pub command_name: String,
    pub old_state: String,
    pub new_state: String,
    pub revision: i64,
    pub occurred_at_ms: i64,
    pub specification_hash: String,
    pub state_hash: String,
    #[serde(deserialize_with = "required_option")]
    pub artifact_hash: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub previous_event_hash: Option<String>,
    pub effects: Vec<String>,
}

impl Record for DomainEvent {
    fn validate(&self) -> Result<()> {
        let p = "DomainEvent";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_id(&format!("{p}.command_id"), &self.command_id)?;
        check_text(&format!("{p}.command_name"), &self.command_name, None)?;
        for (name, value) in [("old_state", &self.old_state), ("new_state", &self.new_state)] {
            require(
                STATES.contains(&value.as_str()),
                &format!("{p}.{name}: expected one of {STATES:?}"),
            )?;
        }
        check_int(&format!("{p}.revision"), self.revision)?;
        check_int(&format!("{p}.occurred_at_ms"), self.occurred_at_ms)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_hash(&format!("{p}.state_hash"), &self.state_hash)?;
        for value in [&self.artifact_hash, &self.previous_event_hash].into_iter().flatten() {
            check_text(&format!("{p}.hash"), value, None)?;
        }
        for effect in &self.effects {
            require(
                EFFECTS.contains(&effect.as_str()),
                &format!("{p}.effects: expected one of {EFFECTS:?}"),
            )?;
        }
        require(self.revision > 0, "event revision must be positive")?;
        digest(&self.specification_hash, "specification_hash")?;
        digest(&self.state_hash, "state_hash")?;
        if let Some(value) = &self.artifact_hash {
            digest(value, "artifact_hash")?;
        }
        if let Some(value) = &self.previous_event_hash {
            digest(value, "previous_event_hash")?;
        }
        let allowed =
            allowed_states(&self.command_name).ok_or_else(|| ValidationError::new("unknown event command"))?;
        require(
            allowed.contains(&self.old_state.as_str()),
            "event command is illegal from old state",
        )?;
        require(
            target_states(&self.command_name)
                .expect("known")
                .contains(&self.new_state.as_str()),
            "event command cannot reach new state",
        )?;
        require(
            self.effects
                .iter()
                .enumerate()
                .all(|(i, e)| !self.effects[..i].contains(e)),
            "event effects must be unique",
        )?;
        let expected: Vec<String> = if self.command_name == "finalize" {
            EFFECTS.iter().map(|e| e.to_string()).collect()
        } else {
            Vec::new()
        };
        require(self.effects == expected, "event effects must agree with command")
    }
}

// ---------------------------------------------------------------- resolutions (v1 or early)

/// `Resolution | EarlyResolution`: decoding tries the plain record first, like the reference union.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum AnyResolution {
    Standard(Resolution),
    Early(EarlyResolution),
}

impl AnyResolution {
    pub fn base(&self) -> &Resolution {
        match self {
            AnyResolution::Standard(r) => r,
            AnyResolution::Early(e) => &e.resolution,
        }
    }

    pub fn resolution_hash(&self) -> Result<String> {
        match self {
            AnyResolution::Standard(r) => r.resolution_hash(),
            AnyResolution::Early(e) => content_hash(e),
        }
    }

    pub fn validate(&self) -> Result<()> {
        match self {
            AnyResolution::Standard(r) => r.validate(),
            AnyResolution::Early(e) => e.validate(),
        }
    }

    pub fn require_proposable(&self, specification: &ForecastSpecification) -> Result<()> {
        match self {
            AnyResolution::Standard(r) => r.require_proposable(specification),
            AnyResolution::Early(e) => e.require_proposable(specification),
        }
    }
}

// ---------------------------------------------------------------- early resolution (v2)

pub fn early_trigger_input_hash(
    forecast_id: &str,
    specification_hash: &str,
    clause_id: &str,
    evidence: &[EvidenceSnapshot],
    source_verifications: &[SourceVerification],
    event_at_ms: i64,
    observed_at_ms: i64,
    event_time_basis: &str,
) -> Result<String> {
    content_hash(
        &json!({"schema_version": 2, "kind": "early_positive_trigger_input", "forecast_id": forecast_id,
                         "specification_hash": specification_hash, "clause_id": clause_id, "evidence": evidence,
                         "source_verifications": source_verifications, "event_at_ms": event_at_ms,
                         "observed_at_ms": observed_at_ms, "event_time_basis": event_time_basis}),
    )
}

pub fn early_qualification_output_hash(input_hash: &str, qualification: &str) -> Result<String> {
    content_hash(
        &json!({"schema_version": 2, "kind": "early_positive_qualification", "input_hash": input_hash,
                         "qualification": qualification, "monotonic_kind": "official_announcement_by_deadline",
                         "proposed_outcome": "YES", "irreversible": true, "conditions_fully_satisfied": true,
                         "invalidation_clear": true}),
    )
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EarlyResolutionTrigger {
    pub schema_version: i64,
    pub forecast_id: String,
    pub specification_hash: String,
    pub clause_id: String,
    pub evidence: Vec<EvidenceSnapshot>,
    pub source_verifications: Vec<SourceVerification>,
    pub event_at_ms: i64,
    pub observed_at_ms: i64,
    pub qualification: String,
    pub qualifier: AIProvenance,
    pub counter_qualifier: AIProvenance,
    pub event_time_basis: String,
    pub monotonic_kind: String,
    pub proposed_outcome: String,
    pub irreversible: bool,
    pub conditions_fully_satisfied: bool,
    pub invalidation_clear: bool,
}

impl Record for EarlyResolutionTrigger {
    fn validate(&self) -> Result<()> {
        let p = "EarlyResolutionTrigger";
        require(
            self.schema_version == 2,
            &format!("{p}.schema_version: expected constant 2"),
        )?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_id(&format!("{p}.clause_id"), &self.clause_id)?;
        require(
            (1..=16).contains(&self.evidence.len()),
            &format!("{p}.evidence: requires 1..16 entries"),
        )?;
        for item in &self.evidence {
            item.validate()?;
        }
        require(
            (1..=16).contains(&self.source_verifications.len()),
            &format!("{p}.source_verifications: requires 1..16 entries"),
        )?;
        for item in &self.source_verifications {
            item.validate()?;
        }
        check_int(&format!("{p}.event_at_ms"), self.event_at_ms)?;
        check_int(&format!("{p}.observed_at_ms"), self.observed_at_ms)?;
        check_text(&format!("{p}.qualification"), &self.qualification, None)?;
        self.qualifier.validate()?;
        self.counter_qualifier.validate()?;
        require(
            matches!(
                self.event_time_basis.as_str(),
                "published_instant" | "observed_upper_bound"
            ),
            &format!("{p}.event_time_basis: unexpected value"),
        )?;
        require(
            self.monotonic_kind == "official_announcement_by_deadline",
            &format!("{p}.monotonic_kind: expected constant"),
        )?;
        require(
            self.proposed_outcome == "YES",
            &format!("{p}.proposed_outcome: expected constant YES"),
        )?;
        require(
            self.irreversible && self.conditions_fully_satisfied && self.invalidation_clear,
            &format!("{p}: expected constant True flags"),
        )?;
        require(
            self.event_at_ms <= self.observed_at_ms,
            "event cannot follow observation",
        )?;
        if self.event_time_basis == "observed_upper_bound" {
            require(
                self.event_at_ms == self.observed_at_ms,
                "observed upper bound must equal observation time, not a fabricated event timestamp",
            )?;
        }
        let hashes: Vec<String> = self
            .evidence
            .iter()
            .map(EvidenceSnapshot::evidence_hash)
            .collect::<Result<_>>()?;
        require(
            hashes.iter().enumerate().all(|(i, h)| !hashes[..i].contains(h)),
            "trigger evidence must be unique",
        )?;
        let ids: Vec<&str> = self.evidence.iter().map(|e| e.evidence_id.as_str()).collect();
        require(
            ids.iter().enumerate().all(|(i, id)| !ids[..i].contains(id)),
            "trigger evidence IDs must be unique",
        )?;
        let mut verified: Vec<&str> = self
            .source_verifications
            .iter()
            .map(|v| v.evidence_hash.as_str())
            .collect();
        verified.sort();
        verified.dedup();
        let mut expected: Vec<&str> = hashes.iter().map(String::as_str).collect();
        expected.sort();
        require(
            self.source_verifications.len() == self.evidence.len() && verified == expected,
            "trigger verification must cover exact evidence",
        )?;
        for item in &self.evidence {
            require(
                item.collector.is_some(),
                "trigger evidence requires collection provenance",
            )?;
            require(
                item.collected_at_ms <= self.observed_at_ms,
                "trigger collection cannot follow observation",
            )?;
            if self.event_time_basis == "published_instant" {
                require(
                    self.event_at_ms <= item.collected_at_ms,
                    "precise event cannot follow collection",
                )?;
            }
        }
        for verification in &self.source_verifications {
            let snapshot = &self.evidence[hashes
                .iter()
                .position(|h| *h == verification.evidence_hash)
                .expect("covered")];
            require(
                verification.verified && verification.source_id == snapshot.source_id,
                "trigger requires verified exact sources",
            )?;
            require(
                snapshot.collected_at_ms <= verification.verifier.created_at_ms
                    && verification.verifier.created_at_ms <= self.qualifier.created_at_ms,
                "trigger source verification order invalid",
            )?;
        }
        require(
            self.qualifier.task == "AMBIGUITY_JUDGE",
            "early qualification requires semantic ambiguity review",
        )?;
        let input = self.input_hash()?;
        require(
            self.qualifier.input_hash == input,
            "qualification input does not bind exact trigger",
        )?;
        require(
            self.qualifier.output_hash == early_qualification_output_hash(&input, &self.qualification)?,
            "qualification output commitment mismatch",
        )?;
        require(
            self.counter_qualifier.task == "COUNTER_JUDGE",
            "early qualification requires counter-review",
        )?;
        require(
            self.counter_qualifier.provider.to_lowercase() != self.qualifier.provider.to_lowercase(),
            "early counter-review must use an independent provider",
        )?;
        require(
            self.counter_qualifier.input_hash == counter_judge_input_hash(&input, &self.qualifier)?,
            "early counter-review input binding mismatch",
        )?;
        require(
            self.counter_qualifier.output_hash == counter_judge_output_hash(&self.qualifier.output_hash, true)?,
            "early counter-review must affirm exact qualification",
        )?;
        require(
            self.observed_at_ms <= self.qualifier.created_at_ms
                && self.qualifier.created_at_ms <= self.counter_qualifier.created_at_ms,
            "early review time order invalid",
        )
    }
}

impl EarlyResolutionTrigger {
    pub fn input_hash(&self) -> Result<String> {
        early_trigger_input_hash(
            &self.forecast_id,
            &self.specification_hash,
            &self.clause_id,
            &self.evidence,
            &self.source_verifications,
            self.event_at_ms,
            self.observed_at_ms,
            &self.event_time_basis,
        )
    }

    pub fn trigger_hash(&self) -> Result<String> {
        content_hash(self)
    }

    pub fn qualified_at_ms(&self) -> i64 {
        self.counter_qualifier.created_at_ms
    }

    pub fn validate_for(&self, specification: &ForecastSpecification) -> Result<()> {
        require(
            self.specification_hash == specification.specification_hash()?,
            "early trigger targets another specification",
        )?;
        require(
            specification
                .rules
                .iter()
                .any(|r| r.clause_id == self.clause_id && r.outcome == "YES"),
            "early trigger must identify the YES clause",
        )?;
        require(
            specification.open_at_ms <= self.event_at_ms
                && self.event_at_ms <= self.observed_at_ms
                && self.observed_at_ms <= self.qualified_at_ms()
                && self.qualified_at_ms() < specification.close_at_ms,
            "early event and completed review must occur within the original open window",
        )?;
        let sources = &specification.source_policy.primary_sources;
        require(
            self.evidence
                .iter()
                .all(|e| sources.iter().any(|s| s.source_id == e.source_id && s.is_official)),
            "early trigger requires published official primary sources",
        )?;
        require(
            self.evidence.iter().all(|e| {
                let source = sources.iter().find(|s| s.source_id == e.source_id).expect("checked");
                url_hostname(&e.url) == url_hostname(&source.url)
            }),
            "early evidence host differs from official source",
        )
    }
}

/// `EarlyResolution(Resolution)`: the resolution fields plus `schema_version: 2` and `trigger`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EarlyResolution {
    #[serde(flatten)]
    pub resolution: Resolution,
    pub trigger: EarlyResolutionTrigger,
}

impl<'de> Deserialize<'de> for EarlyResolution {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let mut map = Map::<String, Value>::deserialize(deserializer)?;
        let trigger = map
            .remove("trigger")
            .ok_or_else(|| serde::de::Error::missing_field("trigger"))?;
        let trigger: EarlyResolutionTrigger = serde_json::from_value(trigger).map_err(serde::de::Error::custom)?;
        let resolution: Resolution = serde_json::from_value(Value::Object(map)).map_err(serde::de::Error::custom)?;
        Ok(EarlyResolution { resolution, trigger })
    }
}

impl Record for EarlyResolution {
    fn validate(&self) -> Result<()> {
        require(
            self.resolution.schema_version == 2,
            "EarlyResolution.schema_version: expected constant 2",
        )?;
        let mut base = self.resolution.clone();
        base.schema_version = 1;
        base.validate_fields()?;
        self.trigger.validate()?;
        base.validate_rules(&self.decision_input_hash()?)?;
        let r = &self.resolution;
        require(
            r.forecast_id == self.trigger.forecast_id && r.specification_hash == self.trigger.specification_hash,
            "early resolution trigger binding mismatch",
        )?;
        require(
            r.proposed_outcome == "YES" && r.rule_matches == [self.trigger.clause_id.clone()],
            "early resolution permits only the qualified YES clause",
        )?;
        require(
            r.evidence == self.trigger.evidence && r.source_verifications == self.trigger.source_verifications,
            "early resolution requires exact retained trigger evidence and verification",
        )?;
        require(
            r.judge.created_at_ms >= self.trigger.qualified_at_ms(),
            "early resolution judge cannot predate semantic review",
        )
    }
}

impl EarlyResolution {
    pub fn decision_input_hash(&self) -> Result<String> {
        let r = &self.resolution;
        content_hash(
            &json!({"schema_version": 2, "kind": "early_resolution_input", "trigger_hash": self.trigger.trigger_hash()?,
                             "resolution_input_hash": resolution_input_hash(&r.forecast_id, &r.specification_hash, &r.evidence, &r.source_verifications)?}),
        )
    }

    pub fn validate_for(&self, specification: &ForecastSpecification) -> Result<()> {
        self.resolution.validate_binding(specification)?;
        self.trigger.validate_for(specification)?;
        require(
            self.resolution.proposed_at_ms >= self.trigger.qualified_at_ms(),
            "early proposal cannot precede qualification",
        )
    }

    pub fn require_proposable(&self, specification: &ForecastSpecification) -> Result<()> {
        self.validate_for(specification)?;
        self.resolution.require_proposable_after()
    }
}

// ---------------------------------------------------------------- aggregate

/// Fields shared by v1 and v2 snapshots (Python `Forecast`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Forecast {
    pub schema_version: i64,
    pub forecast_id: String,
    pub creator_id: String,
    pub specification: ForecastSpecification,
    pub specification_hash: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub state: String,
    pub revision: i64,
    #[serde(deserialize_with = "required_option")]
    pub published_at_ms: Option<i64>,
    #[serde(deserialize_with = "required_option")]
    pub validation_assessment: Option<ValidationAssessment>,
    #[serde(deserialize_with = "required_option")]
    pub resolution: Option<AnyResolution>,
    #[serde(deserialize_with = "required_option")]
    pub challenge_started_at_ms: Option<i64>,
    #[serde(deserialize_with = "required_option")]
    pub challenge_until_ms: Option<i64>,
    pub disputes: Vec<Dispute>,
    pub dispute_reviews: Vec<DisputeReview>,
    #[serde(deserialize_with = "required_option")]
    pub pause: Option<Pause>,
    #[serde(deserialize_with = "required_option")]
    pub finalized_outcome: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub finalized_resolution_hash: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub latest_event: Option<DomainEvent>,
    #[serde(deserialize_with = "required_option")]
    pub audit_head_hash: Option<String>,
}

/// v2 snapshot: the v1 fields plus the accepted early trigger and upgrade provenance.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ForecastV2 {
    #[serde(flatten)]
    pub base: Forecast,
    pub early_trigger: EarlyResolutionTrigger,
    pub upgrade_source: Forecast,
    pub upgraded_from_state_hash: String,
    pub upgraded_from_event_hash: String,
    pub upgraded_at_revision: i64,
    pub upgraded_at_ms: i64,
}

impl<'de> Deserialize<'de> for ForecastV2 {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let mut map = Map::<String, Value>::deserialize(deserializer)?;
        let mut take = |name: &'static str| map.remove(name).ok_or_else(|| serde::de::Error::missing_field(name));
        let early_trigger = take("early_trigger")?;
        let upgrade_source = take("upgrade_source")?;
        let state_hash = take("upgraded_from_state_hash")?;
        let event_hash = take("upgraded_from_event_hash")?;
        let revision = take("upgraded_at_revision")?;
        let at = take("upgraded_at_ms")?;
        let custom = serde::de::Error::custom;
        Ok(ForecastV2 {
            base: serde_json::from_value(Value::Object(map)).map_err(custom)?,
            early_trigger: serde_json::from_value(early_trigger).map_err(custom)?,
            upgrade_source: serde_json::from_value(upgrade_source).map_err(custom)?,
            upgraded_from_state_hash: serde_json::from_value(state_hash).map_err(custom)?,
            upgraded_from_event_hash: serde_json::from_value(event_hash).map_err(custom)?,
            upgraded_at_revision: serde_json::from_value(revision).map_err(custom)?,
            upgraded_at_ms: serde_json::from_value(at).map_err(custom)?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Snapshot {
    V1(Forecast),
    V2(ForecastV2),
}

impl Snapshot {
    pub fn base(&self) -> &Forecast {
        match self {
            Snapshot::V1(f) => f,
            Snapshot::V2(v) => &v.base,
        }
    }

    pub fn resolution_not_before_ms(&self) -> i64 {
        match self {
            Snapshot::V1(f) => f.specification.close_at_ms,
            Snapshot::V2(v) => v.upgraded_at_ms,
        }
    }

    /// `_snapshot_values`: every field except the audit head pair.
    pub fn snapshot_values(&self) -> Result<Map<String, Value>> {
        let mut map = to_value(self)?
            .as_object()
            .cloned()
            .ok_or_else(|| ValidationError::new("snapshot is not an object"))?;
        map.remove("latest_event");
        map.remove("audit_head_hash");
        Ok(map)
    }

    /// `loads_forecast`: a v1 snapshot, else a v2 snapshot, each strictly validated.
    pub fn from_json(text: &str) -> Result<Snapshot> {
        let value = crate::canonical::parse_strict(text)?;
        Snapshot::from_value(value)
    }

    pub fn from_value(value: Value) -> Result<Snapshot> {
        let original = match serde_json::from_value::<Forecast>(value.clone()) {
            Ok(forecast) => match forecast.validate() {
                Ok(()) => return Ok(Snapshot::V1(forecast)),
                Err(error) => error.to_string(),
            },
            Err(error) => error.to_string(),
        };
        match serde_json::from_value::<ForecastV2>(value) {
            Ok(forecast) => match forecast.validate() {
                Ok(()) => Ok(Snapshot::V2(forecast)),
                Err(newer) => Err(ValidationError::new(format!(
                    "Invalid v1/v2 forecast: {original}; {newer}"
                ))),
            },
            Err(newer) => Err(ValidationError::new(format!(
                "Invalid v1/v2 forecast: {original}; {newer}"
            ))),
        }
    }

    pub fn validate(&self) -> Result<()> {
        match self {
            Snapshot::V1(f) => f.validate(),
            Snapshot::V2(v) => v.validate(),
        }
    }
}

impl Forecast {
    fn validate_fields(&self) -> Result<()> {
        let p = "Forecast";
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_id(&format!("{p}.creator_id"), &self.creator_id)?;
        self.specification.validate()?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_int(&format!("{p}.created_at_ms"), self.created_at_ms)?;
        check_int(&format!("{p}.updated_at_ms"), self.updated_at_ms)?;
        require(
            STATES.contains(&self.state.as_str()),
            &format!("{p}.state: expected one of {STATES:?}"),
        )?;
        check_int(&format!("{p}.revision"), self.revision)?;
        if let Some(value) = self.published_at_ms {
            check_int(&format!("{p}.published_at_ms"), value)?;
        }
        if let Some(value) = &self.validation_assessment {
            value.validate()?;
        }
        if let Some(value) = &self.resolution {
            value.validate()?;
        }
        for (name, value) in [
            ("challenge_started_at_ms", self.challenge_started_at_ms),
            ("challenge_until_ms", self.challenge_until_ms),
        ] {
            if let Some(value) = value {
                check_int(&format!("{p}.{name}"), value)?;
            }
        }
        require(
            self.disputes.len() <= MAX_ACTIVE_DISPUTES,
            &format!("{p}.disputes: allows at most {MAX_ACTIVE_DISPUTES} entries"),
        )?;
        for dispute in &self.disputes {
            dispute.validate()?;
        }
        require(
            self.dispute_reviews.len() <= MAX_ACTIVE_DISPUTES,
            &format!("{p}.dispute_reviews: allows at most {MAX_ACTIVE_DISPUTES} entries"),
        )?;
        for review in &self.dispute_reviews {
            review.validate()?;
        }
        if let Some(pause) = &self.pause {
            pause.validate()?;
        }
        if let Some(outcome) = &self.finalized_outcome {
            require(
                crate::models::OUTCOMES.contains(&outcome.as_str()),
                &format!("{p}.finalized_outcome: expected one of {:?}", crate::models::OUTCOMES),
            )?;
        }
        if let Some(value) = &self.finalized_resolution_hash {
            check_text(&format!("{p}.finalized_resolution_hash"), value, None)?;
        }
        if let Some(event) = &self.latest_event {
            event.validate()?;
        }
        if let Some(value) = &self.audit_head_hash {
            check_text(&format!("{p}.audit_head_hash"), value, None)?;
        }
        Ok(())
    }

    /// `Forecast.validate` given the effective snapshot (v1 or v2) for state-hash and expiry rules.
    fn validate_rules(&self, snapshot: &Snapshot) -> Result<()> {
        require(
            self.specification_hash == self.specification.specification_hash()?,
            "specification hash does not identify specification",
        )?;
        require(
            self.created_at_ms <= self.updated_at_ms,
            "aggregate time runs backwards",
        )?;
        require(
            (self.state == "PAUSED") == self.pause.is_some(),
            "PAUSED state and pause context must agree",
        )?;
        let effective_state = self
            .pause
            .as_ref()
            .map_or(self.state.as_str(), |p| p.previous_state.as_str());
        if let Some(pause) = &self.pause {
            require(
                self.created_at_ms <= pause.paused_at_ms && pause.paused_at_ms <= self.updated_at_ms,
                "pause time is outside aggregate history",
            )?;
        }
        let unpublished = matches!(effective_state, "DRAFT" | "VALIDATING");
        require(
            unpublished == self.published_at_ms.is_none(),
            "published timestamp must agree with lifecycle state",
        )?;
        if let Some(published) = self.published_at_ms {
            require(
                self.created_at_ms <= published && published <= self.updated_at_ms,
                "publication time is outside aggregate history",
            )?;
            require(
                published < self.specification.close_at_ms,
                "forecast cannot be published after expiry",
            )?;
            require(
                self.validation_assessment.is_some(),
                "published forecast requires validation",
            )?;
            let assessment = self.validation_assessment.as_ref().expect("checked");
            assessment.require_publishable(&self.specification)?;
            require(
                assessment.validated_at_ms <= published,
                "publication cannot predate completed validation",
            )?;
        } else if let Some(assessment) = &self.validation_assessment {
            require(
                assessment.specification_hash == self.specification_hash,
                "validation is bound to a different specification",
            )?;
        }
        if let Some(assessment) = &self.validation_assessment {
            require(
                assessment.validated_at_ms <= self.updated_at_ms,
                "validation assessment is in the future",
            )?;
        }
        if effective_state == "VALIDATING" {
            require(
                self.validation_assessment.is_none(),
                "pending validation cannot include a completed assessment",
            )?;
        }
        let pre_resolution = matches!(
            effective_state,
            "DRAFT" | "VALIDATING" | "OPEN" | "LOCKED" | "RESOLVING"
        );
        require(
            pre_resolution == self.resolution.is_none(),
            "resolution presence must agree with lifecycle state",
        )?;
        let not_before = snapshot.resolution_not_before_ms();
        if !matches!(effective_state, "DRAFT" | "VALIDATING" | "OPEN") {
            require(self.updated_at_ms >= not_before, "post-open state requires expiry")?;
        }
        if let Some(resolution) = &self.resolution {
            let base = resolution.base();
            require(
                base.forecast_id == self.forecast_id,
                "resolution belongs to another forecast",
            )?;
            resolution.require_proposable(&self.specification)?;
            require(
                not_before <= base.proposed_at_ms && base.proposed_at_ms <= self.updated_at_ms,
                "resolution proposal must be after expiry and within history",
            )?;
        }
        let challenged = matches!(
            effective_state,
            "CHALLENGE" | "DISPUTED" | "ESCALATED" | "FINALIZED" | "ARCHIVED"
        );
        require(
            challenged == self.challenge_started_at_ms.is_some(),
            "challenge start must agree with lifecycle state",
        )?;
        require(
            challenged == self.challenge_until_ms.is_some(),
            "challenge deadline must agree with lifecycle state",
        )?;
        if challenged {
            let proposal = self
                .resolution
                .as_ref()
                .ok_or_else(|| ValidationError::new("challenge requires resolution"))?;
            let start = self.challenge_started_at_ms.expect("checked");
            let deadline = self.challenge_until_ms.expect("checked");
            require(
                proposal.base().proposed_at_ms <= start && start <= self.updated_at_ms,
                "challenge cannot precede resolution",
            )?;
            require(start < deadline, "challenge duration must be positive")?;
        }
        require(self.disputes.len() <= MAX_ACTIVE_DISPUTES, "too many active disputes")?;
        require(
            self.dispute_reviews.len() <= MAX_ACTIVE_DISPUTES,
            "too many active reviews",
        )?;
        let ids: Vec<&str> = self.disputes.iter().map(|d| d.dispute_id.as_str()).collect();
        require(
            ids.iter().enumerate().all(|(i, id)| !ids[..i].contains(id)),
            "duplicate dispute IDs",
        )?;
        let dispute_hashes: Vec<String> = self.disputes.iter().map(Dispute::dispute_hash).collect::<Result<_>>()?;
        require(
            dispute_hashes
                .iter()
                .enumerate()
                .all(|(i, h)| !dispute_hashes[..i].contains(h)),
            "duplicate dispute commitments",
        )?;
        let review_hashes: Vec<&str> = self.dispute_reviews.iter().map(|r| r.dispute_hash.as_str()).collect();
        require(
            review_hashes
                .iter()
                .enumerate()
                .all(|(i, h)| !review_hashes[..i].contains(h)),
            "duplicate reviews",
        )?;
        if !self.disputes.is_empty() || !self.dispute_reviews.is_empty() {
            require(challenged, "disputes require challenge history")?;
        }
        for dispute in &self.disputes {
            require(
                dispute.forecast_id == self.forecast_id,
                "dispute belongs to another forecast",
            )?;
            let resolution = self
                .resolution
                .as_ref()
                .ok_or_else(|| ValidationError::new("dispute requires resolution"))?;
            dispute.validate_for(&self.specification, resolution.base())?;
            let start = self
                .challenge_started_at_ms
                .ok_or_else(|| ValidationError::new("dispute requires challenge start"))?;
            let deadline = self
                .challenge_until_ms
                .ok_or_else(|| ValidationError::new("dispute requires challenge deadline"))?;
            require(
                start <= dispute.submitted_at_ms && dispute.submitted_at_ms < deadline,
                "dispute was submitted outside challenge window",
            )?;
            require(
                dispute.submitted_at_ms <= self.updated_at_ms,
                "dispute is in the future",
            )?;
        }
        for review in &self.dispute_reviews {
            let index = dispute_hashes
                .iter()
                .position(|h| *h == review.dispute_hash)
                .ok_or_else(|| ValidationError::new("review has no corresponding dispute"))?;
            let dispute = &self.disputes[index];
            let resolution = self
                .resolution
                .as_ref()
                .ok_or_else(|| ValidationError::new("review requires resolution"))?;
            review.require_valid_for(dispute, resolution.base(), &self.specification)?;
            require(
                dispute.submitted_at_ms <= review.reviewed_at_ms && review.reviewed_at_ms <= self.updated_at_ms,
                "review time is outside dispute history",
            )?;
        }
        let material = self.dispute_reviews.iter().any(|r| r.material_conflict);
        if effective_state == "DISPUTED" {
            require(!self.disputes.is_empty(), "DISPUTED requires a dispute")?;
        }
        if effective_state == "ESCALATED" {
            require(
                self.disputes.len() == self.dispute_reviews.len(),
                "ESCALATED requires completed dispute reviews",
            )?;
            require(material, "ESCALATED requires material conflict")?;
        }
        if matches!(effective_state, "CHALLENGE" | "FINALIZED" | "ARCHIVED") {
            require(
                self.disputes.len() == self.dispute_reviews.len(),
                "pending disputes block challenge restoration and finalization",
            )?;
            require(!material, "material conflict blocks retained proposal")?;
        }
        let terminal = matches!(effective_state, "FINALIZED" | "ARCHIVED");
        require(
            terminal == self.finalized_outcome.is_some(),
            "final outcome must agree with state",
        )?;
        require(
            terminal == self.finalized_resolution_hash.is_some(),
            "final resolution commitment must agree with state",
        )?;
        if terminal {
            let proposal = self
                .resolution
                .as_ref()
                .ok_or_else(|| ValidationError::new("finalization requires resolution"))?;
            require(
                self.finalized_outcome.as_deref() == Some(proposal.base().proposed_outcome.as_str()),
                "final outcome differs from resolution",
            )?;
            require(
                self.finalized_resolution_hash.as_deref() == Some(proposal.resolution_hash()?.as_str()),
                "final resolution commitment differs from proposal",
            )?;
            let deadline = self
                .challenge_until_ms
                .ok_or_else(|| ValidationError::new("finalization requires deadline"))?;
            require(
                self.updated_at_ms >= deadline,
                "finalization requires completed challenge window",
            )?;
        }
        if self.revision == 0 {
            require(
                self.state == "DRAFT" && self.updated_at_ms == self.created_at_ms,
                "initial snapshot must be a newly created draft",
            )?;
            require(
                self.latest_event.is_none() && self.audit_head_hash.is_none(),
                "initial snapshot cannot have an audit history",
            )?;
            require(
                self.validation_assessment.is_none(),
                "initial draft cannot contain validation",
            )?;
        } else {
            require(
                self.latest_event.is_some() && self.audit_head_hash.is_some(),
                "mutated snapshot requires audit head",
            )?;
            let event = self.latest_event.as_ref().expect("checked");
            require(
                self.audit_head_hash.as_deref() == Some(content_hash(event)?.as_str()),
                "audit head hash is invalid",
            )?;
            require(
                event.forecast_id == self.forecast_id && event.new_state == self.state,
                "audit event does not identify aggregate state",
            )?;
            require(
                event.revision == self.revision && event.occurred_at_ms == self.updated_at_ms,
                "audit event revision/time differs from aggregate",
            )?;
            require(
                event.specification_hash == self.specification_hash,
                "audit event identifies another specification",
            )?;
            require(
                event.state_hash == content_hash(&snapshot.snapshot_values()?)?,
                "audit state commitment does not identify snapshot",
            )?;
            require(
                (self.revision == 1) == event.previous_event_hash.is_none(),
                "audit predecessor must agree with revision",
            )?;
        }
        Ok(())
    }
}

impl Record for Forecast {
    fn validate(&self) -> Result<()> {
        check_schema_version("Forecast", self.schema_version)?;
        self.validate_fields()?;
        self.validate_rules(&Snapshot::V1(self.clone()))
    }
}

impl Record for ForecastV2 {
    fn validate(&self) -> Result<()> {
        let p = "ForecastV2";
        require(
            self.base.schema_version == 2,
            &format!("{p}.schema_version: expected constant 2"),
        )?;
        self.base.validate_fields()?;
        self.early_trigger.validate()?;
        self.upgrade_source.validate()?;
        check_hash(&format!("{p}.upgraded_from_state_hash"), &self.upgraded_from_state_hash)?;
        check_hash(&format!("{p}.upgraded_from_event_hash"), &self.upgraded_from_event_hash)?;
        check_range(
            &format!("{p}.upgraded_at_revision"),
            self.upgraded_at_revision,
            1,
            MAX_SAFE_INTEGER,
        )?;
        check_int(&format!("{p}.upgraded_at_ms"), self.upgraded_at_ms)?;
        let f = &self.base;
        self.early_trigger.validate_for(&f.specification)?;
        let source = &self.upgrade_source;
        require(
            source.state == "OPEN"
                && source.forecast_id == f.forecast_id
                && source.creator_id == f.creator_id
                && source.specification == f.specification
                && source.created_at_ms == f.created_at_ms
                && source.published_at_ms == f.published_at_ms
                && source.validation_assessment == f.validation_assessment,
            "upgrade must retain the exact published source identity and specification",
        )?;
        require(
            content_hash(source)? == self.upgraded_from_state_hash
                && source.audit_head_hash.as_deref() == Some(self.upgraded_from_event_hash.as_str())
                && source.revision + 1 == self.upgraded_at_revision
                && source.updated_at_ms <= self.upgraded_at_ms,
            "upgrade source history commitments or time are invalid",
        )?;
        require(
            self.early_trigger.forecast_id == f.forecast_id,
            "early trigger belongs to another forecast",
        )?;
        require(
            !matches!(f.state.as_str(), "DRAFT" | "VALIDATING" | "OPEN"),
            "early upgrade permanently closes new participation",
        )?;
        require(
            self.early_trigger.qualified_at_ms() <= self.upgraded_at_ms
                && self.upgraded_at_ms < f.specification.close_at_ms,
            "upgrade must follow review and precede expiry",
        )?;
        require(
            self.upgraded_at_revision <= f.revision && self.upgraded_at_ms <= f.updated_at_ms,
            "upgrade is outside aggregate history",
        )?;
        if f.revision == self.upgraded_at_revision {
            let event = f.latest_event.as_ref();
            require(
                event.is_some_and(|e| {
                    e.command_name == "lock"
                        && e.old_state == "OPEN"
                        && e.previous_event_hash.as_deref() == Some(self.upgraded_from_event_hash.as_str())
                        && e.artifact_hash.as_deref() == self.early_trigger.trigger_hash().ok().as_deref()
                }),
                "upgrade requires explicit OPEN-to-LOCKED trigger audit event",
            )?;
        }
        match &f.resolution {
            Some(AnyResolution::Early(early)) => {
                require(
                    early.trigger == self.early_trigger,
                    "resolution cannot substitute the accepted early trigger",
                )?;
            }
            Some(AnyResolution::Standard(resolution)) => {
                require(
                    resolution.proposed_at_ms >= f.specification.close_at_ms,
                    "ordinary replacement resolution must wait for original expiry",
                )?;
            }
            None => {}
        }
        f.validate_rules(&Snapshot::V2(self.clone()))
    }
}

// ---------------------------------------------------------------- commands

/// Command payloads keyed by `kind`; extra fields belong to the variant.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum Payload {
    #[serde(rename = "edit_specification")]
    EditSpecification {
        schema_version: i64,
        specification: ForecastSpecification,
    },
    #[serde(rename = "begin_validation")]
    BeginValidation { schema_version: i64 },
    #[serde(rename = "reject_validation")]
    RejectValidation {
        schema_version: i64,
        assessment: ValidationAssessment,
    },
    #[serde(rename = "publish")]
    Publish {
        schema_version: i64,
        assessment: ValidationAssessment,
    },
    #[serde(rename = "lock")]
    Lock {
        schema_version: i64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        trigger: Option<EarlyResolutionTrigger>,
    },
    #[serde(rename = "begin_resolution")]
    BeginResolution { schema_version: i64 },
    #[serde(rename = "propose_resolution")]
    ProposeResolution {
        schema_version: i64,
        resolution: AnyResolution,
    },
    #[serde(rename = "begin_challenge")]
    BeginChallenge { schema_version: i64, duration_ms: i64 },
    #[serde(rename = "submit_forecast")]
    SubmitForecast {
        schema_version: i64,
        user_forecast: UserForecast,
    },
    #[serde(rename = "submit_dispute")]
    SubmitDispute { schema_version: i64, dispute: Dispute },
    #[serde(rename = "review_dispute")]
    ReviewDispute { schema_version: i64, review: DisputeReview },
    #[serde(rename = "retain_proposal")]
    RetainProposal { schema_version: i64 },
    #[serde(rename = "escalate")]
    Escalate { schema_version: i64 },
    #[serde(rename = "adjudicate_resolution")]
    AdjudicateResolution {
        schema_version: i64,
        resolution: Resolution,
        adjudicator: AIProvenance,
    },
    #[serde(rename = "finalize")]
    Finalize { schema_version: i64 },
    #[serde(rename = "archive")]
    Archive { schema_version: i64 },
    #[serde(rename = "pause_for_provider_outage")]
    PauseForProviderOutage {
        schema_version: i64,
        configured_providers: Vec<String>,
        unavailable_providers: Vec<String>,
        reason: String,
    },
    #[serde(rename = "resume_after_provider_recovery")]
    ResumeAfterProviderRecovery {
        schema_version: i64,
        recovered_provider: String,
    },
}

impl Payload {
    pub fn kind(&self) -> &'static str {
        match self {
            Payload::EditSpecification { .. } => "edit_specification",
            Payload::BeginValidation { .. } => "begin_validation",
            Payload::RejectValidation { .. } => "reject_validation",
            Payload::Publish { .. } => "publish",
            Payload::Lock { .. } => "lock",
            Payload::BeginResolution { .. } => "begin_resolution",
            Payload::ProposeResolution { .. } => "propose_resolution",
            Payload::BeginChallenge { .. } => "begin_challenge",
            Payload::SubmitForecast { .. } => "submit_forecast",
            Payload::SubmitDispute { .. } => "submit_dispute",
            Payload::ReviewDispute { .. } => "review_dispute",
            Payload::RetainProposal { .. } => "retain_proposal",
            Payload::Escalate { .. } => "escalate",
            Payload::AdjudicateResolution { .. } => "adjudicate_resolution",
            Payload::Finalize { .. } => "finalize",
            Payload::Archive { .. } => "archive",
            Payload::PauseForProviderOutage { .. } => "pause_for_provider_outage",
            Payload::ResumeAfterProviderRecovery { .. } => "resume_after_provider_recovery",
        }
    }

    pub fn validate(&self) -> Result<()> {
        match self {
            Payload::EditSpecification {
                schema_version,
                specification,
            } => {
                check_schema_version("EditSpecification", *schema_version)?;
                specification.validate()
            }
            Payload::RejectValidation {
                schema_version,
                assessment,
            }
            | Payload::Publish {
                schema_version,
                assessment,
            } => {
                check_schema_version("Validation", *schema_version)?;
                assessment.validate()
            }
            Payload::Lock {
                schema_version,
                trigger,
            } => match trigger {
                None => check_schema_version("Lock", *schema_version),
                Some(trigger) => {
                    require(*schema_version == 2, "LockEarly.schema_version: expected constant 2")?;
                    trigger.validate()
                }
            },
            Payload::ProposeResolution {
                schema_version,
                resolution,
            } => {
                match resolution {
                    AnyResolution::Standard(_) => check_schema_version("ProposeResolution", *schema_version)?,
                    AnyResolution::Early(_) => require(
                        *schema_version == 2,
                        "ProposeEarlyResolution.schema_version: expected constant 2",
                    )?,
                }
                resolution.validate()
            }
            Payload::BeginChallenge {
                schema_version,
                duration_ms,
            } => {
                check_schema_version("BeginChallenge", *schema_version)?;
                check_range("BeginChallenge.duration_ms", *duration_ms, 1, MAX_SAFE_INTEGER)?;
                require(*duration_ms > 0, "challenge duration must be positive")
            }
            Payload::SubmitForecast {
                schema_version,
                user_forecast,
            } => {
                check_schema_version("SubmitForecast", *schema_version)?;
                user_forecast.validate()
            }
            Payload::SubmitDispute {
                schema_version,
                dispute,
            } => {
                check_schema_version("SubmitDispute", *schema_version)?;
                dispute.validate()
            }
            Payload::ReviewDispute { schema_version, review } => {
                check_schema_version("ReviewDispute", *schema_version)?;
                review.validate()
            }
            Payload::AdjudicateResolution {
                schema_version,
                resolution,
                adjudicator,
            } => {
                check_schema_version("AdjudicateResolution", *schema_version)?;
                resolution.validate()?;
                adjudicator.validate()
            }
            Payload::PauseForProviderOutage {
                schema_version,
                configured_providers,
                unavailable_providers,
                reason,
            } => {
                check_schema_version("PauseForProviderOutage", *schema_version)?;
                Pause {
                    schema_version: 1,
                    previous_state: "RESOLVING".to_string(),
                    paused_at_ms: 0,
                    configured_providers: configured_providers.clone(),
                    unavailable_providers: unavailable_providers.clone(),
                    reason: reason.clone(),
                }
                .validate()
            }
            Payload::ResumeAfterProviderRecovery {
                schema_version,
                recovered_provider,
            } => {
                check_schema_version("ResumeAfterProviderRecovery", *schema_version)?;
                check_id("ResumeAfterProviderRecovery.recovered_provider", recovered_provider)
            }
            Payload::BeginValidation { schema_version }
            | Payload::BeginResolution { schema_version }
            | Payload::RetainProposal { schema_version }
            | Payload::Escalate { schema_version }
            | Payload::Finalize { schema_version }
            | Payload::Archive { schema_version } => check_schema_version(self.kind(), *schema_version),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Command {
    pub schema_version: i64,
    pub idempotency_key: String,
    pub expected_revision: i64,
    pub payload: Payload,
}

impl Record for Command {
    fn validate(&self) -> Result<()> {
        let v2 = matches!(
            &self.payload,
            Payload::Lock { trigger: Some(_), .. }
                | Payload::ProposeResolution {
                    resolution: AnyResolution::Early(_),
                    ..
                }
        );
        require(
            self.schema_version == if v2 { 2 } else { 1 },
            "Command.schema_version: expected constant",
        )?;
        check_id("Command.idempotency_key", &self.idempotency_key)?;
        check_int("Command.expected_revision", self.expected_revision)?;
        self.payload.validate()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommandReceipt {
    pub schema_version: i64,
    pub forecast_id: String,
    pub idempotency_key: String,
    pub command_hash: String,
    pub revision: i64,
    pub event_hash: String,
    pub accepted_at_ms: i64,
    #[serde(deserialize_with = "required_option")]
    pub accepted_user_forecast: Option<UserForecast>,
}

impl Record for CommandReceipt {
    fn validate(&self) -> Result<()> {
        let p = "CommandReceipt";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_id(&format!("{p}.idempotency_key"), &self.idempotency_key)?;
        check_hash(&format!("{p}.command_hash"), &self.command_hash)?;
        check_int(&format!("{p}.revision"), self.revision)?;
        check_hash(&format!("{p}.event_hash"), &self.event_hash)?;
        check_int(&format!("{p}.accepted_at_ms"), self.accepted_at_ms)?;
        if let Some(submission) = &self.accepted_user_forecast {
            submission.validate()?;
        }
        require(self.revision > 0, "receipt revision must be positive")?;
        if let Some(submission) = &self.accepted_user_forecast {
            require(
                submission.forecast_id == self.forecast_id,
                "receipt forecast submission belongs to another forecast",
            )?;
            require(
                submission.submitted_at_ms == self.accepted_at_ms,
                "accepted submission timestamp must match receipt",
            )?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransitionResult {
    pub forecast: Snapshot,
    pub receipt: CommandReceipt,
    pub events: Vec<DomainEvent>,
}

pub fn create_forecast(
    forecast_id: &str,
    creator_id: &str,
    specification: ForecastSpecification,
    now_ms: i64,
) -> Result<Forecast> {
    let forecast = Forecast {
        schema_version: 1,
        forecast_id: forecast_id.to_string(),
        creator_id: creator_id.to_string(),
        specification_hash: specification.specification_hash()?,
        specification,
        created_at_ms: now_ms,
        updated_at_ms: now_ms,
        state: "DRAFT".to_string(),
        revision: 0,
        published_at_ms: None,
        validation_assessment: None,
        resolution: None,
        challenge_started_at_ms: None,
        challenge_until_ms: None,
        disputes: Vec::new(),
        dispute_reviews: Vec::new(),
        pause: None,
        finalized_outcome: None,
        finalized_resolution_hash: None,
        latest_event: None,
        audit_head_hash: None,
    };
    forecast.validate()?;
    Ok(forecast)
}

pub fn adjudication_input_hash(forecast: &Forecast, replacement: &Resolution) -> Result<String> {
    let proposal = forecast
        .resolution
        .as_ref()
        .ok_or_else(|| ValidationError::new("adjudication requires an existing resolution"))?;
    let reviews: Vec<String> = forecast
        .dispute_reviews
        .iter()
        .map(content_hash)
        .collect::<Result<_>>()?;
    content_hash(
        &json!({"schema_version": 1, "previous_resolution_hash": proposal.resolution_hash()?,
                         "dispute_review_hashes": reviews, "replacement_resolution_hash": replacement.resolution_hash()?}),
    )
}

/// The field changes a command applies before `_commit_transition` seals them.
struct Changes {
    values: Map<String, Value>,
    artifact_hash: Option<String>,
    effects: Vec<String>,
    accepted_submission: Option<UserForecast>,
    upgrade_to_v2: bool,
}

fn set<T: Serialize>(values: &mut Map<String, Value>, key: &str, value: &T) -> Result<()> {
    values.insert(key.to_string(), to_value(value)?);
    Ok(())
}

/// `apply_command` / `apply_early_command`: one decided command, persisted by the caller with CAS.
pub fn apply_command(
    snapshot: &Snapshot,
    command: &Command,
    now_ms: i64,
    prior_receipt: Option<&CommandReceipt>,
) -> Transition<TransitionResult> {
    command.validate()?;
    require(
        (0..=MAX_SAFE_INTEGER).contains(&now_ms),
        "now_ms must be a portable nonnegative integer",
    )?;
    let forecast = snapshot.base();
    let command_hash = content_hash(command)?;
    if let Some(receipt) = prior_receipt {
        if receipt.forecast_id != forecast.forecast_id
            || receipt.idempotency_key != command.idempotency_key
            || receipt.command_hash != command_hash
        {
            return Err(LifecycleError::Idempotency(
                "idempotency key was previously used for a different command".to_string(),
            ));
        }
        require(
            receipt.revision <= forecast.revision,
            "receipt is ahead of current aggregate",
        )?;
        require(
            receipt.revision == command.expected_revision + 1,
            "receipt revision does not follow the accepted command revision",
        )?;
        let expected = match &command.payload {
            Payload::SubmitForecast { user_forecast, .. } => Some(user_forecast.clone()),
            _ => None,
        };
        require(
            receipt.accepted_user_forecast == expected,
            "receipt accepted submission does not match the original command payload",
        )?;
        return Ok(TransitionResult {
            forecast: snapshot.clone(),
            receipt: receipt.clone(),
            events: Vec::new(),
        });
    }
    if let Payload::Lock {
        trigger: Some(trigger), ..
    } = &command.payload
    {
        return apply_early_lock(snapshot, command, trigger, now_ms);
    }
    if command.expected_revision != forecast.revision {
        return Err(LifecycleError::Concurrency(
            "expected revision does not match current forecast revision".to_string(),
        ));
    }
    guard(
        now_ms >= forecast.updated_at_ms,
        "command cannot backdate aggregate history",
    )?;
    guard(forecast.revision < MAX_SAFE_INTEGER, "revision overflow")?;
    let kind = command.payload.kind();
    let allowed = allowed_states(kind).expect("known command");
    guard(
        allowed.contains(&forecast.state.as_str()),
        &format!("{kind} is not permitted from {}", forecast.state),
    )?;
    let mut changes = Changes {
        values: Map::new(),
        artifact_hash: None,
        effects: Vec::new(),
        accepted_submission: None,
        upgrade_to_v2: false,
    };
    let values = &mut changes.values;
    match &command.payload {
        Payload::EditSpecification { specification, .. } => {
            set(values, "specification", specification)?;
            set(values, "specification_hash", &specification.specification_hash()?)?;
            values.insert("validation_assessment".to_string(), Value::Null);
            changes.artifact_hash = Some(specification.specification_hash()?);
        }
        Payload::BeginValidation { .. } => {
            set(values, "state", &"VALIDATING")?;
            values.insert("validation_assessment".to_string(), Value::Null);
        }
        Payload::RejectValidation { assessment, .. } => {
            guard(
                assessment.validated_at_ms <= now_ms,
                "validation result is in the future",
            )?;
            guard(
                assessment.specification_hash == forecast.specification_hash,
                "validation assessment targets a different specification",
            )?;
            if assessment.require_publishable(&forecast.specification).is_ok() {
                return Err(LifecycleError::Transition(
                    "publishable validation cannot be recorded as rejected".to_string(),
                ));
            }
            set(values, "state", &"DRAFT")?;
            set(values, "validation_assessment", assessment)?;
            changes.artifact_hash = Some(content_hash(assessment)?);
        }
        Payload::Publish { assessment, .. } => {
            guard(
                now_ms < forecast.specification.close_at_ms,
                "cannot publish an expired forecast",
            )?;
            assessment.require_publishable(&forecast.specification)?;
            set(values, "state", &"OPEN")?;
            set(values, "published_at_ms", &now_ms)?;
            set(values, "validation_assessment", assessment)?;
            changes.artifact_hash = Some(content_hash(assessment)?);
        }
        Payload::Lock { .. } => {
            guard(
                now_ms >= forecast.specification.close_at_ms,
                "cannot lock before expiry",
            )?;
            set(values, "state", &"LOCKED")?;
        }
        Payload::BeginResolution { .. } => {
            guard(
                now_ms >= snapshot.resolution_not_before_ms(),
                "cannot resolve before expiry",
            )?;
            set(values, "state", &"RESOLVING")?;
        }
        Payload::ProposeResolution { resolution, .. } => {
            guard(
                resolution.base().proposed_at_ms == now_ms,
                "proposal time must match command time",
            )?;
            set(values, "state", &"PROPOSED")?;
            set(values, "resolution", resolution)?;
            changes.artifact_hash = Some(resolution.resolution_hash()?);
        }
        Payload::BeginChallenge { duration_ms, .. } => {
            let deadline = now_ms + duration_ms;
            guard(deadline <= MAX_SAFE_INTEGER, "challenge deadline overflow")?;
            set(values, "state", &"CHALLENGE")?;
            set(values, "challenge_started_at_ms", &now_ms)?;
            set(values, "challenge_until_ms", &deadline)?;
        }
        Payload::SubmitForecast { user_forecast, .. } => {
            let spec = &forecast.specification;
            guard(
                spec.open_at_ms <= now_ms && now_ms < spec.close_at_ms,
                "forecast submissions require the open time window",
            )?;
            guard(
                user_forecast.forecast_id == forecast.forecast_id
                    && user_forecast.specification_hash == forecast.specification_hash,
                "submission targets a different forecast or specification",
            )?;
            guard(
                user_forecast.submitted_at_ms == now_ms,
                "submission time must match command time",
            )?;
            changes.accepted_submission = Some(user_forecast.clone());
            changes.artifact_hash = Some(content_hash(user_forecast)?);
        }
        Payload::SubmitDispute { dispute, .. } => {
            let deadline = forecast
                .challenge_until_ms
                .ok_or_else(|| ValidationError::new("dispute requires deadline"))?;
            guard(now_ms < deadline, "dispute window has closed")?;
            guard(
                dispute.submitted_at_ms == now_ms,
                "dispute time must match command time",
            )?;
            guard(
                forecast.disputes.len() < MAX_ACTIVE_DISPUTES,
                "active dispute capacity reached",
            )?;
            guard(
                forecast.disputes.iter().all(|d| d.dispute_id != dispute.dispute_id),
                "dispute ID already exists",
            )?;
            let mut disputes = forecast.disputes.clone();
            disputes.push(dispute.clone());
            set(values, "state", &"DISPUTED")?;
            set(values, "disputes", &disputes)?;
            changes.artifact_hash = Some(dispute.dispute_hash()?);
        }
        Payload::ReviewDispute { review, .. } => {
            guard(review.reviewed_at_ms == now_ms, "review time must match command time")?;
            guard(
                forecast
                    .dispute_reviews
                    .iter()
                    .all(|r| r.dispute_hash != review.dispute_hash),
                "dispute has already been reviewed",
            )?;
            let mut reviews = forecast.dispute_reviews.clone();
            reviews.push(review.clone());
            set(values, "dispute_reviews", &reviews)?;
            changes.artifact_hash = Some(content_hash(review)?);
        }
        Payload::RetainProposal { .. } => {
            guard(
                forecast.disputes.len() == forecast.dispute_reviews.len(),
                "every dispute requires a completed review",
            )?;
            guard(
                !forecast.dispute_reviews.iter().any(|r| r.material_conflict),
                "material conflict requires escalation",
            )?;
            set(values, "state", &"CHALLENGE")?;
            changes.artifact_hash = Some(
                forecast
                    .resolution
                    .as_ref()
                    .ok_or_else(|| ValidationError::new("command requires resolution"))?
                    .resolution_hash()?,
            );
        }
        Payload::Escalate { .. } => {
            guard(
                forecast.disputes.len() == forecast.dispute_reviews.len(),
                "complete all pending dispute reviews before escalation",
            )?;
            guard(
                forecast.dispute_reviews.iter().any(|r| r.material_conflict),
                "escalation requires a material reviewed conflict",
            )?;
            set(values, "state", &"ESCALATED")?;
            changes.artifact_hash = Some(
                forecast
                    .resolution
                    .as_ref()
                    .ok_or_else(|| ValidationError::new("command requires resolution"))?
                    .resolution_hash()?,
            );
        }
        Payload::AdjudicateResolution {
            resolution,
            adjudicator,
            ..
        } => {
            let original = forecast
                .resolution
                .as_ref()
                .ok_or_else(|| ValidationError::new("adjudication requires resolution"))?
                .base()
                .clone();
            guard(
                forecast.disputes.len() == forecast.dispute_reviews.len(),
                "adjudication requires all pending dispute reviews",
            )?;
            guard(
                resolution.proposed_at_ms == now_ms,
                "adjudicated proposal time must match command",
            )?;
            guard(
                adjudicator.task == "ADJUDICATION",
                "independent adjudication task is required",
            )?;
            let originals = [
                original.judge.provider.to_lowercase(),
                original.counter_judge.provider.to_lowercase(),
            ];
            guard(
                !originals.contains(&adjudicator.provider.to_lowercase()),
                "adjudication must use a provider independent from original judges",
            )?;
            guard(
                adjudicator.input_hash == adjudication_input_hash(forecast, resolution)?,
                "adjudication input does not bind proposal and dispute reviews",
            )?;
            guard(
                adjudicator.output_hash == resolution.resolution_hash()?,
                "adjudication output does not identify replacement resolution",
            )?;
            guard(
                adjudicator.created_at_ms == now_ms,
                "adjudication time must match command",
            )?;
            set(values, "state", &"PROPOSED")?;
            set(values, "resolution", resolution)?;
            values.insert("challenge_started_at_ms".to_string(), Value::Null);
            values.insert("challenge_until_ms".to_string(), Value::Null);
            values.insert("disputes".to_string(), json!([]));
            values.insert("dispute_reviews".to_string(), json!([]));
            changes.artifact_hash = Some(content_hash(&command.payload)?);
        }
        Payload::Finalize { .. } => {
            let original = forecast
                .resolution
                .as_ref()
                .ok_or_else(|| ValidationError::new("finalization requires resolution"))?;
            let deadline = forecast
                .challenge_until_ms
                .ok_or_else(|| ValidationError::new("finalization requires deadline"))?;
            guard(now_ms >= deadline, "challenge window has not ended")?;
            guard(
                forecast.disputes.len() == forecast.dispute_reviews.len(),
                "pending disputes block finalization",
            )?;
            guard(
                !forecast.dispute_reviews.iter().any(|r| r.material_conflict),
                "material disputes block finalization",
            )?;
            set(values, "state", &"FINALIZED")?;
            set(values, "finalized_outcome", &original.base().proposed_outcome)?;
            set(values, "finalized_resolution_hash", &original.resolution_hash()?)?;
            changes.artifact_hash = Some(original.resolution_hash()?);
            changes.effects = EFFECTS.iter().map(|e| e.to_string()).collect();
        }
        Payload::Archive { .. } => {
            set(values, "state", &"ARCHIVED")?;
            changes.artifact_hash = forecast.finalized_resolution_hash.clone();
        }
        Payload::PauseForProviderOutage {
            configured_providers,
            unavailable_providers,
            reason,
            ..
        } => {
            let pause = Pause {
                schema_version: 1,
                previous_state: forecast.state.clone(),
                paused_at_ms: now_ms,
                configured_providers: configured_providers.clone(),
                unavailable_providers: unavailable_providers.clone(),
                reason: reason.clone(),
            };
            pause.validate()?;
            set(values, "state", &"PAUSED")?;
            set(values, "pause", &pause)?;
            changes.artifact_hash = Some(content_hash(&pause)?);
        }
        Payload::ResumeAfterProviderRecovery { recovered_provider, .. } => {
            let pause = forecast
                .pause
                .as_ref()
                .ok_or_else(|| ValidationError::new("recovery requires pause context"))?;
            guard(
                pause.configured_providers.contains(recovered_provider),
                "recovered provider is not part of configured routing policy",
            )?;
            set(values, "state", &pause.previous_state)?;
            values.insert("pause".to_string(), Value::Null);
            if let Some(until) = forecast.challenge_until_ms {
                let deadline = until + now_ms - pause.paused_at_ms;
                guard(deadline <= MAX_SAFE_INTEGER, "resumed challenge deadline overflow")?;
                set(values, "challenge_until_ms", &deadline)?;
            }
            changes.artifact_hash = Some(content_hash(&command.payload)?);
        }
    }
    commit_transition(snapshot, command, now_ms, changes)
}

fn apply_early_lock(
    snapshot: &Snapshot,
    command: &Command,
    trigger: &EarlyResolutionTrigger,
    now_ms: i64,
) -> Transition<TransitionResult> {
    let forecast = snapshot.base();
    if command.expected_revision != forecast.revision {
        return Err(LifecycleError::Concurrency(
            "expected revision does not match current forecast revision".to_string(),
        ));
    }
    let v1 = matches!(snapshot, Snapshot::V1(_));
    if !v1
        || forecast.state != "OPEN"
        || !(forecast.updated_at_ms <= now_ms && now_ms < forecast.specification.close_at_ms)
        || forecast.revision >= MAX_SAFE_INTEGER
    {
        return Err(LifecycleError::Transition(
            "early upgrade requires an unexpired v1 OPEN forecast and current time".to_string(),
        ));
    }
    trigger.validate_for(&forecast.specification)?;
    require(
        trigger.forecast_id == forecast.forecast_id && trigger.qualified_at_ms() <= now_ms,
        "early trigger must belong to forecast and have completed review",
    )?;
    require(
        forecast.audit_head_hash.is_some(),
        "upgrade requires existing audit chain",
    )?;
    let mut values = Map::new();
    values.insert("schema_version".to_string(), json!(2));
    set(&mut values, "state", &"LOCKED")?;
    set(&mut values, "early_trigger", trigger)?;
    set(&mut values, "upgrade_source", forecast)?;
    set(&mut values, "upgraded_from_state_hash", &content_hash(forecast)?)?;
    set(
        &mut values,
        "upgraded_from_event_hash",
        forecast.audit_head_hash.as_ref().expect("checked"),
    )?;
    set(&mut values, "upgraded_at_revision", &(forecast.revision + 1))?;
    set(&mut values, "upgraded_at_ms", &now_ms)?;
    let changes = Changes {
        values,
        artifact_hash: Some(trigger.trigger_hash()?),
        effects: Vec::new(),
        accepted_submission: None,
        upgrade_to_v2: true,
    };
    commit_transition(snapshot, command, now_ms, changes)
}

fn commit_transition(
    snapshot: &Snapshot,
    command: &Command,
    now_ms: i64,
    changes: Changes,
) -> Transition<TransitionResult> {
    let forecast = snapshot.base();
    let command_hash = content_hash(command)?;
    let mut values = snapshot.snapshot_values()?;
    for (key, value) in changes.values {
        values.insert(key, value);
    }
    values.insert("updated_at_ms".to_string(), json!(now_ms));
    values.insert("revision".to_string(), json!(forecast.revision + 1));
    let new_state = values["state"].as_str().unwrap_or("").to_string();
    let event = DomainEvent {
        schema_version: 1,
        forecast_id: forecast.forecast_id.clone(),
        command_id: command.idempotency_key.clone(),
        command_name: command.payload.kind().to_string(),
        old_state: forecast.state.clone(),
        new_state,
        revision: forecast.revision + 1,
        occurred_at_ms: now_ms,
        specification_hash: values["specification_hash"].as_str().unwrap_or("").to_string(),
        state_hash: content_hash(&values)?,
        artifact_hash: changes.artifact_hash,
        previous_event_hash: forecast.audit_head_hash.clone(),
        effects: changes.effects,
    };
    let event_hash = content_hash(&event)?;
    values.insert("latest_event".to_string(), to_value(&event)?);
    values.insert("audit_head_hash".to_string(), json!(event_hash));
    let value = Value::Object(values);
    let updated = if changes.upgrade_to_v2 || matches!(snapshot, Snapshot::V2(_)) {
        let v2: ForecastV2 = serde_json::from_value(value).map_err(|e| ValidationError::new(e.to_string()))?;
        v2.validate()?;
        Snapshot::V2(v2)
    } else {
        let v1: Forecast = serde_json::from_value(value).map_err(|e| ValidationError::new(e.to_string()))?;
        v1.validate()?;
        Snapshot::V1(v1)
    };
    let receipt = CommandReceipt {
        schema_version: 1,
        forecast_id: forecast.forecast_id.clone(),
        idempotency_key: command.idempotency_key.clone(),
        command_hash,
        revision: forecast.revision + 1,
        event_hash,
        accepted_at_ms: now_ms,
        accepted_user_forecast: changes.accepted_submission,
    };
    receipt.validate()?;
    Ok(TransitionResult {
        forecast: updated,
        receipt,
        events: vec![event],
    })
}
