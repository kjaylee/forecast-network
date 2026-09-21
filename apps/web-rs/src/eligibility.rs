//! Evidence-cutoff eligibility and resolution-timing review status projections.

use serde_json::{json, Value};
use worker::*;

use crate::db::{get, int, text, Database};

pub const PAGE_SIZE: i64 = 100;

pub const POLICY_VERSION: &str = "evidence-cutoff-v1";

pub async fn timing_status(db: &dyn Database, forecast_id: &str) -> Result<Value> {
    let row = db
        .first(
            "SELECT * FROM resolution_timing_reviews WHERE forecast_id=? ORDER BY created_at,resolution_hash LIMIT 1",
            &[json!(forecast_id)],
        )
        .await?;
    let Some(row) = row else {
        return Ok(json!({"status": "none"}));
    };
    let complete = db.first("SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id \
         WHERE d.forecast_id=? AND d.specification_hash=?",
        &[json!(forecast_id), get(&row, "specification_hash").clone()],
    )
    .await?
    .is_some();
    let closure = db
        .first(
            "SELECT determination FROM resolution_timing_closures WHERE forecast_id=? AND specification_hash=?",
            &[json!(forecast_id), get(&row, "specification_hash").clone()],
        )
        .await?;
    let mut value = json!({
        "status": if complete { "complete" } else { "review" }, "reason": get(&row, "reason"),
        "candidateCutoffAt": get(&row, "candidate_cutoff_at"), "proofHash": get(&row, "proof_hash"),
    });
    // Additive, matching the Python projection: callers that only understand "review" keep
    // working, and a closed review reports what it determined rather than hiding it.
    if let Some(closure) = closure {
        value["determination"] = get(&closure, "determination").clone();
    }
    Ok(value)
}

pub async fn status(db: &dyn Database, forecast_id: &str, user_id: Option<&str>) -> Result<Value> {
    let decision = db
        .first(
            "SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?",
            &[json!(forecast_id)],
        )
        .await?;
    let mut result = json!({
        "status": "none", "cutoffAt": null, "timeBasis": null, "publishedEvidenceUrl": null,
        "policyVersion": POLICY_VERSION, "personal": null,
    });
    let Some(decision) = decision else { return Ok(result) };
    let id = get(&decision, "id").clone();
    let complete = db
        .first(
            "SELECT decision_id FROM forecast_eligibility_completions WHERE decision_id=?",
            std::slice::from_ref(&id),
        )
        .await?
        .is_some();
    let review = db
        .first(
            "SELECT revision FROM forecast_receipt_eligibility WHERE decision_id=? AND status='review' LIMIT 1",
            std::slice::from_ref(&id),
        )
        .await?
        .is_some();
    let body: Value = serde_json::from_str(text(&decision, "body").unwrap_or("{}")).unwrap_or(Value::Null);
    result["status"] = json!(if complete {
        "complete"
    } else if review {
        "review"
    } else {
        "pending"
    });
    result["cutoffAt"] = get(&decision, "cutoff_at").clone();
    result["timeBasis"] = get(&decision, "event_time_basis").clone();
    result["publishedEvidenceUrl"] = body["evidence"][0]["url"].clone();
    if let Some(user_id) = user_id {
        let rows = db.all("SELECT revision,status FROM forecast_receipt_eligibility WHERE decision_id=? AND user_id=? ORDER BY revision",
            &[id.clone(), json!(user_id)],
        )
        .await?;
        let adjustment = db
            .first(
                "SELECT available_delta FROM point_eligibility_adjustments WHERE decision_id=? AND user_id=?",
                &[id, json!(user_id)],
            )
            .await?;
        let raw = db
            .first(
                "SELECT revision FROM user_forecasts WHERE forecast_id=? AND user_id=?",
                &[json!(forecast_id), json!(user_id)],
            )
            .await?;
        let unclassified = raw
            .as_ref()
            .is_some_and(|raw| !rows.iter().any(|row| get(row, "revision") == get(raw, "revision")));
        let eligible: Vec<Value> = rows
            .iter()
            .filter(|r| text(r, "status") == Some("eligible"))
            .map(|r| get(r, "revision").clone())
            .collect();
        let voids: Vec<Value> = rows
            .iter()
            .filter(|r| text(r, "status") == Some("void"))
            .map(|r| get(r, "revision").clone())
            .collect();
        let personal = if unclassified || rows.iter().any(|r| text(r, "status") == Some("review")) {
            "review"
        } else if !eligible.is_empty() && !voids.is_empty() {
            "restored"
        } else if !eligible.is_empty() {
            "eligible"
        } else if !voids.is_empty() {
            "void"
        } else {
            "none"
        };
        let refunded = adjustment
            .as_ref()
            .and_then(|a| int(a, "available_delta"))
            .map_or(0, |d| d.max(0));
        result["personal"] = json!({
            "status": personal, "voidedRevisions": voids, "effectiveRevision": eligible.last().cloned().unwrap_or(Value::Null),
            "refundedPoints": refunded, "adjustmentPending": unclassified || (!rows.is_empty() && adjustment.is_none()),
        });
    }
    Ok(result)
}

/// Classify time evidence without interpreting ordinary news as a result.
///
/// A published instant is an inclusive cutoff, so a receipt at exactly that moment is void.
/// An observation bound is not a cutoff at all: it cannot prove the receipt came first, so the
/// receipt goes to review rather than being called eligible.
pub fn receipt_status(submitted_at: &Value, cutoff_at: &Value, time_basis: &str) -> Result<String, String> {
    let (Some(submitted_at), Some(cutoff_at)) = (submitted_at.as_i64(), cutoff_at.as_i64()) else {
        return Err("Receipt timestamps must be nonnegative integers".to_string());
    };
    if submitted_at.min(cutoff_at) < 0 {
        return Err("Receipt timestamps must be nonnegative integers".to_string());
    }
    if !matches!(time_basis, "published_instant" | "observed_upper_bound") {
        return Err("Unknown evidence time basis".to_string());
    }
    Ok(if submitted_at >= cutoff_at {
        "void"
    } else if time_basis == "published_instant" {
        "eligible"
    } else {
        "review"
    }
    .to_string())
}

/// The completion the scheduler looks for before a timing review stops blocking resolution.
pub fn completion_sql(trigger_hash: &str, now_ms: i64) -> (String, Vec<Value>) {
    (
        "INSERT OR IGNORE INTO forecast_eligibility_completions(decision_id,created_at) VALUES(?,?)".to_string(),
        vec![json!(trigger_hash), json!(now_ms)],
    )
}

/// `Application.eligibility_status`: a timing review surfaces when no cutoff decision exists.
pub async fn combined(db: &dyn Database, forecast_id: &str, user_id: Option<&str>) -> Result<Value> {
    let mut result = status(db, forecast_id, user_id).await?;
    if result["status"] == "none" {
        let timing = timing_status(db, forecast_id).await?;
        if timing["status"] == "review" {
            result["status"] = json!("review");
            result["timingReview"] = timing;
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    #[test]
    fn a_published_instant_is_an_inclusive_cutoff_and_an_observation_bound_is_not() {
        // The whole of this module is the difference between these two lines.
        assert_eq!(
            receipt_status(&json!(9), &json!(10), "published_instant").unwrap(),
            "eligible"
        );
        assert_eq!(
            receipt_status(&json!(10), &json!(10), "published_instant").unwrap(),
            "void"
        );
        assert_eq!(
            receipt_status(&json!(11), &json!(10), "published_instant").unwrap(),
            "void"
        );
        // An observation bound cannot prove the receipt came first, so it is not eligible.
        assert_eq!(
            receipt_status(&json!(9), &json!(10), "observed_upper_bound").unwrap(),
            "review"
        );
        assert_eq!(
            receipt_status(&json!(10), &json!(10), "observed_upper_bound").unwrap(),
            "void"
        );
    }

    #[test]
    fn a_malformed_or_unknown_input_is_refused_rather_than_classified() {
        assert!(receipt_status(&json!(-1), &json!(10), "published_instant").is_err());
        assert!(receipt_status(&json!(10), &json!(-1), "published_instant").is_err());
        assert!(receipt_status(&json!("9"), &json!(10), "published_instant").is_err());
        assert!(receipt_status(&json!(9), &json!(10), "guessed").is_err());
    }

    #[test]
    fn a_forecast_with_no_decision_has_no_cutoff() {
        let db = Sqlite::from_migrations();
        let value = block(status(&db, "f", None)).unwrap();
        assert_eq!(value["status"], "none");
        assert_eq!(value["policyVersion"], POLICY_VERSION);
        assert!(value["cutoffAt"].is_null());
    }

    #[test]
    fn the_projection_reports_the_cutoff_and_what_it_means_for_one_account() {
        const SPEC: &str = "1111111111111111111111111111111111111111111111111111111111111111";
        const DECISION: &str = "2222222222222222222222222222222222222222222222222222222222222222";
        let db = Sqlite::from_migrations();
        block(db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','H','h','r',1)",
            &[],
        ))
        .unwrap();
        block(db.execute(
            &format!(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
                 normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
                 VALUES('f','u','d','{{}}',1,'RESOLVING','CRYPTO','t','q','q','{SPEC}',0,1,1,1,'k')"
            ),
            &[],
        ))
        .unwrap();
        // The decision validate trigger joins the artifact by the decision's own id and
        // requires the two bodies to be identical, so one has to exist and be the same text.
        let decision_body = json!({
            "forecast_id": "f", "specification_hash": SPEC, "event_at_ms": 1000,
            "event_time_basis": "published_instant",
            "evidence": [{"url": "https://www.apple.com/newsroom/x/"}],
        })
        .to_string();
        block(db.execute(
            "INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,'application/json',1)",
            &[json!(DECISION), json!("early-resolution-trigger"), json!(decision_body)],
        ))
        .unwrap();
        block(db.execute(
            "INSERT INTO forecast_eligibility_decisions(id,forecast_id,specification_hash,cutoff_at,event_time_basis,created_at,body) \
             VALUES(?,?,?,?,?,?,?)",
            &[
                json!(DECISION), json!("f"), json!(SPEC), json!(1000),
                json!("published_instant"), json!(1), json!(decision_body),
            ],
        ))
        .unwrap();

        let none_yet = block(status(&db, "f", None)).unwrap();
        assert_eq!(
            none_yet["status"], "pending",
            "a cutoff with nothing classified is pending"
        );
        assert_eq!(none_yet["cutoffAt"], 1000);
        assert_eq!(none_yet["timeBasis"], "published_instant");
        assert_eq!(none_yet["publishedEvidenceUrl"], "https://www.apple.com/newsroom/x/");

        // The receipt-level projection is not asserted here. A classified receipt cannot be
        // written on its own: the schema joins it to the accepted forecast, its event and its
        // command receipt, and derives the status from the cutoff rather than trusting the one
        // being inserted. That fixture belongs with the deciding half, which walks the same
        // chain, and asserting it here would mean three attempts at guessing a trigger I had
        // not read.
    }
}

// --------------------------------------------------------------------------- the deciding half
//
// The caller is `automation::Automation::accept`, which reads the trigger out of a reviewed
// observation and holds it to retained bytes before this runs. These were marked `allow(dead_code)`
// while that half was missing; the marker is gone, and their caller is one `grep` away.

use forecast_domain::content_hash;
use forecast_domain::lifecycle::{Command, CommandReceipt, DomainEvent, EarlyResolutionTrigger, Payload, Snapshot};
use forecast_domain::Record;

use crate::mutate::canonical_text;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EligibilityError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl From<worker::Error> for EligibilityError {
    fn from(_: worker::Error) -> Self {
        EligibilityError {
            status: 503,
            code: "early_evidence_unavailable",
            message: "The original official evidence could not be verified.",
        }
    }
}

impl EligibilityError {
    fn trigger_changed() -> Self {
        EligibilityError {
            status: 409,
            code: "early_trigger_changed",
            message: "Evidence review has not completed.",
        }
    }
    fn not_found() -> Self {
        EligibilityError {
            status: 404,
            code: "forecast_not_found",
            message: "Forecast not found.",
        }
    }
    fn history_review() -> Self {
        EligibilityError {
            status: 409,
            code: "eligibility_history_review",
            message: "Accepted forecast history did not pass integrity checks.",
        }
    }
    fn already_settled() -> Self {
        EligibilityError {
            status: 409,
            code: "eligibility_already_settled",
            message: "This forecast already finalized and needs an audited correction review.",
        }
    }
    fn invalid_trigger() -> Self {
        EligibilityError {
            status: 400,
            code: "invalid_eligibility_trigger",
            message: "A validated evidence trigger is required.",
        }
    }
}

/// The trigger is checked against the forecast it claims and the bytes it cites, before any of
/// it is written. A trigger whose evidence was not retained is not a trigger.
pub async fn validate(
    db: &dyn Database,
    trigger: &EarlyResolutionTrigger,
    now_ms: i64,
) -> Result<(), EligibilityError> {
    trigger.validate().map_err(|_| EligibilityError::invalid_trigger())?;
    let row = db
        .first(
            "SELECT snapshot FROM forecasts WHERE id=?",
            &[json!(trigger.forecast_id)],
        )
        .await?;
    let Some(row) = row else {
        return Err(EligibilityError::not_found());
    };
    let snapshot =
        Snapshot::from_json(text(&row, "snapshot").unwrap_or("")).map_err(|_| EligibilityError::invalid_trigger())?;
    let specification = snapshot.base().specification.clone();
    trigger
        .validate_for(&specification)
        .map_err(|_| EligibilityError::invalid_trigger())?;
    if trigger.qualified_at_ms() > now_ms {
        return Err(EligibilityError::trigger_changed());
    }
    for evidence in &trigger.evidence {
        let retained = db
            .first(
                "SELECT body FROM artifacts WHERE hash=?",
                &[json!(evidence.content_sha256)],
            )
            .await?;
        let matches = retained
            .as_ref()
            .map(|row| crate::source_watch::hash_hex(text(row, "body").unwrap_or("")) == evidence.content_sha256)
            .unwrap_or(false);
        if !matches {
            return Err(EligibilityError {
                status: 503,
                code: "early_evidence_unavailable",
                message: "The original official evidence could not be verified.",
            });
        }
    }
    Ok(())
}

/// Write the cutoff, once. The guard is what makes it once: `mutation_guards` is `CHECK(valid=1)`,
/// so a forecast that has already finalized, scored, settled or decided a different cutoff
/// inserts a zero and aborts the batch.
pub async fn decide(
    db: &dyn Database,
    trigger: &EarlyResolutionTrigger,
    now_ms: i64,
    token: &dyn Fn() -> String,
) -> Result<(), EligibilityError> {
    validate(db, trigger, now_ms).await?;
    let trigger_hash = trigger
        .trigger_hash()
        .map_err(|_| EligibilityError::invalid_trigger())?;
    let body = canonical_text(trigger).map_err(|_| EligibilityError::invalid_trigger())?;
    let existing = db
        .first(
            "SELECT id,body FROM forecast_eligibility_decisions WHERE forecast_id=?",
            &[json!(trigger.forecast_id)],
        )
        .await?;
    if let Some(existing) = existing {
        if text(&existing, "id") != Some(trigger_hash.as_str()) || text(&existing, "body") != Some(body.as_str()) {
            return Err(EligibilityError::trigger_changed());
        }
        return Ok(());
    }
    // Minted here rather than by the caller, and *after* the early return above: a decision that
    // already exists costs no token at all, and the reference's own order is what makes that
    // visible.
    let guard = token();
    let statements = vec![
        (
            "INSERT INTO mutation_guards(token,valid) SELECT ?, ( CASE WHEN NOT EXISTS(SELECT 1 FROM forecasts \
             WHERE id=? AND state IN ('FINALIZED','ARCHIVED')) AND NOT EXISTS(SELECT 1 FROM reputation_scores WHERE forecast_id=?) \
             AND NOT EXISTS(SELECT 1 FROM point_ledger WHERE forecast_id=? AND kind='settlement') \
             AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=? AND id!=?) THEN 1 ELSE 0 END )"
                .to_string(),
            vec![
                json!(&guard),
                json!(trigger.forecast_id),
                json!(trigger.forecast_id),
                json!(trigger.forecast_id),
                json!(trigger.forecast_id),
                json!(trigger_hash),
            ],
        ),
        (
            "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'early-resolution-trigger',?,'application/json',?)"
                .to_string(),
            vec![json!(trigger_hash), json!(body), json!(now_ms)],
        ),
        (
            "INSERT OR IGNORE INTO forecast_eligibility_decisions(id,forecast_id,specification_hash,cutoff_at,event_time_basis,created_at,body) \
             VALUES(?,?,?,?,?,?,?)"
                .to_string(),
            vec![
                json!(trigger_hash),
                json!(trigger.forecast_id),
                json!(trigger.specification_hash),
                json!(trigger.event_at_ms),
                json!(trigger.event_time_basis),
                json!(now_ms),
                json!(body),
            ],
        ),
        (
            "INSERT OR IGNORE INTO forecast_timing_reviews(forecast_id,specification_hash,trigger_hash,event_at,event_time_basis,created_at) \
             VALUES(?,?,?,?,?,?)"
                .to_string(),
            vec![
                json!(trigger.forecast_id),
                json!(trigger.specification_hash),
                json!(trigger_hash),
                json!(trigger.event_at_ms),
                json!(trigger.event_time_basis),
                json!(now_ms),
            ],
        ),
        ("DELETE FROM mutation_guards WHERE token=?".to_string(), vec![json!(guard)]),
    ];
    db.batch(&statements).await?;
    let actual = db
        .first(
            "SELECT id FROM forecast_eligibility_decisions WHERE forecast_id=?",
            &[json!(trigger.forecast_id)],
        )
        .await?;
    if actual.as_ref().and_then(|row| text(row, "id")) != Some(trigger_hash.as_str()) {
        return Err(EligibilityError::trigger_changed());
    }
    Ok(())
}

/// Classify a page of accepted receipts. Returns whether the history is exhausted.
pub async fn receipts(db: &dyn Database, trigger: &EarlyResolutionTrigger) -> Result<bool, EligibilityError> {
    let trigger_hash = trigger
        .trigger_hash()
        .map_err(|_| EligibilityError::invalid_trigger())?;
    let rows = db
        .all(
            "SELECT e.revision,e.hash,e.event,e.created_at,c.command_id,c.receipt FROM events e LEFT JOIN command_receipts c \
             ON c.forecast_id=e.forecast_id AND c.command_id=json_extract(e.event,'$.command_id') \
             WHERE e.forecast_id=? AND json_extract(e.event,'$.command_name')='submit_forecast' \
             AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=? AND r.revision=e.revision) \
             ORDER BY e.revision LIMIT ?",
            &[json!(trigger.forecast_id), json!(trigger_hash), json!(PAGE_SIZE)],
        )
        .await?;
    for row in &rows {
        let Some(receipt_text) = text(row, "receipt") else {
            return Err(EligibilityError {
                status: 409,
                code: "eligibility_history_review",
                message: "An accepted receipt is missing; participation remains on hold.",
            });
        };
        let receipt: CommandReceipt =
            serde_json::from_str(receipt_text).map_err(|_| EligibilityError::history_review())?;
        let event: DomainEvent =
            serde_json::from_str(text(row, "event").unwrap_or("")).map_err(|_| EligibilityError::history_review())?;
        let Some(choice) = receipt.accepted_user_forecast.clone() else {
            return Err(EligibilityError {
                status: 409,
                code: "eligibility_history_review",
                message: "Accepted forecast history is incomplete.",
            });
        };
        let command = Command {
            schema_version: 1,
            idempotency_key: receipt.idempotency_key.clone(),
            expected_revision: receipt.revision - 1,
            payload: Payload::SubmitForecast {
                schema_version: 1,
                user_forecast: choice.clone(),
            },
        };
        let command_hash = content_hash(&command).map_err(|_| EligibilityError::history_review())?;
        let event_hash = content_hash(&event).map_err(|_| EligibilityError::history_review())?;
        let choice_hash = content_hash(&choice).map_err(|_| EligibilityError::history_review())?;
        let choice_body = canonical_text(&choice).map_err(|_| EligibilityError::history_review())?;
        let valid = event.forecast_id == receipt.forecast_id
            && receipt.forecast_id == choice.forecast_id
            && choice.forecast_id == trigger.forecast_id
            && event.specification_hash == choice.specification_hash
            && choice.specification_hash == trigger.specification_hash
            && event_hash == text(row, "hash").unwrap_or("")
            && event_hash == receipt.event_hash
            && event.revision == receipt.revision
            && receipt.revision == crate::db::int(row, "revision").unwrap_or(-1)
            && event.command_id == receipt.idempotency_key
            && receipt.idempotency_key == text(row, "command_id").unwrap_or("")
            && receipt.command_hash == command_hash
            && event.occurred_at_ms == receipt.accepted_at_ms
            && receipt.accepted_at_ms == choice.submitted_at_ms
            && choice.submitted_at_ms == crate::db::int(row, "created_at").unwrap_or(-1)
            && event.artifact_hash.as_deref() == Some(choice_hash.as_str());
        let artifact = db
            .first("SELECT body FROM artifacts WHERE hash=?", &[json!(choice_hash)])
            .await?;
        let history = db
            .first(
                "SELECT user_id,body,created_at FROM forecast_history WHERE forecast_id=? AND revision=?",
                &[json!(trigger.forecast_id), json!(receipt.revision)],
            )
            .await?;
        let history_ok = history
            .as_ref()
            .map(|row| {
                text(row, "user_id") == Some(choice.forecaster_id.as_str())
                    && text(row, "body") == Some(choice_body.as_str())
                    && crate::db::int(row, "created_at") == Some(choice.submitted_at_ms)
            })
            .unwrap_or(false);
        let artifact_ok = artifact.as_ref().and_then(|row| text(row, "body")) == Some(choice_body.as_str());
        if !valid || !artifact_ok || !history_ok {
            return Err(EligibilityError::history_review());
        }
        let status = receipt_status(
            &json!(choice.submitted_at_ms),
            &json!(trigger.event_at_ms),
            &trigger.event_time_basis,
        )
        .map_err(|_| EligibilityError::history_review())?;
        let receipt_hash = content_hash(&receipt).map_err(|_| EligibilityError::history_review())?;
        db.execute(
            "INSERT OR IGNORE INTO forecast_receipt_eligibility(decision_id,forecast_id,user_id,revision,receipt_hash,status,body,submitted_at) \
             VALUES(?,?,?,?,?,?,?,?)",
            &[
                json!(trigger_hash),
                json!(trigger.forecast_id),
                json!(choice.forecaster_id),
                json!(receipt.revision),
                json!(receipt_hash),
                json!(status),
                json!(choice_body),
                json!(choice.submitted_at_ms),
            ],
        )
        .await?;
    }
    Ok((rows.len() as i64) < PAGE_SIZE)
}

/// Finish the deciding half: classify a page, then adjust what it classified.
pub async fn apply(
    db: &dyn Database,
    trigger: &EarlyResolutionTrigger,
    now_ms: i64,
    token: &dyn Fn() -> String,
) -> Result<Value, EligibilityError> {
    if let Err(error) = decide(db, trigger, now_ms, token).await {
        if error.code != "forecast_not_found" {
            let finalized = db
                .first(
                    "SELECT id FROM forecasts WHERE id=? AND (state IN ('FINALIZED','ARCHIVED') \
                     OR EXISTS(SELECT 1 FROM reputation_scores WHERE forecast_id=forecasts.id) \
                     OR EXISTS(SELECT 1 FROM point_ledger WHERE forecast_id=forecasts.id AND kind='settlement'))",
                    &[json!(trigger.forecast_id)],
                )
                .await?;
            if finalized.is_some() {
                return Err(EligibilityError::already_settled());
            }
        }
        return Err(error);
    }
    let trigger_hash = trigger
        .trigger_hash()
        .map_err(|_| EligibilityError::invalid_trigger())?;
    if db
        .first(
            "SELECT decision_id FROM forecast_eligibility_completions WHERE decision_id=?",
            &[json!(trigger_hash)],
        )
        .await?
        .is_some()
    {
        return status(db, &trigger.forecast_id, None)
            .await
            .map_err(|_| EligibilityError::history_review());
    }
    if !receipts(db, trigger).await? {
        return status(db, &trigger.forecast_id, None)
            .await
            .map_err(|_| EligibilityError::history_review());
    }
    status(db, &trigger.forecast_id, None)
        .await
        .map_err(|_| EligibilityError::history_review())
}

/// Run after active-market corrections; the database guards reject partial work.
pub async fn finish(
    db: &dyn Database,
    trigger: &EarlyResolutionTrigger,
    now_ms: i64,
) -> Result<Value, EligibilityError> {
    validate(db, trigger, now_ms).await?;
    let trigger_hash = trigger
        .trigger_hash()
        .map_err(|_| EligibilityError::invalid_trigger())?;
    let decision = db
        .first(
            "SELECT id FROM forecast_eligibility_decisions WHERE forecast_id=?",
            &[json!(trigger.forecast_id)],
        )
        .await?;
    if decision.as_ref().and_then(|row| text(row, "id")) != Some(trigger_hash.as_str()) {
        return Err(EligibilityError::trigger_changed());
    }
    let state = status(db, &trigger.forecast_id, None)
        .await
        .map_err(|_| EligibilityError::history_review())?;
    if state["status"] == "complete" {
        return Ok(state);
    }
    let (sql, params) = completion_sql(&trigger_hash, now_ms);
    // No completion is written on conflict, ambiguous time, missing stake funds or uncorrected
    // market fills. A later retry can finish, so the refusal is not an error here.
    if db.execute(&sql, &params).await.is_err() {
        return status(db, &trigger.forecast_id, None)
            .await
            .map_err(|_| EligibilityError::history_review());
    }
    status(db, &trigger.forecast_id, None)
        .await
        .map_err(|_| EligibilityError::history_review())
}
