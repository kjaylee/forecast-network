//! Evidence-cutoff eligibility and resolution-timing review status projections.

use serde_json::{json, Value};
use worker::*;

use crate::db::{all, first, get, int, text};

pub const POLICY_VERSION: &str = "evidence-cutoff-v1";

pub async fn timing_status(session: &D1DatabaseSession, forecast_id: &str) -> Result<Value> {
    let row = first(
        session,
        "SELECT * FROM resolution_timing_reviews WHERE forecast_id=? ORDER BY created_at,resolution_hash LIMIT 1",
        &[json!(forecast_id)],
    )
    .await?;
    let Some(row) = row else {
        return Ok(json!({"status": "none"}));
    };
    let complete = first(
        session,
        "SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id \
         WHERE d.forecast_id=? AND d.specification_hash=?",
        &[json!(forecast_id), get(&row, "specification_hash").clone()],
    )
    .await?
    .is_some();
    let closure = first(
        session,
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

pub async fn status(session: &D1DatabaseSession, forecast_id: &str, user_id: Option<&str>) -> Result<Value> {
    let decision = first(
        session,
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
    let complete = first(
        session,
        "SELECT decision_id FROM forecast_eligibility_completions WHERE decision_id=?",
        std::slice::from_ref(&id),
    )
    .await?
    .is_some();
    let review = first(
        session,
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
        let rows = all(
            session,
            "SELECT revision,status FROM forecast_receipt_eligibility WHERE decision_id=? AND user_id=? ORDER BY revision",
            &[id.clone(), json!(user_id)],
        )
        .await?;
        let adjustment = first(
            session,
            "SELECT available_delta FROM point_eligibility_adjustments WHERE decision_id=? AND user_id=?",
            &[id, json!(user_id)],
        )
        .await?;
        let raw = first(
            session,
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

/// `Application.eligibility_status`: a timing review surfaces when no cutoff decision exists.
pub async fn combined(session: &D1DatabaseSession, forecast_id: &str, user_id: Option<&str>) -> Result<Value> {
    let mut result = status(session, forecast_id, user_id).await?;
    if result["status"] == "none" {
        let timing = timing_status(session, forecast_id).await?;
        if timing["status"] == "review" {
            result["status"] = json!("review");
            result["timingReview"] = timing;
        }
    }
    Ok(result)
}
