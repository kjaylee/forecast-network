//! `ResolutionTiming`: the gate that decides whether a resolution may be committed.
//!
//! It exists to stop a result being finalized while the publication time of its evidence is in
//! question — that is, to stop rewards and reputation being credited on evidence that may have
//! arrived after participation closed. Everything here is in service of that one question, and
//! the two ways it answers are worth keeping apart:
//!
//! - An open review blocks every outcome. Nothing is rewarded.
//! - A closed review admits exactly the outcome the closure determined, and nothing else. The
//!   closure released the blocker; it did not make the evidence supportable.
//!
//! Both are needed. An exception carved into one caller and not the others is a reward leaking
//! through whichever caller was forgotten, which is why the blockers are a view and the review
//! is closed rather than bypassed.

use forecast_domain::python_json_bytes;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use worker::*;

use forecast_domain::lifecycle::{AnyResolution, Forecast};
use forecast_domain::models::EvidenceSnapshot;

use crate::article::{article_content, instant_ms};
use crate::db::{text, Database};
use crate::routes::RouteError;

/// Set by `close_indeterminate`; the only determination a review can reach.
pub const INVALID: &str = "INVALID";
pub const INDETERMINATE_REASON: &str = "publication_time_unknown";

fn review_error() -> RouteError {
    RouteError::Failed(
        409,
        "resolution_timing_review",
        "The publication time of resolution evidence needs review before results or points can be finalized.",
    )
}

fn determined_error(_determination: &str) -> RouteError {
    RouteError::Failed(
        409,
        "resolution_timing_determined",
        "The publication-time review for this forecast is closed and determines INVALID. No other result can be finalized from this evidence.",
    )
}

async fn completed(db: &dyn Database, forecast_id: &str, specification_hash: &str) -> Result<bool, RouteError> {
    Ok(db.first("SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id \
         WHERE d.forecast_id=? AND d.specification_hash=?",
        &[json!(forecast_id), json!(specification_hash)],
    )
    .await?
    .is_some())
}

/// The determination a closed review carries, if one is in force.
pub async fn closure_determination(
    db: &dyn Database,
    forecast_id: &str,
    specification_hash: &str,
) -> Result<Option<String>, RouteError> {
    Ok(db
        .first(
            "SELECT determination FROM resolution_timing_closures WHERE forecast_id=? AND specification_hash=?",
            &[json!(forecast_id), json!(specification_hash)],
        )
        .await?
        .and_then(|row| text(&row, "determination").map(str::to_string)))
}

/// Whether this resolution may be committed, in the reference's order of checks.
pub async fn check(
    db: &dyn Database,
    forecast: &Forecast,
    resolution: &AnyResolution,
    now_ms: i64,
    artifacts: &[(String, String, String)],
) -> Result<(), RouteError> {
    let base = resolution.base();
    if base.forecast_id != forecast.forecast_id || base.specification_hash != forecast.specification_hash {
        return Err(review_error());
    }
    base.validate_for(&forecast.specification).map_err(|_| review_error())?;
    if matches!(resolution, AnyResolution::Early(_)) {
        return Ok(());
    }
    if completed(db, &forecast.forecast_id, &forecast.specification_hash).await? {
        return Ok(());
    }
    if let Some(determination) = closure_determination(db, &forecast.forecast_id, &forecast.specification_hash).await? {
        // The review is closed, so the guard has stopped applying everywhere at once -- including
        // in the outbox and the registry, which read the blocker view rather than asking this
        // function. That is only safe because of the line below: the closure admits its own
        // determination and nothing else, so a reward still cannot be credited from evidence
        // whose publication time could not be placed.
        if base.proposed_outcome != determination {
            return Err(determined_error(&determination));
        }
        return Ok(());
    }
    if db
        .first(
            "SELECT 1 FROM resolution_timing_reviews WHERE forecast_id=? AND specification_hash=?",
            &[json!(forecast.forecast_id), json!(forecast.specification_hash)],
        )
        .await?
        .is_some()
    {
        return Err(review_error());
    }
    analyze(db, forecast, resolution, now_ms, artifacts).await
}

/// The part that reads the retained bytes, in the reference's order, returning the error it
/// would have raised once a review is written.
async fn analyze(
    db: &dyn Database,
    forecast: &Forecast,
    resolution: &AnyResolution,
    now_ms: i64,
    artifacts: &[(String, String, String)],
) -> Result<(), RouteError> {
    let base = resolution.base();
    let latest = db.first("SELECT MAX(at) AS at FROM ( \
         SELECT submitted_at AS at FROM user_forecasts WHERE forecast_id=? UNION ALL \
         SELECT created_at AS at FROM events WHERE forecast_id=? AND json_extract(event,'$.command_name')='submit_forecast' UNION ALL \
         SELECT json_extract(receipt,'$.accepted_at_ms') AS at FROM command_receipts WHERE forecast_id=? \
         AND json_extract(receipt,'$.accepted_user_forecast') IS NOT NULL UNION ALL \
         SELECT f.created_at AS at FROM market_fills f JOIN point_markets m ON m.forecast_id=f.forecast_id \
         WHERE f.forecast_id=? AND m.mode='active')",
        &vec![json!(forecast.forecast_id); 4],
    )
    .await?;
    let Some(last_at) = latest.as_ref().and_then(|row| crate::db::int(row, "at")) else {
        // No participation to be late for: a forecast nobody joined cannot be resolved in
        // someone's favour, so the timing question does not arise.
        return Ok(());
    };
    let mut retained: Vec<(String, Vec<Value>)> = Vec::new();
    let mut proof: Vec<Value> = Vec::new();
    let mut reasons: Vec<String> = Vec::new();
    let mut candidates: Vec<i64> = Vec::new();
    for evidence in &base.evidence {
        // The reference reads the verification by the snapshot's own hash, which is derived,
        // not stored.
        let evidence_hash = evidence.evidence_hash().map_err(|_| review_error())?;
        let (valid, bodies) = inspect(db, evidence, artifacts).await?;
        let mut publication: Option<String> = None;
        let mut precision = "unknown".to_string();
        let mut at: Option<i64> = None;
        let reason;
        if !valid {
            reason = if bodies.is_empty() {
                "evidence_unavailable"
            } else {
                "evidence_integrity"
            };
        } else if !base
            .source_verifications
            .iter()
            .any(|item| item.evidence_hash == evidence_hash && item.verified)
        {
            reason = "evidence_unverified";
        } else {
            let (_, (published, where_from)) = article_content(&bodies[0]).map_err(|_| review_error())?;
            publication = published.clone();
            precision = where_from.to_string();
            if publication.is_none() || precision != "instant" {
                reason = "publication_time_unknown";
            } else {
                let instant = instant_ms(publication.as_deref().unwrap_or_default());
                if instant < 0 || instant > now_ms.min(evidence.collected_at_ms) {
                    reason = "publication_time_inconsistent";
                } else if last_at < 0 {
                    reason = "receipt_time_inconsistent";
                } else if instant <= last_at {
                    reason = "evidence_may_predate_participation";
                    candidates.push(instant);
                } else {
                    reason = "after_last_receipt";
                }
                at = Some(instant);
            }
            // A body supplied with the request is retained, so a later review reads the same
            // bytes rather than re-fetching a page that may have changed.
            if let Some((_, body, media_type)) = artifacts.iter().find(|(hash, _, _)| *hash == evidence.content_sha256)
            {
                retained.push((
                    "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) \
                     VALUES(?,'resolution-timing-evidence',?,?,?)"
                        .to_string(),
                    vec![
                        json!(evidence.content_sha256),
                        json!(body),
                        json!(media_type),
                        json!(now_ms),
                    ],
                ));
            }
        }
        proof.push(json!({
            "contentHash": evidence.content_sha256, "url": evidence.url, "hashVerified": valid,
            "publication": publication, "precision": precision, "publishedAt": at, "reason": reason,
        }));
        if reason != "after_last_receipt" {
            reasons.push(reason.to_string());
        }
    }
    if reasons.is_empty() {
        return Ok(());
    }
    let body = canonical_body(
        &serde_json::to_string(&json!({
            "schemaVersion": 1, "forecastId": forecast.forecast_id,
            "specificationHash": forecast.specification_hash,
            "resolutionHash": base.resolution_hash().unwrap_or_default(),
            "lastReceiptAt": last_at, "evidence": proof,
        }))
        .map_err(|error| RouteError::Worker(error.into()))?,
    );
    retained.push((
        "INSERT OR IGNORE INTO resolution_timing_reviews(forecast_id,specification_hash,resolution_hash,\
         reason,last_receipt_at,candidate_cutoff_at,proof_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?,?)"
            .to_string(),
        vec![
            json!(forecast.forecast_id),
            json!(forecast.specification_hash),
            json!(base.resolution_hash().unwrap_or_default()),
            json!(reasons[0]),
            json!(last_at),
            json!(candidates.iter().min().copied()),
            json!(hex::encode(Sha256::digest(body.as_bytes()))),
            json!(body),
            json!(now_ms),
        ],
    ));
    db.batch(&retained).await?;
    Err(review_error())
}

/// The reference serializes with `sort_keys=True, separators=(",",":")` and the default
/// `ensure_ascii` — the second of the two canonical rules, not the commitment rule
/// `canonical_bytes` implements. The proof hash is taken over this exact text, and the evidence
/// it covers carries article publication strings, so an accented one would hash differently.
fn canonical_body(body: &str) -> String {
    let Ok(value) = serde_json::from_str::<Value>(body) else {
        return body.to_string();
    };
    String::from_utf8(python_json_bytes(&value).unwrap_or_default()).unwrap_or_else(|_| body.to_string())
}

/// Whether the retained bytes are the evidence the resolution claims, and the first of them.
async fn inspect(
    db: &dyn Database,
    evidence: &EvidenceSnapshot,
    supplied: &[(String, String, String)],
) -> Result<(bool, Vec<String>), RouteError> {
    let stored = db
        .first(
            "SELECT body FROM artifacts WHERE hash=?",
            &[json!(evidence.content_sha256)],
        )
        .await?;
    let mut bodies: Vec<String> = supplied
        .iter()
        .filter(|(hash, _, _)| *hash == evidence.content_sha256)
        .map(|(_, body, _)| body.clone())
        .collect();
    if let Some(row) = &stored {
        if let Some(body) = text(row, "body") {
            bodies.push(body.to_string());
        }
    }
    let valid = !bodies.is_empty()
        && bodies
            .iter()
            .all(|body| hex::encode(Sha256::digest(body.as_bytes())) == evidence.content_sha256);
    Ok((valid, bodies))
}

/// `close_indeterminate`: close a review whose own reason settles the only result left.
///
/// Only `publication_time_unknown` qualifies. The review recorded the evidence as authentic and
/// its publication time as absent, and nothing that arrives later changes that, so no outcome
/// can be credited from it. This writes down the conclusion the review already reached rather
/// than overriding it: the closure is bound to that review's proof and to the exact evidence
/// item the review marked unplaceable, so it cannot be manufactured from an unrelated review.
pub async fn close_indeterminate(db: &dyn Database, forecast: &Forecast, now_ms: i64) -> Result<bool, RouteError> {
    let specification_hash = forecast.specification_hash.as_str();
    if closure_determination(db, &forecast.forecast_id, specification_hash)
        .await?
        .is_some()
    {
        return Ok(true);
    }
    if completed(db, &forecast.forecast_id, specification_hash).await? {
        return Ok(true);
    }
    let Some(row) = db
        .first(
            "SELECT proof_hash, body FROM resolution_timing_reviews WHERE forecast_id=? AND specification_hash=? \
         AND reason='publication_time_unknown' ORDER BY created_at,resolution_hash LIMIT 1",
            &[json!(forecast.forecast_id), json!(specification_hash)],
        )
        .await?
    else {
        return Ok(false);
    };
    let (Some(proof_hash), Some(proof_body)) = (text(&row, "proof_hash"), text(&row, "body")) else {
        return Ok(false);
    };
    let Ok(proof) = serde_json::from_str::<Value>(proof_body) else {
        return Ok(false);
    };
    let evidence = proof["evidence"].as_array().and_then(|items| {
        items
            .iter()
            .find(|item| item["reason"] == INDETERMINATE_REASON)
            .and_then(|item| item["contentHash"].as_str())
            .map(str::to_string)
    });
    // A review whose proof does not carry the reason it is filed under is not something this can
    // close; leave it for a human rather than guess.
    let Some(evidence) = evidence else {
        return Ok(false);
    };
    let body = canonical_body(
        &serde_json::to_string(&json!({
            "schemaVersion": 1, "forecastId": forecast.forecast_id, "specificationHash": specification_hash,
            "reviewProofHash": proof_hash, "determination": INVALID, "reason": INDETERMINATE_REASON,
            "evidenceHash": evidence,
        }))
        .map_err(|error| RouteError::Worker(error.into()))?,
    );
    let statement = (
        "INSERT INTO resolution_timing_closures(forecast_id,specification_hash,review_proof_hash,\
         determination,reason,evidence_hash,proof_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?,?)"
            .to_string(),
        vec![
            json!(forecast.forecast_id),
            json!(specification_hash),
            json!(proof_hash),
            json!(INVALID),
            json!(INDETERMINATE_REASON),
            json!(evidence),
            json!(hex::encode(Sha256::digest(body.as_bytes()))),
            json!(body),
            json!(now_ms),
        ],
    );
    if let Err(error) = db.batch(&[statement]).await {
        // The row is keyed and immutable, so what matters is that it exists, not who wrote it.
        // Anything else -- including a validation trigger refusing the write because the forecast
        // already moved -- has to surface.
        if closure_determination(db, &forecast.forecast_id, specification_hash)
            .await?
            .is_none()
        {
            return Err(RouteError::Worker(error));
        }
    }
    Ok(true)
}

/// `status`: what the review says, plus the determination when one is in force.
pub async fn status(db: &dyn Database, forecast_id: &str) -> Result<Value, RouteError> {
    let Some(row) = db
        .first(
            "SELECT * FROM resolution_timing_reviews WHERE forecast_id=? ORDER BY created_at,resolution_hash LIMIT 1",
            &[json!(forecast_id)],
        )
        .await?
    else {
        return Ok(json!({"status": "none"}));
    };
    let specification_hash = text(&row, "specification_hash").unwrap_or_default();
    let complete = completed(db, forecast_id, specification_hash).await?;
    let mut value = json!({
        "status": if complete { "complete" } else { "review" },
        "reason": text(&row, "reason"),
        "candidateCutoffAt": crate::db::int(&row, "candidate_cutoff_at"),
        "proofHash": text(&row, "proof_hash"),
    });
    // Additive: callers that only understand "review" keep working, and a closed review is
    // reported rather than hidden inside the same word.
    if let Some(determination) = closure_determination(db, forecast_id, specification_hash).await? {
        value["determination"] = json!(determination);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_instant_becomes_the_same_millisecond_python_would() {
        // The values are `int(datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()*1000)`
        // for each, taken from the reference rather than computed by hand.
        assert_eq!(instant_ms("1970-01-01T00:00:00Z"), 0);
        assert_eq!(instant_ms("2026-09-15T10:00:00Z"), 1_789_466_400_000);
        assert_eq!(instant_ms("2026-09-15T10:00:00+09:00"), 1_789_434_000_000);
        assert_eq!(instant_ms("2026-09-15T10:00:00-05:00"), 1_789_484_400_000);
        // A leap day, so the civil-date arithmetic is exercised rather than assumed.
        assert_eq!(instant_ms("2024-02-29T00:00:00Z"), 1709164800000);
    }

    /// The schema is where the guard actually lives: five consumers read the blocker view and
    /// the state trigger reads the view. This runs against the migrations that are deployed
    /// rather than against a description of them, which is what the SQLite double exists for.
    #[test]
    fn a_closure_releases_the_blocker_view_and_the_state_trigger() {
        use crate::db::{Database, Sqlite};
        const SPEC: &str = "1111111111111111111111111111111111111111111111111111111111111111";
        const RESOLUTION: &str = "2222222222222222222222222222222222222222222222222222222222222222";
        const PROOF: &str = "3333333333333333333333333333333333333333333333333333333333333333";
        const EVIDENCE: &str = "4444444444444444444444444444444444444444444444444444444444444444";
        let db = Sqlite::from_migrations();
        let run = |sql: &str| futures_lite::future::block_on(db.execute(sql, &[])).expect("statement");
        let count = |sql: &str| {
            futures_lite::future::block_on(db.first(sql, &[]))
                .expect("query")
                .and_then(|row| crate::db::int(&row, "n"))
                .unwrap_or(-1)
        };
        let review_body = format!(
            "{{\"schemaVersion\":1,\"forecastId\":\"f\",\"specificationHash\":\"{SPEC}\",\
             \"resolutionHash\":\"{RESOLUTION}\",\"lastReceiptAt\":10,\
             \"evidence\":[{{\"contentHash\":\"{EVIDENCE}\",\"reason\":\"publication_time_unknown\"}}]}}"
        );
        let closure_body = |review_proof: &str| {
            format!(
                "{{\"determination\":\"INVALID\",\"evidenceHash\":\"{EVIDENCE}\",\"forecastId\":\"f\",\
                 \"reason\":\"publication_time_unknown\",\"reviewProofHash\":\"{review_proof}\",\
                 \"specificationHash\":\"{SPEC}\"}}"
            )
        };
        run("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','H','h','r',1)");
        run(&format!(
            "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
             normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
             VALUES('f','u','d','{{}}',1,'RESOLVING','CRYPTO','t','q','q','{SPEC}',0,1,1,1,'k')"
        ));
        run(&format!(
            "INSERT INTO resolution_timing_reviews(forecast_id,specification_hash,resolution_hash,reason,\
             last_receipt_at,candidate_cutoff_at,proof_hash,body,created_at) \
             VALUES('f','{SPEC}','{RESOLUTION}','publication_time_unknown',10,NULL,'{PROOF}','{review_body}',1)"
        ));

        assert_eq!(count("SELECT COUNT(*) AS n FROM forecast_resolution_blockers"), 1);
        assert!(
            futures_lite::future::block_on(db.execute("UPDATE forecasts SET state='FINALIZED' WHERE id='f'", &[]))
                .is_err(),
            "an unresolved review must hold the rewarded states"
        );

        // A closure is bound to the review it concludes; a proof it does not name is refused.
        let wrong = format!(
            "INSERT INTO resolution_timing_closures(forecast_id,specification_hash,review_proof_hash,\
             determination,reason,evidence_hash,proof_hash,body,created_at) \
             VALUES('f','{SPEC}','{RESOLUTION}','INVALID','publication_time_unknown','{EVIDENCE}','{PROOF}',\
             '{}',1)",
            closure_body(RESOLUTION)
        );
        assert!(
            futures_lite::future::block_on(db.execute(&wrong, &[])).is_err(),
            "a closure has to name the proof of the review it concludes"
        );

        let right = format!(
            "INSERT INTO resolution_timing_closures(forecast_id,specification_hash,review_proof_hash,\
             determination,reason,evidence_hash,proof_hash,body,created_at) \
             VALUES('f','{SPEC}','{PROOF}','INVALID','publication_time_unknown','{EVIDENCE}','{PROOF}',\
             '{}',1)",
            closure_body(PROOF)
        );
        run(&right);
        assert_eq!(
            count("SELECT COUNT(*) AS n FROM forecast_resolution_blockers"),
            0,
            "the closure is what turns the boolean false everywhere at once"
        );
        assert!(
            futures_lite::future::block_on(db.execute("UPDATE forecasts SET state='FINALIZED' WHERE id='f'", &[]))
                .is_ok(),
            "and the state trigger stops holding the forecast"
        );
    }

    /// `RouteError` carries a `worker::Error`, so it has no `Debug` and cannot be `.expect`ed.
    fn ok<T>(outcome: std::result::Result<T, crate::routes::RouteError>, what: &str) -> T {
        match outcome {
            Ok(value) => value,
            Err(_) => panic!("{what} failed"),
        }
    }

    /// A real domain object from the golden vectors, so the test reads what the port reads
    /// rather than a hand-built approximation of it.
    fn golden_forecast() -> Forecast {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/lifecycle-golden.json");
        let value: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).expect("golden vectors")).expect("json");
        let first = &value["steps"][0]["before"];
        forecast_domain::lifecycle::Snapshot::from_json(&first.to_string())
            .expect("snapshot")
            .base()
            .clone()
    }

    /// The projection the scheduler and the operator both read, against the deployed schema.
    #[test]
    fn the_status_reports_a_review_and_not_a_determination_it_does_not_have() {
        use crate::db::{Database, Sqlite};
        let forecast = golden_forecast();
        let (id, spec) = (forecast.forecast_id.clone(), forecast.specification_hash.clone());
        let db = Sqlite::from_migrations();
        let run = |sql: &str| futures_lite::future::block_on(db.execute(sql, &[])).expect("statement");

        let none = ok(futures_lite::future::block_on(status(&db, &id)), "status");
        assert_eq!(none["status"], "none", "a forecast with no review has no timing state");

        run("INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','H','h','r',1)");
        run(&format!(
            "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
             normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
             VALUES('{id}','u','d','{{}}',1,'RESOLVING','CRYPTO','t','q','q','{spec}',0,1,1,1,'k')"
        ));
        run(&format!(
            "INSERT INTO resolution_timing_reviews(forecast_id,specification_hash,resolution_hash,reason,\
             last_receipt_at,candidate_cutoff_at,proof_hash,body,created_at) \
             VALUES('{id}','{spec}','2222222222222222222222222222222222222222222222222222222222222222',\
             'evidence_may_predate_participation',4242,7,'3333333333333333333333333333333333333333333333333333333333333333',\
             '{{\"forecastId\":\"{id}\",\"specificationHash\":\"{spec}\",\
             \"resolutionHash\":\"2222222222222222222222222222222222222222222222222222222222222222\",\
             \"lastReceiptAt\":4242}}',1)"
        ));
        let open = ok(futures_lite::future::block_on(status(&db, &id)), "status");
        assert_eq!(open["status"], "review");
        assert_eq!(open["reason"], "evidence_may_predate_participation");
        assert_eq!(open["candidateCutoffAt"], 7);
        assert_eq!(
            open["proofHash"],
            "3333333333333333333333333333333333333333333333333333333333333333"
        );
        assert!(
            open.get("determination").is_none(),
            "an open review has no determination: {open}"
        );

        // Only `publication_time_unknown` is settled by its own proof; a review that says the
        // evidence may predate participation is a judgement and stays one.
        let closed = ok(
            futures_lite::future::block_on(close_indeterminate(&db, &forecast, 1)),
            "close",
        );
        assert!(!closed, "this reason is not one a closure can reach");
        assert_eq!(
            ok(futures_lite::future::block_on(status(&db, &id)), "status")["status"],
            "review"
        );
    }

    #[test]
    fn the_proof_body_is_canonical_so_its_hash_matches_the_reference() {
        let body = canonical_body(r#"{"b":1,"a":{"d":2,"c":3}}"#);
        assert_eq!(body, r#"{"a":{"c":3,"d":2},"b":1}"#);
    }

    #[test]
    fn the_proof_body_escapes_the_way_the_reference_does() {
        // The proof hash is taken over this text, and the evidence it covers carries article
        // publication strings — so this is the `ensure_ascii` rule and not the commitment rule.
        // The vector is the one that pins both rules, run through the function the module uses.
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/canonical-json-golden.json");
        let document: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).expect("canonical json golden")).expect("json");
        for case in document["cases"].as_array().expect("cases") {
            let name = case["name"].as_str().unwrap();
            let text = serde_json::to_string(&case["value"]).expect("text");
            assert_eq!(
                canonical_body(&text),
                case["escaped"].as_str().unwrap(),
                "{name}: the review body rule"
            );
        }
        // Text that is not JSON is passed through rather than silently replaced by an empty body.
        assert_eq!(canonical_body("not json"), "not json");
    }
}
