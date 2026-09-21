//! The operator's adjudication: a supplied verdict enters the record without a model, and the
//! receipt is what makes a retry a retry rather than a second adjudication. Split from
//! `writes.rs`, whose helpers it still uses.

use serde_json::{json, Map, Value};

use forecast_domain::lifecycle::Payload;

use crate::api_response;
use crate::mutate::{hash_of, load_snapshot, mutate, random_token, record_artifact, Mutation};
use crate::routes::{Context, RouteError};
use crate::writes::*;

/// `Application.adjudicate_forecast`: the exceptional ADMIN-caller path.
///
/// The HTTP adapter authenticates the operator before this is reached. This boundary never
/// manufactures a verdict, modifies provenance, or finalizes: a successful adjudication enters
/// PROPOSED, and the ordinary scheduler then opens a fresh, complete challenge window.
///
/// The idempotency receipt is written *inside* the same batch as the command, so a retry that
/// arrives after a lost response returns the original receipt rather than adjudicating twice.
/// A prepared operator decision, named rather than positional: six of these are strings or records,
/// and a call whose order nobody can check by reading it is a call nobody can review.
pub struct Adjudication<'a> {
    pub forecast_id: &'a str,
    pub resolution: &'a forecast_domain::models::Resolution,
    pub adjudicator: &'a forecast_domain::models::AIProvenance,
    /// `(hash, kind, body, media type)` — the evidence supplied *with* the request.
    pub artifacts: &'a [(String, String, String, String)],
    pub idempotency_key: &'a str,
    pub expected_revision: Option<i64>,
    pub token: &'a dyn Fn() -> String,
}

pub async fn adjudicate_forecast(
    db: &dyn crate::db::Database,
    now_ms: i64,
    adjudication: Adjudication<'_>,
) -> std::result::Result<Value, RouteError> {
    let Adjudication {
        forecast_id,
        resolution,
        adjudicator,
        artifacts,
        idempotency_key,
        expected_revision,
        token,
    } = adjudication;
    if expected_revision.is_some_and(|revision| revision < 0) {
        return Err(invalid());
    }
    if artifacts.len() > 32 {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "Check the format and number of evidence artifacts.",
        ));
    }
    let resolution_hash = resolution.resolution_hash().map_err(|_| invalid())?;
    let request = json!({
        "kind": "adjudicate",
        "forecastId": forecast_id,
        "resolutionHash": resolution_hash,
        "adjudicatorHash": hash_of(adjudicator)?,
        "revision": expected_revision,
        "artifacts": artifacts
            .iter()
            .map(|(hash, kind, _, media)| json!({"hash": hash, "kind": kind, "mediaType": media}))
            .collect::<Vec<_>>(),
    });
    let operator = "admin:adjudication";
    if let Some(prior) = prior_row(db, operator, idempotency_key, &request).await? {
        let mut receipt = prior_result(&prior)?;
        receipt["forecast"] = card_row(db, forecast_id, now_ms).await?;
        return Ok(receipt);
    }
    let snapshot = load_snapshot(db, forecast_id).await?;
    let forecast = snapshot.base().clone();
    if expected_revision.is_some_and(|revision| forecast.revision != revision) {
        return Err(crate::mutate::conflict());
    }
    if forecast.state != "ESCALATED" {
        return Err(RouteError::Failed(
            409,
            "adjudication_not_allowed",
            "Only forecasts awaiting independent adjudication can be processed.",
        ));
    }
    // A prepared operator decision can travel over HTTP without rewriting its hash-bound
    // timestamps. Reject future, stale or backdated decisions.
    let decision_at = resolution.proposed_at_ms;
    if !(forecast.updated_at_ms <= decision_at && decision_at <= now_ms) || now_ms - decision_at > 15 * 60 * 1000 {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "The independent decision must follow the current record and have been created within the last 15 minutes.",
        ));
    }
    let mut extra = crate::source_watch::artifact_sql(artifacts, now_ms)
        .map_err(|refusal| RouteError::Failed(refusal.status(), refusal.code(), refusal.message()))?;
    // Every piece of evidence the replacement resolution cites has to be *retained* — supplied with
    // the request or already in the store — and to hash to what the resolution claims.
    let supplied: std::collections::HashMap<&str, &str> = artifacts
        .iter()
        .map(|(hash, _, body, _)| (hash.as_str(), body.as_str()))
        .collect();
    for evidence in &resolution.evidence {
        let body = match supplied.get(evidence.content_sha256.as_str()) {
            Some(body) => Some((*body).to_string()),
            None => crate::scheduler::read_artifact(db, &evidence.content_sha256)
                .await
                .map_err(|_| {
                    RouteError::Failed(
                        503,
                        "forecast_storage_unavailable",
                        "The evidence store is temporarily unavailable.",
                    )
                })?,
        };
        let Some(body) = body else {
            return Err(RouteError::Failed(
                422,
                "missing_resolution_artifact",
                "Every original evidence artifact for the replacement resolution must be retained.",
            ));
        };
        if crate::source_watch::hash_hex(&body) != evidence.content_sha256 {
            return Err(RouteError::Failed(
                422,
                "resolution_artifact_mismatch",
                "The retained resolution evidence does not match its hash.",
            ));
        }
    }
    let payload = Payload::AdjudicateResolution {
        schema_version: 1,
        resolution: resolution.clone(),
        adjudicator: adjudicator.clone(),
    };
    let command_key = format!(
        "admin:{}",
        forecast_domain::content_hash(&json!({"operator": operator, "key": idempotency_key}))
            .map_err(|error| RouteError::Worker(error.to_string().into()))?
    );
    // The domain validates the source-verification commitments, the exact specification, the
    // reviewed-dispute bindings, the independent provider and every timestamp. A preview is how
    // this boundary learns what the receipt will say without writing anything.
    let preview = forecast_domain::lifecycle::apply_command(
        &snapshot,
        &forecast_domain::lifecycle::Command {
            schema_version: 1,
            idempotency_key: command_key.clone(),
            expected_revision: forecast.revision,
            payload: payload.clone(),
        },
        decision_at,
        None,
    )
    .map_err(|_| {
        RouteError::Failed(
            422,
            "adjudication_validation_failed",
            "The decision failed independence, immutable criteria, or evidence linkage verification.",
        )
    })?;
    let response = json!({
        "adjudication": {
            "resolutionHash": resolution_hash,
            "eventHash": preview.receipt.event_hash,
            "revision": preview.receipt.revision,
            "acceptedAt": decision_at,
        }
    });
    extra.push(record_artifact(resolution, "adjudicated_resolution", None, now_ms)?);
    extra.push(record_artifact(adjudicator, "independent_adjudicator", None, now_ms)?);
    extra.push(operation(
        operator,
        idempotency_key,
        &request,
        forecast_id,
        &response,
        now_ms,
    )?);
    for verification in &resolution.source_verifications {
        extra.push(record_artifact(
            verification,
            "adjudication_source_verification",
            None,
            now_ms,
        )?);
    }
    let timing_artifacts = artifacts
        .iter()
        .map(|(hash, _, body, media)| (hash.clone(), body.clone(), media.clone()))
        .collect();
    let outcome = mutate(
        db,
        Mutation {
            snapshot: &snapshot,
            payload,
            key: command_key,
            now_ms: decision_at,
            extra,
            job_token: None,
            timing_artifacts,
        },
        now_ms,
        token,
        None,
    )
    .await;
    if let Err(error) = outcome {
        // A refusal and a lost race look the same from here, so the receipt is read back before
        // deciding which it was.
        if let Some(prior) = prior_row(db, operator, idempotency_key, &request).await? {
            let mut receipt = prior_result(&prior)?;
            receipt["forecast"] = card_row(db, forecast_id, now_ms).await?;
            return Ok(receipt);
        }
        return Err(error);
    }
    let mut result = response;
    result["forecast"] = card_row(db, forecast_id, now_ms).await?;
    Ok(result)
}

/// `POST /api/admin/forecasts/{id}/adjudicate`: a prepared operator verdict.
///
/// The route layer authenticates the operator before this is reached; what is checked here is the
/// *shape* of the decision, because a missing revision is not a decision anybody can replay.
/// `revision` is required exactly — a prepared verdict that does not say which revision it was
/// prepared against is a verdict that may be applied to the wrong one.
pub async fn adjudicate(context: &Context<'_>, forecast_id: &str, body: &Map<String, Value>) -> Handler {
    let revision = match body.get("revision").and_then(Value::as_i64) {
        Some(revision) if revision >= 0 => revision,
        _ => return Err(RouteError::Input),
    };
    let resolution: forecast_domain::models::Resolution =
        serde_json::from_value(body.get("resolution").cloned().unwrap_or(Value::Null))
            .map_err(|_| RouteError::Input)?;
    let adjudicator: forecast_domain::models::AIProvenance =
        serde_json::from_value(body.get("adjudicator").cloned().unwrap_or(Value::Null))
            .map_err(|_| RouteError::Input)?;
    let mut artifacts = Vec::new();
    for record in body.get("artifacts").and_then(Value::as_array).unwrap_or(&Vec::new()) {
        let field = |name: &str| record.get(name).and_then(Value::as_str).map(str::to_string);
        let (Some(hash), Some(kind), Some(body_text)) = (field("content_hash"), field("kind"), field("body")) else {
            return Err(RouteError::Input);
        };
        let media = field("media_type").unwrap_or_else(|| "application/json".to_string());
        artifacts.push((hash, kind, body_text, media));
    }
    let key = body.get("idempotencyKey").and_then(Value::as_str).unwrap_or("");
    let db = crate::db::D1(context.session);
    let coordinator = crate::application::coordinator(context.env);
    let evidence = crate::application::evidence_fetcher();
    let collector = crate::application::text_fetcher();
    let application = crate::application::Application {
        db: &db,
        ai: &coordinator,
        evidence: &evidence,
        collector: &collector,
        reader: crate::ai::early::Retained(&db),
        now_ms: context.now_ms,
        token: &random_token,
        daily_limit: AI_DAILY_LIMIT,
        source_watch_enabled: crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
        live_markets_enabled: crate::admin::flag(context.env, "LIVE_MARKETS_ENABLED"),
        // An adjudication enters PROPOSED and never finalizes, so no chain gate is consulted.
        registry: None,
    };
    let result = application
        .adjudicate_forecast(Adjudication {
            forecast_id,
            resolution: &resolution,
            adjudicator: &adjudicator,
            artifacts: &artifacts,
            idempotency_key: key,
            expected_revision: Some(revision),
            token: &random_token,
        })
        .await?;
    Ok(api_response(result, 200, false)?)
}

/// `adjudicate_forecast`, replayed against the reference's own recorded state.
#[cfg(test)]
mod adjudication_tests {
    use super::*;
    use crate::golden::{assert_all_cases_known, assert_case, block, entry, load, static_database, Tokens};

    const REPLAYED: [&str; 7] = [
        "adjudicate:ok",
        "adjudicate:retry",
        "adjudicate:idempotency-conflict",
        "adjudicate:missing-artifact",
        "adjudicate:not-independent",
        "adjudicate:stale-revision",
        "adjudicate:backdated",
    ];

    const NOT_REPLAYED: [(&str, &str); 0] = [];

    #[test]
    fn the_vector_has_no_case_this_replay_silently_skips() {
        assert_all_cases_known(&load("adjudication-golden.json"), &REPLAYED, &NOT_REPLAYED);
    }

    /// The prepared decision, parsed from the vector rather than rebuilt.
    struct Prepared {
        resolution: forecast_domain::models::Resolution,
        adjudicator: forecast_domain::models::AIProvenance,
        artifacts: Vec<(String, String, String, String)>,
        forecast_id: String,
        expected_revision: i64,
        key: String,
    }

    fn prepared(case: &Value, key: &str) -> Prepared {
        let decision = &case["decision"];
        Prepared {
            resolution: serde_json::from_value(decision["resolution"].clone()).expect("resolution"),
            adjudicator: serde_json::from_value(decision["adjudicator"].clone()).expect("adjudicator"),
            artifacts: decision["artifacts"]
                .as_array()
                .expect("artifacts")
                .iter()
                .map(|item| {
                    (
                        item["hash"].as_str().unwrap_or("").to_string(),
                        item["kind"].as_str().unwrap_or("").to_string(),
                        item["body"].as_str().unwrap_or("").to_string(),
                        item["mediaType"].as_str().unwrap_or("").to_string(),
                    )
                })
                .collect(),
            forecast_id: decision["forecastId"].as_str().unwrap_or("").to_string(),
            expected_revision: decision["expectedRevision"].as_i64().unwrap_or(0),
            key: key.to_string(),
        }
    }

    fn refusal(error: &RouteError) -> Value {
        match error {
            RouteError::Failed(status, code, message) => {
                json!({"status": status, "code": code, "message": message})
            }
            RouteError::NotFound(code, message) => json!({"status": 404, "code": code, "message": message}),
            other => json!({"code": format!("{other:?}"), "message": ""}),
        }
    }

    #[test]
    fn the_reference_adjudication_is_reproduced_case_for_case() {
        let document = load("adjudication-golden.json");
        for name in REPLAYED {
            let case = entry(&document, name);
            let db = static_database(&case["initial"]);
            // The instant the case *started* at, not the one it ended at: an action may move the
            // clock part-way through, and the two calls either side of it are at two instants.
            let started_ms = case["nowBefore"].as_i64().unwrap_or(0);
            let now_ms = case["now"].as_i64().unwrap_or(0);
            let tokens = Tokens::new(Tokens::recorded(case));
            let call = |at: i64, prepared: &Prepared, artifacts: &[(String, String, String, String)]| {
                block(adjudicate_forecast(
                    db,
                    at,
                    Adjudication {
                        forecast_id: &prepared.forecast_id,
                        resolution: &prepared.resolution,
                        adjudicator: &prepared.adjudicator,
                        artifacts,
                        idempotency_key: &prepared.key,
                        expected_revision: Some(prepared.expected_revision),
                        token: &|| tokens.next(),
                    },
                ))
            };
            let mut prepared = prepared(case, "operator-valid-key");
            let result = match name {
                "adjudicate:ok" => call(started_ms, &prepared, &prepared.artifacts).map(Some),
                "adjudicate:retry" => {
                    // The same command twice, with a scheduler pass between: the second call is a
                    // retry of the first, and the reference's own `events: 1` is what says so.
                    prepared.key = "operator-retry-key".to_string();
                    let first = call(started_ms, &prepared, &prepared.artifacts);
                    if let Err(error) = &first {
                        panic!("{name}: the first adjudication failed: {}", refusal(error));
                    }
                    let first = first.unwrap_or(Value::Null);
                    let evidence: crate::ai::resolution::EvidenceFetcher = Box::new(|_, _| Box::pin(async { Err(()) }));
                    block(crate::scheduler::run_due_jobs(
                        &crate::scheduler::Scheduler {
                            db,
                            coordinator: &crate::golden::refusing_coordinator(),
                            fetch: &evidence,
                            now_ms,
                            clock: &|| now_ms,
                            daily_limit: 100,
                            registry: None,
                            reader: &crate::golden::Refusing,
                        },
                        5,
                        &mut || tokens.next(),
                    ))
                    .expect("a pass");
                    call(now_ms, &prepared, &prepared.artifacts).map(|retry| {
                        Some(json!({
                            "first": first,
                            "retry": retry,
                            "events": block(crate::db::Database::first(db,
                                "SELECT COUNT(*) AS n FROM events WHERE json_extract(event,'$.command_name')='adjudicate_resolution'",
                                &[],
                            ))
                            .ok()
                            .flatten()
                            .and_then(|row| crate::db::int(&row, "n"))
                            .unwrap_or(0),
                        }))
                    })
                }
                "adjudicate:idempotency-conflict" => {
                    prepared.key = "operator-retry-key".to_string();
                    call(started_ms, &prepared, &[]).map(Some)
                }
                "adjudicate:missing-artifact" => {
                    prepared.key = "operator-missing-key".to_string();
                    call(started_ms, &prepared, &[]).map(Some)
                }
                "adjudicate:not-independent" => {
                    prepared.key = "operator-biased-key".to_string();
                    let judge = prepared.resolution.judge.provider.clone();
                    prepared.adjudicator.provider = judge;
                    call(started_ms, &prepared, &prepared.artifacts).map(Some)
                }
                "adjudicate:stale-revision" => {
                    prepared.key = "operator-stale-key".to_string();
                    prepared.expected_revision -= 1;
                    call(started_ms, &prepared, &prepared.artifacts).map(Some)
                }
                "adjudicate:backdated" => {
                    // The clock moved past the decision rather than the record moving back: the
                    // rejection is about how long ago the decision was prepared.
                    prepared.key = "operator-backdated-key".to_string();
                    call(now_ms, &prepared, &prepared.artifacts).map(Some)
                }
                other => panic!("{other} has no replay"),
            };
            let (result, error) = match result {
                Ok(value) => (value, None),
                Err(error) => (None, Some(refusal(&error))),
            };
            assert_case(name, case, &result, &error, db);
            tokens.assert_drained(name, "");
        }
    }
}
