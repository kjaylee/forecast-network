//! Immutable forecasting records and their invariants (`forecast_domain.models`). Hashes commit
//! to complete records; every `validate()` mirrors the Python reference message for message.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::errors::{require, Result, ValidationError};
use crate::fields::{
    check_enum, check_hash, check_id, check_range, check_schema_version, check_text, required_option, Record,
};
use crate::{content_hash, MAX_SAFE_INTEGER};

pub const OUTCOMES: [&str; 3] = ["YES", "NO", "INVALID"];
pub const CHOICES: [&str; 2] = ["YES", "NO"];
pub const CATEGORIES: [&str; 7] = [
    "TECHNOLOGY",
    "CRYPTO",
    "SCIENCE",
    "ENTERTAINMENT",
    "WORLD",
    "SPORTS",
    "OTHER",
];
pub const AI_TASKS: [&str; 10] = [
    "MARKET_COMPILER",
    "AMBIGUITY_JUDGE",
    "DUPLICATE_DETECTOR",
    "SOURCE_VERIFIER",
    "EVIDENCE_COLLECTOR",
    "RESOLUTION_JUDGE",
    "COUNTER_JUDGE",
    "DISPUTE_ANALYST",
    "INDEPENDENT_REJUDGE",
    "ADJUDICATION",
];
pub const CONFLICT_STATUSES: [&str; 3] = ["CLEAR", "RESOLVED", "UNRESOLVED"];
pub const REVIEW_DISPOSITIONS: [&str; 3] = ["RETAIN_PROPOSAL", "MATERIAL_CONFLICT", "INVALID_EVIDENCE"];

fn hash<T: Serialize>(value: &T) -> Result<String> {
    content_hash(value)
}

fn count(path: &str, value: i64) -> Result<()> {
    check_range(path, value, 0, MAX_SAFE_INTEGER)
}

fn bp(path: &str, value: i64) -> Result<()> {
    check_range(path, value, 0, 10000)
}

/// `urlsplit(value).hostname` (lower-cased, brackets stripped) or None when the URL is malformed.
pub fn url_hostname(value: &str) -> Option<String> {
    let parsed = url::Url::parse(value).ok()?;
    parsed
        .host_str()
        .map(|h| h.trim_start_matches('[').trim_end_matches(']').to_lowercase())
}

fn https_url(value: &str) -> Result<()> {
    let parsed = url::Url::parse(value).map_err(|_| ValidationError::new("source URL is malformed"))?;
    let port_ok = parsed.port().is_none_or(|p| p >= 1);
    require(
        parsed.scheme() == "https"
            && parsed.host_str().is_some_and(|h| !h.is_empty())
            && parsed.username().is_empty()
            && parsed.password().is_none()
            && parsed.fragment().is_none()
            && !value.contains('#')
            && !value.chars().any(char::is_whitespace)
            && port_ok,
        "source URL must be absolute HTTPS without credentials, whitespace or fragment",
    )
}

fn unique<T: PartialEq>(items: &[T]) -> bool {
    items.iter().enumerate().all(|(i, item)| !items[..i].contains(item))
}

// ---------------------------------------------------------------- specification

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleClause {
    pub schema_version: i64,
    pub clause_id: String,
    pub outcome: String,
    pub condition: String,
}

impl Record for RuleClause {
    fn validate(&self) -> Result<()> {
        check_schema_version("RuleClause", self.schema_version)?;
        check_id("RuleClause.clause_id", &self.clause_id)?;
        check_enum("RuleClause.outcome", &self.outcome, &OUTCOMES)?;
        check_text("RuleClause.condition", &self.condition, None)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub schema_version: i64,
    pub source_id: String,
    pub name: String,
    pub url: String,
    pub is_official: bool,
}

impl Record for Source {
    fn validate(&self) -> Result<()> {
        check_schema_version("Source", self.schema_version)?;
        check_id("Source.source_id", &self.source_id)?;
        check_text("Source.name", &self.name, None)?;
        check_text("Source.url", &self.url, None)?;
        https_url(&self.url)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourcePolicy {
    pub schema_version: i64,
    pub primary_sources: Vec<Source>,
    pub fallback_sources: Vec<Source>,
}

impl Record for SourcePolicy {
    fn validate(&self) -> Result<()> {
        check_schema_version("SourcePolicy", self.schema_version)?;
        require(
            !self.primary_sources.is_empty(),
            "SourcePolicy.primary_sources: requires at least 1 entries",
        )?;
        for source in self.sources() {
            source.validate()?;
        }
        require(
            self.primary_sources.iter().all(|s| s.is_official),
            "primary resolution sources must be official",
        )?;
        let ids: Vec<&str> = self.sources().map(|s| s.source_id.as_str()).collect();
        require(unique(&ids), "source IDs must be unique across the policy")?;
        let urls: Vec<&str> = self.sources().map(|s| s.url.as_str()).collect();
        require(unique(&urls), "source URLs must be unique across the policy")
    }
}

impl SourcePolicy {
    pub fn sources(&self) -> impl Iterator<Item = &Source> {
        self.primary_sources.iter().chain(self.fallback_sources.iter())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DuplicateCandidate {
    pub schema_version: i64,
    pub forecast_id: String,
    pub specification_hash: String,
    pub similarity_bp: i64,
    pub materially_different_rules: bool,
    pub explanation: String,
}

impl Record for DuplicateCandidate {
    fn validate(&self) -> Result<()> {
        let p = "DuplicateCandidate";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        bp(&format!("{p}.similarity_bp"), self.similarity_bp)?;
        check_text(&format!("{p}.explanation"), &self.explanation, None)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ForecastSpecification {
    pub schema_version: i64,
    pub canonical_question: String,
    pub rules: Vec<RuleClause>,
    pub open_at_ms: i64,
    pub close_at_ms: i64,
    pub source_policy: SourcePolicy,
    pub invalidation_rules: Vec<String>,
    pub category: String,
    pub share_title: String,
    pub ambiguity_score_bp: i64,
    pub duplicate_candidates: Vec<DuplicateCandidate>,
}

impl Record for ForecastSpecification {
    fn validate(&self) -> Result<()> {
        let p = "ForecastSpecification";
        check_schema_version(p, self.schema_version)?;
        check_text(&format!("{p}.canonical_question"), &self.canonical_question, None)?;
        require(self.rules.len() == 3, &format!("{p}.rules: requires exactly 3 entries"))?;
        for rule in &self.rules {
            rule.validate()?;
        }
        count(&format!("{p}.open_at_ms"), self.open_at_ms)?;
        count(&format!("{p}.close_at_ms"), self.close_at_ms)?;
        self.source_policy.validate()?;
        for (index, rule) in self.invalidation_rules.iter().enumerate() {
            check_text(&format!("{p}.invalidation_rules[{index}]"), rule, None)?;
        }
        check_enum(&format!("{p}.category"), &self.category, &CATEGORIES)?;
        check_text(&format!("{p}.share_title"), &self.share_title, None)?;
        bp(&format!("{p}.ambiguity_score_bp"), self.ambiguity_score_bp)?;
        for candidate in &self.duplicate_candidates {
            candidate.validate()?;
        }
        require(self.open_at_ms < self.close_at_ms, "opening must precede closing")?;
        let mut outcomes: Vec<&str> = self.rules.iter().map(|r| r.outcome.as_str()).collect();
        outcomes.sort();
        outcomes.dedup();
        require(
            outcomes == ["INVALID", "NO", "YES"],
            "specification requires one YES, NO and INVALID clause",
        )?;
        require(unique(&self.clause_ids()), "clause IDs must be unique")?;
        require(unique(&self.invalidation_rules), "invalidation rules must be unique")?;
        let candidates: Vec<&str> = self
            .duplicate_candidates
            .iter()
            .map(|c| c.forecast_id.as_str())
            .collect();
        require(unique(&candidates), "duplicate candidate forecast IDs must be unique")
    }
}

impl ForecastSpecification {
    pub fn specification_hash(&self) -> Result<String> {
        hash(self)
    }

    pub fn clause_ids(&self) -> Vec<&str> {
        self.rules.iter().map(|r| r.clause_id.as_str()).collect()
    }

    pub fn source_ids(&self) -> Vec<&str> {
        self.source_policy.sources().map(|s| s.source_id.as_str()).collect()
    }

    pub fn validate_rule_references(&self, clause_ids: &[&str]) -> Result<()> {
        let known = self.clause_ids();
        require(
            clause_ids.iter().all(|id| known.contains(id)),
            "unknown specification rule clause",
        )
    }
}

// ---------------------------------------------------------------- AI provenance and validation

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AIProvenance {
    pub schema_version: i64,
    pub task: String,
    pub provider: String,
    pub model: String,
    pub model_version: String,
    pub policy_version: String,
    pub input_hash: String,
    pub output_hash: String,
    pub created_at_ms: i64,
}

impl Record for AIProvenance {
    fn validate(&self) -> Result<()> {
        let p = "AIProvenance";
        check_schema_version(p, self.schema_version)?;
        check_enum(&format!("{p}.task"), &self.task, &AI_TASKS)?;
        check_id(&format!("{p}.provider"), &self.provider)?;
        check_text(&format!("{p}.model"), &self.model, None)?;
        check_text(&format!("{p}.model_version"), &self.model_version, None)?;
        check_text(&format!("{p}.policy_version"), &self.policy_version, None)?;
        check_hash(&format!("{p}.input_hash"), &self.input_hash)?;
        check_hash(&format!("{p}.output_hash"), &self.output_hash)?;
        count(&format!("{p}.created_at_ms"), self.created_at_ms)
    }
}

pub fn ambiguity_output_hash(
    specification_hash: &str,
    objectively_resolvable: bool,
    ambiguity_passed: bool,
    ambiguity_limit_bp: i64,
    explanation: &str,
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "ambiguity_output", "specification_hash": specification_hash,
                 "objectively_resolvable": objectively_resolvable, "ambiguity_passed": ambiguity_passed,
                 "ambiguity_limit_bp": ambiguity_limit_bp, "explanation": explanation}),
    )
}

pub fn duplicate_output_hash(
    specification_hash: &str,
    duplicate_check_completed: bool,
    duplicate_similarity_threshold_bp: i64,
    explanation: &str,
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "duplicate_output", "specification_hash": specification_hash,
                 "duplicate_check_completed": duplicate_check_completed,
                 "duplicate_similarity_threshold_bp": duplicate_similarity_threshold_bp, "explanation": explanation}),
    )
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidationAssessment {
    pub schema_version: i64,
    pub specification_hash: String,
    pub deterministic_check_version: String,
    pub deterministic_passed: bool,
    pub objectively_resolvable: bool,
    pub ambiguity_passed: bool,
    pub duplicate_check_completed: bool,
    pub ambiguity_limit_bp: i64,
    pub duplicate_similarity_threshold_bp: i64,
    pub compiler: AIProvenance,
    pub ambiguity_judge: AIProvenance,
    pub duplicate_detector: AIProvenance,
    pub validated_at_ms: i64,
    pub explanation: String,
}

impl Record for ValidationAssessment {
    fn validate(&self) -> Result<()> {
        let p = "ValidationAssessment";
        check_schema_version(p, self.schema_version)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_text(
            &format!("{p}.deterministic_check_version"),
            &self.deterministic_check_version,
            None,
        )?;
        bp(&format!("{p}.ambiguity_limit_bp"), self.ambiguity_limit_bp)?;
        check_range(
            &format!("{p}.duplicate_similarity_threshold_bp"),
            self.duplicate_similarity_threshold_bp,
            1,
            10000,
        )?;
        self.compiler.validate()?;
        self.ambiguity_judge.validate()?;
        self.duplicate_detector.validate()?;
        count(&format!("{p}.validated_at_ms"), self.validated_at_ms)?;
        check_text(&format!("{p}.explanation"), &self.explanation, None)?;
        require(
            self.compiler.task == "MARKET_COMPILER",
            "compiler provenance task mismatch",
        )?;
        require(
            self.compiler.output_hash == self.specification_hash,
            "compiler output must bind the exact specification",
        )?;
        for (provenance, task) in [
            (&self.ambiguity_judge, "AMBIGUITY_JUDGE"),
            (&self.duplicate_detector, "DUPLICATE_DETECTOR"),
        ] {
            require(provenance.task == task, "validation provenance task mismatch")?;
            require(
                provenance.input_hash == self.specification_hash,
                "validation input must bind the exact specification",
            )?;
        }
        require(
            [&self.compiler, &self.ambiguity_judge, &self.duplicate_detector]
                .iter()
                .all(|item| item.created_at_ms <= self.validated_at_ms),
            "validation cannot predate its AI decisions",
        )?;
        require(
            self.ambiguity_judge.output_hash
                == ambiguity_output_hash(
                    &self.specification_hash,
                    self.objectively_resolvable,
                    self.ambiguity_passed,
                    self.ambiguity_limit_bp,
                    &self.explanation,
                )?,
            "ambiguity decision output commitment mismatch",
        )?;
        require(
            self.duplicate_detector.output_hash
                == duplicate_output_hash(
                    &self.specification_hash,
                    self.duplicate_check_completed,
                    self.duplicate_similarity_threshold_bp,
                    &self.explanation,
                )?,
            "duplicate decision output commitment mismatch",
        )
    }
}

impl ValidationAssessment {
    pub fn require_publishable(&self, specification: &ForecastSpecification) -> Result<()> {
        require(
            self.specification_hash == specification.specification_hash()?,
            "validation proof refers to a different specification",
        )?;
        require(
            self.deterministic_passed
                && self.objectively_resolvable
                && self.ambiguity_passed
                && self.duplicate_check_completed,
            "all publication validation checks must pass",
        )?;
        require(
            specification.ambiguity_score_bp <= self.ambiguity_limit_bp,
            "specification ambiguity exceeds its validated limit",
        )?;
        require(
            specification
                .duplicate_candidates
                .iter()
                .all(|c| c.similarity_bp < self.duplicate_similarity_threshold_bp || c.materially_different_rules),
            "materially equivalent duplicate cannot publish",
        )
    }
}

// ---------------------------------------------------------------- submissions and evidence

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UserForecast {
    pub schema_version: i64,
    pub forecaster_id: String,
    pub forecast_id: String,
    pub specification_hash: String,
    pub outcome: String,
    pub confidence: i64,
    pub submitted_at_ms: i64,
}

impl Record for UserForecast {
    fn validate(&self) -> Result<()> {
        let p = "UserForecast";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.forecaster_id"), &self.forecaster_id)?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_enum(&format!("{p}.outcome"), &self.outcome, &CHOICES)?;
        check_range(&format!("{p}.confidence"), self.confidence, 0, 100)?;
        count(&format!("{p}.submitted_at_ms"), self.submitted_at_ms)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceSnapshot {
    pub schema_version: i64,
    pub evidence_id: String,
    pub source_id: String,
    pub url: String,
    pub content_sha256: String,
    pub snapshot_uri: String,
    pub collected_at_ms: i64,
    #[serde(deserialize_with = "required_option")]
    pub collector: Option<AIProvenance>,
}

impl Record for EvidenceSnapshot {
    fn validate(&self) -> Result<()> {
        let p = "EvidenceSnapshot";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.evidence_id"), &self.evidence_id)?;
        check_id(&format!("{p}.source_id"), &self.source_id)?;
        check_text(&format!("{p}.url"), &self.url, None)?;
        check_hash(&format!("{p}.content_sha256"), &self.content_sha256)?;
        check_text(&format!("{p}.snapshot_uri"), &self.snapshot_uri, None)?;
        count(&format!("{p}.collected_at_ms"), self.collected_at_ms)?;
        if let Some(collector) = &self.collector {
            collector.validate()?;
        }
        https_url(&self.url)?;
        require(
            self.snapshot_uri == format!("urn:sha256:{}", self.content_sha256),
            "evidence snapshot URI must bind its immutable content digest",
        )?;
        if let Some(collector) = &self.collector {
            require(
                collector.task == "EVIDENCE_COLLECTOR",
                "evidence collector provenance task mismatch",
            )?;
            require(
                collector.output_hash == self.content_sha256,
                "collector output must bind retained evidence content",
            )?;
            require(
                collector.created_at_ms <= self.collected_at_ms,
                "snapshot cannot predate evidence collection",
            )?;
        }
        Ok(())
    }
}

impl EvidenceSnapshot {
    pub fn evidence_hash(&self) -> Result<String> {
        hash(self)
    }
}

pub fn source_verification_output_hash(
    evidence_hash: &str,
    source_id: &str,
    verified: bool,
    explanation: &str,
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "source_verification_output", "evidence_hash": evidence_hash,
                 "source_id": source_id, "verified": verified, "explanation": explanation}),
    )
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceVerification {
    pub schema_version: i64,
    pub evidence_hash: String,
    pub source_id: String,
    pub verified: bool,
    pub explanation: String,
    pub verifier: AIProvenance,
}

impl Record for SourceVerification {
    fn validate(&self) -> Result<()> {
        let p = "SourceVerification";
        check_schema_version(p, self.schema_version)?;
        check_hash(&format!("{p}.evidence_hash"), &self.evidence_hash)?;
        check_id(&format!("{p}.source_id"), &self.source_id)?;
        check_text(&format!("{p}.explanation"), &self.explanation, None)?;
        self.verifier.validate()?;
        require(
            self.verifier.task == "SOURCE_VERIFIER",
            "source verification task mismatch",
        )?;
        require(
            self.verifier.input_hash == self.evidence_hash,
            "source verification must bind the exact evidence snapshot",
        )?;
        require(
            self.verifier.output_hash
                == source_verification_output_hash(
                    &self.evidence_hash,
                    &self.source_id,
                    self.verified,
                    &self.explanation,
                )?,
            "source verification decision output commitment mismatch",
        )
    }
}

pub fn resolution_input_hash(
    forecast_id: &str,
    specification_hash: &str,
    evidence: &[EvidenceSnapshot],
    source_verifications: &[SourceVerification],
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "resolution_input", "forecast_id": forecast_id,
                 "specification_hash": specification_hash, "evidence": evidence, "source_verifications": source_verifications}),
    )
}

pub struct ResolutionOutput<'a> {
    pub decision_input_hash: &'a str,
    pub proposed_outcome: &'a str,
    pub confidence_bp: i64,
    pub rule_matches: &'a [String],
    pub rule_conflicts: &'a [String],
    pub reason_summary: &'a str,
    pub conflict_status: &'a str,
    pub conflict_explanation: Option<&'a str>,
}

pub fn resolution_output_hash(output: &ResolutionOutput<'_>) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "resolution_output", "decision_input_hash": output.decision_input_hash,
                 "proposed_outcome": output.proposed_outcome, "confidence_bp": output.confidence_bp,
                 "rule_matches": output.rule_matches, "rule_conflicts": output.rule_conflicts,
                 "reason_summary": output.reason_summary, "conflict_status": output.conflict_status,
                 "conflict_explanation": output.conflict_explanation}),
    )
}

pub fn counter_judge_input_hash(decision_input_hash: &str, judge: &AIProvenance) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "counter_judge_input", "decision_input_hash": decision_input_hash, "judge": judge}),
    )
}

pub fn counter_judge_output_hash(judge_output_hash: &str, agrees: bool) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "counter_judge_output", "judge_output_hash": judge_output_hash, "agrees": agrees}),
    )
}

// ---------------------------------------------------------------- resolution

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Resolution {
    pub schema_version: i64,
    pub forecast_id: String,
    pub specification_hash: String,
    pub proposed_outcome: String,
    pub confidence_bp: i64,
    pub evidence: Vec<EvidenceSnapshot>,
    pub source_verifications: Vec<SourceVerification>,
    pub rule_matches: Vec<String>,
    pub rule_conflicts: Vec<String>,
    pub reason_summary: String,
    pub judge: AIProvenance,
    pub counter_judge: AIProvenance,
    pub counter_judge_agrees: bool,
    pub conflict_status: String,
    pub proposed_at_ms: i64,
    #[serde(deserialize_with = "required_option")]
    pub conflict_explanation: Option<String>,
}

impl Record for Resolution {
    fn validate(&self) -> Result<()> {
        self.validate_fields()?;
        self.validate_rules(&self.decision_input_hash()?)
    }
}

impl Resolution {
    pub fn validate_fields(&self) -> Result<()> {
        let p = "Resolution";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_enum(&format!("{p}.proposed_outcome"), &self.proposed_outcome, &OUTCOMES)?;
        bp(&format!("{p}.confidence_bp"), self.confidence_bp)?;
        require(
            !self.evidence.is_empty(),
            &format!("{p}.evidence: requires at least 1 entries"),
        )?;
        for item in &self.evidence {
            item.validate()?;
        }
        require(
            !self.source_verifications.is_empty(),
            &format!("{p}.source_verifications: requires at least 1 entries"),
        )?;
        for item in &self.source_verifications {
            item.validate()?;
        }
        require(
            !self.rule_matches.is_empty(),
            &format!("{p}.rule_matches: requires at least 1 entries"),
        )?;
        for (index, item) in self.rule_matches.iter().enumerate() {
            check_text(&format!("{p}.rule_matches[{index}]"), item, None)?;
        }
        require(
            unique(&self.rule_matches),
            &format!("{p}.rule_matches: duplicate entries"),
        )?;
        for (index, item) in self.rule_conflicts.iter().enumerate() {
            check_text(&format!("{p}.rule_conflicts[{index}]"), item, None)?;
        }
        require(
            unique(&self.rule_conflicts),
            &format!("{p}.rule_conflicts: duplicate entries"),
        )?;
        check_text(&format!("{p}.reason_summary"), &self.reason_summary, None)?;
        self.judge.validate()?;
        self.counter_judge.validate()?;
        check_enum(
            &format!("{p}.conflict_status"),
            &self.conflict_status,
            &CONFLICT_STATUSES,
        )?;
        count(&format!("{p}.proposed_at_ms"), self.proposed_at_ms)?;
        if let Some(explanation) = &self.conflict_explanation {
            check_text(&format!("{p}.conflict_explanation"), explanation, None)?;
        }
        Ok(())
    }

    /// Shared semantic rules; `decision_input_hash` differs for early resolutions.
    pub fn validate_rules(&self, decision_input_hash: &str) -> Result<()> {
        let hashes: Vec<String> = self
            .evidence
            .iter()
            .map(EvidenceSnapshot::evidence_hash)
            .collect::<Result<_>>()?;
        require(unique(&hashes), "resolution evidence snapshots must be unique")?;
        let ids: Vec<&str> = self.evidence.iter().map(|e| e.evidence_id.as_str()).collect();
        require(unique(&ids), "resolution evidence IDs must be unique")?;
        require(
            self.source_verifications.len() == self.evidence.len(),
            "every resolution evidence snapshot requires one verification",
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
            verified == expected,
            "source verifications must cover the exact evidence snapshots",
        )?;
        for verification in &self.source_verifications {
            let snapshot = &self.evidence[hashes
                .iter()
                .position(|h| *h == verification.evidence_hash)
                .expect("covered")];
            require(
                verification.source_id == snapshot.source_id,
                "source verification refers to a different source",
            )?;
            require(
                verification.verifier.created_at_ms >= snapshot.collected_at_ms,
                "source verification cannot predate evidence collection",
            )?;
        }
        require(self.judge.task == "RESOLUTION_JUDGE", "resolution judge task mismatch")?;
        require(
            self.counter_judge.task == "COUNTER_JUDGE",
            "counter-judge task mismatch",
        )?;
        require(
            self.judge.input_hash == decision_input_hash,
            "resolution judge input must bind evidence, verification and specification",
        )?;
        require(
            self.counter_judge.input_hash == counter_judge_input_hash(decision_input_hash, &self.judge)?,
            "counter-judge input must bind evidence and the exact judge decision",
        )?;
        require(
            self.judge.output_hash
                == resolution_output_hash(&ResolutionOutput {
                    decision_input_hash,
                    proposed_outcome: &self.proposed_outcome,
                    confidence_bp: self.confidence_bp,
                    rule_matches: &self.rule_matches,
                    rule_conflicts: &self.rule_conflicts,
                    reason_summary: &self.reason_summary,
                    conflict_status: &self.conflict_status,
                    conflict_explanation: self.conflict_explanation.as_deref(),
                })?,
            "resolution decision output commitment mismatch",
        )?;
        require(
            self.counter_judge.output_hash
                == counter_judge_output_hash(&self.judge.output_hash, self.counter_judge_agrees)?,
            "counter-judge decision output commitment mismatch",
        )?;
        require(
            self.counter_judge.created_at_ms >= self.judge.created_at_ms,
            "counter-judge cannot predate the resolution judge",
        )?;
        let evidence_ready_at = self
            .source_verifications
            .iter()
            .map(|v| v.verifier.created_at_ms)
            .max()
            .expect("nonempty");
        require(
            self.judge.created_at_ms >= evidence_ready_at,
            "resolution judge cannot predate source verification",
        )?;
        require(
            self.proposed_at_ms >= self.counter_judge.created_at_ms,
            "proposal cannot predate its counter-judge",
        )?;
        require(
            !self.rule_matches.iter().any(|m| self.rule_conflicts.contains(m)),
            "a rule cannot simultaneously match and conflict",
        )?;
        if self.conflict_status == "CLEAR" {
            require(
                self.rule_conflicts.is_empty() && self.counter_judge_agrees,
                "clear resolution cannot contain conflicts or judge disagreement",
            )?;
            require(
                self.conflict_explanation.is_none(),
                "clear resolution has no conflict explanation",
            )?;
        } else {
            require(
                self.conflict_explanation.is_some(),
                "conflicting resolution requires an explicit explanation",
            )?;
        }
        if self.conflict_status == "RESOLVED" {
            require(
                self.counter_judge_agrees,
                "resolved conflict requires counter-judge agreement",
            )?;
        }
        Ok(())
    }

    pub fn decision_input_hash(&self) -> Result<String> {
        resolution_input_hash(
            &self.forecast_id,
            &self.specification_hash,
            &self.evidence,
            &self.source_verifications,
        )
    }

    pub fn resolution_hash(&self) -> Result<String> {
        hash(self)
    }

    /// Specification binding shared by ordinary and early resolutions.
    pub fn validate_binding(&self, specification: &ForecastSpecification) -> Result<()> {
        require(
            self.specification_hash == specification.specification_hash()?,
            "resolution refers to a different specification",
        )?;
        let references: Vec<&str> = self
            .rule_matches
            .iter()
            .chain(self.rule_conflicts.iter())
            .map(String::as_str)
            .collect();
        specification.validate_rule_references(&references)?;
        let mut matched: Vec<&str> = specification
            .rules
            .iter()
            .filter(|r| self.rule_matches.contains(&r.clause_id))
            .map(|r| r.outcome.as_str())
            .collect();
        matched.sort();
        matched.dedup();
        require(
            matched == [self.proposed_outcome.as_str()],
            "resolution may match only its selected outcome clause",
        )?;
        let source_ids = specification.source_ids();
        require(
            self.evidence.iter().all(|e| source_ids.contains(&e.source_id.as_str())),
            "resolution evidence source is outside the published source policy",
        )?;
        require(
            self.evidence.iter().all(|e| {
                let source = specification
                    .source_policy
                    .sources()
                    .find(|s| s.source_id == e.source_id)
                    .expect("checked");
                url_hostname(&e.url) == url_hostname(&source.url)
            }),
            "evidence URL is outside its declared source host",
        )
    }

    pub fn validate_for(&self, specification: &ForecastSpecification) -> Result<()> {
        self.validate_binding(specification)?;
        require(
            self.evidence
                .iter()
                .all(|e| e.collected_at_ms >= specification.close_at_ms),
            "resolution evidence must be collected after forecast expiry",
        )
    }

    pub fn require_proposable_after(&self) -> Result<()> {
        require(
            self.source_verifications.iter().all(|v| v.verified),
            "resolution requires verified evidence sources",
        )?;
        require(
            self.conflict_status != "UNRESOLVED" && self.counter_judge_agrees,
            "unresolved evidence or judge conflict blocks proposal",
        )
    }

    pub fn require_proposable(&self, specification: &ForecastSpecification) -> Result<()> {
        self.validate_for(specification)?;
        self.require_proposable_after()
    }
}

// ---------------------------------------------------------------- disputes

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dispute {
    pub schema_version: i64,
    pub dispute_id: String,
    pub disputant_id: String,
    pub forecast_id: String,
    pub specification_hash: String,
    pub resolution_hash: String,
    pub claim: String,
    pub evidence: Vec<EvidenceSnapshot>,
    pub rule_clause_id: String,
    pub explanation: String,
    pub submitted_at_ms: i64,
}

impl Record for Dispute {
    fn validate(&self) -> Result<()> {
        let p = "Dispute";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.dispute_id"), &self.dispute_id)?;
        check_id(&format!("{p}.disputant_id"), &self.disputant_id)?;
        check_id(&format!("{p}.forecast_id"), &self.forecast_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_hash(&format!("{p}.resolution_hash"), &self.resolution_hash)?;
        check_text(&format!("{p}.claim"), &self.claim, None)?;
        require(
            !self.evidence.is_empty(),
            &format!("{p}.evidence: requires at least 1 entries"),
        )?;
        for item in &self.evidence {
            item.validate()?;
        }
        check_id(&format!("{p}.rule_clause_id"), &self.rule_clause_id)?;
        check_text(&format!("{p}.explanation"), &self.explanation, None)?;
        count(&format!("{p}.submitted_at_ms"), self.submitted_at_ms)?;
        let ids: Vec<&str> = self.evidence.iter().map(|e| e.evidence_id.as_str()).collect();
        require(unique(&ids), "dispute evidence IDs must be unique")?;
        require(
            self.evidence.iter().all(|e| e.collected_at_ms <= self.submitted_at_ms),
            "dispute cannot predate its evidence",
        )
    }
}

impl Dispute {
    pub fn dispute_hash(&self) -> Result<String> {
        hash(self)
    }

    pub fn evidence_hash(&self) -> Result<String> {
        hash(&json!({"schema_version": 1, "kind": "dispute_evidence", "evidence": self.evidence}))
    }

    pub fn validate_for(&self, specification: &ForecastSpecification, resolution: &Resolution) -> Result<()> {
        require(
            self.forecast_id == resolution.forecast_id,
            "dispute forecast binding mismatch",
        )?;
        require(
            self.specification_hash == specification.specification_hash()?
                && self.specification_hash == resolution.specification_hash,
            "dispute specification binding mismatch",
        )?;
        require(
            self.resolution_hash == resolution.resolution_hash()?,
            "dispute proposal binding mismatch",
        )?;
        specification.validate_rule_references(&[self.rule_clause_id.as_str()])?;
        require(
            self.submitted_at_ms >= resolution.proposed_at_ms,
            "dispute cannot predate the proposal",
        )
    }
}

pub fn dispute_review_input_hash(
    dispute_hash: &str,
    specification_hash: &str,
    resolution_hash: &str,
    evidence_hash: &str,
    evidence_validation: &AIProvenance,
    counter_analysis: &AIProvenance,
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "dispute_review_input", "dispute_hash": dispute_hash,
                 "specification_hash": specification_hash, "resolution_hash": resolution_hash, "evidence_hash": evidence_hash,
                 "evidence_validation": evidence_validation, "counter_analysis": counter_analysis}),
    )
}

pub fn dispute_evidence_output_hash(evidence_hash: &str, evidence_validated: bool) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "dispute_evidence_output", "evidence_hash": evidence_hash, "evidence_validated": evidence_validated}),
    )
}

pub fn dispute_analysis_output_hash(
    dispute_hash: &str,
    evidence_hash: &str,
    material_conflict: bool,
    reason_summary: &str,
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "dispute_analysis_output", "dispute_hash": dispute_hash,
                 "evidence_hash": evidence_hash, "material_conflict": material_conflict, "reason_summary": reason_summary}),
    )
}

pub fn dispute_review_output_hash(
    review_input_hash: &str,
    evidence_validated: bool,
    material_conflict: bool,
    disposition: &str,
    reason_summary: &str,
) -> Result<String> {
    hash(
        &json!({"schema_version": 1, "kind": "dispute_review_output", "review_input_hash": review_input_hash,
                 "evidence_validated": evidence_validated, "material_conflict": material_conflict,
                 "disposition": disposition, "reason_summary": reason_summary}),
    )
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DisputeReview {
    pub schema_version: i64,
    pub dispute_hash: String,
    pub specification_hash: String,
    pub resolution_hash: String,
    pub evidence_hash: String,
    pub evidence_validated: bool,
    pub material_conflict: bool,
    pub disposition: String,
    pub evidence_validation: AIProvenance,
    pub counter_analysis: AIProvenance,
    pub independent_judge: AIProvenance,
    pub reason_summary: String,
    pub reviewed_at_ms: i64,
}

impl Record for DisputeReview {
    fn validate(&self) -> Result<()> {
        let p = "DisputeReview";
        check_schema_version(p, self.schema_version)?;
        for (name, value) in [
            ("dispute_hash", &self.dispute_hash),
            ("specification_hash", &self.specification_hash),
            ("resolution_hash", &self.resolution_hash),
            ("evidence_hash", &self.evidence_hash),
        ] {
            check_hash(&format!("{p}.{name}"), value)?;
        }
        check_enum(&format!("{p}.disposition"), &self.disposition, &REVIEW_DISPOSITIONS)?;
        self.evidence_validation.validate()?;
        self.counter_analysis.validate()?;
        self.independent_judge.validate()?;
        check_text(&format!("{p}.reason_summary"), &self.reason_summary, None)?;
        count(&format!("{p}.reviewed_at_ms"), self.reviewed_at_ms)?;
        let expected = match self.disposition.as_str() {
            "RETAIN_PROPOSAL" => (true, false),
            "MATERIAL_CONFLICT" => (true, true),
            _ => (false, false),
        };
        require(
            (self.evidence_validated, self.material_conflict) == expected,
            "review disposition must agree with evidence validation and material conflict",
        )?;
        require(
            self.evidence_validation.task == "SOURCE_VERIFIER",
            "dispute evidence validation provenance task mismatch",
        )?;
        require(
            self.evidence_validation.input_hash == self.evidence_hash,
            "dispute evidence validation must bind its evidence bundle",
        )?;
        require(
            self.counter_analysis.task == "DISPUTE_ANALYST",
            "dispute counter-analysis provenance task mismatch",
        )?;
        require(
            self.counter_analysis.input_hash == self.dispute_hash,
            "counter-analysis must bind the exact dispute",
        )?;
        require(
            self.independent_judge.task == "INDEPENDENT_REJUDGE",
            "dispute independent review provenance task mismatch",
        )?;
        let review_input = self.review_input_hash()?;
        require(
            self.independent_judge.input_hash == review_input,
            "independent review must bind the dispute and preceding analyses",
        )?;
        require(
            self.evidence_validation.output_hash
                == dispute_evidence_output_hash(&self.evidence_hash, self.evidence_validated)?,
            "dispute evidence decision output mismatch",
        )?;
        require(
            self.counter_analysis.output_hash
                == dispute_analysis_output_hash(
                    &self.dispute_hash,
                    &self.evidence_hash,
                    self.material_conflict,
                    &self.reason_summary,
                )?,
            "dispute analysis decision output mismatch",
        )?;
        require(
            self.independent_judge.output_hash
                == dispute_review_output_hash(
                    &review_input,
                    self.evidence_validated,
                    self.material_conflict,
                    &self.disposition,
                    &self.reason_summary,
                )?,
            "independent re-judge decision output mismatch",
        )?;
        require(
            self.independent_judge.provider.to_lowercase() != self.counter_analysis.provider.to_lowercase(),
            "independent re-judge must use a different provider from counter-analysis",
        )?;
        require(
            self.evidence_validation.created_at_ms <= self.counter_analysis.created_at_ms
                && self.counter_analysis.created_at_ms <= self.independent_judge.created_at_ms
                && self.independent_judge.created_at_ms <= self.reviewed_at_ms,
            "dispute review steps must follow their causal order",
        )
    }
}

impl DisputeReview {
    pub fn review_input_hash(&self) -> Result<String> {
        dispute_review_input_hash(
            &self.dispute_hash,
            &self.specification_hash,
            &self.resolution_hash,
            &self.evidence_hash,
            &self.evidence_validation,
            &self.counter_analysis,
        )
    }

    pub fn require_valid_for(
        &self,
        dispute: &Dispute,
        resolution: &Resolution,
        specification: &ForecastSpecification,
    ) -> Result<()> {
        dispute.validate_for(specification, resolution)?;
        require(
            self.dispute_hash == dispute.dispute_hash()?,
            "review refers to a different dispute",
        )?;
        require(
            self.specification_hash == specification.specification_hash()?,
            "review specification binding mismatch",
        )?;
        require(
            self.resolution_hash == resolution.resolution_hash()?,
            "review proposal binding mismatch",
        )?;
        require(
            self.evidence_hash == dispute.evidence_hash()?,
            "review evidence binding mismatch",
        )?;
        require(
            self.evidence_validation.created_at_ms >= dispute.submitted_at_ms,
            "dispute review cannot predate dispute submission",
        )?;
        let originals = [
            resolution.judge.provider.to_lowercase(),
            resolution.counter_judge.provider.to_lowercase(),
        ];
        require(
            !originals.contains(&self.independent_judge.provider.to_lowercase()),
            "dispute re-judge must be independent of both original decision providers",
        )
    }
}

/// Serialize a record to a JSON object for hashing subsets.
pub fn to_value<T: Serialize>(value: &T) -> Result<Value> {
    serde_json::to_value(value).map_err(|e| ValidationError::new(e.to_string()))
}
