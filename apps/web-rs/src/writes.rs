//! User write routes: compile and publish, comments, shares, follows, activity, profile, disputes,
//! evidence reports and forecast submissions (`Application` write paths). The adjudication, the
//! sign-in surface and the operator's writes are `adjudication`, `auth_routes` and
//! `operator_routes`, which share this module's helpers.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use worker::*;

use forecast_domain::lifecycle::{Payload, Snapshot};
use forecast_domain::models::UserForecast;

use crate::api_response;
use crate::db::{all, batch, first, get, int, text, Row};
use crate::mutate::{
    canonical_text, hash_of, load_snapshot, mutate, random_token, record_artifact, Mutation, Statement,
};
use crate::projections::{card, quality_card_sql};
use crate::reads::public_user;
use crate::routes::{Context, RouteError};

pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 86_400_000;
pub const MAX_STAKE: i64 = 1000;
pub const POINTS_POLICY_VERSION: &str = "participation-points-v1";
/// `Application`'s daily AI budget. One call's worth of work per question, bounded per day.
pub const AI_DAILY_LIMIT: i64 = 200;

pub(crate) type Handler = std::result::Result<Response, RouteError>;

pub fn invalid() -> RouteError {
    RouteError::Input
}

pub fn invalid_message(message: &'static str) -> RouteError {
    RouteError::Failed(400, "invalid_input", message)
}

/// `text(value, limit)`: trimmed, bounded, no control characters.
pub fn checked_text(value: &Value, limit: usize) -> std::result::Result<String, RouteError> {
    let text = value.as_str().ok_or_else(invalid)?.trim().to_string();
    let length = text.chars().count();
    if !(1..=limit).contains(&length) || text.chars().any(|c| (c as u32) < 32) {
        return Err(invalid());
    }
    Ok(text)
}

/// `_key`: `[A-Za-z0-9_.:-]{8,120}`.
pub fn idempotency_key(value: &Value) -> std::result::Result<String, RouteError> {
    let key = value.as_str().unwrap_or("");
    let valid = (8..=120).contains(&key.len())
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'));
    if valid {
        Ok(key.to_string())
    } else {
        Err(invalid_message(
            "A request identifier is required. Refresh the page and try again.",
        ))
    }
}

/// Atomic fixed-window counter; the Worker also calls this for IP limits.
pub async fn rate_limit(
    session: &D1DatabaseSession,
    now_ms: i64,
    scope: &str,
    limit: i64,
    window_ms: i64,
) -> std::result::Result<(), RouteError> {
    let bucket = now_ms.div_euclid(window_ms);
    let rows = all(
        session,
        "INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES(?,?,1,?) ON CONFLICT(scope,bucket) DO UPDATE SET count=count+1 RETURNING count",
        &[json!(scope), json!(bucket), json!((bucket + 1) * window_ms)],
    )
    .await?;
    if rows
        .first()
        .and_then(|r| int(r, "count"))
        .is_some_and(|count| count > limit)
    {
        return Err(RouteError::Failed(
            429,
            "rate_limited",
            "Too many requests. Please try again later.",
        ));
    }
    Ok(())
}

/// `Application._card`: the quality card for one forecast.
pub(crate) async fn card_row(
    db: &dyn crate::db::Database,
    forecast_id: &str,
    now_ms: i64,
) -> std::result::Result<Value, RouteError> {
    let sql = format!(
        "{} WHERE f.id=?",
        quality_card_sql(now_ms, &[forecast_id.to_string()]).ok_or(RouteError::Input)?
    );
    let row = db
        .first(&sql, &[json!(forecast_id)])
        .await?
        .ok_or(RouteError::NotFound("forecast_not_found", "Forecast not found."))?;
    Ok(card(&row))
}

/// `POST /api/forecasts/compile`: turn a question into a reviewable draft.
///
/// The order is the reference's and every step is load-bearing. The question is bounded *before*
/// the lease, so a malformed one costs nothing; the lease comes before the model, so one account
/// cannot have two compilations in flight; the duplicate candidates are read after the model has
/// seen them but before anything is written; and the draft is written in one batch with the
/// specification and assessment it names, so a draft cannot exist whose own record disagrees with
/// it. A closing time in the past is refused here rather than stored, because a draft that cannot
/// be published is not a draft.
pub async fn compile_forecast(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    user_row(context.session, user_id).await?;
    let question = crate::auth::checked_text(body.get("question"), 1000, 10)
        .map_err(|_| RouteError::Failed(400, "invalid_input", "Please check your input."))?;
    if question.chars().count() > 1000 {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "The original question must contain at most 1,000 characters.",
        ));
    }
    let db = crate::db::D1(context.session);
    let owner = format!("user:{user_id}");
    let lease = random_token();
    crate::scheduler::ai_lease(&db, &owner, &lease, context.now_ms, AI_DAILY_LIMIT)
        .await
        .map_err(|code| RouteError::Failed(429, "ai_unavailable", Box::leak(code.into_boxed_str())))?;
    let outcome = compile(context.env, &db, user_id, &question, context.now_ms, false).await;
    crate::scheduler::release_ai(&db, &owner, &lease).await;
    let result = outcome?;
    Ok(api_response(result, 200, false)?)
}

pub(crate) async fn compile(
    env: &Env,
    db: &crate::db::D1<'_>,
    user_id: &str,
    question: &str,
    now_ms: i64,
    canonical_series: bool,
) -> std::result::Result<Value, RouteError> {
    let candidates = crate::ai::compiler_wire::candidate_forecasts(db, question)
        .await
        .map_err(RouteError::Worker)?;
    let coordinator = crate::application::coordinator(env);
    let evidence = crate::application::evidence_fetcher();
    let compiled = crate::ai::compile::compile_question(
        &coordinator,
        &evidence,
        question,
        &candidates,
        now_ms,
        // Only an operator-declared canonical series may treat a shifted explicit measurement
        // interval as a distinct contract; a question a person wrote never does, which is why this
        // is a parameter with one call site that sets it.
        canonical_series,
    )
    .await
    .map_err(|error| {
        let mapped = crate::ai::error::ai_error(&error, false);
        RouteError::Failed(mapped.status, mapped.code, mapped.message)
    })?;
    // The freshness gate runs before the draft exists: a question whose event a retained article
    // already establishes is refused, not stored and refused later.
    let reader = crate::ai::early::Retained(db);
    let collector = crate::application::text_fetcher();
    let automation = crate::automation::Automation::new(
        db,
        Some(&collector),
        Some(&coordinator),
        &reader,
        now_ms,
        &random_token,
        crate::admin::flag(env, "SOURCE_WATCH_ENABLED"),
    );
    automation
        .check_creation(&compiled.specification)
        .await
        .map_err(|error| {
            RouteError::Failed(
                400,
                "source_temporarily_unavailable",
                Box::leak(error.message().into_boxed_str()),
            )
        })?;
    if compiled.specification.close_at_ms <= now_ms {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "The closing time must be in the future.",
        ));
    }
    let draft_id = format!("d_{}", &random_token()[..24]);
    let expiry = now_ms + HOUR_MS;
    let specification = crate::mutate::canonical_text(&compiled.specification)?;
    let assessment = crate::mutate::canonical_text(&compiled.assessment)?;
    let mut statements = crate::source_watch::artifact_sql(&retained_from(&compiled.artifacts), now_ms)
        .map_err(|refusal| RouteError::Failed(refusal.status(), refusal.code(), refusal.message()))?;
    statements.push(record_artifact(&compiled.specification, "specification", None, now_ms)?);
    statements.push(record_artifact(&compiled.assessment, "validation", None, now_ms)?);
    statements.push((
        "INSERT INTO drafts(id,user_id,specification,assessment,ai_forecast,created_at,expires_at) VALUES(?,?,?,?,?,?,?)"
            .to_string(),
        vec![
            json!(draft_id),
            json!(user_id),
            json!(specification),
            json!(assessment),
            match &compiled.ai_forecast {
                Some(forecast) => json!(crate::source_watch::compact(forecast)),
                None => Value::Null,
            },
            json!(now_ms),
            json!(expiry),
        ],
    ));
    crate::db::Database::batch(db, &statements)
        .await
        .map_err(RouteError::Worker)?;
    Ok(json!({
        "draftId": draft_id,
        "specification": crate::projections::specification(&serde_json::to_value(&compiled.specification).unwrap_or(Value::Null)),
        "assessment": {
            "publishable": true,
            "explanation": compiled.assessment.explanation,
            "provider": compiled.assessment.compiler.provider,
            "model": compiled.assessment.compiler.model,
        },
        "duplicateCandidates": compiled.specification.duplicate_candidates.iter().map(|item| json!({
            "id": item.forecast_id,
            "similarity": item.similarity_bp as f64 / 10000.0,
            "explanation": item.explanation,
        })).collect::<Vec<_>>(),
        "aiForecast": compiled.ai_forecast,
        "expiresAt": expiry,
    }))
}

/// The retained bytes a compiled artifact names, in the shape `artifact_sql` takes.
pub(crate) fn retained_from(artifacts: &[crate::ai::coordinator::Artifact]) -> Vec<crate::source_watch::Retained> {
    artifacts
        .iter()
        .map(|artifact| {
            (
                artifact.hash.clone(),
                artifact.kind.to_string(),
                artifact.body.clone(),
                "application/json".to_string(),
            )
        })
        .collect()
}

/// `POST /api/forecasts`: publish a draft.
///
/// The forecast is created and carried through *two* domain commands in memory before anything is
/// written — `BeginValidation` then `Publish` — because the record that is stored has to be the one
/// the domain would have produced, and both transitions are what make it that. The draft is marked
/// published in the same batch, under a `published_id IS NULL` guard, which is what makes two
/// simultaneous publishes of one draft produce one forecast rather than two.
///
/// A failure is read back before it is reported: a lost response and a lost race look the same from
/// here, so the receipt is consulted first, then the duplicate criteria, and only then is the
/// caller told the request conflicted.
pub async fn publish_forecast(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    user_row(context.session, user_id).await?;
    let draft_id = crate::auth::checked_text(body.get("draftId"), 128, 1)
        .map_err(|_| RouteError::Failed(400, "invalid_input", "Please check your input."))?;
    let key = body.get("idempotencyKey").and_then(Value::as_str).unwrap_or("");
    let db = crate::db::D1(context.session);
    let result = publish(context, &db, user_id, &draft_id, key).await?;
    Ok(api_response(result, 201, false)?)
}

/// The publication itself, without the route's body parsing: an operator seed publishes through the
/// same path a person does, and it must not go round it.
pub(crate) async fn publish(
    context: &Context<'_>,
    db: &crate::db::D1<'_>,
    user_id: &str,
    draft_id: &str,
    key: &str,
) -> std::result::Result<Value, RouteError> {
    let request = json!({"kind": "publish", "draftId": draft_id});
    if let Some(prior) = prior_row(db, user_id, key, &request).await? {
        let forecast_id = text(&prior, "forecast_id").unwrap_or("").to_string();
        return Ok(json!({"forecast": card_row(db, &forecast_id, context.now_ms).await?}));
    }
    let draft = crate::db::Database::first(
        db,
        "SELECT * FROM drafts WHERE id=? AND user_id=?",
        &[json!(draft_id), json!(user_id)],
    )
    .await?
    .ok_or(RouteError::NotFound("draft_not_found", "Draft not found."))?;
    if let Some(published) = text(&draft, "published_id") {
        return Ok(json!({"forecast": card_row(db, published, context.now_ms).await?}));
    }
    let now = context.now_ms;
    if int(&draft, "expires_at").unwrap_or(0) <= now {
        return Err(RouteError::Failed(
            410,
            "draft_expired",
            "This draft has expired. Please submit the question for review again.",
        ));
    }
    rate_limit(context.session, now, &format!("publish:{user_id}"), 10, DAY_MS).await?;
    let specification: forecast_domain::models::ForecastSpecification =
        serde_json::from_str(text(&draft, "specification").unwrap_or("")).map_err(|_| invalid())?;
    let assessment: forecast_domain::models::ValidationAssessment =
        serde_json::from_str(text(&draft, "assessment").unwrap_or("")).map_err(|_| invalid())?;
    // The freshness gate again, at the moment of publication: a draft made an hour ago may name an
    // event a retained article has since established.
    let coordinator = crate::application::coordinator(context.env);
    let collector = crate::application::text_fetcher();
    let reader = crate::ai::early::Retained(db);
    crate::automation::Automation::new(
        db,
        Some(&collector),
        Some(&coordinator),
        &reader,
        now,
        &random_token,
        crate::admin::flag(context.env, "SOURCE_WATCH_ENABLED"),
    )
    .check_creation(&specification)
    .await
    .map_err(|error| {
        RouteError::Failed(
            400,
            "source_temporarily_unavailable",
            Box::leak(error.message().into_boxed_str()),
        )
    })?;
    let forecast_id = format!("f_{}", &random_token()[..24]);
    let created = forecast_domain::lifecycle::create_forecast(
        &forecast_id,
        user_id,
        specification.clone(),
        int(&draft, "created_at").unwrap_or(now),
    )
    .map_err(|_| invalid())?;
    let snapshot = forecast_domain::lifecycle::Snapshot::V1(created);
    let validated = forecast_domain::lifecycle::apply_command(
        &snapshot,
        &forecast_domain::lifecycle::Command {
            schema_version: 1,
            idempotency_key: "begin-validation".to_string(),
            expected_revision: 0,
            payload: Payload::BeginValidation { schema_version: 1 },
        },
        now,
        None,
    )
    .map_err(|_| invalid())?;
    let published = forecast_domain::lifecycle::apply_command(
        &validated.forecast,
        &forecast_domain::lifecycle::Command {
            schema_version: 1,
            idempotency_key: "publish".to_string(),
            expected_revision: 1,
            payload: Payload::Publish {
                schema_version: 1,
                assessment: assessment.clone(),
            },
        },
        now,
        None,
    )
    .map_err(|_| invalid())?;
    let forecast = published.forecast.base().clone();
    let mut statements: Vec<Statement> = vec![
        (
            "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,             normalized_question,specification_hash,open_at,close_at,created_at,updated_at,ai_forecast,mutation_key)              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                .to_string(),
            vec![
                json!(forecast_id),
                json!(user_id),
                json!(draft_id),
                json!(crate::mutate::canonical_text(&published.forecast)?),
                json!(forecast.revision),
                json!(forecast.state),
                json!(specification.category),
                json!(specification.share_title),
                json!(specification.canonical_question),
                json!(crate::automation::casefold(&specification.canonical_question)
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")),
                json!(specification.specification_hash().map_err(|_| invalid())?),
                json!(specification.open_at_ms),
                json!(specification.close_at_ms),
                json!(now),
                json!(now),
                draft.get("ai_forecast").cloned().unwrap_or(Value::Null),
                json!(key),
            ],
        ),
        (
            "UPDATE drafts SET published_id=? WHERE id=? AND user_id=? AND published_id IS NULL".to_string(),
            vec![json!(forecast_id), json!(draft_id), json!(user_id)],
        ),
    ];
    statements.extend(crate::mutate::event_statements(&validated)?);
    statements.extend(crate::mutate::event_statements(&published)?);
    if crate::admin::flag(context.env, "SOLANA_REGISTRY_ENABLED") {
        statements.push(crate::registry_chain::registry_enable_sql(&forecast_id));
    }
    statements.push(operation(user_id, key, &request, &forecast_id, &json!({}), now)?);
    // The title is the fixed phrase and the *body* is the share title — read the SELECT against the
    // column list, because the two are in the opposite order to what the names suggest.
    statements.push((
        "INSERT OR IGNORE INTO activity(id,user_id,forecast_id,kind,title,body,created_at) \
         SELECT ?||':'||follower_id,follower_id,?,'creator_published',?,?,? FROM follows WHERE creator_id=?"
            .to_string(),
        vec![
            json!(format!("published:{forecast_id}")),
            json!(forecast_id),
            json!("New forecast from a creator you follow"),
            json!(specification.share_title),
            json!(now),
            json!(user_id),
        ],
    ));
    if crate::db::Database::batch(db, &statements).await.is_err() {
        if let Some(prior) = prior_row(db, user_id, key, &request).await? {
            let forecast_id = text(&prior, "forecast_id").unwrap_or("").to_string();
            return Ok(json!({"forecast": card_row(db, &forecast_id, context.now_ms).await?}));
        }
        let duplicate = crate::db::Database::first(
            db,
            "SELECT id FROM forecasts WHERE specification_hash=? OR (normalized_question=? AND close_at=?)",
            &[
                json!(specification.specification_hash().map_err(|_| invalid())?),
                json!(crate::automation::casefold(&specification.canonical_question)
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")),
                json!(specification.close_at_ms),
            ],
        )
        .await?;
        if duplicate.is_some() {
            return Err(RouteError::Failed(
                409,
                "duplicate_forecast",
                "A forecast with the same resolution criteria already exists. Please join the existing forecast.",
            ));
        }
        return Err(crate::mutate::conflict());
    }
    Ok(json!({"forecast": card_row(db, &forecast_id, context.now_ms).await?}))
}

/// `require_expected_user`: the displayed account is a precondition, not a decoration.
///
/// A profile can change in another tab, so an action that moves points or wallet settings carries
/// the account the caller believed they were acting as. A missing precondition is the caller's to
/// fix; a mismatched one is a different account entirely, and those are two different answers.
pub(crate) fn require_expected_user(body: &Map<String, Value>, user_id: &str) -> std::result::Result<(), RouteError> {
    let Some(expected) = body.get("expectedUserId").and_then(Value::as_str) else {
        return Err(RouteError::Failed(
            400,
            "account_precondition_required",
            "Reload your profile before changing points or wallet settings.",
        ));
    };
    if expected != user_id {
        return Err(RouteError::Failed(
            409,
            "account_changed",
            "Your signed-in account changed. Reload your profile before continuing.",
        ));
    }
    Ok(())
}

/// `POST /api/me/share-card`: publish the profile card, so nothing is public until it is shared.
pub async fn share_card(context: &Context<'_>, user_id: &str, body: &Map<String, Value>) -> Handler {
    // The body's key set is checked exactly: a publication that accepted anything else would be a
    // way to hand this route fields it does not read.
    let exact = body.len() == 1 && body.contains_key("expectedUserId");
    if !exact || !body["expectedUserId"].is_string() {
        return Err(RouteError::Failed(
            400,
            "invalid_input",
            "Profile publication requires the displayed account precondition.",
        ));
    }
    if body["expectedUserId"].as_str() != Some(user_id) {
        return Err(RouteError::Failed(
            409,
            "profile_owner_changed",
            "Your signed-in profile changed. Reload your profile before sharing.",
        ));
    }
    rate_limit(
        context.session,
        context.now_ms,
        &format!("profile-card:{user_id}"),
        10,
        HOUR_MS,
    )
    .await?;
    let db = crate::db::D1(context.session);
    let result = crate::profile_cards::create(&db, user_id, context.now_ms)
        .await
        .map_err(|error| RouteError::Failed(error.status, error.code, error.message))?;
    Ok(api_response(result, 201, false)?)
}

/// `POST /api/forecasts/{id}/disputes`: a participant challenges the proposed resolution.
///
/// The evidence is *collected here*, under the application's own AI lease, and only then is the
/// dispute built: a dispute whose evidence could not be retained is not a dispute anybody can
/// review, and recording one would put an unreviewable claim in the record. The two failures are
/// told apart because the adjudication shows a different thing for each.
pub async fn submit_dispute(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    user_row(context.session, user_id).await?;
    let invalid = || RouteError::Failed(400, "invalid_input", "Please check your input.");
    let claim = crate::auth::checked_text(body.get("claim"), 1000, 1).map_err(|_| invalid())?;
    let explanation = crate::auth::checked_text(body.get("explanation"), 3000, 1).map_err(|_| invalid())?;
    let evidence_url = crate::auth::checked_text(body.get("evidenceUrl"), 2000, 1).map_err(|_| invalid())?;
    let rule_clause_id = crate::auth::checked_text(body.get("ruleClauseId"), 128, 1).map_err(|_| invalid())?;
    let revision = match body.get("revision").and_then(Value::as_i64) {
        Some(revision) if revision >= 0 => revision,
        _ => return Err(crate::writes::invalid()),
    };
    let key = body.get("idempotencyKey").and_then(Value::as_str).unwrap_or("");
    let request = json!({
        "kind": "dispute", "forecastId": forecast_id, "claim": claim,
        "evidenceUrl": evidence_url, "ruleClauseId": rule_clause_id,
        "explanation": explanation, "revision": revision,
    });
    let db = crate::db::D1(context.session);
    if let Some(prior) = prior_row(&db, user_id, key, &request).await? {
        let mut receipt = prior_result(&prior)?;
        receipt["forecast"] = card_row(&db, forecast_id, context.now_ms).await?;
        return Ok(api_response(receipt, 200, false)?);
    }
    let snapshot = load_snapshot(&db, forecast_id).await?;
    let forecast = snapshot.base().clone();
    if forecast.revision != revision {
        return Err(crate::mutate::conflict());
    }
    // A dispute is a challenge, so it may only be filed inside the challenge window — and the
    // window is the *record's* own, not a fresh one.
    if !matches!(forecast.state.as_str(), "CHALLENGE" | "DISPUTED")
        || forecast.challenge_until_ms.is_none_or(|until| until <= context.now_ms)
    {
        return Err(RouteError::Failed(
            409,
            "challenge_closed",
            "The challenge window is closed.",
        ));
    }
    if !forecast.specification.clause_ids().contains(&rule_clause_id.as_str()) {
        return Err(invalid());
    }
    rate_limit(
        context.session,
        context.now_ms,
        &format!("dispute:{user_id}"),
        10,
        DAY_MS,
    )
    .await?;
    let owner = format!("user:{user_id}");
    let lease = random_token();
    crate::scheduler::ai_lease(&db, &owner, &lease, context.now_ms, AI_DAILY_LIMIT)
        .await
        .map_err(|code| RouteError::Failed(503, "ai_unavailable", Box::leak(code.into_boxed_str())))?;
    // The collector owns the transport: `DisputeCollector`'s future is `'static`, so the closure
    // cannot borrow one that lives in this frame.
    let fetch = std::rc::Rc::new(crate::application::text_fetcher());
    let collector: Box<crate::ai::dispute::DisputeCollector> = Box::new(
        move |specification: &forecast_domain::models::ForecastSpecification, url: String, now_ms: i64| {
            let fetch = fetch.clone();
            let specification = specification.clone();
            Box::pin(async move {
                crate::sources::collect_dispute(|target| fetch(target, Vec::new()), &specification, &url, now_ms).await
            })
        },
    );
    let started = context.now_ms;
    let outcome = crate::ai::dispute::collect_dispute_evidence(
        &collector,
        &forecast.specification,
        &evidence_url,
        context.now_ms,
    )
    .await;
    crate::scheduler::release_ai(&db, &owner, &lease).await;
    let (evidence, artifact) = outcome.map_err(|error| {
        let mapped = crate::ai::error::ai_error(
            &error,
            matches!(error, crate::ai::coordinator::CoordinatorError::Unavailable { .. }),
        );
        RouteError::Failed(mapped.status, mapped.code, mapped.message)
    })?;
    let _ = started;
    let now = context.now_ms;
    let Some(resolution) = forecast.resolution.as_ref() else {
        return Err(crate::mutate::conflict());
    };
    let dispute = forecast_domain::models::Dispute {
        schema_version: 1,
        dispute_id: format!("d_{}", &random_token()[..24]),
        disputant_id: user_id.to_string(),
        forecast_id: forecast_id.to_string(),
        specification_hash: forecast.specification_hash.clone(),
        resolution_hash: resolution.resolution_hash().map_err(|_| invalid())?,
        claim,
        evidence: vec![evidence],
        rule_clause_id,
        explanation,
        submitted_at_ms: now,
    };
    let response = json!({"dispute": {"id": dispute.dispute_id, "claim": dispute.claim,
                                      "submittedAt": now, "hash": dispute.dispute_hash().map_err(|_| invalid())?}});
    let retained = vec![(
        artifact.hash.clone(),
        artifact.kind.to_string(),
        artifact.body.clone(),
        "application/json".to_string(),
    )];
    let mut extra = crate::source_watch::artifact_sql(&retained, now)
        .map_err(|refusal| RouteError::Failed(refusal.status(), refusal.code(), refusal.message()))?;
    extra.push(record_artifact(&dispute, "dispute", None, now)?);
    let operation_key = format!(
        "user:{}",
        forecast_domain::content_hash(&json!({"user": user_id, "key": key}))
            .map_err(|error| RouteError::Worker(error.to_string().into()))?
    );
    extra.push(operation(user_id, key, &request, forecast_id, &response, now)?);
    mutate(
        &db,
        Mutation {
            snapshot: &snapshot,
            payload: Payload::SubmitDispute {
                schema_version: 1,
                dispute,
            },
            key: operation_key,
            now_ms: now,
            extra,
            job_token: None,
            timing_artifacts: Vec::new(),
        },
        now,
        &random_token,
        None,
    )
    .await?;
    let mut result = response;
    result["forecast"] = card_row(&db, forecast_id, context.now_ms).await?;
    Ok(api_response(result, 200, false)?)
}

/// `POST /api/forecasts/{id}/evidence`: a forecaster reports an official announcement.
///
/// The application is assembled here rather than held on the route context, because every transport
/// it needs is created from bindings the context does not carry — and because a request that does
/// not use them should not build them.
pub async fn report_evidence(context: &Context<'_>, user_id: &str, forecast_id: &str, url: &Value) -> Handler {
    let Some(url) = url.as_str() else {
        return Err(RouteError::Failed(400, "invalid_input", "Please check your input."));
    };
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
        // A report never commits anything to a chain, so no adapter is consulted on this path.
        registry: None,
    };
    let report = application.report_evidence(user_id, forecast_id, url).await?;
    Ok(api_response(report, 200, false)?)
}

/// `report_evidence`'s refusals, as the route layer reports them.
///
/// The codes are the reference's own and a *closed* set — every one of them is raised in that
/// function and nowhere else — so each is mapped to a static rather than carried through as text.
/// A route error wants `&'static str`, and the alternative to naming them here is naming them
/// nowhere.
impl From<crate::source_watch::WatchError> for RouteError {
    fn from(error: crate::source_watch::WatchError) -> Self {
        match error {
            // The refusal's own status and text are the reference's and are repeated below rather
            // than carried: a route error wants statics, and these are a closed set that belongs to
            // one function. They are repeated *exactly*, which is what makes the repetition safe to
            // check by reading.
            crate::source_watch::WatchError::Refused { code, .. } => match code.as_str() {
                "authentication_required" => {
                    RouteError::Unauthorized("authentication_required", "Please sign in to continue.")
                }
                "evidence_report_closed" => RouteError::Failed(
                    409,
                    "evidence_report_closed",
                    "This forecast is no longer accepting evidence reports.",
                ),
                "evidence_report_url" => RouteError::Failed(
                    400,
                    "evidence_report_url",
                    "Report a public https page on one of this question's official sources.",
                ),
                "evidence_report_source" => RouteError::Failed(
                    400,
                    "evidence_report_source",
                    "Only the question's published official sources can be reported.",
                ),
                "rate_limited" => RouteError::Failed(429, "rate_limited", "Too many requests. Please try again later."),
                _ => RouteError::Failed(400, "invalid_input", "Please check your input."),
            },
            // A watcher that cannot fetch is not a bad request; it is an outage of a dependency.
            other => RouteError::Worker(worker::Error::from(other.message())),
        }
    }
}

pub async fn user_row(session: &D1DatabaseSession, user_id: &str) -> std::result::Result<Row, RouteError> {
    first(session, "SELECT * FROM users WHERE id=?", &[json!(user_id)])
        .await?
        .ok_or(RouteError::Unauthorized(
            "authentication_required",
            "Please sign in to continue.",
        ))
}

pub(crate) async fn prior(
    session: &D1DatabaseSession,
    user_id: &str,
    key: &str,
    request: &Value,
) -> std::result::Result<Option<Row>, RouteError> {
    let row = first(
        session,
        "SELECT * FROM operations WHERE user_id=? AND operation_key=?",
        &[json!(user_id), json!(key)],
    )
    .await?;
    if let Some(row) = &row {
        if text(row, "request_hash") != Some(hash_of(request)?.as_str()) {
            return Err(RouteError::Failed(
                409,
                "idempotency_conflict",
                "This request identifier has already been used for different content.",
            ));
        }
    }
    Ok(row)
}

/// `_prior`, over the `Database` trait rather than a session: the adjudication path is reachable
/// from tests, and a helper that demanded a D1 session would make the path unreachable there.
pub(crate) async fn prior_row(
    db: &dyn crate::db::Database,
    user_id: &str,
    key: &str,
    request: &Value,
) -> std::result::Result<Option<Row>, RouteError> {
    let row = db
        .first(
            "SELECT * FROM operations WHERE user_id=? AND operation_key=?",
            &[json!(user_id), json!(key)],
        )
        .await?;
    if let Some(row) = &row {
        if text(row, "request_hash") != Some(hash_of(request)?.as_str()) {
            return Err(RouteError::Failed(
                409,
                "idempotency_conflict",
                "This request identifier has already been used for different content.",
            ));
        }
    }
    Ok(row)
}

pub(crate) fn operation(
    user_id: &str,
    key: &str,
    request: &Value,
    forecast_id: &str,
    result: &Value,
    now_ms: i64,
) -> Result<Statement> {
    Ok((
        "INSERT INTO operations(user_id,operation_key,request_hash,forecast_id,result,created_at) VALUES(?,?,?,?,?,?)"
            .to_string(),
        vec![
            json!(user_id),
            json!(key),
            json!(hash_of(request)?),
            json!(forecast_id),
            json!(canonical_text(result)?),
            json!(now_ms),
        ],
    ))
}

pub(crate) fn prior_result(row: &Row) -> std::result::Result<Value, RouteError> {
    serde_json::from_str(text(row, "result").unwrap_or("{}")).map_err(|e| RouteError::Worker(e.into()))
}

pub(crate) async fn card_of(context: &Context<'_>, forecast_id: &str) -> std::result::Result<Value, RouteError> {
    let sql = format!(
        "{} WHERE f.id=?",
        quality_card_sql(context.now_ms, &[forecast_id.to_string()]).ok_or_else(invalid)?
    );
    let row = first(context.session, &sql, &[json!(forecast_id)])
        .await?
        .ok_or(RouteError::NotFound("forecast_not_found", "Forecast not found."))?;
    Ok(card(&row))
}

// ---------------------------------------------------------------- comments, shares, follows, activity, profile

pub async fn add_comment(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    let session = context.session;
    let user = user_row(session, user_id).await?;
    let text_value = checked_text(body.get("text").unwrap_or(&Value::Null), 2000)?;
    let request = json!({"kind": "comment", "forecastId": forecast_id, "text": text_value});
    let key = idempotency_key(body.get("idempotencyKey").unwrap_or(&json!("")))?;
    if let Some(row) = prior(session, user_id, &key, &request).await? {
        return Ok(api_response(prior_result(&row)?, 200, false)?);
    }
    load_snapshot(&crate::db::D1(session), forecast_id).await?;
    rate_limit(session, context.now_ms, &format!("comment:{user_id}"), 30, HOUR_MS).await?;
    let now = context.now_ms;
    let cid = format!("c_{}", &random_token()[..24]);
    let result = json!({"comment": {"id": cid, "text": text_value, "createdAt": now, "user": public_user(&user)}});
    let statements = vec![
        (
            "INSERT INTO comments(id,forecast_id,user_id,body,created_at) VALUES(?,?,?,?,?)".to_string(),
            vec![
                json!(cid),
                json!(forecast_id),
                json!(user_id),
                json!(text_value),
                json!(now),
            ],
        ),
        operation(user_id, &key, &request, forecast_id, &result, now)?,
    ];
    if batch(session, statements).await.is_err() {
        if let Some(row) = prior(session, user_id, &key, &request).await? {
            return Ok(api_response(prior_result(&row)?, 200, false)?);
        }
        return Err(RouteError::Worker("comment_storage".into()));
    }
    Ok(api_response(result, 200, false)?)
}

pub async fn record_share(context: &Context<'_>, forecast_id: &str, user_id: Option<&str>) -> Handler {
    let session = context.session;
    load_snapshot(&crate::db::D1(session), forecast_id).await?;
    if let Some(user_id) = user_id {
        user_row(session, user_id).await?;
        let bucket = context.now_ms.div_euclid(DAY_MS);
        batch(
            session,
            vec![
                (
                    "UPDATE forecasts SET share_count=share_count+1 WHERE id=? AND NOT EXISTS (SELECT 1 FROM share_receipts WHERE forecast_id=? AND actor=? AND bucket=?)".to_string(),
                    vec![json!(forecast_id), json!(forecast_id), json!(user_id), json!(bucket)],
                ),
                ("INSERT OR IGNORE INTO share_receipts(forecast_id,actor,bucket) VALUES(?,?,?)".to_string(), vec![json!(forecast_id), json!(user_id), json!(bucket)]),
            ],
        )
        .await?;
    }
    Ok(api_response(json!({"ok": true}), 200, false)?)
}

pub async fn follow(context: &Context<'_>, user_id: &str, creator_id: &str, following: &Value) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    let Some(following) = following.as_bool() else {
        return Err(invalid());
    };
    if user_id == creator_id {
        return Err(invalid());
    }
    user_row(session, creator_id).await?;
    rate_limit(session, context.now_ms, &format!("follow:{user_id}"), 100, HOUR_MS).await?;
    let statement = if following {
        (
            "INSERT OR IGNORE INTO follows(follower_id,creator_id,created_at) VALUES(?,?,?)".to_string(),
            vec![json!(user_id), json!(creator_id), json!(context.now_ms)],
        )
    } else {
        (
            "DELETE FROM follows WHERE follower_id=? AND creator_id=?".to_string(),
            vec![json!(user_id), json!(creator_id)],
        )
    };
    batch(session, vec![statement]).await?;
    Ok(api_response(json!({"following": following}), 200, false)?)
}

pub async fn read_activity(context: &Context<'_>, user_id: &str) -> Handler {
    user_row(context.session, user_id).await?;
    batch(
        context.session,
        vec![(
            "UPDATE activity SET read_at=? WHERE user_id=? AND read_at IS NULL".to_string(),
            vec![json!(context.now_ms), json!(user_id)],
        )],
    )
    .await?;
    Ok(api_response(json!({"ok": true}), 200, false)?)
}

pub async fn update_profile(context: &Context<'_>, user_id: &str, display_name: &Value) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    rate_limit(session, context.now_ms, &format!("profile:{user_id}"), 20, HOUR_MS).await?;
    let name = checked_text(display_name, 40)?;
    batch(
        session,
        vec![(
            "UPDATE users SET display_name=? WHERE id=?".to_string(),
            vec![json!(name), json!(user_id)],
        )],
    )
    .await?;
    let user = user_row(session, user_id).await?;
    let points = crate::points::summary(&crate::db::D1(session), user_id).await?;
    Ok(api_response(
        json!({"user": public_user(&user), "points": points}),
        200,
        false,
    )?)
}

// ---------------------------------------------------------------- forecast submissions

pub(crate) fn submission_view(choice: &UserForecast, revision: i64) -> Value {
    let yes = choice.outcome == "YES";
    json!({"outcome": choice.outcome, "confidence": choice.confidence, "probability": if yes { choice.confidence } else { 100 - choice.confidence },
           "submittedAt": choice.submitted_at_ms, "revision": revision})
}

pub(crate) fn sha_json(values: &Value) -> String {
    hex::encode(Sha256::digest(
        serde_json::to_string(values).unwrap_or_default().as_bytes(),
    ))
}

/// `points.reservation_sql`: reserve/release an explicit total stake inside the accepted CAS.
pub fn reservation_sql(
    user_id: &str,
    forecast_id: &str,
    amount: i64,
    outcome: &str,
    forecast_revision: i64,
    operation_id: &str,
    now: i64,
) -> std::result::Result<Vec<Statement>, RouteError> {
    if !(0..=MAX_STAKE).contains(&amount) {
        return Err(RouteError::Failed(
            400,
            "invalid_stake",
            "Use zero practice points or a whole-number stake from 1 to 1,000.",
        ));
    }
    if outcome != "YES" && outcome != "NO" {
        return Err(RouteError::Failed(
            400,
            "invalid_stake",
            "A points stake must have a YES or NO forecast choice.",
        ));
    }
    if forecast_revision < 1 || now < 0 {
        return Err(RouteError::Failed(
            400,
            "invalid_points_request",
            "Invalid participation points revision or timestamp.",
        ));
    }
    let identity = sha_json(&json!([user_id, operation_id]));
    let ledger_id = format!("reservation:{identity}");
    let request_hash = sha_json(&json!([user_id, forecast_id, amount, outcome, forecast_revision]));
    let guards: Vec<String> = ["operation", "position", "balance"]
        .iter()
        .map(|k| format!("{ledger_id}:{k}"))
        .collect();
    Ok(vec![
        (
            "INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'operation',CASE WHEN NOT EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM point_ledger WHERE id=? AND user_id=? AND kind='reservation' AND request_hash=?) THEN 1 ELSE 0 END".to_string(),
            vec![json!(guards[0]), json!(ledger_id), json!(ledger_id), json!(user_id), json!(request_hash)],
        ),
        (
            "INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'position',CASE WHEN EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM forecasts f LEFT JOIN point_positions p ON p.user_id=? AND p.forecast_id=f.id WHERE f.id=? AND f.state='OPEN' AND f.open_at<=? AND ?<f.close_at AND f.revision=? AND (p.user_id IS NULL OR (p.status IN ('practice','committed') AND p.forecast_revision<?))) THEN 1 ELSE 0 END".to_string(),
            vec![json!(guards[1]), json!(ledger_id), json!(user_id), json!(forecast_id), json!(now), json!(now), json!(forecast_revision), json!(forecast_revision)],
        ),
        (
            "INSERT INTO point_write_guards(id,kind,passed) SELECT ?,'balance',CASE WHEN EXISTS(SELECT 1 FROM point_ledger WHERE id=?) OR EXISTS(SELECT 1 FROM point_accounts a LEFT JOIN point_positions p ON p.user_id=a.user_id AND p.forecast_id=? WHERE a.user_id=? AND a.available+COALESCE(p.amount,0)-?>=0 AND a.committed+?-COALESCE(p.amount,0)>=0) THEN 1 ELSE 0 END".to_string(),
            vec![json!(guards[2]), json!(ledger_id), json!(forecast_id), json!(user_id), json!(amount), json!(amount)],
        ),
        (
            "INSERT INTO point_ledger(id,user_id,kind,forecast_id,operation_id,request_hash,available_delta,committed_delta,available_after,committed_after,stake,outcome,forecast_revision,policy_version,created_at) SELECT ?,a.user_id,'reservation',?,?,?,COALESCE(p.amount,0)-?,?-COALESCE(p.amount,0),a.available+COALESCE(p.amount,0)-?,a.committed+?-COALESCE(p.amount,0),?,?,?,COALESCE(p.policy_version,?),? FROM point_accounts a LEFT JOIN point_positions p ON p.user_id=a.user_id AND p.forecast_id=? WHERE a.user_id=? AND NOT EXISTS(SELECT 1 FROM point_ledger WHERE id=?)".to_string(),
            vec![
                json!(ledger_id), json!(forecast_id), json!(operation_id), json!(request_hash), json!(amount), json!(amount), json!(amount), json!(amount),
                json!(amount), json!(outcome), json!(forecast_revision), json!(POINTS_POLICY_VERSION), json!(now), json!(forecast_id), json!(user_id), json!(ledger_id),
            ],
        ),
        ("DELETE FROM point_write_guards WHERE id IN (?,?,?)".to_string(), vec![json!(guards[0]), json!(guards[1]), json!(guards[2])]),
    ])
}

pub(crate) async fn submission_response(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    mut receipt: Value,
) -> Handler {
    let session = context.session;
    let eligibility = crate::eligibility::combined(&crate::db::D1(session), forecast_id, Some(user_id)).await?;
    if eligibility["status"] != "none" {
        let effective = first(
            session,
            "SELECT body,revision FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?",
            &[json!(forecast_id), json!(user_id)],
        )
        .await?;
        let original = receipt.get("myForecast").cloned().unwrap_or(Value::Null);
        receipt["originalReceipt"] = original;
        receipt["myForecast"] = match effective {
            Some(row) => {
                let choice: UserForecast =
                    serde_json::from_str(text(&row, "body").unwrap_or("")).map_err(|e| RouteError::Worker(e.into()))?;
                submission_view(&choice, int(&row, "revision").unwrap_or(0))
            }
            None => Value::Null,
        };
    }
    let mut data = json!({"forecast": card_of(context, forecast_id).await?});
    if let (Value::Object(target), Value::Object(fields)) = (&mut data, receipt) {
        for (key, value) in fields {
            target.insert(key, value);
        }
    }
    data["points"] = crate::points::summary(&crate::db::D1(session), user_id).await?;
    data["eligibility"] = eligibility;
    data["stake"] = crate::points::position_for(&crate::db::D1(session), user_id, forecast_id).await?;
    Ok(api_response(data, 200, false)?)
}

pub async fn submit_forecast(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Handler {
    let session = context.session;
    user_row(session, user_id).await?;
    let outcome = body
        .get("outcome")
        .and_then(Value::as_str)
        .filter(|o| matches!(*o, "YES" | "NO"))
        .ok_or_else(invalid)?
        .to_string();
    let confidence = body
        .get("confidence")
        .and_then(crate::discovery::integer)
        .filter(|c| (0..=100).contains(c))
        .ok_or_else(invalid)?;
    let revision = body
        .get("revision")
        .and_then(crate::discovery::integer)
        .filter(|r| *r >= 0)
        .ok_or_else(invalid)?;
    let stake_points = match body.get("stakePoints") {
        None | Some(Value::Null) => None,
        Some(value) => Some(
            crate::discovery::integer(value)
                .filter(|s| (0..=1000).contains(s))
                .ok_or(RouteError::Failed(
                    400,
                    "invalid_stake",
                    "Choose practice with 0 points or a stake from 1 to 1,000 points.",
                ))?,
        ),
    };
    let mut request = json!({"kind": "forecast", "forecastId": forecast_id, "outcome": outcome, "confidence": confidence, "revision": revision});
    if let Some(stake) = stake_points {
        request["stakePoints"] = json!(stake);
    }
    let key = idempotency_key(body.get("idempotencyKey").unwrap_or(&json!("")))?;
    if let Some(row) = prior(session, user_id, &key, &request).await? {
        return submission_response(context, user_id, forecast_id, prior_result(&row)?).await;
    }
    if first(
        session,
        "SELECT body FROM active_participation_holds WHERE forecast_id=?",
        &[json!(forecast_id)],
    )
    .await?
    .is_some()
    {
        return Err(RouteError::Failed(
            409,
            "participation_on_hold",
            "Participation is on hold while newly available evidence is reviewed.",
        ));
    }
    let snapshot = load_snapshot(&crate::db::D1(session), forecast_id).await?;
    let forecast = snapshot.base();
    if forecast.revision != revision {
        return Err(crate::mutate::conflict());
    }
    if stake_points.is_none() {
        let position = crate::points::position_for(&crate::db::D1(session), user_id, forecast_id).await?;
        if position["status"] == "committed" && position["amount"].as_i64().unwrap_or(0) > 0 {
            return Err(RouteError::Failed(
                409,
                "stake_required",
                "This forecast already has a stake. Refresh and explicitly confirm the stake amount.",
            ));
        }
    }
    let amount = stake_points.unwrap_or(0);
    rate_limit(session, context.now_ms, &format!("forecast:{user_id}"), 100, HOUR_MS).await?;
    let now = context.now_ms;
    let choice = UserForecast {
        schema_version: 1,
        forecaster_id: user_id.to_string(),
        forecast_id: forecast_id.to_string(),
        specification_hash: forecast.specification_hash.clone(),
        outcome: outcome.clone(),
        confidence,
        submitted_at_ms: now,
    };
    let probability = if outcome == "YES" { confidence } else { 100 - confidence };
    let response = json!({"myForecast": submission_view(&choice, revision + 1)});
    let choice_json = canonical_text(&choice)?;
    let mut extra: Vec<Statement> = vec![
        record_artifact(&choice, "user_forecast", None, now)?,
        (
            "INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,revision,body) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(forecast_id,user_id) DO UPDATE SET outcome=excluded.outcome,confidence=excluded.confidence,yes_probability=excluded.yes_probability,submitted_at=excluded.submitted_at,revision=excluded.revision,body=excluded.body".to_string(),
            vec![json!(forecast_id), json!(user_id), json!(outcome), json!(confidence), json!(probability), json!(now), json!(revision + 1), json!(choice_json)],
        ),
        (
            "INSERT INTO forecast_history(forecast_id,revision,user_id,body,crowd_probability,participant_count,created_at) SELECT ?,?,?,?,AVG(yes_probability),COUNT(*),? FROM user_forecasts WHERE forecast_id=?".to_string(),
            vec![json!(forecast_id), json!(revision + 1), json!(user_id), json!(choice_json), json!(now), json!(forecast_id)],
        ),
    ];
    let operation_id = hash_of(&json!({"user": user_id, "key": key}))?;
    extra.extend(reservation_sql(
        user_id,
        forecast_id,
        amount,
        &outcome,
        revision + 1,
        &operation_id,
        now,
    )?);
    extra.push(operation(user_id, &key, &request, forecast_id, &response, now)?);
    let mutation = Mutation {
        snapshot: &snapshot,
        payload: Payload::SubmitForecast {
            schema_version: 1,
            user_forecast: choice,
        },
        key: format!("user:{operation_id}"),
        now_ms: now,
        extra,
        job_token: None,
        // The submission's own evidence is fetched by the resolver, not carried here.
        timing_artifacts: Vec::new(),
    };
    // A submission is not a finalize, so there is no chain gate to consult.
    match mutate(&crate::db::D1(session), mutation, now, &random_token, None).await {
        Ok(_) => submission_response(context, user_id, forecast_id, response).await,
        Err(error) => {
            // A transport error can arrive after the D1 batch committed; the durable receipt wins.
            let prior_row = match prior(session, user_id, &key, &request).await {
                Ok(row) => row,
                Err(RouteError::Failed(..))
                | Err(RouteError::Input)
                | Err(RouteError::NotFound(..))
                | Err(RouteError::Unauthorized(..)) => return Err(error),
                Err(_) => {
                    return Err(RouteError::Failed(
                        503,
                        "forecast_storage_unavailable",
                        "The forecast could not be confirmed. Retry with the same request identifier.",
                    ))
                }
            };
            if let Some(row) = prior_row {
                return submission_response(context, user_id, forecast_id, prior_result(&row)?).await;
            }
            if !matches!(error, RouteError::Worker(_)) {
                return Err(error);
            }
            // A missing account is a refusal now, and this path only wants the summary if there is
            // one: the refusal is what the *route* reports, and swallowing it here is the same
            // `None` the read model used to return.
            let points = crate::points::summary(&crate::db::D1(session), user_id).await.ok();
            let position = crate::points::position_for(&crate::db::D1(session), user_id, forecast_id)
                .await
                .ok();
            let (Some(points), Some(position)) = (points, position) else {
                return Err(RouteError::Failed(
                    503,
                    "forecast_storage_unavailable",
                    "The forecast could not be saved. Please try again later.",
                ));
            };
            let hold = if position["status"] == "committed" {
                position["amount"].as_i64().unwrap_or(0)
            } else {
                0
            };
            if amount > points["available"].as_i64().unwrap_or(0) + hold {
                return Err(RouteError::Failed(
                    409,
                    "insufficient_points",
                    "You do not have enough available points for this stake.",
                ));
            }
            Err(RouteError::Failed(
                503,
                "forecast_storage_unavailable",
                "The forecast could not be saved. Please try again later.",
            ))
        }
    }
}

pub(crate) fn _snapshot_kind(snapshot: &Snapshot) -> &'static str {
    match snapshot {
        Snapshot::V1(_) => "v1",
        Snapshot::V2(_) => "v2",
    }
}

pub(crate) fn _row_get(row: &Row) -> &Value {
    get(row, "id")
}
