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

use serde_json::json;

use forecast_domain::lifecycle::Payload;
use forecast_domain::lifecycle::Snapshot;

use crate::ai::coordinator::Coordinator;
use crate::ai::resolution::{propose_resolution, EvidenceFetcher};
use crate::db::{int, text, Database};
use crate::resolution_timing::{close_indeterminate, status as timing_status};

pub const LEASE_MS: i64 = 300_000;
pub const CHALLENGE_MS: i64 = 48 * 3_600_000;
const DAY_MS: i64 = 86_400_000;
const MAX_TRANSITIONS_PER_LEASE: usize = 6;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Sweep {
    pub processed: i64,
    pub failed: i64,
}

/// The failure reason an operator reads. The generic message is the fallback and is wrong often
/// enough that every mapped code here was added after it sent a diagnosis somewhere else.
pub fn reason_for(code: &str) -> String {
    match code {
        "resolution_timing_review" | "early_eligibility_review" => "Evidence publication time and receipt eligibility are being reviewed. No result rewards or reputation will be credited until that review is complete.".to_string(),
        "resolution_timing_determined" => "The publication-time review is closed and determines INVALID. Only that result can be finalized, so the next attempt proposes it.".to_string(),
        "resolution_domain_rejected" => "The AI resolution was refused by the immutable domain checks, most often because it did not cite the clause matching its own outcome. The retained judge output names what it proposed.".to_string(),
        "ai_workflow_timeout" => "Resolution is on hold because the AI review timed out. Another review will follow the retry schedule and daily limit.".to_string(),
        "ai_daily_limit" => "Resolution is on hold because the daily AI limit was reached. Review will resume after the limit resets.".to_string(),
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
    pub ai_token: &'a str,
    pub daily_limit: i64,
}

/// One lease's worth of transitions, at most six, stopping when the state stops changing.
pub async fn advance_job(job: Job<'_>) -> Result<(), String> {
    let Job {
        db,
        coordinator,
        fetch,
        forecast_id,
        job_token,
        now_ms,
        ai_token,
        daily_limit,
    } = job;
    for _ in 0..MAX_TRANSITIONS_PER_LEASE {
        let snapshot = load(db, forecast_id).await?;
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
            .map_err(|error| error.to_string())?
            .is_some();
        if blocked {
            return Err("early_eligibility_review".to_string());
        }
        let key = format!("job:{}", forecast.revision);
        let at = now_ms;
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
                ai_lease(db, &owner, ai_token, now_ms, daily_limit).await?;
                // Whether the evidence can be placed relative to participation is something this
                // layer knows and the judge cannot see. Without telling it, the judge keeps
                // answering YES or NO, the timing gate keeps refusing, and the forecast retries
                // forever — which is what happened to two of them.
                let timing = timing_status(db, forecast_id)
                    .await
                    .map_err(|_| "resolution_timing_unavailable".to_string())?;
                let indeterminate = timing["reason"] == json!("publication_time_unknown");
                let determined = timing["determination"].as_str();
                let outcome =
                    propose_resolution(coordinator, fetch, &forecast, now_ms, indeterminate, determined).await;
                release_ai(db, &owner, ai_token).await;
                let result = outcome.map_err(|error| error.code().unwrap_or("ai_unavailable").to_string())?;
                let resolution = result.resolution;
                if let Ok(retained) = serde_json::to_value(&resolution) {
                    if let Ok(artifact) = crate::ai::coordinator::artifact("resolution", &retained) {
                        let _ = retain(db, &artifact, now_ms).await;
                    }
                }
                Payload::ProposeResolution {
                    schema_version: 1,
                    resolution: forecast_domain::lifecycle::AnyResolution::Standard(resolution),
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
                match pending {
                    Some(_) => return Err("dispute_review_not_ported".to_string()),
                    None if forecast.dispute_reviews.iter().any(|review| review.material_conflict) => {
                        Payload::Escalate { schema_version: 1 }
                    }
                    None => Payload::RetainProposal { schema_version: 1 },
                }
            }
            "PAUSED" => return Err("provider_recovery_not_ported".to_string()),
            _ => return Ok(()),
        };
        crate::mutate::mutate(
            db,
            crate::mutate::Mutation {
                snapshot: &snapshot,
                payload,
                key,
                now_ms: at,
                extra: Vec::new(),
                job_token: Some(job_token.to_string()),
            },
            now_ms,
        )
        .await
        .map_err(|error| match error {
            crate::routes::RouteError::Failed(_, code, _) => code.to_string(),
            crate::routes::RouteError::Worker(error) => format!("worker_error: {error}"),
            crate::routes::RouteError::Invalid => "invalid".to_string(),
            crate::routes::RouteError::Input => "input".to_string(),
            crate::routes::RouteError::NotFound(code, _) => format!("not_found: {code}"),
            crate::routes::RouteError::Unauthorized(code, _) => format!("unauthorized: {code}"),
        })?;
    }
    Ok(())
}

/// `run_due_jobs`: claim what is due under a lease, advance it, and record what happened.
pub async fn run_due_jobs(
    db: &dyn Database,
    coordinator: &Coordinator,
    fetch: &EvidenceFetcher,
    now_ms: i64,
    limit: i64,
    tokens: &mut dyn FnMut() -> String,
    daily_limit: i64,
) -> Result<Sweep, String> {
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
        let ai_token = tokens();
        let outcome = advance_job(Job {
            db,
            coordinator,
            fetch,
            forecast_id: &forecast_id,
            job_token: &token,
            now_ms,
            ai_token: &ai_token,
            daily_limit,
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
            Err(code) => {
                sweep.failed += 1;
                let reason = reason_for(&code);
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
    pub daily_limit: i64,
}

impl Scheduler<'_> {
    pub async fn run(&self, limit: i64, tokens: &mut dyn FnMut() -> String) -> Result<Sweep, String> {
        run_due_jobs(
            self.db,
            self.coordinator,
            self.fetch,
            self.now_ms,
            limit,
            tokens,
            self.daily_limit,
        )
        .await
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
            &db,
            &coordinator,
            &fetch,
            close_at - 1,
            5,
            &mut tokens,
            100,
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
            ai_token: "ai1",
            daily_limit: 100,
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
            &db,
            &coordinator,
            &fetch,
            close_at + 1,
            5,
            &mut tokens,
            100,
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
        let inside = block(run_due_jobs(&db, &coordinator, &fetch, until - 1, 5, &mut tokens, 100)).unwrap();
        assert_eq!(inside, Sweep::default());
        assert_eq!(
            block(db.first("SELECT state FROM forecasts WHERE id=?", &[json!(id)]))
                .unwrap()
                .unwrap()["state"],
            "CHALLENGE"
        );

        // Past it, the same row is due and finalizing it needs no provider.
        let past = block(run_due_jobs(&db, &coordinator, &fetch, until + 1, 5, &mut tokens, 100)).unwrap();
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
