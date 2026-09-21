//! `run_due_jobs` / `_advance_job`: the loop that moves a forecast through its lifecycle.
//!
//! Two orderings in here are load-bearing, and both were found by things going wrong:
//!
//! - A timing review that has already settled the only answer its evidence supports is closed
//!   **before** the blocker guard is consulted. The guard reads a view, and the review could only
//!   be closed from inside the state the review was blocking entry to — which is why two forecasts
//!   sat unresolvable for a day rather than retrying into a different outcome.
//! - The hold is taken before any AI is scheduled. A model cannot be the thing that stops
//!   participation, because a model is the thing being waited on.
//!
//! The lease exists for the same reason: one worker may be inside an AI call for a forecast at a
//! time, and the database says so rather than this process remembering.
//!
//! **What the vectors reach, and what only a reading does.** The resolution, dispute and sweep
//! goldens drive `run_due_jobs` over a v1 snapshot, which is every state the *default* lifecycle
//! passes through. Two arms are not covered by any of them — a v2 question proposing from
//! `RESOLVING`, and a paused job recovering its `RESOLVING` work — and both were wrong until they
//! were read against the reference: each called the v1 pipeline for a v2 snapshot, because the
//! snapshot had been narrowed to its base before the branch that distinguishes them. (A v2
//! question reaches `RESOLVING` the ordinary way: `LOCKED` applies `BeginResolution` in both
//! languages, and the proposal — the branch in question — belongs to the arm after it.) The branch
//! is now the reference's own, verified by that reading and by nothing else.
//!
//! A vector for it is *reachable* and was half-built: an `AutomationIntegrationTests` fixture whose
//! forecast is upgraded by `automation.accept` becomes a real `ForecastV2` in `LOCKED`, and
//! `run_due_jobs` then drives the arm — with the clock past the trigger's `qualified_at_ms`, and
//! with the coordinator replaced by the real one over the early pipeline's scripted conversation
//! (`[resolution_outputs()[1], counter()]`, as `generate_early_golden.proposal_case` scripts it).
//! Two blockers were found and passed. The first is the fixture's evidence *host*:
//! `reviewed_trigger`'s default article is not on an approved authoritative host, so the pipeline
//! refuses with `SourceRejected: Source host is not in the approved authoritative-source registry`
//! before it calls anyone — the Apple newsroom URL the automation golden already uses is. The
//! second is that `reviewed_trigger` binds its trigger to the *fixture's* forecast, so accepting it
//! against the Apple-rooted question it must be paired with answers `The forecast changed while the
//! event was reviewed`. A fixture that reaches this arm therefore needs `reviewed_trigger`'s shape
//! rebuilt for that forecast — evidence, verification, event times, qualification, qualifier and
//! counter-qualifier provenance — which is the next unit of work rather than a line of setup.

use serde_json::{json, Value};

use forecast_domain::lifecycle::Payload;
use forecast_domain::lifecycle::{AnyResolution, Snapshot};

use crate::ai::coordinator::Coordinator;
use crate::ai::early::propose_early_resolution;
use crate::ai::resolution::{propose_resolution, EvidenceFetcher};
use crate::db::{int, text, Database};
use crate::mutate::Statement;
use crate::resolution_timing::{close_indeterminate, status as timing_status};

pub const LEASE_MS: i64 = 300_000;
pub const CHALLENGE_MS: i64 = 48 * 3_600_000;
const DAY_MS: i64 = 86_400_000;
const MAX_TRANSITIONS_PER_LEASE: usize = 6;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Sweep {
    pub processed: i64,
    pub failed: i64,
    /// What the outbox delivered in the same pass. Named `effects` because that is what the
    /// reference calls it on the wire, and an operator reads that name.
    pub effects: i64,
}

/// `_process_outbox`: deliver what a finalized question owes — its reputation, its settlement and
/// the notification a participant reads.
///
/// The exactly-once rule is in the *selection*, not in a flag: every statement carries
/// `EXISTS(SELECT 1 FROM outbox WHERE id=? AND status='pending')` in its own `WHERE` clause and the
/// status flips in the same batch, so a replay selects nothing because the row is no longer
/// pending. A question whose evidence review is still open is excluded from the selection
/// entirely, which is why a blocked forecast is not scored, settled or announced while it waits.
///
/// `adapter_configured` is the reference's `self.registry is not None`. Without a chain adapter
/// there is nothing to hand a commitment to, so the row is left exactly where it is rather than
/// being marked as though something had taken it.
pub async fn process_outbox(
    db: &dyn Database,
    now_ms: i64,
    limit: i64,
    adapter_configured: bool,
) -> Result<i64, String> {
    if adapter_configured {
        db.execute(
            "UPDATE outbox SET status='processed',processed_at=? WHERE kind='RESOLUTION_COMMITMENT_REQUIRED'              AND status='awaiting_adapter' AND EXISTS (SELECT 1 FROM registry_delivery d JOIN registry_intents i              USING(forecast_id,revision) JOIN forecasts f ON f.id=d.forecast_id WHERE d.forecast_id=outbox.forecast_id              AND d.status='confirmed' AND d.revision=f.revision AND f.state IN ('FINALIZED','ARCHIVED')              AND json_extract(i.snapshot,'$.audit_head_hash')=json_extract(f.snapshot,'$.audit_head_hash'))",
            &[json!(now_ms)],
        )
        .await
        .map_err(|error| error.to_string())?;
    }
    let rows = db
        .all(
            "SELECT * FROM outbox WHERE status='pending' AND NOT EXISTS              (SELECT 1 FROM forecast_resolution_blockers b WHERE b.forecast_id=outbox.forecast_id)              ORDER BY created_at,id LIMIT ?",
            &[json!(limit)],
        )
        .await
        .map_err(|error| error.to_string())?;
    let mut processed = 0;
    for row in &rows {
        let id = row.get("id").cloned().unwrap_or(Value::Null);
        let forecast_id = text(row, "forecast_id").unwrap_or("").to_string();
        let now = now_ms;
        if text(row, "kind") == Some("RESOLUTION_COMMITMENT_REQUIRED") {
            // Handed to the adapter, which is a different thing from delivered: the status says so
            // rather than claiming the work is done.
            db.execute(
                "UPDATE outbox SET status='awaiting_adapter' WHERE id=? AND status='pending'",
                &[id],
            )
            .await
            .map_err(|error| error.to_string())?;
            continue;
        }
        let mut statements: Vec<Statement> = Vec::new();
        match text(row, "kind") {
            Some("REPUTATION_UPDATE_REQUIRED") => {
                statements.push((
                    "INSERT OR IGNORE INTO reputation_scores(forecast_id,user_id,category,outcome,probability,                     correct,brier_score,created_at) SELECT f.id,v.user_id,f.category,f.finalized_outcome,                     v.yes_probability,CASE WHEN f.finalized_outcome='INVALID' THEN NULL                      WHEN f.finalized_outcome=v.outcome THEN 1 ELSE 0 END,                     CASE WHEN f.finalized_outcome='INVALID' THEN NULL ELSE                      (v.yes_probability/100.0-CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END)*                     (v.yes_probability/100.0-CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END) END,?                      FROM forecasts f JOIN eligible_user_forecasts v ON v.forecast_id=f.id WHERE f.id=?                      AND f.state IN ('FINALIZED','ARCHIVED') AND EXISTS(SELECT 1 FROM outbox WHERE id=? AND status='pending')"
                        .to_string(),
                    vec![json!(now), json!(forecast_id), id.clone()],
                ));
                statements.extend(
                    crate::points::settlement_sql(&forecast_id, now).map_err(|error| error.message.to_string())?,
                );
            }
            Some("RESULT_NOTIFICATION_REQUIRED") => {
                statements.push((
                    "INSERT OR IGNORE INTO activity(id,user_id,forecast_id,kind,title,body,created_at)                      SELECT ?||':'||v.user_id,v.user_id,f.id,'forecast_finalized',f.title,                     'The forecast was finalized as '||f.finalized_outcome||'.',?                      FROM forecasts f JOIN eligible_user_forecasts v ON v.forecast_id=f.id WHERE f.id=?                      AND f.state IN ('FINALIZED','ARCHIVED') AND EXISTS(SELECT 1 FROM outbox WHERE id=? AND status='pending')"
                        .to_string(),
                    vec![id.clone(), json!(now), json!(forecast_id), id.clone()],
                ));
            }
            // A kind this version does not know is left pending rather than marked done: the row
            // is a claim nothing here can honour, and marking it processed would lose it.
            _ => continue,
        }
        statements.push((
            "UPDATE outbox SET status='processed',processed_at=? WHERE id=? AND status='pending'".to_string(),
            vec![json!(now), id],
        ));
        db.batch(&statements).await.map_err(|error| error.to_string())?;
        processed += 1;
    }
    Ok(processed)
}

/// The failure reason an operator reads. The generic message is the fallback and is wrong often
/// enough that every mapped code here was added after it sent a diagnosis somewhere else.
pub fn reason_for(code: &str) -> String {
    match code {
        "resolution_timing_review" | "early_eligibility_review" => "Evidence publication time and receipt eligibility are being reviewed. No result rewards or reputation will be credited until that review is complete.".to_string(),
        "resolution_timing_determined" => "The publication-time review is closed and determines INVALID. Only that result can be finalized, so the next attempt proposes it.".to_string(),
        "resolution_domain_rejected" => "The AI resolution was refused by the immutable domain checks, most often because it did not cite the clause matching its own outcome. The retained judge output names what it proposed.".to_string(),
        "ai_workflow_timeout" => "Resolution is on hold because the AI review timed out. Another review will follow the retry schedule and daily limit.".to_string(),
        // The reference branches on the *status* here (429), which covers both the daily AI
        // budget and the request-rate limit.
        "ai_daily_limit" | "rate_limited" => "Resolution is on hold because the daily AI limit was reached. Review will resume after the limit resets.".to_string(),
        _ => "Resolution is on hold because evidence or independent review is insufficient. No result will be finalized before another review.".to_string(),
    }
}

/// `_ai_lease`: one AI call per owner at a time, and the budget checked before it starts.
pub async fn ai_lease(
    db: &dyn Database,
    owner: &str,
    token: &str,
    now_ms: i64,
    daily_limit: i64,
) -> Result<(), String> {
    db.execute(
        "INSERT INTO ai_leases(owner,token,expires_at) VALUES(?,?,?) ON CONFLICT(owner) \
         DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at WHERE ai_leases.expires_at<=?",
        &[json!(owner), json!(token), json!(now_ms + LEASE_MS), json!(now_ms)],
    )
    .await
    .map_err(|error| error.to_string())?;
    let lease = db
        .first("SELECT token FROM ai_leases WHERE owner=?", &[json!(owner)])
        .await
        .map_err(|error| error.to_string())?;
    if lease.as_ref().and_then(|row| text(row, "token")) != Some(token) {
        return Err("ai_work_in_progress".to_string());
    }
    if rate_limit(db, "ai:global", daily_limit, DAY_MS, now_ms).await.is_err() {
        release_ai(db, owner, token).await;
        return Err("ai_daily_limit".to_string());
    }
    Ok(())
}

/// `rate_limit`: an atomic fixed-window counter, in one round trip.
///
/// The increment and the read are the same statement, so the bound holds across workers rather
/// than in whichever one happens to be counting.
pub async fn rate_limit(db: &dyn Database, scope: &str, limit: i64, window_ms: i64, now_ms: i64) -> Result<(), String> {
    let bucket = now_ms / window_ms;
    let rows = db
        .execute(
            "INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES(?,?,1,?) ON CONFLICT(scope,bucket) \
             DO UPDATE SET count=count+1 RETURNING count",
            &[json!(scope), json!(bucket), json!((bucket + 1) * window_ms)],
        )
        .await
        .map_err(|error| error.to_string())?;
    let count = rows.first().and_then(|row| int(row, "count")).unwrap_or(0);
    if count > limit {
        return Err("rate_limited".to_string());
    }
    Ok(())
}

pub async fn release_ai(db: &dyn Database, owner: &str, token: &str) {
    let _ = db
        .execute(
            "DELETE FROM ai_leases WHERE owner=? AND token=?",
            &[json!(owner), json!(token)],
        )
        .await;
}

/// What one leased attempt needs, named: eight positional arguments of which four are strings is
/// a call whose order nobody can check by reading it.
pub struct Job<'a> {
    pub db: &'a dyn Database,
    pub coordinator: &'a Coordinator,
    pub fetch: &'a EvidenceFetcher,
    pub forecast_id: &'a str,
    pub job_token: &'a str,
    pub now_ms: i64,
    /// The AI lease's token *source*, not a token: the reference mints one inside `_advance_job`,
    /// and only once it has decided this attempt needs a model at all. A token taken before that
    /// decision is a token the next caller cannot have.
    pub ai_tokens: &'a mut dyn FnMut() -> String,
    pub daily_limit: i64,
    /// `self.random_token`. A command's guard token comes from the application, as the reference
    /// takes it, rather than from this module's own source.
    pub token: &'a dyn Fn() -> String,
    /// The chain adapter, when one is configured. `None` is a real configuration: without it a
    /// finalize is a purely local decision.
    pub gate: Option<&'a dyn crate::mutate::FinalizationGate>,
    /// `self.now_ms`. A *callable*, because `_bounded_ai` measures its acceptance deadline against
    /// it: the cap is 240 seconds of wall clock, not of the pass's captured instant.
    pub clock: &'a dyn Fn() -> i64,
    /// Reading retained bytes. A dispute review re-reads the evidence it was filed against, so the
    /// reader travels with the job rather than being invented where it is used.
    pub reader: &'a dyn crate::ai::early::ArtifactReader,
}

/// What a failed attempt carries that a bare code does not.
///
/// The reference's exceptions are not a code. An AI refusal carries the artifacts it produced
/// before refusing, so a later reviewer can see what it proposed rather than only that it was
/// refused; and an outage carries *which* providers were unreachable, which is the difference
/// between one provider being down and all of them being down — and only the second is worth
/// pausing a question for. A port that reduced all of this to a string would lose exactly the
/// things the caller below branches on.
#[derive(Debug, Clone)]
pub enum JobFailure {
    /// `AIUnavailable`.
    AiUnavailable {
        providers: Vec<String>,
        artifacts: Vec<crate::ai::coordinator::Artifact>,
    },
    /// `AIRejected`, and everything this layer raises itself.
    Refused {
        code: String,
        artifacts: Vec<crate::ai::coordinator::Artifact>,
    },
}

impl From<String> for JobFailure {
    fn from(code: String) -> Self {
        JobFailure::Refused {
            code,
            artifacts: Vec::new(),
        }
    }
}

impl JobFailure {
    fn new(code: &str) -> Self {
        JobFailure::Refused {
            code: code.to_string(),
            artifacts: Vec::new(),
        }
    }

    /// The name the operator's reason is chosen by, and the one the deferral is recognised by.
    pub fn code(&self) -> &str {
        match self {
            JobFailure::AiUnavailable { .. } => "ai_unavailable",
            JobFailure::Refused { code, .. } => code,
        }
    }

    fn artifacts(&self) -> &[crate::ai::coordinator::Artifact] {
        match self {
            JobFailure::AiUnavailable { artifacts, .. } | JobFailure::Refused { artifacts, .. } => artifacts,
        }
    }

    fn providers(&self) -> &[String] {
        match self {
            JobFailure::AiUnavailable { providers, .. } => providers,
            JobFailure::Refused { .. } => &[],
        }
    }
}

/// One lease's worth of transitions, at most six, stopping when the state stops changing.
pub async fn advance_job(job: Job<'_>) -> Result<(), JobFailure> {
    let Job {
        db,
        coordinator,
        fetch,
        forecast_id,
        job_token,
        now_ms,
        ai_tokens,
        daily_limit,
        token,
        gate,
        clock,
        reader,
    } = job;
    for _ in 0..MAX_TRANSITIONS_PER_LEASE {
        let snapshot = load(db, forecast_id).await.map_err(JobFailure::from)?;
        // What this transition carries beyond the command: the judge's artifacts, and the evidence
        // the timing review reads. Both are per-lease, not accumulated across the loop.
        let mut extra: Vec<Statement> = Vec::new();
        let mut timing_artifacts: Vec<(String, String, String)> = Vec::new();
        let forecast = snapshot.base().clone();
        let state = forecast.state.clone();
        if state == "RESOLVING" {
            // Before the guard below, and for the reason given at the top of the file.
            let _ = close_indeterminate(db, &forecast, now_ms).await;
        }
        let blocked = db
            .first(
                "SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|error| JobFailure::from(error.to_string()))?
            .is_some();
        if blocked {
            return Err(JobFailure::new("early_eligibility_review"));
        }
        let key = format!("job:{}", forecast.revision);
        // A review carries its own instant: the review's `reviewed_at_ms`, not the pass's clock.
        let mut at = now_ms;
        let payload: Payload = match state.as_str() {
            // The early-resolution trigger is what makes a v2 lock; a v1 forecast locks without
            // one, and this port owns the v1 lifecycle.
            "OPEN" => Payload::Lock {
                schema_version: 1,
                trigger: None,
            },
            "LOCKED" => Payload::BeginResolution { schema_version: 1 },
            "RESOLVING" => {
                let owner = format!("resolution:{forecast_id}");
                let ai_token = ai_tokens();
                ai_lease(db, &owner, &ai_token, now_ms, daily_limit)
                    .await
                    .map_err(JobFailure::from)?;
                // Whether the evidence can be placed relative to participation is something this
                // layer knows and the judge cannot see. Without telling it, the judge keeps
                // answering YES or NO, the timing gate keeps refusing, and the forecast retries
                // forever — which is what happened to two of them.
                let timing = timing_status(db, forecast_id)
                    .await
                    .map_err(|_| JobFailure::new("resolution_timing_unavailable"))?;
                let indeterminate = timing["reason"] == json!("publication_time_unknown");
                let determined = timing["determination"].as_str();
                // `_bounded_ai`: the complete workflow finishes before its durable lease, and no
                // result arriving after the cap is accepted. This port cannot cancel the call the
                // way `asyncio.timeout` does — the runtime has no cancellation to offer — so what
                // it reproduces is the *acceptance* rule rather than the interruption: the work may
                // run long, and its answer is discarded rather than used.
                let started = (clock)();
                // A v2 question proposes an *early* resolution: the upgrade replaced the question
                // the judge is asked, and the payload's schema version is what says so. The
                // reference branches here on the snapshot's own type — and a port that read
                // `.base()` first, which is what this arm did, would lock a v2 question against
                // the question it used to be. The timing review above is still computed for both:
                // the reference computes it before the branch too, and it can refuse either.
                let outcome = match &snapshot {
                    Snapshot::V2(v2) => propose_early_resolution(coordinator, reader, v2, now_ms)
                        .await
                        .map(|result| (2, AnyResolution::Early(result.resolution), result.artifacts)),
                    Snapshot::V1(_) => {
                        propose_resolution(coordinator, fetch, &forecast, now_ms, indeterminate, determined)
                            .await
                            .map(|result| (1, AnyResolution::Standard(result.resolution), result.artifacts))
                    }
                };
                release_ai(db, &owner, &ai_token).await;
                if workflow_deadline_passed(started, (clock)()) {
                    return Err(JobFailure::new("ai_workflow_timeout"));
                }
                let (schema_version, resolution, artifacts) = outcome.map_err(|error| match error {
                    crate::ai::coordinator::CoordinatorError::Unavailable { providers, artifacts } => {
                        JobFailure::AiUnavailable { providers, artifacts }
                    }
                    crate::ai::coordinator::CoordinatorError::Rejected { code, artifacts, .. } => {
                        JobFailure::Refused { code, artifacts }
                    }
                })?;
                if let Ok(retained) = serde_json::to_value(&resolution) {
                    if let Ok(artifact) = crate::ai::coordinator::artifact("resolution", &retained) {
                        let _ = retain(db, &artifact, now_ms).await;
                    }
                }
                // The artifacts the judge produced travel with the command *and* with the timing
                // review: the review reads the bytes the judge read, rather than re-fetching a page
                // that may have changed since.
                let rows: Vec<crate::source_watch::Retained> = artifacts
                    .iter()
                    .map(|artifact| {
                        (
                            artifact.hash.clone(),
                            artifact.kind.to_string(),
                            artifact.body.clone(),
                            "application/json".to_string(),
                        )
                    })
                    .collect();
                extra = crate::source_watch::artifact_sql(&rows, now_ms)
                    .map_err(|refusal| JobFailure::new(refusal.code()))?;
                timing_artifacts = artifacts
                    .iter()
                    .map(|artifact| {
                        (
                            artifact.hash.clone(),
                            artifact.body.clone(),
                            "application/json".to_string(),
                        )
                    })
                    .collect();
                Payload::ProposeResolution {
                    schema_version,
                    resolution,
                }
            }
            "PROPOSED" => Payload::BeginChallenge {
                schema_version: 1,
                duration_ms: CHALLENGE_MS,
            },
            "CHALLENGE" => {
                // The challenge window is the dispute period; finalizing inside it would skip it.
                if forecast.challenge_until_ms.is_none_or(|until| now_ms < until) {
                    return Ok(());
                }
                Payload::Finalize { schema_version: 1 }
            }
            "DISPUTED" => {
                let reviewed: Vec<String> = forecast
                    .dispute_reviews
                    .iter()
                    .map(|review| review.dispute_hash.clone())
                    .collect();
                let pending = forecast.disputes.iter().find(|dispute| {
                    dispute
                        .dispute_hash()
                        .map(|hash| !reviewed.contains(&hash))
                        .unwrap_or(false)
                });
                match pending.cloned() {
                    Some(pending) => {
                        // A dispute is reviewed once, under the application's own AI lease, and
                        // the lease is released whatever the review answers — a review that was
                        // refused must not hold the question.
                        let owner = format!("review:{forecast_id}");
                        let ai_token = ai_tokens();
                        ai_lease(db, &owner, &ai_token, now_ms, daily_limit)
                            .await
                            .map_err(JobFailure::from)?;
                        let started = (clock)();
                        let outcome =
                            crate::ai::dispute::review_dispute(coordinator, reader, &forecast, &pending, now_ms).await;
                        release_ai(db, &owner, &ai_token).await;
                        if workflow_deadline_passed(started, (clock)()) {
                            return Err(JobFailure::new("ai_workflow_timeout"));
                        }
                        let result = outcome.map_err(|error| match error {
                            crate::ai::coordinator::CoordinatorError::Unavailable { providers, artifacts } => {
                                JobFailure::AiUnavailable { providers, artifacts }
                            }
                            crate::ai::coordinator::CoordinatorError::Rejected { code, artifacts, .. } => {
                                JobFailure::Refused { code, artifacts }
                            }
                        })?;
                        let review = result.review;
                        at = review.reviewed_at_ms;
                        let rows: Vec<crate::source_watch::Retained> = result
                            .artifacts
                            .iter()
                            .map(|artifact| {
                                (
                                    artifact.hash.clone(),
                                    artifact.kind.to_string(),
                                    artifact.body.clone(),
                                    "application/json".to_string(),
                                )
                            })
                            .collect();
                        extra = crate::source_watch::artifact_sql(&rows, now_ms)
                            .map_err(|refusal| JobFailure::new(refusal.code()))?;
                        extra.push(
                            crate::mutate::record_artifact(&review, "dispute_review", None, now_ms)
                                .map_err(|error| JobFailure::from(format!("{error:?}")))?,
                        );
                        timing_artifacts = result
                            .artifacts
                            .iter()
                            .map(|artifact| {
                                (
                                    artifact.hash.clone(),
                                    artifact.body.clone(),
                                    "application/json".to_string(),
                                )
                            })
                            .collect();
                        Payload::ReviewDispute {
                            schema_version: 1,
                            review,
                        }
                    }
                    // No dispute is waiting to be reviewed. A material conflict among the reviews
                    // is what escalation is *for*; without one the proposal stands.
                    None if forecast.dispute_reviews.iter().any(|review| review.material_conflict) => {
                        Payload::Escalate { schema_version: 1 }
                    }
                    None => Payload::RetainProposal { schema_version: 1 },
                }
            }
            // Recovery must be demonstrated by a successful task, not by a scheduled timer or by a
            // provider merely remaining configured.
            "PAUSED" => {
                return recover_job(Recovery {
                    db,
                    coordinator,
                    fetch,
                    snapshot: &snapshot,
                    job_token,
                    now_ms,
                    ai_tokens: &mut *ai_tokens,
                    daily_limit,
                    token,
                    gate,
                    clock,
                    reader,
                })
                .await;
            }
            _ => return Ok(()),
        };
        crate::mutate::mutate(
            db,
            crate::mutate::Mutation {
                snapshot: &snapshot,
                payload,
                key,
                now_ms: at,
                extra,
                job_token: Some(job_token.to_string()),
                timing_artifacts,
            },
            now_ms,
            token,
            gate,
        )
        .await
        .map_err(|error| {
            JobFailure::from(match error {
                crate::routes::RouteError::Failed(_, code, _) => code.to_string(),
                crate::routes::RouteError::Worker(error) => format!("worker_error: {error}"),
                crate::routes::RouteError::Invalid => "invalid".to_string(),
                crate::routes::RouteError::Input => "input".to_string(),
                crate::routes::RouteError::NotFound(code, _) => format!("not_found: {code}"),
                crate::routes::RouteError::Unauthorized(code, _) => format!("unauthorized: {code}"),
            })
        })?;
    }
    Ok(())
}

/// Whether an AI failure is an *outage*: every configured provider unreachable, and at least one
/// configured.
///
/// The reference's own line is `if providers and set(unavailable) == set(providers)`, and both
/// halves matter. Set equality is what tells "the provider is down" from "one of several is down"
/// — the second is a blip the coordinator can route around, and pausing for it would stop a
/// question nobody has a problem answering. The non-empty half matters because an application with
/// no providers configured has nothing to be unavailable.
pub fn full_outage(configured: &[String], unavailable: &[String]) -> bool {
    if configured.is_empty() {
        return false;
    }
    let mut expected: Vec<&String> = configured.iter().collect();
    expected.sort();
    expected.dedup();
    let mut reported: Vec<&String> = unavailable.iter().collect();
    reported.sort();
    reported.dedup();
    reported == expected
}

/// What `_recover_job` needs. Named rather than positional, for the reason `Job` is.
struct Recovery<'a> {
    db: &'a dyn Database,
    coordinator: &'a Coordinator,
    fetch: &'a EvidenceFetcher,
    snapshot: &'a Snapshot,
    job_token: &'a str,
    now_ms: i64,
    ai_tokens: &'a mut dyn FnMut() -> String,
    daily_limit: i64,
    token: &'a dyn Fn() -> String,
    gate: Option<&'a dyn crate::mutate::FinalizationGate>,
    clock: &'a dyn Fn() -> i64,
    reader: &'a dyn crate::ai::early::ArtifactReader,
}

/// The work the paused task was doing, re-attempted. Named, because eight positional arguments
/// of which three are strings is a call whose order nobody can check by reading it.
struct Work<'a> {
    db: &'a dyn Database,
    coordinator: &'a Coordinator,
    fetch: &'a EvidenceFetcher,
    reader: &'a dyn crate::ai::early::ArtifactReader,
    /// The whole snapshot rather than its base: a v2 question is recovered through the *early*
    /// pipeline, and a base forecast has already lost the distinction by the time it arrives.
    snapshot: &'a Snapshot,
    previous: &'a str,
    now_ms: i64,
    clock: &'a dyn Fn() -> i64,
}
type Recovered = (Payload, i64, String, Value, Vec<crate::ai::coordinator::Artifact>);

async fn recover_work(work: Work<'_>) -> Result<Recovered, JobFailure> {
    let Work {
        db,
        coordinator,
        fetch,
        reader,
        snapshot,
        previous,
        now_ms,
        clock,
    } = work;
    let forecast = snapshot.base();
    if previous == "RESOLVING" {
        let started = (clock)();
        let timing = timing_status(db, &forecast.forecast_id)
            .await
            .map_err(|_| JobFailure::new("resolution_timing_unavailable"))?;
        let indeterminate = timing["reason"] == json!("publication_time_unknown");
        let determined = timing["determination"].as_str();
        // The same branch the LOCKED arm takes, for the same reason: a v2 question's recovery is
        // an early proposal, and the schema version is what carries that to the command.
        let outcome = match snapshot {
            Snapshot::V2(v2) => propose_early_resolution(coordinator, reader, v2, now_ms)
                .await
                .map(|result| (2, AnyResolution::Early(result.resolution), result.artifacts)),
            Snapshot::V1(_) => propose_resolution(coordinator, fetch, forecast, now_ms, indeterminate, determined)
                .await
                .map(|result| (1, AnyResolution::Standard(result.resolution), result.artifacts)),
        };
        if workflow_deadline_passed(started, (clock)()) {
            return Err(JobFailure::new("ai_workflow_timeout"));
        }
        let (schema_version, resolution, artifacts) = outcome.map_err(|error| match error {
            crate::ai::coordinator::CoordinatorError::Unavailable { providers, artifacts } => {
                JobFailure::AiUnavailable { providers, artifacts }
            }
            crate::ai::coordinator::CoordinatorError::Rejected { code, artifacts, .. } => {
                JobFailure::Refused { code, artifacts }
            }
        })?;
        let provider = resolution.base().judge.provider.clone();
        let at = resolution.base().proposed_at_ms;
        let record = serde_json::to_value(&resolution).unwrap_or(Value::Null);
        return Ok((
            Payload::ProposeResolution {
                schema_version,
                resolution,
            },
            at,
            provider,
            record,
            artifacts,
        ));
    }
    let reviewed: Vec<String> = forecast
        .dispute_reviews
        .iter()
        .map(|review| review.dispute_hash.clone())
        .collect();
    let pending = forecast
        .disputes
        .iter()
        .find(|dispute| {
            dispute
                .dispute_hash()
                .map(|hash| !reviewed.contains(&hash))
                .unwrap_or(false)
        })
        .cloned()
        .ok_or_else(|| JobFailure::new("dispute_review_not_ported"))?;
    let started = (clock)();
    let result = crate::ai::dispute::review_dispute(coordinator, reader, forecast, &pending, now_ms)
        .await
        .map_err(|error| match error {
            crate::ai::coordinator::CoordinatorError::Unavailable { providers, artifacts } => {
                JobFailure::AiUnavailable { providers, artifacts }
            }
            crate::ai::coordinator::CoordinatorError::Rejected { code, artifacts, .. } => {
                JobFailure::Refused { code, artifacts }
            }
        })?;
    if workflow_deadline_passed(started, (clock)()) {
        return Err(JobFailure::new("ai_workflow_timeout"));
    }
    let review = result.review;
    let provider = review.independent_judge.provider.clone();
    let at = review.reviewed_at_ms;
    let record = serde_json::to_value(&review).unwrap_or(Value::Null);
    Ok((
        Payload::ReviewDispute {
            schema_version: 1,
            review,
        },
        at,
        provider,
        record,
        result.artifacts,
    ))
}

/// `_recover_job`: a paused question resumes only on a *successful* task.
///
/// Two commands, and the order matters: the pause is lifted, and only then is the work that failed
/// re-attempted. A port that did them the other way round would have to lift the pause to record
/// an attempt, which is the opposite of "recovery is demonstrated".
async fn recover_job(recovery: Recovery<'_>) -> Result<(), JobFailure> {
    let Recovery {
        db,
        coordinator,
        fetch,
        snapshot,
        job_token,
        now_ms,
        ai_tokens,
        daily_limit,
        token,
        gate,
        clock,
        reader,
    } = recovery;
    let forecast = snapshot.base().clone();
    let Some(pause) = forecast.pause.as_ref() else {
        return Ok(());
    };
    let previous = pause.previous_state.clone();
    // Only tasks that caused an outage in this application are recovered. A hypothetical
    // administrative pause needs explicit operator handling.
    if !matches!(previous.as_str(), "RESOLVING" | "DISPUTED") {
        return Ok(());
    }
    let owner = format!("recovery:{}", forecast.forecast_id);
    let ai_token = ai_tokens();
    ai_lease(db, &owner, &ai_token, now_ms, daily_limit)
        .await
        .map_err(JobFailure::from)?;
    let outcome = recover_work(Work {
        db,
        coordinator,
        fetch,
        reader,
        snapshot,
        previous: &previous,
        now_ms,
        clock,
    })
    .await;
    release_ai(db, &owner, &ai_token).await;
    let (payload, at, provider, record, artifacts) = outcome?;
    let resume = crate::mutate::mutate(
        db,
        crate::mutate::Mutation {
            snapshot,
            payload: Payload::ResumeAfterProviderRecovery {
                schema_version: 1,
                recovered_provider: provider,
            },
            key: format!("job:resume:{}", forecast.revision),
            now_ms: at,
            extra: Vec::new(),
            job_token: Some(job_token.to_string()),
            timing_artifacts: Vec::new(),
        },
        now_ms,
        token,
        gate,
    )
    .await
    .map_err(|error| JobFailure::from(format!("{error:?}")))?;
    let rows: Vec<crate::source_watch::Retained> = artifacts
        .iter()
        .map(|artifact| {
            (
                artifact.hash.clone(),
                artifact.kind.to_string(),
                artifact.body.clone(),
                "application/json".to_string(),
            )
        })
        .collect();
    let mut extra =
        crate::source_watch::artifact_sql(&rows, now_ms).map_err(|refusal| JobFailure::new(refusal.code()))?;
    extra.push(
        crate::mutate::record_artifact(&record, "provider_recovery_result", None, now_ms)
            .map_err(|error| JobFailure::from(format!("{error:?}")))?,
    );
    let timing_artifacts = artifacts
        .iter()
        .map(|artifact| {
            (
                artifact.hash.clone(),
                artifact.body.clone(),
                "application/json".to_string(),
            )
        })
        .collect();
    crate::mutate::mutate(
        db,
        crate::mutate::Mutation {
            snapshot: &resume,
            payload,
            key: format!("job:recovered:{}", resume.base().revision),
            now_ms: at,
            extra,
            job_token: Some(job_token.to_string()),
            timing_artifacts,
        },
        now_ms,
        token,
        gate,
    )
    .await
    .map_err(|error| JobFailure::from(format!("{error:?}")))?;
    Ok(())
}

/// `run_due_jobs`: claim what is due under a lease, advance it, and record what happened.
///
/// Every failure here is *recorded*, not raised: a forecast that cannot advance has to go back into
/// the queue with a reason an operator can read, and a pass that stopped at the first refusal would
/// leave every other due question untried.
pub async fn run_due_jobs(
    scheduler: &Scheduler<'_>,
    limit: i64,
    tokens: &mut dyn FnMut() -> String,
) -> Result<Sweep, String> {
    let Scheduler {
        db,
        coordinator,
        fetch,
        now_ms,
        clock,
        daily_limit,
        registry,
        reader,
    } = *scheduler;
    let adapter_configured = registry.is_some();
    let limit = limit.clamp(1, 50);
    let rows = db
        .all(
            "SELECT id FROM forecasts WHERE job_until<=? AND retry_at<=? AND \
             ((state='OPEN' AND close_at<=?) OR state IN ('LOCKED','RESOLVING','PROPOSED','DISPUTED','PAUSED') \
             OR (state='CHALLENGE' AND challenge_until<=?)) ORDER BY close_at,id LIMIT ?",
            &[json!(now_ms), json!(now_ms), json!(now_ms), json!(now_ms), json!(limit)],
        )
        .await
        .map_err(|error| error.to_string())?;
    let mut sweep = Sweep::default();
    for row in rows {
        let forecast_id = text(&row, "id").unwrap_or("").to_string();
        let token = tokens();
        // The claim is a compare-and-set, and it is confirmed by reading the token back rather
        // than by a returned row: the read is what tells two workers racing for one row apart
        // from one worker that matched nothing.
        db.execute(
            "UPDATE forecasts SET job_token=?,job_until=? WHERE id=? AND job_until<=?",
            &[
                json!(token),
                json!(now_ms + LEASE_MS),
                json!(forecast_id),
                json!(now_ms),
            ],
        )
        .await
        .map_err(|error| error.to_string())?;
        let held = db
            .first("SELECT job_token FROM forecasts WHERE id=?", &[json!(forecast_id)])
            .await
            .ok()
            .flatten();
        if held.as_ref().and_then(|row| text(row, "job_token")) != Some(token.as_str()) {
            continue;
        }
        // The job's lease token and the command guard token both come from the application's one
        // source, as the reference's do — and the guard source has to be an `Fn` while the lease
        // source is an `FnMut`. A `RefCell` lets the two accessors share it without the borrow
        // checker having to know they are never live at the same instant.
        let source = std::cell::RefCell::new(&mut *tokens);
        let mut ai_tokens = || source.borrow_mut()();
        let guard_token = || source.borrow_mut()();
        let outcome = advance_job(Job {
            db,
            coordinator,
            fetch,
            forecast_id: &forecast_id,
            job_token: &token,
            now_ms,
            ai_tokens: &mut ai_tokens,
            daily_limit,
            token: &guard_token,
            gate: registry,
            clock,
            reader,
        })
        .await;
        match outcome {
            Ok(()) => {
                sweep.processed += 1;
                db.execute(
                    "UPDATE forecasts SET retry_at=0,failure_count=0,job_error=NULL WHERE id=? AND job_token=?",
                    &[json!(forecast_id), json!(token)],
                )
                .await
                .map_err(|error| error.to_string())?;
            }
            Err(failure) if failure.code() == "chain_finalization_deferred" => {
                // Not a failure. The chain agrees with us and refuses only on its own clock, so
                // the local side is ahead of it rather than broken. Recording that as an error
                // counted the forecast as one nothing can clear, and grew a retry backoff against
                // a wall that time alone moves.
                //
                // The lease is still released below: the reference releases it in a `finally`, so
                // a deferral does not leave the row claimed by a worker that has moved on.
                wait_for_chain(db, &forecast_id, &token, now_ms)
                    .await
                    .map_err(|error| error.to_string())?;
            }
            Err(failure) => {
                sweep.failed += 1;
                // The artifacts a refusal produced are kept even though the work failed: they are
                // what a later reviewer reads to see *what* was proposed.
                if !failure.artifacts().is_empty() {
                    retain_rejected(db, failure.artifacts())
                        .await
                        .map_err(|error| error.to_string())?;
                }
                // Every configured provider being unreachable is an outage rather than a
                // retryable refusal, and a question waiting on one is paused with the reason
                // recorded rather than retried against the same wall.
                if matches!(failure, JobFailure::AiUnavailable { .. }) {
                    let configured = coordinator.configured_providers();
                    if full_outage(&configured, failure.providers()) {
                        let snapshot = load(db, &forecast_id).await.map_err(|error| error.to_string())?;
                        let forecast = snapshot.base().clone();
                        if matches!(
                            forecast.state.as_str(),
                            "RESOLVING" | "PROPOSED" | "CHALLENGE" | "DISPUTED" | "ESCALATED"
                        ) {
                            crate::mutate::mutate(
                                db,
                                crate::mutate::Mutation {
                                    snapshot: &snapshot,
                                    payload: Payload::PauseForProviderOutage {
                                        schema_version: 1,
                                        configured_providers: configured.clone(),
                                        unavailable_providers: configured.clone(),
                                        reason:
                                            "Resolution is paused because all configured AI providers are unavailable."
                                                .to_string(),
                                    },
                                    key: format!("job:pause:{}", forecast.revision),
                                    now_ms,
                                    extra: Vec::new(),
                                    job_token: Some(token.clone()),
                                    timing_artifacts: Vec::new(),
                                },
                                now_ms,
                                &crate::mutate::random_token,
                                registry,
                            )
                            .await
                            .map_err(|error| format!("{error:?}"))?;
                        }
                    }
                }
                let reason = reason_for(failure.code());
                // The backoff is bounded, and it grows from the count the database holds rather
                // than from anything this process remembers.
                db.execute(
                    "UPDATE forecasts SET failure_count=failure_count+1,job_error=?,retry_at=?+MIN(21600000,60000*(1<<MIN(failure_count,8))) \
                     WHERE id=? AND job_token=?",
                    &[json!(reason), json!(now_ms), json!(forecast_id), json!(token)],
                )
                .await
                .map_err(|error| error.to_string())?;
            }
        }
        db.execute(
            "UPDATE forecasts SET job_token=NULL,job_until=0 WHERE id=? AND job_token=?",
            &[json!(forecast_id), json!(token)],
        )
        .await
        .map_err(|error| error.to_string())?;
    }
    // The outbox is drained by the same pass, and the count it reports is part of what the pass
    // says it did.
    let effects = process_outbox(db, now_ms, limit * 3, adapter_configured).await?;
    sweep.effects = effects;
    // Housekeeping the reference does in the same pass, so nothing accumulates unbounded.
    for (sql, params) in [
        ("DELETE FROM sessions WHERE expires_at<=?", vec![json!(now_ms)]),
        ("DELETE FROM ai_leases WHERE expires_at<=?", vec![json!(now_ms)]),
        (
            "DELETE FROM rate_limits WHERE expires_at<=?",
            vec![json!(now_ms - DAY_MS)],
        ),
    ] {
        db.execute(sql, &params).await.map_err(|error| error.to_string())?;
    }
    Ok(sweep)
}

async fn load(db: &dyn Database, forecast_id: &str) -> Result<Snapshot, String> {
    let row = db
        .first("SELECT snapshot FROM forecasts WHERE id=?", &[json!(forecast_id)])
        .await
        .map_err(|error| format!("read_failed:{error}"))?;
    let Some(row) = row else {
        return Err("forecast_not_found".to_string());
    };
    Snapshot::from_json(text(&row, "snapshot").unwrap_or("")).map_err(|error| format!("snapshot_invalid:{error}"))
}

async fn retain(db: &dyn Database, artifact: &crate::ai::coordinator::Artifact, now_ms: i64) -> Result<(), String> {
    db.execute(
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
        &[
            json!(artifact.hash),
            json!(artifact.kind),
            json!(artifact.body),
            json!("application/json"),
            json!(now_ms),
        ],
    )
    .await
    .map_err(|error| error.to_string())?;
    Ok(())
}

/// What the scheduler needs from outside itself, named rather than positional.
pub struct Scheduler<'a> {
    pub db: &'a dyn Database,
    pub coordinator: &'a Coordinator,
    pub fetch: &'a EvidenceFetcher,
    pub now_ms: i64,
    /// `self.now_ms`, for the acceptance deadline rather than for the pass's instant.
    pub clock: &'a dyn Fn() -> i64,
    pub daily_limit: i64,
    /// The chain adapter, when one is configured. `None` in the reference is a real configuration,
    /// not an absence: without an adapter a resolution commitment row is left where it is and a
    /// finalize is a purely local decision.
    pub registry: Option<&'a dyn crate::mutate::FinalizationGate>,
    /// Reading retained bytes, for the dispute review a recovery may have to run.
    pub reader: &'a dyn crate::ai::early::ArtifactReader,
}

impl Scheduler<'_> {
    pub async fn run(&self, limit: i64, tokens: &mut dyn FnMut() -> String) -> Result<Sweep, String> {
        run_due_jobs(self, limit, tokens).await
    }
}

/// `AI_WORKFLOW_TIMEOUT_SECONDS`.
pub const AI_WORKFLOW_TIMEOUT_SECONDS: i64 = 240;

/// `read_artifact`.
pub async fn read_artifact(db: &dyn Database, digest: &str) -> Result<Option<String>, String> {
    let row = db
        .first("SELECT body FROM artifacts WHERE hash=?", &[json!(digest)])
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.as_ref().and_then(|row| text(row, "body").map(str::to_string)))
}

/// `_retain_rejected`: the artifacts a refusal carried are kept even though the work failed.
///
/// They are what a later reviewer reads to see *what* the model proposed, so dropping them on the
/// failure path would leave every rejection with only a code.
pub async fn retain_rejected(db: &dyn Database, artifacts: &[crate::ai::coordinator::Artifact]) -> Result<(), String> {
    if artifacts.is_empty() {
        return Ok(());
    }
    let rows: Vec<(String, String, String, String)> = artifacts
        .iter()
        .map(|artifact| {
            (
                artifact.hash.clone(),
                artifact.kind.to_string(),
                artifact.body.clone(),
                "application/json".to_string(),
            )
        })
        .collect();
    let statements = crate::source_watch::artifact_sql(&rows, 0).map_err(|error| error.message())?;
    db.batch(&statements).await.map_err(|error| error.to_string())?;
    Ok(())
}

/// The acceptance half of `_bounded_ai`.
///
/// The reference wraps the work in `asyncio.timeout(240)` *and* checks the wall clock afterwards,
/// because the injected clock also catches a suspension past the lease boundary — a Worker that
/// was frozen for minutes returns with a result the deadline rule has already invalidated. The
/// timer belongs to the seam that makes the call; this is the part that decides whether the answer
/// that came back may be accepted.
pub fn workflow_deadline_passed(started_ms: i64, now_ms: i64) -> bool {
    now_ms - started_ms >= AI_WORKFLOW_TIMEOUT_SECONDS * 1000
}

/// `_bounded_ai`'s refusal.
pub fn workflow_timeout() -> (u16, &'static str, &'static str) {
    (
        504,
        "ai_workflow_timeout",
        "The AI review timed out. No result was finalized.",
    )
}

/// `_wait_for_chain`: re-arm a deferred forecast for the moment the chain says it may finalize.
///
/// Scheduled at the *recorded* deadline rather than on the ordinary failure backoff, because the
/// deadline is when the answer changes: every poll before it earns the same refusal, and a
/// backoff-driven retry would spend the lease to be told so. `prepare_finalization` has just written
/// that deadline, so this reads what the chain said rather than deciding it here.
///
/// A missing or already-past deadline falls back to a short retry, which keeps a forecast moving
/// when the recorded state is not what this expects — a forecast whose row was never written would
/// otherwise wait forever.
/// Nothing calls this yet either: `_wait_for_chain` answers a `ChainDeadlineNotReached` that
/// `registry_chain` raises, and the chain-write path is the write phase this Worker still forwards.
#[allow(dead_code)]
pub const CHAIN_RETRY_MS: i64 = 300_000;

#[allow(dead_code)]
pub async fn wait_for_chain(db: &dyn Database, forecast_id: &str, token: &str, now_ms: i64) -> Result<(), String> {
    let row = db
        .first(
            "SELECT chain_deadline FROM registry_forecasts WHERE forecast_id=?",
            &[json!(forecast_id)],
        )
        .await
        .map_err(|error| error.to_string())?;
    let deadline = row.as_ref().and_then(|row| int(row, "chain_deadline"));
    let ready_at = match deadline {
        Some(deadline) if deadline > now_ms => deadline,
        _ => now_ms + CHAIN_RETRY_MS,
    };
    // Guarded by the job token, so a lease that has already moved on is not re-armed by the
    // worker that lost it.
    db.execute(
        "UPDATE forecasts SET retry_at=? WHERE id=? AND job_token=?",
        &[json!(ready_at), json!(forecast_id), json!(token)],
    )
    .await
    .map_err(|error| error.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use serde_json::Value;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    /// The snapshot a transition is applied to, taken from the golden step that applies it.
    ///
    /// Picking by state alone is not enough: the golden has eight snapshots in `OPEN`, and only
    /// one of them is the forecast the `lock` step locks. A hand-built `{"state": "OPEN"}` is not
    /// a forecast at all, and `Snapshot::from_json` says so.
    fn snapshot_for(kind: &str) -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/lifecycle-golden.json");
        let golden: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).expect("golden vectors")).expect("json");
        golden["steps"]
            .as_array()
            .expect("steps")
            .iter()
            .find(|step| step["command"]["payload"]["kind"] == json!(kind) && step.get("after").is_some())
            .map(|step| step["before"].clone())
            .unwrap_or_else(|| panic!("the golden has no {kind} transition"))
    }

    /// Insert a forecast whose row and snapshot agree, which is what the scheduler reads.
    fn fixture(db: &Sqlite, kind: &str) -> (String, i64, Option<i64>) {
        let snapshot = snapshot_for(kind);
        let creator = snapshot["creator_id"].as_str().unwrap_or("u").to_string();
        block(db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,1)",
            &[
                json!(creator),
                json!("H"),
                json!(format!("h-{creator}")),
                json!(format!("r-{creator}")),
            ],
        ))
        .unwrap();
        // The row id has to be the snapshot's own `forecast_id`: every guard in the mutation path
        // compares the two, and a row filed under a different name is a row the forecast cannot
        // find — which is what the first version of this fixture did.
        let id = snapshot["forecast_id"].as_str().unwrap_or("").to_string();
        let close_at = snapshot["specification"]["close_at_ms"].as_i64().unwrap_or(0);
        let challenge_until = snapshot["challenge_until_ms"].as_i64();
        block(db.execute(
            "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
             normalized_question,specification_hash,open_at,close_at,created_at,updated_at,challenge_until,mutation_key) \
             VALUES(?,?,?,?,?,?,'CRYPTO','t','q','q',?,0,?,1,1,?,?)",
            &[
                // The row's revision has to be the snapshot's revision for the same reason.
                json!(id), json!(creator), json!(format!("d-{id}")), json!(snapshot.to_string()),
                json!(snapshot["revision"].as_i64().unwrap_or(0)),
                json!(snapshot["state"].as_str().unwrap_or("")), json!(snapshot["specification_hash"]), json!(close_at),
                json!(challenge_until), json!(format!("k-{id}")),
            ],
        ))
        .unwrap();
        (id, close_at, challenge_until)
    }

    #[test]
    fn a_forecast_that_is_not_due_is_not_touched() {
        let db = Sqlite::from_migrations();
        // Still open, so nothing is due however many times the sweep runs.
        let (_id, close_at, _) = fixture(&db, "lock");
        let coordinator = Coordinator {
            providers: Vec::new(),
            fetch: Box::new(|_, _, _| Box::pin(async { Err(()) })),
        };
        let fetch: EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
        let mut counter = 0;
        let mut tokens = move || {
            counter += 1;
            format!("t{counter}")
        };
        let sweep = block(run_due_jobs(
            &Scheduler {
                db: &db,
                coordinator: &coordinator,
                fetch: &fetch,
                now_ms: close_at - 1,
                clock: &|| close_at,
                daily_limit: 100,
                registry: None,
                reader: &crate::golden::Refusing,
            },
            5,
            &mut tokens,
        ))
        .unwrap();
        assert_eq!(sweep, Sweep::default(), "a forecast before its close is not due");
    }

    #[test]
    fn a_closed_forecast_is_locked_and_the_transition_is_recorded() {
        // Driven through `advance_job` rather than `run_due_jobs`, because the selection query is
        // covered by the tests either side of this one and a fixture that satisfies it as well is
        // a second thing to get wrong in a test about the transition.
        let db = Sqlite::from_migrations();
        let (id, close_at, _) = fixture(&db, "lock");
        let coordinator = Coordinator {
            providers: Vec::new(),
            fetch: Box::new(|_, _, _| Box::pin(async { Err(()) })),
        };
        let fetch: EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
        block(db.execute(
            "UPDATE forecasts SET job_token='t1', job_until=? WHERE id=?",
            &[json!(close_at + 1000), json!(id)],
        ))
        .unwrap();
        let outcome = block(advance_job(Job {
            db: &db,
            coordinator: &coordinator,
            fetch: &fetch,
            forecast_id: &id,
            job_token: "t1",
            now_ms: close_at + 1,
            ai_tokens: &mut || "ai1".to_string(),
            daily_limit: 100,
            token: &crate::mutate::random_token,
            gate: None,
            clock: &|| 0,
            reader: &crate::golden::Refusing,
        }));
        // The lease carries several transitions and reports success only if all of them succeed,
        // so this ends at the first thing needing a provider. What matters is that the lock
        // happened, and that the ledger says so.
        assert!(outcome.is_err(), "no provider is configured");
        let state = block(db.first("SELECT state FROM forecasts WHERE id=?", &[json!(id)]))
            .unwrap()
            .unwrap()["state"]
            .as_str()
            .unwrap_or("")
            .to_string();
        assert!(matches!(state.as_str(), "LOCKED" | "RESOLVING"), "{state}");
        let events = block(db.all(
            "SELECT json_extract(event,'$.command_name') AS name FROM events ORDER BY revision",
            &[],
        ))
        .unwrap();
        let names: Vec<&str> = events.iter().filter_map(|row| text(row, "name")).collect();
        assert!(names.contains(&"lock"), "{names:?}");
    }

    #[test]
    fn a_resolving_forecast_with_no_provider_records_a_failure_rather_than_losing_the_lease() {
        let db = Sqlite::from_migrations();
        let (id, close_at, _) = fixture(&db, "propose_resolution");
        let coordinator = Coordinator {
            providers: Vec::new(),
            fetch: Box::new(|_, _, _| Box::pin(async { Err(()) })),
        };
        let fetch: EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
        let mut counter = 0;
        let mut tokens = move || {
            counter += 1;
            format!("t{counter}")
        };
        let sweep = block(run_due_jobs(
            &Scheduler {
                db: &db,
                coordinator: &coordinator,
                fetch: &fetch,
                now_ms: close_at + 1,
                clock: &|| close_at + 1,
                daily_limit: 100,
                registry: None,
                reader: &crate::golden::Refusing,
            },
            5,
            &mut tokens,
        ))
        .unwrap();
        assert_eq!(
            sweep.failed, 1,
            "no provider is configured, so the proposal cannot be made"
        );
        assert_eq!(
            block(db.first("SELECT failure_count FROM forecasts WHERE id=?", &[json!(id)]))
                .unwrap()
                .unwrap()["failure_count"],
            1
        );
        // The lease is released whatever happened, or the forecast would be stuck behind a token
        // no live worker holds.
        let row = block(db.first("SELECT job_token, job_error FROM forecasts WHERE id=?", &[json!(id)]))
            .unwrap()
            .unwrap();
        assert!(text(&row, "job_token").is_none());
        assert!(text(&row, "job_error").is_some(), "the operator is told why");
    }

    #[test]
    fn a_challenge_inside_its_window_is_left_alone() {
        let db = Sqlite::from_migrations();
        let (id, close_at, challenge_until) = fixture(&db, "finalize");
        let until = challenge_until.expect("the golden's challenge window");
        let coordinator = Coordinator {
            providers: Vec::new(),
            fetch: Box::new(|_, _, _| Box::pin(async { Err(()) })),
        };
        let fetch: EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
        let mut counter = 0;
        let mut tokens = move || {
            counter += 1;
            format!("t{counter}")
        };
        // Inside the window the row is not even selected: the query only has a CHALLENGE branch
        // for one whose window has closed. Nothing is attempted, which is the point.
        let inside = block(run_due_jobs(
            &Scheduler {
                db: &db,
                coordinator: &coordinator,
                fetch: &fetch,
                now_ms: until - 1,
                clock: &|| until,
                daily_limit: 100,
                registry: None,
                reader: &crate::golden::Refusing,
            },
            5,
            &mut tokens,
        ))
        .unwrap();
        assert_eq!(inside, Sweep::default());
        assert_eq!(
            block(db.first("SELECT state FROM forecasts WHERE id=?", &[json!(id)]))
                .unwrap()
                .unwrap()["state"],
            "CHALLENGE"
        );

        // Past it, the same row is due and finalizing it needs no provider.
        let past = block(run_due_jobs(
            &Scheduler {
                db: &db,
                coordinator: &coordinator,
                fetch: &fetch,
                now_ms: until + 1,
                clock: &|| until,
                daily_limit: 100,
                registry: None,
                reader: &crate::golden::Refusing,
            },
            5,
            &mut tokens,
        ))
        .unwrap();
        assert_eq!(past.processed, 1);
        assert_eq!(
            block(db.first("SELECT state FROM forecasts WHERE id=?", &[json!(id)]))
                .unwrap()
                .unwrap()["state"],
            "FINALIZED"
        );
        let _ = close_at;
    }

    #[test]
    fn every_mapped_failure_says_something_different_from_the_fallback() {
        // The generic message is the one that sent a diagnosis after the wrong cause, so each
        // mapped code has to be distinguishable from it.
        let fallback = reason_for("");
        for code in [
            "resolution_timing_review",
            "resolution_timing_determined",
            "resolution_domain_rejected",
            "ai_workflow_timeout",
            "ai_daily_limit",
        ] {
            assert_ne!(reason_for(code), fallback, "{code}");
        }
    }
}

#[cfg(test)]
mod chain_tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    /// The three cases the rule names: a deadline in the future is waited for, a past one is a
    /// short retry, and a missing row is a short retry — the last of which is what keeps a
    /// forecast moving when its registry row was never written.
    #[test]
    fn a_deferred_forecast_is_rearmed_at_the_chain_deadline() {
        let db = Sqlite::from_migrations();
        let now = 1_800_000_000_000i64;
        db.run(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','U','u','r',1)",
            &[],
        )
        .expect("user");
        db.run(
            concat!(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,",
                "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) ",
                "VALUES('f','u','d','{}',1,'OPEN','CRYPTO','t','q','q',?,0,?,1,1,'k')",
            ),
            &[json!("a".repeat(64)), json!(now + 1_000_000)],
        )
        .expect("forecast");
        // The lease the guard checks: the reference passes the token the row holds.
        db.run("UPDATE forecasts SET job_token='token' WHERE id='f'", &[])
            .expect("lease");
        let retry_at = |db: &Sqlite| {
            block(db.execute("SELECT retry_at FROM forecasts WHERE id='f'", &[]))
                .expect("forecast")
                .first()
                .and_then(|row| int(row, "retry_at"))
        };
        // No registry row at all: the short retry.
        block(wait_for_chain(&db, "f", "token", now)).expect("wait");
        assert_eq!(retry_at(&db), Some(now + CHAIN_RETRY_MS));

        // A deadline in the future: waited for, and the token is what admits the write.
        db.run(
            concat!(
                "INSERT INTO registry_forecasts(forecast_id,enabled,confirmed_revision,confirmed_state,",
                "chain_deadline,chain_time,observed_at) VALUES('f',1,1,'OPEN',?,0,0)",
            ),
            &[json!(now + 600_000)],
        )
        .expect("registry row");
        block(wait_for_chain(&db, "f", "token", now)).expect("wait");
        assert_eq!(retry_at(&db), Some(now + 600_000));

        // A deadline that has passed, and a token that no longer holds the lease.
        db.run("UPDATE registry_forecasts SET chain_deadline=?", &[json!(now - 1)])
            .expect("past deadline");
        block(wait_for_chain(&db, "f", "other", now)).expect("wait");
        assert_eq!(retry_at(&db), Some(now + 600_000), "a lost lease is not re-armed");
        block(wait_for_chain(&db, "f", "token", now)).expect("wait");
        assert_eq!(retry_at(&db), Some(now + CHAIN_RETRY_MS));
    }
}

/// The outbox, the settlement statement and the sweep that drains both, replayed against the
/// reference's own recorded state.
#[cfg(test)]
mod outbox_tests {
    use super::*;
    use crate::golden::{assert_all_cases_known, assert_case, block, entry, load, static_database, Tokens};

    const REPLAYED: [&str; 18] = [
        "settlement:valid",
        "settlement:empty-id",
        "settlement:control-char",
        "settlement:too-long",
        "settlement:negative-time",
        "settlement:over-ceiling",
        "outbox:empty",
        "outbox:commitment",
        "outbox:unknown",
        "outbox:blocked",
        "outbox:reputation",
        "outbox:notification",
        "outbox:not-finalized",
        "sweep:before-deadline",
        "sweep:finalize",
        "sweep:cleanup",
        "sweep:blocked",
        "sweep:chain-deferred",
    ];

    /// `settlement:wrong-type` passes Python's `True` where the statement wants an instant. This
    /// port cannot express that: the parameter is an `i64`, so the refusal is unreachable rather
    /// than reproduced. It is listed here so the vector cannot grow a case nobody looked at.
    const NOT_REPLAYED: [(&str, &str); 1] = [(
        "settlement:wrong-type",
        "the reference passes `True` where it wants an int; this port's parameter is an `i64`, so \
         the check cannot fail",
    )];

    #[test]
    fn the_vector_has_no_case_this_replay_silently_skips() {
        assert_all_cases_known(&load("outbox-golden.json"), &REPLAYED, &NOT_REPLAYED);
    }

    /// A gate that always defers, standing in for the chain.
    ///
    /// The *decision* is not what this case tests: `prepare_finalization` is held to
    /// `solana-rpc-golden` and it reads a chain the vector does not carry. What is tested here is
    /// what the sweep does with the answer — that a deferral is not a failure, and that the
    /// question is re-armed for the instant the chain named rather than for the failure backoff.
    struct Deferring;

    impl crate::mutate::FinalizationGate for Deferring {
        fn prepare<'a>(&'a self, _forecast_id: &'a str) -> crate::mutate::GateFuture<'a> {
            Box::pin(async { crate::mutate::Finalization::Deferred })
        }
    }

    fn sweep(
        db: &crate::db::Sqlite,
        case: &Value,
        tokens: &Tokens,
        registry: Option<&dyn crate::mutate::FinalizationGate>,
    ) -> Result<Value, String> {
        let coordinator = Coordinator {
            providers: Vec::new(),
            fetch: Box::new(|_, _, _| Box::pin(async { Err(()) })),
        };
        let fetch: EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
        let now_ms = case["now"].as_i64().unwrap_or(0);
        let limit = 3;
        let mut source = || tokens.next();
        block(run_due_jobs(
            &Scheduler {
                db,
                coordinator: &coordinator,
                fetch: &fetch,
                now_ms,
                clock: &|| now_ms,
                daily_limit: 100,
                registry,
                reader: &crate::golden::Refusing,
            },
            limit,
            &mut source,
        ))
        .map(|sweep| json!({"processed": sweep.processed, "failed": sweep.failed, "effects": sweep.effects}))
    }

    fn outcome(db: &'static crate::db::Sqlite, case: &Value) -> (Option<Value>, Option<Value>, Tokens) {
        let tokens = Tokens::new(Tokens::recorded(case));
        let name = case["call"].as_str().expect("name");
        let now_ms = case["now"].as_i64().unwrap_or(0);
        let input = &case["input"];
        let result: Result<Value, Value> = match name {
            _ if name.starts_with("settlement:") => {
                let forecast_id = input["forecastId"].as_str().unwrap_or("");
                let at = input["now"].as_i64().unwrap_or(0);
                match crate::points::settlement_sql(forecast_id, at) {
                    Ok(statements) => Ok(json!(statements
                        .into_iter()
                        .map(|(sql, params)| json!({"sql": sql, "params": params}))
                        .collect::<Vec<_>>())),
                    Err(error) => Err(json!({"status": error.status, "code": error.code, "message": error.message})),
                }
            }
            "sweep:chain-deferred" => {
                let gate = Deferring;
                sweep(db, case, &tokens, Some(&gate)).map_err(|code| json!({"code": code, "message": code}))
            }
            "sweep:before-deadline" | "sweep:finalize" | "sweep:cleanup" | "sweep:blocked" => {
                sweep(db, case, &tokens, None).map_err(|code| json!({"code": code, "message": code}))
            }
            "outbox:commitment" => {
                // Twice: a commitment row is handed to the adapter rather than delivered, so the
                // second pass has nothing left to look at.
                let limit = input["limit"].as_i64().unwrap_or(15);
                let first = block(process_outbox(db, now_ms, limit, false));
                let second = block(process_outbox(db, now_ms, limit, false));
                match (first, second) {
                    (Ok(first), Ok(second)) => Ok(json!({"first": first, "second": second})),
                    (Err(detail), _) | (_, Err(detail)) => Err(json!({"code": detail, "message": detail})),
                }
            }
            _ => {
                let limit = input["limit"].as_i64().unwrap_or(15);
                match block(process_outbox(db, now_ms, limit, false)) {
                    Ok(count) => Ok(json!(count)),
                    Err(detail) => Err(json!({"code": detail, "message": detail})),
                }
            }
        };
        match result {
            Ok(value) => (Some(value), None, tokens),
            Err(error) => (None, Some(error), tokens),
        }
    }

    #[test]
    fn the_reference_outbox_and_sweep_are_reproduced_case_for_case() {
        let document = load("outbox-golden.json");
        for name in REPLAYED {
            let case = entry(&document, name);
            if case.get("initial").is_none() {
                // A pure function: the statement and its refusals, with no database to leave behind.
                let (result, error, _) = outcome_for_pure(case);
                assert_case(name, case, &result, &error, &crate::db::Sqlite::from_migrations());
                continue;
            }
            let db = static_database(&case["initial"]);
            let (result, error, tokens) = outcome(db, case);
            assert_case(name, case, &result, &error, db);
            tokens.assert_drained(name, "");
        }
    }

    /// The settlement cases have no database at all, so the replay runs them against an empty one
    /// purely so the comparison machinery has something to hand.
    fn outcome_for_pure(case: &Value) -> (Option<Value>, Option<Value>, Tokens) {
        let input = &case["input"];
        let forecast_id = input["forecastId"].as_str().unwrap_or("");
        let at = input["now"].as_i64().unwrap_or(0);
        let tokens = Tokens::new(Vec::new());
        match crate::points::settlement_sql(forecast_id, at) {
            Ok(statements) => (
                Some(json!(statements
                    .into_iter()
                    .map(|(sql, params)| json!({"sql": sql, "params": params}))
                    .collect::<Vec<_>>())),
                None,
                tokens,
            ),
            Err(error) => (
                None,
                Some(json!({"status": error.status, "code": error.code, "message": error.message})),
                tokens,
            ),
        }
    }
}

#[cfg(test)]
mod outage_tests {
    use super::*;

    fn names(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    /// `if providers and set(unavailable) == set(providers)`.
    ///
    /// Both halves are pinned here because both are load-bearing and neither is obvious: a
    /// *partial* outage is a blip the coordinator routes around, and an application with no
    /// providers configured has nothing to be unavailable.
    #[test]
    fn only_a_complete_outage_pauses_a_question() {
        let all = names(&["provider-a", "provider-b", "independent-provider"]);
        assert!(full_outage(&all, &all), "every provider unreachable is an outage");
        assert!(
            full_outage(&all, &names(&["provider-a", "provider-b", "independent-provider"])),
            "the comparison is over sets, not over order"
        );
        assert!(
            full_outage(
                &all,
                &names(&["independent-provider", "provider-a", "provider-b", "provider-a"])
            ),
            "and not over repeats"
        );
        assert!(
            !full_outage(&all, &names(&["provider-a"])),
            "one of three is not an outage"
        );
        assert!(
            !full_outage(&all, &names(&["provider-a", "provider-b"])),
            "two of three is not an outage either"
        );
        assert!(
            !full_outage(
                &all,
                &names(&["provider-a", "provider-b", "independent-provider", "provider-d"])
            ),
            "a provider nobody configured is not a configured provider"
        );
        assert!(
            !full_outage(&[], &[]),
            "nothing configured is nothing to be unavailable"
        );
        assert!(
            !full_outage(&[], &names(&["provider-a"])),
            "and nor is a report about nothing"
        );
    }
}

/// The two `DISPUTED` outcomes the sweep reaches without asking anything.
#[cfg(test)]
mod dispute_sweep_tests {
    use super::*;
    use crate::golden::{assert_all_cases_known, assert_case, block, entry, load, static_database, Tokens};

    const REPLAYED: [&str; 2] = ["dispute:escalate", "dispute:retain"];

    /// The reviewed-dispute outcome is not here: the reference's fixture builds the review directly
    /// rather than through a transport, so there is no conversation to serve. `ai-dispute-golden`
    /// holds `review_dispute` itself; the sweep's wiring of it is transcribed without a vector, and
    /// naming it is better than leaving its absence to be noticed.
    const NOT_REPLAYED: [(&str, &str); 0] = [];

    #[test]
    fn the_vector_has_no_case_this_replay_silently_skips() {
        assert_all_cases_known(&load("dispute-sweep-golden.json"), &REPLAYED, &NOT_REPLAYED);
    }

    #[test]
    fn the_reference_dispute_sweep_is_reproduced_case_for_case() {
        let document = load("dispute-sweep-golden.json");
        for name in REPLAYED {
            let case = entry(&document, name);
            let db = static_database(&case["initial"]);
            let now_ms = case["now"].as_i64().unwrap_or(0);
            let tokens = Tokens::new(Tokens::recorded(case));
            let evidence: crate::ai::resolution::EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
            let coordinator = crate::golden::refusing_coordinator();
            let mut source = || tokens.next();
            let outcome = block(run_due_jobs(
                &Scheduler {
                    db,
                    coordinator: &coordinator,
                    fetch: &evidence,
                    now_ms,
                    clock: &|| now_ms,
                    daily_limit: 100,
                    registry: None,
                    reader: &crate::golden::Refusing,
                },
                3,
                &mut source,
            ));
            let forecast_id = case["input"]["forecastId"].as_str().unwrap_or("");
            let state = block(crate::mutate::load_snapshot(db, forecast_id))
                .ok()
                .map(|snapshot| snapshot.base().state.clone())
                .unwrap_or_default();
            let (result, error) = match outcome {
                Ok(sweep) => (
                    Some(json!({
                        "sweep": {"processed": sweep.processed, "failed": sweep.failed, "effects": sweep.effects},
                        "state": state,
                    })),
                    None,
                ),
                Err(code) => (None, Some(json!({"code": code, "message": code}))),
            };
            assert_case(name, case, &result, &error, db);
            tokens.assert_drained(name, "");
        }
    }
}
