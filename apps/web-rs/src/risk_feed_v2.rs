//! The v2 risk producer: typed `[start, end)` targets, reviewed profiles, separated clocks.
//!
//! Nothing here edits a published question, rewrites a retained estimate's timestamps, or lets a
//! payload authorize its own mapping profile — those are the three things the reference's own
//! docstring names, and each of them is a rule rather than a check: the binding is compared against
//! the *published* measurement interval, the clocks come from a retained artifact, and the profile
//! has to have been admitted under the same feed before the binding that cites it is allowed in.
//!
//! The ordering in `operational_bindings_v2` and `_selection_rank` is where a port diverges
//! silently — a different tie order publishes a different envelope under the same signature — so
//! the pure parts live in `risk_ops` with their own cases, and this module only supplies the rows.

use crate::ai::window::measurement_window;
use crate::db::{self, Database, Row};
use crate::reputation::qualified_cohorts;
use crate::risk_feed::SNAPSHOT_SQL;
use crate::risk_ops::{
    clock_is_stale, coverage_status, order_operational, profile_set_hash, signal_is_admissible, worth_clock_check,
    DEFINED_CHANNELS_SQL, LATEST_CLOCK_SQL, MAX_BINDINGS, OPERATIONAL_SQL, PROFILE_SET_SQL, STALE_CANDIDATE_SQL,
};
use crate::wallets::BoxFuture;
use forecast_domain::risk_feed::{
    freshness_as_of_ms, signing_bytes_v2, CanonicalRiskDefinitionV2, ChannelCoverageV2, RiskFeedBindingV2,
    RiskFeedPayloadV2, RiskFeedSignalV2, RiskMappingProfileV2, SignedRiskFeedV2, FEED_TTL_MS,
};
use forecast_domain::{canonical_bytes, content_hash, require, Record, ValidationError};
use serde_json::{json, Value};

/// `RiskFeedPayloadV2.purpose`.
pub const PURPOSE: &str = "forecast-risk-feed-v2";

/// `FEED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")`.
fn valid_feed_id(feed_id: &str) -> bool {
    let mut characters = feed_id.chars();
    match characters.next() {
        Some(first) if first.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    let rest = characters.collect::<String>();
    rest.len() <= 127
        && rest
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
}

/// `CLOCK_SQL`: the retained clock artifact for one estimate, addressed by the estimate's own
/// canonical hash rather than by a column — the identity is the hash of the body.
pub const CLOCK_SQL: &str = concat!(
    "SELECT body FROM artifacts WHERE hash=(SELECT clock_artifact_hash FROM risk_prediction_clocks_v2 ",
    "WHERE estimate_artifact_hash=?)",
);

/// The reference raises `ValidationError` from every `require`, and the caller maps it to a server
/// fault: a feed that cannot be assembled is not a request to answer differently.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeedError(pub String);

impl From<ValidationError> for FeedError {
    fn from(error: ValidationError) -> Self {
        Self(error.to_string())
    }
}

fn require_that(condition: bool, message: &str) -> Result<(), FeedError> {
    require(condition, message).map_err(FeedError::from)
}

fn storage() -> FeedError {
    FeedError("feed storage unavailable".to_string())
}

/// `storage`, for the sibling module that would otherwise repeat the message.
pub fn storage_unavailable() -> FeedError {
    storage()
}

fn dumps<T: serde::Serialize>(value: &T) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

fn row_of(value: &Value) -> Row {
    value.as_object().cloned().unwrap_or_default()
}

/// `_actor`: whether the caller *is* an admin is the route's business, and this says so rather
/// than growing a second authorization path.
pub fn actor(approved_by: &str) -> Result<(), FeedError> {
    require_that(
        !approved_by.trim().is_empty() && approved_by.len() <= 128,
        "authenticated approver required",
    )
}

/// `_feed`.
pub fn feed(feed_id: &str) -> Result<(), FeedError> {
    require_that(valid_feed_id(feed_id), "invalid feed ID")
}

/// `admit_definition`.
pub async fn admit_definition(
    db: &dyn Database,
    feed_id: &str,
    definition: &CanonicalRiskDefinitionV2,
    approved_by: &str,
    now_ms: i64,
) -> Result<String, FeedError> {
    definition.validate().map_err(FeedError::from)?;
    actor(approved_by)?;
    feed(feed_id)?;
    let digest = content_hash(definition).map_err(FeedError::from)?;
    db.execute(
        concat!(
            "INSERT INTO risk_feed_definitions_v2(definition_hash,feed_id,channel,definition_json,approved_by,approved_at) ",
            "VALUES(?,?,?,?,?,?) ON CONFLICT(definition_hash) DO NOTHING",
        ),
        &[
            json!(digest),
            json!(feed_id),
            json!(definition.channel),
            json!(dumps(definition)),
            json!(approved_by),
            json!(now_ms),
        ],
    )
    .await
    .map_err(|_| storage())?;
    Ok(digest)
}

/// `admit_profile`.
pub async fn admit_profile(
    db: &dyn Database,
    feed_id: &str,
    profile: &RiskMappingProfileV2,
    approved_by: &str,
    now_ms: i64,
) -> Result<String, FeedError> {
    profile.validate().map_err(FeedError::from)?;
    actor(approved_by)?;
    feed(feed_id)?;
    let digest = content_hash(profile).map_err(FeedError::from)?;
    db.execute(
        concat!(
            "INSERT INTO risk_feed_profiles_v2(profile_hash,feed_id,profile_json,approved_by,approved_at) ",
            "VALUES(?,?,?,?,?) ON CONFLICT(profile_hash) DO NOTHING",
        ),
        &[
            json!(digest),
            json!(feed_id),
            json!(dumps(profile)),
            json!(approved_by),
            json!(now_ms),
        ],
    )
    .await
    .map_err(|_| storage())?;
    Ok(digest)
}

/// `_approved`. The retained records are re-hashed rather than trusted: a row whose body no longer
/// hashes to the key it is stored under is a record that has been edited, and a binding citing it
/// would be authorizing itself against something nobody approved.
pub async fn approved(
    db: &dyn Database,
    feed_id: &str,
    binding: &RiskFeedBindingV2,
) -> Result<(CanonicalRiskDefinitionV2, RiskMappingProfileV2), FeedError> {
    let definition_row = db
        .first(
            "SELECT definition_json FROM risk_feed_definitions_v2 WHERE definition_hash=? AND feed_id=?",
            &[json!(binding.definition_hash), json!(feed_id)],
        )
        .await
        .map_err(|_| storage())?;
    let profile_row = db
        .first(
            "SELECT profile_json FROM risk_feed_profiles_v2 WHERE profile_hash=? AND feed_id=?",
            &[json!(binding.mapping_profile_hash), json!(feed_id)],
        )
        .await
        .map_err(|_| storage())?;
    require_that(
        definition_row.is_some() && profile_row.is_some(),
        "binding references unadmitted records",
    )?;
    let definition = CanonicalRiskDefinitionV2::from_json(
        db::text(definition_row.as_ref().unwrap(), "definition_json").unwrap_or(""),
    )
    .map_err(FeedError::from)?;
    let profile =
        RiskMappingProfileV2::from_json(db::text(profile_row.as_ref().unwrap(), "profile_json").unwrap_or(""))
            .map_err(FeedError::from)?;
    require_that(
        content_hash(&definition).ok().as_deref() == Some(binding.definition_hash.as_str())
            && content_hash(&profile).ok().as_deref() == Some(binding.mapping_profile_hash.as_str()),
        "retained record identity mismatch",
    )?;
    require_that(
        definition.channel == binding.channel
            && definition.asset == binding.asset
            && definition.policy_horizon_ms == binding.policy_horizon_ms
            && definition.mapping_kind == binding.mapping_kind
            && binding.mapping_kind == profile.mapping_kind
            && definition.mapping_profile_id == binding.mapping_profile_id
            && binding.mapping_profile_id == profile.profile_id
            && definition.mapping_profile_version == binding.mapping_profile_version
            && binding.mapping_profile_version == profile.profile_version,
        "binding disagrees with its admitted definition or profile",
    )?;
    Ok((definition, profile))
}

/// `approve_binding_v2`. The typed target bounds must equal the immutable published question's own
/// UTC interval — not merely overlap it, and not merely be plausible.
pub async fn approve_binding_v2(
    db: &dyn Database,
    feed_id: &str,
    binding: &RiskFeedBindingV2,
    approved_by: &str,
    now_ms: i64,
) -> Result<(), FeedError> {
    binding.validate().map_err(FeedError::from)?;
    actor(approved_by)?;
    feed(feed_id)?;
    require_that(
        binding.authorization_valid_from_ms <= now_ms && now_ms < binding.authorization_valid_until_ms,
        "binding authorization is not current",
    )?;
    let row = db
        .first(
            "SELECT specification_hash,category,open_at,close_at,state,snapshot FROM forecasts WHERE id=?",
            &[json!(binding.forecast_id)],
        )
        .await
        .map_err(|_| storage())?;
    require_that(row.is_some(), "unknown canonical forecast")?;
    let row = row.unwrap();
    require_that(
        db::text(&row, "specification_hash") == Some(binding.specification_hash.as_str())
            && db::text(&row, "category") == Some(binding.category.as_str())
            && db::text(&row, "state") == Some("OPEN"),
        "binding does not match the published specification",
    )?;
    let snapshot: Value = serde_json::from_str(db::text(&row, "snapshot").unwrap_or("")).unwrap_or(Value::Null);
    let question = snapshot["specification"]["canonical_question"].as_str().unwrap_or("");
    // `measurement_window` answers with the interval the question itself states. A question that
    // does not state one cannot be the target of a typed binding at all.
    let window = measurement_window(question)
        .map_err(|_| FeedError("published question has no explicit [start, end) measurement interval".to_string()))?;
    let Some(window) = window else {
        return Err(FeedError(
            "published question has no explicit [start, end) measurement interval".to_string(),
        ));
    };
    require_that(
        window["start_at_ms"].as_i64() == Some(binding.target_start_ms)
            && window["end_at_ms"].as_i64() == Some(binding.target_end_ms),
        "typed target disagrees with the published measurement interval",
    )?;
    require_that(
        db::int(&row, "close_at") == Some(binding.target_end_ms),
        "measurement end must equal the published deadline",
    )?;
    require_that(
        content_hash(&json!({
            "specification_hash": binding.specification_hash,
            "window": window["canonical_expression"],
        }))
        .ok()
        .as_deref()
            == Some(binding.question_event_definition_hash.as_str()),
        "question event definition hash mismatch",
    )?;
    approved(db, feed_id, binding).await?;
    db.batch(&[
        (
            concat!(
                "INSERT INTO risk_feed_bindings_v2(binding_id,feed_id,forecast_id,binding_json,approved_by,approved_at) ",
                "VALUES(?,?,?,?,?,?)",
            )
            .to_string(),
            vec![
                json!(binding.binding_id),
                json!(feed_id),
                json!(binding.forecast_id),
                json!(dumps(binding)),
                json!(approved_by),
                json!(now_ms),
            ],
        ),
        (
            "INSERT INTO risk_feed_heads(feed_id,sequence) VALUES(?,0) ON CONFLICT(feed_id) DO NOTHING".to_string(),
            vec![json!(feed_id)],
        ),
    ])
    .await
    .map_err(|_| storage())?;
    Ok(())
}

/// `revoke_binding_v2`: an append-only revocation, so published evidence stays immutable.
pub async fn revoke_binding_v2(
    db: &dyn Database,
    binding_id: &str,
    revoked_by: &str,
    now_ms: i64,
    reason: &str,
) -> Result<(), FeedError> {
    require_that(
        !revoked_by.trim().is_empty() && revoked_by.len() <= 128 && (1..=1000).contains(&reason.trim().len()),
        "revocation requires authenticated actor and bounded reason",
    )?;
    db.execute(
        "INSERT INTO risk_feed_binding_revocations_v2(binding_id,revoked_by,revoked_at,reason) VALUES(?,?,?,?)",
        &[json!(binding_id), json!(revoked_by), json!(now_ms), json!(reason)],
    )
    .await
    .map_err(|_| storage())?;
    Ok(())
}

/// `operational_bindings_v2`: bindings whose *operational* validity contains now. A binding that is
/// only authorized stays out, which is what keeps a mapped question out of the feed until its
/// target window is the one being asked about.
pub async fn operational_bindings_v2(
    db: &dyn Database,
    feed_id: &str,
    now_ms: i64,
) -> Result<Vec<RiskFeedBindingV2>, FeedError> {
    require_that((0..(1i64 << 53)).contains(&now_ms), "invalid binding selection time")?;
    let rows = db
        .all(OPERATIONAL_SQL, &[json!(feed_id), json!(now_ms), json!(now_ms)])
        .await
        .map_err(|_| storage())?;
    // The limit is 65 so that 64 can be stored and the 65th is the evidence of overflow.
    require_that(rows.len() <= MAX_BINDINGS, "feed binding capacity exceeded")?;
    let mut bindings: Vec<RiskFeedBindingV2> = rows
        .iter()
        .map(|row| RiskFeedBindingV2::from_json(db::text(row, "binding_json").unwrap_or("")).map_err(FeedError::from))
        .collect::<Result<_, _>>()?;
    order_operational(&mut bindings);
    Ok(bindings)
}

/// `_selection_rank`, as the total order `order_operational` implements. Kept here as a name for
/// the reader: the rule itself lives with its cases in `risk_ops`.
pub fn selection_rank(
    binding: &RiskFeedBindingV2,
    definition: &CanonicalRiskDefinitionV2,
    now_ms: i64,
) -> (String, bool, i64, String) {
    (
        binding.channel.clone(),
        now_ms < binding.target_start_ms + definition.sampling_grid_ms,
        -binding.target_start_ms,
        binding.binding_id.clone(),
    )
}

/// `_pool`: one source's mean over its members, with the clock roles kept separate.
///
/// The pool's information set closes with its *newest* member while freshness is judged from its
/// *oldest* — both are recorded, because collapsing them into one timestamp is how a feed reports
/// an estimate as fresher than the evidence behind it.
fn pool(
    binding: &RiskFeedBindingV2,
    source: &str,
    rows: &[Value],
    evidence: &Value,
    dependence: &str,
) -> Result<RiskFeedSignalV2, FeedError> {
    let count = rows.len() as i64;
    let total: i64 = rows.iter().filter_map(|row| row["probability"].as_i64()).sum();
    let mut times: Vec<i64> = rows.iter().filter_map(|row| row["submitted_at"].as_i64()).collect();
    times.sort();
    let earliest = times.first().copied().unwrap_or(0);
    let latest = times.last().copied().unwrap_or(0);
    let value = RiskFeedSignalV2 {
        schema_version: 1,
        binding_id: binding.binding_id.clone(),
        source: source.to_string(),
        value_kind: "question_probability".to_string(),
        question_probability_bp: (total * 200 + count) / (2 * count),
        confidence_bp: (3000 + count * 100).min(9000),
        sample_count: count,
        units: "basis-points".to_string(),
        forecast_as_of_ms: latest,
        information_cutoff_ms: latest,
        evaluation_started_at_ms: earliest,
        evaluation_completed_at_ms: latest,
        source_capture_started_at_ms: earliest,
        source_capture_completed_at_ms: latest,
        source_watermark_ms: None,
        source_bundle_hash: content_hash(rows).map_err(FeedError::from)?,
        estimate_hash: content_hash(evidence).map_err(FeedError::from)?,
        coverage_evidence_hash: content_hash(&json!({
            "members": rows.iter().filter_map(|row| row["user_id"].clone().as_str().map(str::to_string)).collect::<Vec<_>>(),
        }))
        .map_err(FeedError::from)?,
        evidence_hash: content_hash(evidence).map_err(FeedError::from)?,
        dependence_group: dependence.to_string(),
        estimator_version: "eligible-mean-v1".to_string(),
        oldest_member_as_of_ms: Some(earliest),
        newest_member_as_of_ms: Some(latest),
        constituent_dataset_hash: Some(content_hash(rows).map_err(FeedError::from)?),
        calibration_status: "provisional".to_string(),
    };
    value.validate().map_err(FeedError::from)?;
    Ok(value)
}

/// `_ai_signal`. A compile-time estimate carries no capture or completion clocks, and the reference
/// refuses to invent them: without a clock artifact there is no signal at all.
fn ai_signal(
    binding: &RiskFeedBindingV2,
    ai: &Value,
    clock: Option<&Value>,
    dependence: &str,
) -> Result<Option<RiskFeedSignalV2>, FeedError> {
    require_that(
        ai.is_object()
            && ai["specification_hash"] == json!(binding.specification_hash)
            && ai["yes_probability_bp"]
                .as_i64()
                .is_some_and(|p| (0..=10000).contains(&p))
            && ai["as_of_ms"].as_i64().is_some(),
        "invalid AI probability provenance",
    )?;
    let Some(clock) = clock else {
        return Ok(None);
    };
    require_that(
        clock["version"] == json!("risk-prediction-clock-v1")
            && clock["specification_hash"] == json!(binding.specification_hash)
            && clock["forecast_as_of_ms"] == ai["as_of_ms"],
        "clock artifact does not describe this estimate",
    )?;
    let estimate = content_hash(ai).map_err(FeedError::from)?;
    let value = RiskFeedSignalV2 {
        schema_version: 1,
        binding_id: binding.binding_id.clone(),
        source: "ai".to_string(),
        value_kind: "question_probability".to_string(),
        question_probability_bp: ai["yes_probability_bp"].as_i64().unwrap_or(0),
        confidence_bp: 3000,
        sample_count: 1,
        units: "basis-points".to_string(),
        forecast_as_of_ms: ai["as_of_ms"].as_i64().unwrap_or(0),
        information_cutoff_ms: clock["information_cutoff_ms"].as_i64().unwrap_or(0),
        evaluation_started_at_ms: clock["evaluation_started_at_ms"].as_i64().unwrap_or(0),
        evaluation_completed_at_ms: clock["evaluation_completed_at_ms"].as_i64().unwrap_or(0),
        source_capture_started_at_ms: clock["source_capture_started_at_ms"].as_i64().unwrap_or(0),
        source_capture_completed_at_ms: clock["source_capture_completed_at_ms"].as_i64().unwrap_or(0),
        source_watermark_ms: clock["source_watermark_ms"].as_i64(),
        source_bundle_hash: clock["source_bundle_hash"].as_str().unwrap_or("").to_string(),
        estimate_hash: estimate.clone(),
        coverage_evidence_hash: clock["source_bundle_hash"].as_str().unwrap_or("").to_string(),
        evidence_hash: estimate,
        dependence_group: dependence.to_string(),
        // `f"{provider}:{model}:refresh-v2"[:128]`.
        estimator_version: format!(
            "{}:{}:refresh-v2",
            ai["provider"].as_str().unwrap_or(""),
            ai["model"].as_str().unwrap_or("")
        )
        .chars()
        .take(128)
        .collect(),
        oldest_member_as_of_ms: None,
        newest_member_as_of_ms: None,
        constituent_dataset_hash: None,
        calibration_status: "provisional".to_string(),
    };
    value.validate().map_err(FeedError::from)?;
    Ok(Some(value))
}

/// `signals_v2`. The admissibility filter is applied last, because these signals are signed: one
/// the Python side rejects and this admits is an envelope no consumer can verify.
pub fn signals_v2(
    binding: &RiskFeedBindingV2,
    snapshot: &Value,
    clock: Option<&Value>,
    now_ms: i64,
) -> Result<Vec<RiskFeedSignalV2>, FeedError> {
    require_that(
        snapshot["specification_hash"] == json!(binding.specification_hash)
            && snapshot["category"] == json!(binding.category),
        "immutable binding changed",
    )?;
    let rows = snapshot["submissions"].as_array().cloned().unwrap_or_default();
    let history = snapshot["history"].as_array().cloned().unwrap_or_default();
    require_that(
        rows.len() <= 1_000_000 && history.len() <= 100_000,
        "invalid source population",
    )?;
    for row in &rows {
        require_that(
            row["probability"].as_i64().is_some_and(|p| (0..=100).contains(&p))
                && row["submitted_at"].as_i64().is_some(),
            "malformed eligible submission",
        )?;
    }
    let cohorts = qualified_cohorts(
        &history.iter().map(row_of).collect::<Vec<Row>>(),
        &binding.forecast_id,
        &binding.category,
        now_ms,
    )
    .map_err(|error| FeedError(error.0))?;
    let top: Vec<String> = cohorts["top"]
        .as_array()
        .map(|ids| ids.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default();
    let dependence = content_hash(&json!({"upstream": "forecast-network-service-v1"})).map_err(FeedError::from)?;
    let mut out: Vec<RiskFeedSignalV2> = Vec::new();
    if !snapshot["ai"].is_null() {
        let decoded: Value = serde_json::from_str(snapshot["ai"].as_str().unwrap_or("")).unwrap_or(Value::Null);
        if let Some(signal) = ai_signal(binding, &decoded, clock, &dependence)? {
            out.push(signal);
        }
    }
    if !rows.is_empty() {
        out.push(pool(
            binding,
            "crowd",
            &rows,
            &json!({"binding": binding, "eligible_rows": rows}),
            &dependence,
        )?);
    }
    let top_rows: Vec<Value> = rows
        .iter()
        .filter(|row| {
            row["user_id"]
                .as_str()
                .is_some_and(|id| top.iter().any(|user| user == id))
        })
        .cloned()
        .collect();
    if !top_rows.is_empty() {
        out.push(pool(
            binding,
            "top",
            &top_rows,
            &json!({"binding": binding, "eligible_rows": top_rows, "qualification_history": history}),
            &dependence,
        )?);
    }
    Ok(out
        .into_iter()
        .filter(|signal| {
            signal_is_admissible(
                binding,
                signal.forecast_as_of_ms,
                signal.evaluation_completed_at_ms,
                now_ms,
            )
        })
        .collect())
}

/// `_coverage`. Every channel in the vocabulary is reported, including the ones with no admitted
/// definition: a channel that disappears from the list cannot be noticed as missing.
async fn coverage(
    db: &dyn Database,
    feed_id: &str,
    covered: &std::collections::BTreeMap<String, String>,
    withheld: &std::collections::BTreeMap<String, String>,
) -> Result<Vec<ChannelCoverageV2>, FeedError> {
    let rows = db
        .all(DEFINED_CHANNELS_SQL, &[json!(feed_id)])
        .await
        .map_err(|_| storage())?;
    let defined: std::collections::BTreeSet<String> = rows
        .iter()
        .filter_map(|row| db::text(row, "channel").map(str::to_string))
        .collect();
    let mut channels: Vec<&str> = crate::risk_ops::CHANNELS.to_vec();
    channels.sort_unstable();
    Ok(channels
        .iter()
        .map(|channel| {
            let (status, binding_id, reason) = coverage_status(channel, covered, withheld, &defined);
            ChannelCoverageV2 {
                schema_version: 1,
                channel: (*channel).to_string(),
                status: status.to_string(),
                binding_id: binding_id.map(str::to_string),
                reason: reason.map(str::to_string),
            }
        })
        .collect())
}

/// `publish_feed_v2`.
///
/// The signer is awaited after every snapshot has been read, which is why the guard batch exists:
/// during the await a binding can be revoked, an estimate refreshed, or a question paused, and the
/// guard re-evaluates each snapshot in SQL so a publication that lost that race is refused rather
/// than signed. The guard tokens are deleted by an explicit list rather than a `LIKE` pattern,
/// because a binding id may contain the pattern's own wildcards.
#[allow(clippy::too_many_arguments)]
pub async fn publish_feed_v2(
    db: &dyn Database,
    feed_id: &str,
    genesis_hash: &str,
    key_id: &str,
    public_key_hex: &str,
    signer: &dyn Fn(Vec<u8>) -> BoxFuture<Result<Vec<u8>, ()>>,
    now_ms: i64,
    weight_set_hash: &str,
    weight_set_version: &str,
    calibration_cohort_id: &str,
) -> Result<SignedRiskFeedV2, FeedError> {
    let head = db
        .first(
            concat!(
                "SELECT h.sequence,COALESCE((SELECT MAX(created_at) FROM risk_feed_publications_v2 p ",
                "WHERE p.feed_id=h.feed_id),0) last_time FROM risk_feed_heads h WHERE feed_id=?",
            ),
            &[json!(feed_id)],
        )
        .await
        .map_err(|_| storage())?;
    require_that(head.is_some(), "feed has no approved canonical bindings")?;
    let head = head.unwrap();
    let (sequence, last_time) = (
        db::int(&head, "sequence").unwrap_or(0),
        db::int(&head, "last_time").unwrap_or(0),
    );
    require_that(now_ms >= last_time, "publication time moved backwards")?;

    let operational = operational_bindings_v2(db, feed_id, now_ms).await?;
    let mut approvals: Vec<(RiskFeedBindingV2, CanonicalRiskDefinitionV2, RiskMappingProfileV2)> = Vec::new();
    for binding in operational {
        let (definition, profile) = approved(db, feed_id, &binding).await?;
        approvals.push((binding, definition, profile));
    }
    // Per channel: mature episodes first, then the newest target start, then identity.
    approvals.sort_by_key(|entry| selection_rank(&entry.0, &entry.1, now_ms));

    let mut bindings: Vec<RiskFeedBindingV2> = Vec::new();
    let mut collected: Vec<RiskFeedSignalV2> = Vec::new();
    let mut snapshots: Vec<(Vec<Value>, String)> = Vec::new();
    let mut selected: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    let mut withheld: std::collections::BTreeMap<String, String> = std::collections::BTreeMap::new();
    let mut expiry = now_ms + FEED_TTL_MS;
    for (binding, _definition, profile) in &approvals {
        // An older overlapping episode stays retained but is not published twice.
        if selected.contains(&binding.channel) {
            continue;
        }
        let params: Vec<Value> = vec![
            json!(binding.authorization_valid_from_ms),
            json!(now_ms),
            json!(now_ms),
            json!(now_ms),
            json!(binding.forecast_id),
            json!(now_ms),
            json!(now_ms),
        ];
        let found = db.first(SNAPSHOT_SQL, &params).await.map_err(|_| storage())?;
        let Some(found) = found else {
            withheld.insert(binding.channel.clone(), "question not currently eligible".to_string());
            continue;
        };
        let snapshot = db::get(&found, "snapshot").as_str().unwrap_or("").to_string();
        let decoded: Value = serde_json::from_str(&snapshot).unwrap_or(Value::Null);
        let mut clock: Option<Value> = None;
        if !decoded["ai"].is_null() {
            // The retained estimate artifact's identity is the hash of its own canonical body.
            let estimate: Value = serde_json::from_str(decoded["ai"].as_str().unwrap_or("")).unwrap_or(Value::Null);
            let hash = content_hash(&estimate).map_err(FeedError::from)?;
            let clock_row = db.first(CLOCK_SQL, &[json!(hash)]).await.map_err(|_| storage())?;
            if let Some(clock_row) = clock_row {
                clock = serde_json::from_str(db::text(&clock_row, "body").unwrap_or("")).ok();
            }
        }
        let values: Vec<RiskFeedSignalV2> = signals_v2(binding, &decoded, clock.as_ref(), now_ms)?
            .into_iter()
            .filter(|signal| now_ms - freshness_as_of_ms(signal) < profile.max_forecast_age_ms)
            .collect();
        if values.is_empty() {
            withheld.insert(
                binding.channel.clone(),
                "no fresh estimate with complete clock provenance".to_string(),
            );
            continue;
        }
        // The expiry is the *earliest* instant at which the payload would misrepresent itself:
        // a signal that ages out, or a binding whose operational window closes.
        expiry = expiry.min(binding.operational_valid_until_ms);
        for signal in &values {
            expiry = expiry.min(freshness_as_of_ms(signal) + profile.max_forecast_age_ms);
        }
        bindings.push(binding.clone());
        selected.insert(binding.channel.clone());
        withheld.remove(&binding.channel);
        collected.extend(values);
        snapshots.push((params, snapshot));
    }
    require_that(expiry > now_ms, "every current estimate expires before publication")?;
    require_that(bindings.len() <= 12, "feed binding capacity exceeded")?;
    bindings.sort_by(|a, b| a.binding_id.cmp(&b.binding_id));
    let covered: std::collections::BTreeMap<String, String> = bindings
        .iter()
        .map(|binding| (binding.channel.clone(), binding.binding_id.clone()))
        .collect();
    let channel_coverage = coverage(db, feed_id, &covered, &withheld).await?;
    let profile_hashes: Vec<String> = db
        .all(PROFILE_SET_SQL, &[json!(feed_id)])
        .await
        .map_err(|_| storage())?
        .iter()
        .filter_map(|row| db::text(row, "profile_hash").map(str::to_string))
        .collect();
    collected.sort_by(|a, b| (&a.binding_id, &a.source).cmp(&(&b.binding_id, &b.source)));
    let payload = RiskFeedPayloadV2 {
        schema_version: 1,
        purpose: PURPOSE.to_string(),
        genesis_hash: genesis_hash.to_string(),
        feed_id: feed_id.to_string(),
        sequence: sequence + 1,
        key_id: key_id.to_string(),
        issued_at_ms: now_ms,
        expires_at_ms: expiry,
        bindings: bindings.clone(),
        signals: collected,
        channel_coverage,
        profile_set_hash: profile_set_hash(feed_id, &profile_hashes).map_err(|error| FeedError(error.to_string()))?,
        weight_set_hash: weight_set_hash.to_string(),
        weight_set_version: weight_set_version.to_string(),
        calibration_cohort_id: calibration_cohort_id.to_string(),
    };
    payload.validate().map_err(FeedError::from)?;
    let signature = signer(signing_bytes_v2(&payload).map_err(FeedError::from)?)
        .await
        .map_err(|_| FeedError("risk feed signer unavailable".to_string()))?;
    require_that(signature.len() == 64, "signer returned invalid Ed25519 signature")?;
    let envelope = SignedRiskFeedV2 {
        schema_version: 1,
        payload,
        public_key_hex: public_key_hex.to_string(),
        signature_hex: hex::encode(&signature),
    };
    let digest = content_hash(&envelope.payload).map_err(FeedError::from)?;
    let guard = format!("risk-feed-v2:{digest}");
    let mut statements: Vec<(String, Vec<Value>)> = vec![(
        concat!(
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM risk_feed_heads ",
            "WHERE feed_id=? AND sequence=?) THEN 1 ELSE 0 END",
        )
        .to_string(),
        vec![json!(guard), json!(feed_id), json!(sequence)],
    )];
    for binding in &bindings {
        statements.push((
            concat!(
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS(",
                "SELECT 1 FROM risk_feed_binding_revocations_v2 WHERE binding_id=?) THEN 1 ELSE 0 END",
            )
            .to_string(),
            vec![
                json!(format!("{guard}:binding:{}", binding.binding_id)),
                json!(binding.binding_id),
            ],
        ));
    }
    for (index, (params, snapshot)) in snapshots.iter().enumerate() {
        let mut bound = vec![json!(format!("{guard}:{index}"))];
        bound.extend(params.iter().cloned());
        bound.push(json!(snapshot));
        statements.push((
            format!("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN ({SNAPSHOT_SQL})=? THEN 1 ELSE 0 END"),
            bound,
        ));
    }
    // Every guard token is already known, so the delete lists them: a `LIKE` would treat a `%` or
    // `_` inside a binding id as a pattern.
    let tokens: Vec<Value> = statements.iter().map(|statement| statement.1[0].clone()).collect();
    statements.extend([
        (
            concat!(
                "INSERT INTO risk_feed_publications_v2(feed_id,sequence,payload_hash,envelope_json,created_at) ",
                "VALUES(?,?,?,?,?)",
            )
            .to_string(),
            vec![
                json!(feed_id),
                json!(sequence + 1),
                json!(digest),
                json!(dumps(&envelope)),
                json!(now_ms),
            ],
        ),
        (
            "UPDATE risk_feed_heads SET sequence=? WHERE feed_id=? AND sequence=?".to_string(),
            vec![json!(sequence + 1), json!(feed_id), json!(sequence)],
        ),
        (
            format!(
                "DELETE FROM mutation_guards WHERE token IN ({})",
                vec!["?"; tokens.len()].join(",")
            ),
            tokens,
        ),
    ]);
    db.batch(&statements)
        .await
        .map_err(|_| FeedError("risk feed publication did not persist".to_string()))?;
    Ok(envelope)
}

/// `configure_operation`. The weight set has to be an *admitted* artifact whose body names the
/// version being configured; a reference to a set nobody retained is a reference to nothing.
#[allow(clippy::too_many_arguments)]
pub async fn configure_operation(
    db: &dyn Database,
    feed_id: &str,
    weight_set_hash: &str,
    weight_set_version: &str,
    calibration_cohort_id: &str,
    enabled: bool,
    configured_by: &str,
    now_ms: i64,
) -> Result<(), FeedError> {
    actor(configured_by)?;
    feed(feed_id)?;
    require_that(
        weight_set_hash.len() == 64
            && weight_set_hash
                .chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
        "invalid weight set hash",
    )?;
    require_that(
        valid_feed_id(weight_set_version) && valid_feed_id(calibration_cohort_id),
        "invalid weight version or cohort identity",
    )?;
    let weight = db
        .first(
            "SELECT body FROM artifacts WHERE hash=? AND kind='risk-weight-set'",
            &[json!(weight_set_hash)],
        )
        .await
        .map_err(|_| storage())?;
    let admitted = weight.as_ref().is_some_and(|row| {
        serde_json::from_str::<Value>(db::text(row, "body").unwrap_or(""))
            .ok()
            .and_then(|body| body["version"].as_str().map(str::to_string))
            .as_deref()
            == Some(weight_set_version)
    });
    require_that(admitted, "weight reference is not admitted")?;
    db.execute(
        concat!(
            "INSERT INTO risk_feed_operations_v2(feed_id,weight_set_hash,weight_set_version,calibration_cohort_id,enabled,",
            "configured_by,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(feed_id) DO UPDATE SET weight_set_hash=excluded.",
            "weight_set_hash,weight_set_version=excluded.weight_set_version,calibration_cohort_id=excluded.",
            "calibration_cohort_id,enabled=excluded.enabled,configured_by=excluded.configured_by,updated_at=excluded.updated_at",
        ),
        &[
            json!(feed_id),
            json!(weight_set_hash),
            json!(weight_set_version),
            json!(calibration_cohort_id),
            json!(i64::from(enabled)),
            json!(configured_by),
            json!(now_ms),
        ],
    )
    .await
    .map_err(|_| storage())?;
    Ok(())
}

/// `stale_bindings_v2`: bindings operational now or within half a forecast age whose clock-backed
/// estimate is missing or aging. The SQL is the reference's own, asserted in `risk_ops`; the clock
/// decides, which is why the candidates are only candidates.
pub async fn stale_bindings_v2(db: &dyn Database, feed_id: &str, now_ms: i64) -> Result<Vec<String>, FeedError> {
    let rows = db
        .all(STALE_CANDIDATE_SQL, &[json!(feed_id), json!(now_ms)])
        .await
        .map_err(|_| storage())?;
    let mut stale = Vec::new();
    for row in rows {
        let binding =
            RiskFeedBindingV2::from_json(db::text(&row, "binding_json").unwrap_or("")).map_err(FeedError::from)?;
        let profile =
            RiskMappingProfileV2::from_json(db::text(&row, "profile_json").unwrap_or("")).map_err(FeedError::from)?;
        // An exact-dated binding past its target start can never be refreshed into something as of
        // the target start, so refreshing it would spend budget to publish nothing.
        if !worth_clock_check(&binding, &profile, now_ms) {
            continue;
        }
        let latest = db
            .first(LATEST_CLOCK_SQL, &[json!(binding.forecast_id)])
            .await
            .map_err(|_| storage())?;
        let as_of = latest.as_ref().and_then(|row| db::int(row, "as_of"));
        if clock_is_stale(&profile, as_of, now_ms) {
            stale.push(binding.binding_id);
        }
    }
    Ok(stale)
}

/// `seed`: `risk_feed_series.create_due_episodes`' seeder. `publish`: the feed's own publication
/// call. Both are injected because each reaches outside this module, and a tick that reached for
/// them directly could not be run against a fixture.
pub type SeedCallback = crate::risk_feed_series::SeedCallback;
pub type PublishCallback = dyn Fn(&str, &str, &str, &str) -> BoxFuture<Result<SignedRiskFeedV2, ()>>;

/// `operate_feeds_v2`: one scheduled tick — due episodes, at most one budgeted refresh per feed,
/// then a signed publication.
///
/// Each phase is timed and retained with the tick. A tick was measured taking 130 seconds with no
/// refresh and no episode due, so the time was not being spent where the work was and there was no
/// way to tell where it was. These phases say where, from inside the Worker, which is the only
/// place that can see a cold start.
pub async fn operate_feeds_v2(
    db: &dyn Database,
    now_ms: i64,
    refresh: &dyn Fn(String) -> BoxFuture<Result<(), ()>>,
    publish: &PublishCallback,
    seed: Option<&SeedCallback>,
) -> Result<Vec<Value>, FeedError> {
    let started = instant();
    let episodes = match seed {
        Some(seed) => crate::risk_feed_series::create_due_episodes(db, now_ms, seed).await?,
        None => Vec::new(),
    };
    let episodes_ms = instant() - started;
    let listed = instant();
    let feeds = db
        .all(
            "SELECT * FROM risk_feed_operations_v2 WHERE enabled=1 ORDER BY feed_id LIMIT 8",
            &[],
        )
        .await
        .map_err(|_| storage())?;
    let mut outcomes = Vec::new();
    for feed in feeds {
        let feed_id = db::text(&feed, "feed_id").unwrap_or("").to_string();
        let interesting: Vec<Value> = episodes
            .iter()
            .filter(|episode| !episode["created"].is_null() || !episode["failure"].is_null())
            .cloned()
            .collect();
        let mut outcome = json!({
            "feedId": feed_id, "refreshed": Value::Null, "published": Value::Null,
            "episodes": interesting,
            "phaseMs": {"episodes": episodes_ms, "list": instant() - listed},
        });
        let mark = instant();
        let stale = stale_bindings_v2(db, &feed_id, now_ms).await?;
        outcome["phaseMs"]["stale"] = json!(instant() - mark);
        if let Some(first) = stale.first() {
            let mark = instant();
            match refresh(first.clone()).await {
                Ok(()) => outcome["refreshed"] = json!(first),
                // Budget, lease or provider failure: the publication still reports honestly.
                Err(()) => outcome["refreshFailure"] = json!("RefreshUnavailable"),
            }
            outcome["phaseMs"]["refresh"] = json!(instant() - mark);
        }
        let mark = instant();
        let published = publish(
            &feed_id,
            db::text(&feed, "weight_set_hash").unwrap_or(""),
            db::text(&feed, "weight_set_version").unwrap_or(""),
            db::text(&feed, "calibration_cohort_id").unwrap_or(""),
        )
        .await;
        match published {
            Ok(envelope) => {
                outcome["published"] = json!(envelope.payload.sequence);
                outcome["covered"] = json!(envelope
                    .payload
                    .channel_coverage
                    .iter()
                    .filter(|row| row.status == "covered")
                    .map(|row| row.channel.clone())
                    .collect::<Vec<_>>());
            }
            Err(()) => outcome["publishFailure"] = json!("PublishUnavailable"),
        }
        outcome["phaseMs"]["publish"] = json!(instant() - mark);
        db.execute(
            "INSERT OR IGNORE INTO risk_feed_operation_log_v2(feed_id,tick_at,outcome,detail) VALUES(?,?,?,?)",
            &[
                json!(feed_id),
                json!(now_ms),
                json!(if outcome["published"].is_null() {
                    "withheld"
                } else {
                    "published"
                }),
                json!(crate::risk_feed_series::detail_json(&outcome)),
            ],
        )
        .await
        .map_err(|_| storage())?;
        outcomes.push(outcome);
    }
    Ok(outcomes)
}

/// Milliseconds since an arbitrary origin, for the phase timings the tick retains.
fn instant() -> i64 {
    #[cfg(target_arch = "wasm32")]
    {
        worker::js_sys::Date::now() as i64
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_millis() as i64)
            .unwrap_or(0)
    }
}

/// `operations_health`: the operator view of the pipeline — feed age, tick failures, series
/// schedule, source-watch staleness.
///
/// Two decisions are visible in it. Staleness is scaled to each source's own interval, because a
/// flat five minutes on a sixty-minute publisher is a jitter detector rather than a health signal.
/// And a failed attempt for an episode that then published is the retry working, not a problem, so
/// it is not counted — counting those held the pipeline at degraded while both series were
/// producing episodes.
pub async fn operations_health(db: &dyn Database, now_ms: i64) -> Result<Value, FeedError> {
    let mut feeds = Vec::new();
    for feed in db
        .all("SELECT * FROM risk_feed_operations_v2 ORDER BY feed_id LIMIT 8", &[])
        .await
        .map_err(|_| storage())?
    {
        let feed_id = db::text(&feed, "feed_id").unwrap_or("").to_string();
        let latest = db
            .first(
                concat!(
                    "SELECT sequence,created_at,envelope_json FROM risk_feed_publications_v2 WHERE feed_id=? ",
                    "ORDER BY sequence DESC LIMIT 1",
                ),
                &[json!(feed_id)],
            )
            .await
            .map_err(|_| storage())?;
        let recent = db
            .all(
                concat!(
                    "SELECT outcome,detail FROM risk_feed_operation_log_v2 WHERE feed_id=? AND tick_at>? ",
                    "ORDER BY tick_at DESC LIMIT 30",
                ),
                &[json!(feed_id), json!(now_ms - 1_800_000)],
            )
            .await
            .map_err(|_| storage())?;
        let covered = latest.as_ref().and_then(|row| {
            serde_json::from_str::<Value>(db::text(row, "envelope_json").unwrap_or(""))
                .ok()
                .map(|envelope| {
                    envelope["payload"]["channel_coverage"]
                        .as_array()
                        .map(|rows| {
                            rows.iter()
                                .filter(|row| row["status"] == json!("covered"))
                                .filter_map(|row| row["channel"].as_str().map(str::to_string))
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default()
                })
        });
        let failed = recent
            .iter()
            .filter(|row| db::text(row, "detail").is_some_and(|detail| detail.contains("Failure")))
            .count();
        feeds.push(json!({
            "feedId": feed_id,
            "enabled": db::int(&feed, "enabled").unwrap_or(0) != 0,
            "latestSequence": latest.as_ref().and_then(|row| db::int(row, "sequence")),
            "latestAgeMs": latest.as_ref().and_then(|row| db::int(row, "created_at")).map(|created| now_ms - created),
            "coveredChannels": covered,
            "ticksLast30m": recent.len(),
            "failedTicksLast30m": failed,
        }));
    }
    let mut series = Vec::new();
    for row in db
        .all(
            "SELECT series_id,enabled,series_json FROM risk_feed_series_v2 ORDER BY series_id LIMIT 16",
            &[],
        )
        .await
        .map_err(|_| storage())?
    {
        let series_id = db::text(&row, "series_id").unwrap_or("").to_string();
        let latest_start = db
            .first(
                concat!(
                    "SELECT MAX(json_extract(binding_json,'$.target_start_ms')) AS start FROM risk_feed_bindings_v2 b ",
                    "WHERE json_extract(binding_json,'$.series_id')=? AND NOT EXISTS(",
                    "SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id)",
                ),
                &[json!(series_id)],
            )
            .await
            .map_err(|_| storage())?;
        let failures = db
            .first(
                concat!(
                    "SELECT COUNT(*) AS n FROM risk_feed_series_log_v2 l WHERE l.series_id=? AND l.attempted_at>? ",
                    "AND l.outcome LIKE 'failed:%' AND NOT EXISTS(SELECT 1 FROM risk_feed_bindings_v2 b ",
                    "WHERE json_extract(b.binding_json,'$.series_id')=l.series_id ",
                    "AND json_extract(b.binding_json,'$.target_start_ms')=l.target_start_ms)",
                ),
                &[json!(series_id), json!(now_ms - 86_400_000)],
            )
            .await
            .map_err(|_| storage())?;
        let cadence = serde_json::from_str::<Value>(db::text(&row, "series_json").unwrap_or(""))
            .ok()
            .and_then(|body| body["cadence_ms"].as_i64());
        let start = latest_start.as_ref().and_then(|row| db::int(row, "start"));
        series.push(json!({
            "seriesId": series_id,
            "enabled": db::int(&row, "enabled").unwrap_or(0) != 0,
            "latestEpisodeStartMs": start,
            "nextEpisodeStartMs": start.and_then(|start| cadence.map(|cadence| start + cadence)),
            "failedUnpublishedAttemptsLast24h": failures.as_ref().and_then(|row| db::int(row, "n")).unwrap_or(0),
        }));
    }
    let sources = db
        .first(
            concat!(
                "SELECT COUNT(*) AS total, SUM(failure_count>0) AS failing, ",
                "SUM(failure_count=0 AND (checked_at IS NULL OR checked_at<?-interval_ms-MAX(300000,interval_ms/4))) AS stale ",
                "FROM official_watch_sources WHERE enabled=1",
            ),
            &[json!(now_ms)],
        )
        .await
        .map_err(|_| storage())?;
    let stuck = db
        .first("SELECT COUNT(*) AS n FROM forecasts WHERE job_error IS NOT NULL", &[])
        .await
        .map_err(|_| storage())?;
    // A forecast carrying a `job_error` has failed an attempt and not been cleared since. These
    // accumulate silently — nothing counts them — so they are reported, not alerted on, because
    // they need a decision rather than a wake-up call.
    Ok(json!({
        "serverTime": now_ms,
        "feeds": feeds,
        "series": series,
        "stuckForecasts": stuck.as_ref().and_then(|row| db::int(row, "n")).unwrap_or(0),
        "sourceWatch": sources.as_ref().map(|row| json!({
            "total": db::int(row, "total").unwrap_or(0),
            "failing": db::int(row, "failing").unwrap_or(0),
            "stale": db::int(row, "stale").unwrap_or(0),
        })),
    }))
}

/// `training_export`: finalized, unheld canonical questions with the exact signed estimates the
/// feed carried.
///
/// The labels come only from the finalization event and the finalized outcome, and eligibility
/// waits for any open decision. Every retained signal for a binding is exported so a consumer can
/// deduplicate by source and train against the numbers it *once consumed*, never re-derived ones.
pub async fn training_export(db: &dyn Database, feed_id: &str, now_ms: i64, limit: i64) -> Result<Value, FeedError> {
    require_that((1..=2000).contains(&limit), "invalid export bound")?;
    let rows = db
        .all(
            concat!(
                "SELECT b.binding_id,b.binding_json,f.id AS forecast_id,f.finalized_outcome,z.finalized_at,",
                " MAX(z.finalized_at,COALESCE(c.created_at,0)) AS eligibility_at ",
                "FROM risk_feed_bindings_v2 b JOIN forecasts f ON f.id=b.forecast_id ",
                "JOIN forecast_quality_finalizations z ON z.forecast_id=f.id ",
                "LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=f.id ",
                "LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id ",
                "WHERE b.feed_id=? AND f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome IN ('YES','NO') ",
                "AND (d.id IS NULL OR c.decision_id IS NOT NULL) ",
                "AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id) ",
                "ORDER BY z.finalized_at,b.binding_id LIMIT ?",
            ),
            &[json!(feed_id), json!(limit)],
        )
        .await
        .map_err(|_| storage())?;
    let mut bindings = Vec::new();
    for row in rows {
        let binding_id = db::text(&row, "binding_id").unwrap_or("").to_string();
        let publications = db
            .all(
                concat!(
                    "SELECT sequence,created_at,envelope_json FROM risk_feed_publications_v2 WHERE feed_id=? ",
                    "AND envelope_json LIKE ? ORDER BY sequence LIMIT 5000",
                ),
                &[json!(feed_id), json!(format!("%\"{binding_id}\"%"))],
            )
            .await
            .map_err(|_| storage())?;
        let mut seen: std::collections::BTreeMap<String, Value> = std::collections::BTreeMap::new();
        for publication in &publications {
            let envelope: Value =
                serde_json::from_str(db::text(publication, "envelope_json").unwrap_or("")).unwrap_or(Value::Null);
            for signal in envelope["payload"]["signals"].as_array().cloned().unwrap_or_default() {
                let Some(source) = signal["source"].as_str().map(str::to_string) else {
                    continue;
                };
                if signal["binding_id"] == json!(binding_id) && !seen.contains_key(&source) {
                    let mut entry = signal.clone();
                    entry["sequence"] = db::get(publication, "sequence").clone();
                    seen.insert(source, entry);
                }
            }
        }
        bindings.push(json!({
            "binding": serde_json::from_str::<Value>(db::text(&row, "binding_json").unwrap_or("")).unwrap_or(Value::Null),
            "forecastId": db::text(&row, "forecast_id"),
            "outcome": db::text(&row, "finalized_outcome"),
            "finalizedAtMs": db::int(&row, "finalized_at"),
            "eligibilityAtMs": db::int(&row, "eligibility_at"),
            "firstSignals": seen.into_values().collect::<Vec<_>>(),
        }));
    }
    Ok(json!({
        "feedId": feed_id, "exportedAtMs": now_ms, "bindings": bindings,
        "policy": "first retained signal per source",
    }))
}

/// `latest_feed_v2`: the newest publication, or `None` for a feed that has never published.
pub async fn latest_feed_v2(db: &dyn Database, feed_id: &str) -> Result<Option<SignedRiskFeedV2>, FeedError> {
    let row = db
        .first(
            "SELECT envelope_json FROM risk_feed_publications_v2 WHERE feed_id=? ORDER BY sequence DESC LIMIT 1",
            &[json!(feed_id)],
        )
        .await
        .map_err(|_| storage())?;
    match row {
        Some(row) => Ok(Some(
            SignedRiskFeedV2::from_json(db::text(&row, "envelope_json").unwrap_or("")).map_err(FeedError::from)?,
        )),
        None => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use sha2::{Digest, Sha256};
    use std::cell::RefCell;

    const GENESIS: &str = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1";

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/risk-feed-v2-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("risk feed v2 golden")).expect("json")
    }

    /// The same synthetic signer the vector used, so the recorded signature is a digest of the
    /// exact payload rather than of a constant.
    fn sign(data: &[u8]) -> Vec<u8> {
        let first = Sha256::digest(data);
        let mut second = Sha256::new();
        second.update(b"second:");
        second.update(data);
        [first.to_vec(), second.finalize().to_vec()].concat()
    }

    fn restore(db: &Sqlite, table: &str, rows: &[Value]) {
        for row in rows {
            let fields = row.as_object().expect("a fixture row is an object");
            let columns: Vec<&str> = fields.keys().map(String::as_str).collect();
            let placeholders = vec!["?"; columns.len()].join(",");
            let params: Vec<Value> = columns.iter().map(|name| fields[*name].clone()).collect();
            db.run(
                &format!("INSERT INTO {table}({}) VALUES({placeholders})", columns.join(",")),
                &params,
            )
            .unwrap_or_else(|error| panic!("{table}: {error}"));
        }
    }

    fn envelope_of(envelope: SignedRiskFeedV2) -> Value {
        serde_json::to_value(&envelope).expect("an envelope is serializable")
    }

    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, FeedError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
        match produced {
            Ok(value) => {
                assert!(
                    entry["error"].is_null(),
                    "{name}: succeeded where the reference refused"
                );
                assert_eq!(value, entry["result"], "{name}: a different result");
            }
            Err(error) => {
                assert!(
                    !entry["error"].is_null(),
                    "{name}: refused with {error:?} where the reference succeeded"
                );
                assert_eq!(
                    error.0,
                    entry["error"]["message"].as_str().unwrap(),
                    "{name}: a different refusal"
                );
            }
        }
        *index += 1;
    }

    #[test]
    fn the_reference_v2_producer_is_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let db = Sqlite::from_migrations();
        // The snapshot the approval gate reads, restored before anything else.
        for table in ["users", "forecasts", "artifacts"] {
            restore(
                &db,
                table,
                document["rows"][table]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .as_slice(),
            );
        }
        let forecast = document["rows"]["forecasts"][0]
            .as_object()
            .cloned()
            .expect("a forecast row");
        let now = db::int(&forecast, "open_at").unwrap();
        // The admitted definition, profile and binding come out of the vector: the reference's own
        // serializations are the fixture, so a port cannot be run against a different one.
        let definition = CanonicalRiskDefinitionV2::from_json(
            document["rows"]["risk_feed_definitions_v2"][0]["definition_json"]
                .as_str()
                .unwrap_or(""),
        )
        .expect("the admitted definition");
        let profile = RiskMappingProfileV2::from_json(
            document["rows"]["risk_feed_profiles_v2"][0]["profile_json"]
                .as_str()
                .unwrap_or(""),
        )
        .expect("the admitted profile");
        // `operational:after` answers with the binding the approval wrote, which is the record the
        // rest of the replay uses. It is looked up by name rather than by position: a vector that
        // gained a case would otherwise silently hand this the wrong call's result.
        let after = calls
            .iter()
            .find(|entry| entry["call"] == json!("operational:after"))
            .expect("the operational listing");
        let binding: RiskFeedBindingV2 =
            serde_json::from_value(after["result"][0].clone()).expect("the admitted binding");

        let signed: RefCell<Vec<Value>> = RefCell::new(Vec::new());
        let signer = |data: Vec<u8>| -> BoxFuture<Result<Vec<u8>, ()>> {
            signed.borrow_mut().push(json!(hex::encode(&data)));
            Box::pin(async move { Ok(sign(&data)) })
        };
        let mut index = 0usize;

        // --- admission.
        check(
            &calls,
            &mut index,
            block(admit_definition(&db, "risk-v2", &definition, "  ", now)).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(admit_definition(&db, "risk v2", &definition, "admin", now)).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(admit_definition(
                &db,
                "risk-v2",
                &definition,
                "authenticated-admin",
                now,
            ))
            .map(|d| json!(d)),
        );
        check(
            &calls,
            &mut index,
            block(admit_definition(
                &db,
                "risk-v2",
                &definition,
                "authenticated-admin",
                now,
            ))
            .map(|d| json!(d)),
        );
        check(
            &calls,
            &mut index,
            block(admit_profile(&db, "risk-v2", &profile, "authenticated-admin", now)).map(|d| json!(d)),
        );

        // --- the approval gate: every way the typed target can disagree with the question. The
        // record has to stay *valid* for the gate to be what refuses it, so the two interval cases
        // move the operational bounds with the target.
        // One way a binding can disagree with its question, applied to a copy of the record.
        type Adjust = fn(&mut RiskFeedBindingV2);
        let adjust: [(&str, Adjust); 6] = [
            ("b1", |b| {
                b.binding_id = "b1".to_string();
                b.target_start_ms += 300_000;
                b.operational_valid_from_ms += 300_000;
            }),
            ("b2", |b| {
                b.binding_id = "b2".to_string();
                b.target_end_ms -= 300_000;
                b.operational_valid_until_ms -= 300_000;
            }),
            ("b3", |b| {
                b.binding_id = "b3".to_string();
                b.definition_hash = "0".repeat(64);
            }),
            ("b4", |b| {
                b.binding_id = "b4".to_string();
                b.mapping_profile_hash = "0".repeat(64);
            }),
            ("b5", |b| {
                b.binding_id = "b5".to_string();
                b.question_event_definition_hash = "0".repeat(64);
            }),
            ("b6", |b| {
                b.binding_id = "b6".to_string();
                b.asset = "USDT".to_string();
            }),
        ];
        for (_, change) in adjust {
            let mut candidate = binding.clone();
            change(&mut candidate);
            check(
                &calls,
                &mut index,
                block(approve_binding_v2(&db, "risk-v2", &candidate, "admin", now)).map(|_| Value::Null),
            );
        }
        check(
            &calls,
            &mut index,
            block(approve_binding_v2(
                &db,
                "risk-v2",
                &binding,
                "admin",
                binding.authorization_valid_from_ms - 1,
            ))
            .map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(approve_binding_v2(&db, "risk-v2", &binding, "authenticated-admin", now)).map(|_| Value::Null),
        );

        let listed = |at: i64| {
            block(operational_bindings_v2(&db, "risk-v2", at)).map(|bindings| {
                json!(bindings
                    .iter()
                    .map(|b| serde_json::to_value(b).unwrap())
                    .collect::<Vec<_>>())
            })
        };
        let publish = |at: i64| -> Result<Value, FeedError> {
            block(publish_feed_v2(
                &db,
                "risk-v2",
                GENESIS,
                "test-key",
                &"a".repeat(64),
                &signer,
                at,
                &"d".repeat(64),
                "source-calibration-v2",
                "usdc-depeg-1d-w48-containing",
            ))
            .map(envelope_of)
        };

        // --- before the operational window there is no episode at all.
        check(&calls, &mut index, listed(now));
        check(&calls, &mut index, publish(now));
        // --- the compile-time estimate carries no capture clock, so the channel reports a signed
        // `unavailable` rather than a manufactured probability.
        let opened = now + 2 * 3_600_000 + 1000;
        check(&calls, &mut index, listed(opened));
        check(&calls, &mut index, publish(opened));
        check(
            &calls,
            &mut index,
            block(stale_bindings_v2(&db, "risk-v2", opened)).map(|ids| json!(ids)),
        );
        check(
            &calls,
            &mut index,
            block(latest_feed_v2(&db, "risk-v2")).map(|envelope| match envelope {
                Some(envelope) => envelope_of(envelope),
                None => Value::Null,
            }),
        );

        // --- revocation stops the next publication from carrying the binding.
        check(
            &calls,
            &mut index,
            block(revoke_binding_v2(&db, &binding.binding_id, "", opened, "operator")).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(revoke_binding_v2(
                &db,
                &binding.binding_id,
                "authenticated-admin",
                opened,
                "operator withdrew the mapped question",
            ))
            .map(|_| Value::Null),
        );
        check(&calls, &mut index, listed(opened));

        assert_eq!(index, calls.len(), "every recorded call is replayed");
        // The signature is over the text, so a port that assembled the payload and signed something
        // else would match the envelope and fail here.
        assert_eq!(
            json!(*signed.borrow()),
            document["signed"],
            "the signer saw different bytes"
        );
        for (table, expected) in document["rows"].as_object().expect("rows") {
            if table == "users" || table == "forecasts" || table == "artifacts" {
                continue; // Restored above; the producer does not write them.
            }
            let (rows, _) = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .expect("rows");
            assert_eq!(json!(rows), *expected, "{table}: different rows");
        }
    }
}
