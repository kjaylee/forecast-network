//! `GET /api/forecasts/{id}` (`Application.forecast_detail`): the persisted snapshot projected
//! exactly as the Python service does, plus market, eligibility, points and registry views.

use serde_json::{json, Value};
use worker::*;

use forecast_domain::content_hash;

use crate::api_response;
use crate::db::{batch, first, get, text, Row};
use crate::discovery::{self, candidate_sql};
use crate::projections::{card, display_translation, quality_card_sql};
use crate::routes::{Context, RouteError};

pub const EVIDENCE_REWARD_POINTS: i64 = 100;

fn hash_of(value: &Value) -> Result<String> {
    content_hash(value).map_err(|e| worker::Error::from(e.to_string()))
}

fn specification(spec: &Value) -> Value {
    let sources = |items: &Value| -> Vec<Value> {
        items
            .as_array()
            .map(|list| {
                list.iter()
                    .map(|s| json!({"name": s["name"], "url": s["url"]}))
                    .collect()
            })
            .unwrap_or_default()
    };
    json!({
        "canonicalQuestion": spec["canonical_question"], "shareTitle": spec["share_title"],
        "category": spec["category"].as_str().unwrap_or("").to_lowercase(), "openAt": spec["open_at_ms"], "closeAt": spec["close_at_ms"],
        "rules": spec["rules"].as_array().map(|rules| rules.iter().map(|r| json!({"clauseId": r["clause_id"], "outcome": r["outcome"], "condition": r["condition"]})).collect::<Vec<_>>()).unwrap_or_default(),
        "primarySources": sources(&spec["source_policy"]["primary_sources"]),
        "fallbackSources": sources(&spec["source_policy"]["fallback_sources"]),
        "invalidationRules": spec["invalidation_rules"],
        "ambiguityScore": spec["ambiguity_score_bp"].as_f64().unwrap_or(0.0) / 10000.0,
    })
}

fn resolution(value: &Value) -> Result<Value> {
    if value.is_null() {
        return Ok(Value::Null);
    }
    // Early resolutions (v2) carry the proposal inside `trigger`; the read model shows the same fields.
    let evidence = |items: &Value| -> Vec<Value> {
        items.as_array().map(|list| list.iter().map(|e| json!({"url": e["url"], "hash": e["content_sha256"], "snapshotUri": e["snapshot_uri"], "collectedAt": e["collected_at_ms"]})).collect()).unwrap_or_default()
    };
    let provider = |p: &Value| json!({"provider": p["provider"], "model": p["model"], "modelVersion": p["model_version"], "task": p["task"]});
    Ok(json!({
        "proposedOutcome": value["proposed_outcome"], "confidence": value["confidence_bp"].as_f64().unwrap_or(0.0) / 100.0,
        "reasonSummary": value["reason_summary"], "hash": hash_of(value)?, "reviewedAt": value["proposed_at_ms"],
        "ruleMatches": value["rule_matches"], "ruleConflicts": value["rule_conflicts"],
        "evidence": evidence(&value["evidence"]),
        "providers": [provider(&value["judge"]), provider(&value["counter_judge"])],
    }))
}

fn early_projection(forecast: &Value) -> Result<Value> {
    if forecast["schema_version"] != json!(2) || forecast["early_trigger"].is_null() {
        return Ok(Value::Null);
    }
    let trigger = &forecast["early_trigger"];
    let mut sources = Vec::new();
    for item in trigger["evidence"].as_array().map(|v| v.as_slice()).unwrap_or(&[]) {
        sources.push(json!({"url": item["url"], "sourceId": item["source_id"], "evidenceHash": hash_of(item)?}));
    }
    Ok(json!({
        "triggerHash": hash_of(trigger)?, "eventTimeBasis": trigger["event_time_basis"], "eventAt": trigger["event_at_ms"],
        "observedAt": trigger["observed_at_ms"], "qualifiedAt": trigger["counter_qualifier"]["created_at_ms"],
        "originalCloseAt": forecast["specification"]["close_at_ms"], "qualification": trigger["qualification"],
        "upgradedAt": forecast["upgraded_at_ms"], "sources": sources,
    }))
}

fn comment(row: &Row) -> Value {
    json!({"id": get(row, "id"), "text": get(row, "body"), "createdAt": get(row, "created_at"),
           "user": {"id": get(row, "user_id"), "displayName": get(row, "display_name"), "handle": get(row, "handle")}})
}

fn submission(body: &Value, revision: &Value) -> Value {
    let confidence = body["confidence"].as_i64().unwrap_or(0);
    let yes = body["outcome"] == json!("YES");
    json!({"outcome": body["outcome"], "confidence": confidence, "probability": if yes { confidence } else { 100 - confidence },
           "submittedAt": body["submitted_at_ms"], "revision": revision})
}

pub async fn forecast_detail(
    context: &Context<'_>,
    forecast_id: &str,
    user_id: Option<&str>,
) -> std::result::Result<Response, RouteError> {
    let session = context.session;
    let now = context.now_ms;
    let id = json!(forecast_id);
    let user = json!(user_id.unwrap_or(""));
    let card_sql = format!(
        "{} WHERE f.id=?",
        quality_card_sql(now, &[forecast_id.to_string()]).ok_or(RouteError::Input)?
    );
    let results = batch(
        session,
        vec![
            ("SELECT snapshot FROM forecasts WHERE id=?".to_string(), vec![id.clone()]),
            (card_sql, vec![id.clone()]),
            ("SELECT job_error,retry_at FROM forecasts WHERE id=?".to_string(), vec![id.clone()]),
            ("SELECT event,hash FROM events WHERE forecast_id=? ORDER BY revision DESC LIMIT 500".to_string(), vec![id.clone()]),
            ("SELECT c.*,u.display_name,u.handle FROM comments c JOIN users u ON u.id=c.user_id WHERE forecast_id=? ORDER BY c.created_at DESC,c.id DESC LIMIT 100".to_string(), vec![id.clone()]),
            ("SELECT crowd_probability,participant_count,created_at FROM forecast_history WHERE forecast_id=? AND NOT EXISTS (SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=forecast_history.forecast_id AND (d.event_time_basis='observed_upper_bound' OR forecast_history.created_at>=d.cutoff_at)) ORDER BY revision DESC LIMIT 300".to_string(), vec![id.clone()]),
            ("SELECT body,revision FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?".to_string(), vec![id.clone(), user.clone()]),
            ("SELECT body AS display_translation,translated_at,content_hash AS translation_hash,specification_hash FROM forecast_translations WHERE forecast_id=? AND language='en' AND specification_hash=(SELECT specification_hash FROM forecasts WHERE id=?)".to_string(), vec![id.clone(), id.clone()]),
            (format!("{} WHERE f.id=?", candidate_sql(now, None)), vec![id.clone()]),
        ],
    )
    .await?;
    let [snapshot_rows, card_rows, job_rows, events, comments, history, own_rows, translation_rows, quality_rows] =
        <[Vec<Row>; 9]>::try_from(results).map_err(|_| RouteError::Worker("batch shape".into()))?;
    if snapshot_rows.is_empty() || card_rows.is_empty() {
        return Err(RouteError::NotFound("forecast_not_found", "Forecast not found."));
    }
    let forecast: Value = serde_json::from_str(text(&snapshot_rows[0], "snapshot").unwrap_or(""))
        .map_err(|e| RouteError::Worker(e.into()))?;
    let mut item = card(&card_rows[0]);
    if let Some(row) = quality_rows.first() {
        item["quality"] = discovery::score_forecast(row, now).map_err(|e| RouteError::Worker(e.0.into()))?;
    }
    let job = job_rows.first();
    let own = own_rows.first();
    let translation_row = translation_rows.first();
    item["specification"] = specification(&forecast["specification"]);
    item["challengeUntil"] = forecast["challenge_until_ms"].clone();
    item["finalizedOutcome"] = forecast["finalized_outcome"].clone();
    item["pauseReason"] = if !forecast["pause"].is_null() {
        json!("Resolution is paused because all configured AI providers are unavailable.")
    } else {
        job.map_or(Value::Null, |j| get(j, "job_error").clone())
    };
    item["retryAt"] = job
        .and_then(|j| get(j, "retry_at").as_i64().filter(|r| *r != 0))
        .map_or(Value::Null, |r| json!(r));
    let resolution_view = resolution(&forecast["resolution"])?;
    let mut audit = Vec::new();
    for event in events.iter().rev() {
        let raw: Value =
            serde_json::from_str(text(event, "event").unwrap_or("{}")).map_err(|e| RouteError::Worker(e.into()))?;
        audit.push(json!({"command": raw["command_name"], "oldState": raw["old_state"], "newState": raw["new_state"], "at": raw["occurred_at_ms"],
                          "hash": get(event, "hash"), "artifactHash": raw["artifact_hash"], "revision": raw["revision"]}));
    }
    let reviews: Vec<&Value> = forecast["dispute_reviews"]
        .as_array()
        .map(|v| v.iter().collect())
        .unwrap_or_default();
    let mut disputes = Vec::new();
    for dispute in forecast["disputes"].as_array().map(|v| v.as_slice()).unwrap_or(&[]) {
        let dispute_hash = hash_of(dispute)?;
        let review = reviews.iter().find(|r| r["dispute_hash"] == json!(dispute_hash));
        disputes.push(json!({
            "id": dispute["dispute_id"], "claim": dispute["claim"], "ruleClauseId": dispute["rule_clause_id"], "explanation": dispute["explanation"],
            "submittedAt": dispute["submitted_at_ms"], "hash": dispute_hash,
            "evidence": dispute["evidence"].as_array().map(|e| e.iter().map(|x| json!({"url": x["url"], "hash": x["content_sha256"]})).collect::<Vec<_>>()).unwrap_or_default(),
            "review": review.map_or(Value::Null, |r| json!({"reasonSummary": r["reason_summary"], "materialConflict": r["material_conflict"], "reviewedAt": r["reviewed_at_ms"]})),
        }));
    }
    item["earlyResolution"] = early_projection(&forecast)?;
    let env = context.env;
    let var = |name: &str| env.var(name).map(|v| v.to_string()).unwrap_or_default();
    let registry_enabled = var("SOLANA_REGISTRY_ENABLED") == "true" && !var("SOLANA_PROGRAM_ID").is_empty();
    if registry_enabled {
        let program: [u8; 32] = bs58::decode(var("SOLANA_PROGRAM_ID"))
            .into_vec()
            .ok()
            .and_then(|v| v.try_into().ok())
            .ok_or_else(|| RouteError::Worker("program id".into()))?;
        item["chain"] = crate::registry::status(session, &program, forecast_id).await?;
    }
    let reports = first(
        session,
        "SELECT COUNT(*) AS n,(SELECT status FROM evidence_reports WHERE forecast_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1) AS mine FROM evidence_reports WHERE forecast_id=?",
        &[id.clone(), user.clone(), id],
    )
    .await?;
    let live_enabled = var("LIVE_MARKETS_ENABLED") == "true";
    let attestation_available = registry_enabled && !var("SOLANA_RELAYER").is_empty();
    let points = match user_id {
        Some(user_id) => match crate::points::summary(session, user_id).await? {
            Some(summary) => summary,
            None => {
                return Err(RouteError::Unauthorized(
                    "points_account_missing",
                    "Sign in to view your participation points.",
                ))
            }
        },
        None => Value::Null,
    };
    let stake = match user_id {
        Some(user_id) => crate::points::position_for(session, user_id, forecast_id).await?,
        None => Value::Null,
    };
    let my_forecast = match own {
        Some(row) => {
            let body: Value =
                serde_json::from_str(text(row, "body").unwrap_or("{}")).map_err(|e| RouteError::Worker(e.into()))?;
            submission(&body, get(row, "revision"))
        }
        None => Value::Null,
    };
    let data = json!({
        "forecast": item,
        "market": crate::markets::market(session, forecast_id, live_enabled).await?,
        "resolution": resolution_view, "disputes": disputes, "audit": audit,
        "evidenceReports": {"count": reports.as_ref().map_or(json!(0), |r| get(r, "n").clone()),
                            "mine": reports.as_ref().map_or(Value::Null, |r| get(r, "mine").clone()), "reward": EVIDENCE_REWARD_POINTS},
        "attestation": crate::registry::attestation(session, user_id, forecast_id, attestation_available).await?,
        "eligibility": crate::eligibility::combined(&crate::db::D1(session), forecast_id, user_id).await?,
        "points": points, "stake": stake,
        "displayTranslation": translation_row.and_then(display_translation).unwrap_or(Value::Null),
        "auditTruncated": events.len() == 500, "historyTruncated": history.len() == 300,
        "comments": comments.iter().map(comment).collect::<Vec<_>>(),
        "myForecast": my_forecast,
        "history": history.iter().rev().map(|row| json!({"at": get(row, "created_at"), "probability": get(row, "crowd_probability"), "count": get(row, "participant_count")})).collect::<Vec<_>>(),
    });
    Ok(api_response(data, 200, false)?)
}
