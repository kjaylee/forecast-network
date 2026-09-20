//! Operator-only fresh estimates for exact approved canonical bindings.
//!
//! The HTTP caller authenticates; budget, lease and guarded persistence live here. What makes that
//! split load-bearing is the guard: the estimate is produced by an AI call that is *awaited*, and
//! during it the binding can be revoked, the forecast paused, or a participation hold placed. The
//! guard re-runs the whole "is this binding still current" query inside the batch, so a refresh
//! that lost that race is refused rather than written.

use crate::ai::coordinator::Artifact;
use crate::ai::coordinator::CoordinatorError;
use crate::ai::error::ai_error;
use crate::db::{self, Database, Row};
use crate::scheduler::{retain_rejected, workflow_deadline_passed, workflow_timeout};
use crate::wallets::BoxFuture;
use forecast_domain::lifecycle::Forecast;
use forecast_domain::models::ForecastSpecification;
use forecast_domain::risk_feed::{RiskFeedBinding, RiskFeedBindingV2};
use forecast_domain::{canonical_bytes, content_hash, Record};
use serde_json::{json, Map, Value};

/// `CURRENT`: the binding has to be approved, unrevoked, on an open forecast inside its own
/// validity, with no active hold and no open eligibility decision.
pub const CURRENT: &str = concat!(
    "SELECT f.*,b.binding_json FROM risk_feed_bindings b JOIN forecasts f\n",
    " ON f.id=json_extract(b.binding_json,'$.forecast_id')\n",
    "WHERE b.binding_id=? AND f.specification_hash=json_extract(b.binding_json,'$.specification_hash')\n",
    " AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?\n",
    " AND json_extract(b.binding_json,'$.valid_from_ms')<=?\n",
    " AND json_extract(b.binding_json,'$.valid_until_ms')>?\n",
    " AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations r WHERE r.binding_id=b.binding_id)\n",
    " AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)\n",
    " AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.id\n",
    "  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id))\n",
);

/// `CURRENT_V2`. v2 approvals separate *authorization* — advance forecasting allowed — from
/// operational use, so the currency check reads the authorization window instead.
pub const CURRENT_V2: &str = concat!(
    "SELECT f.*,b.binding_json FROM risk_feed_bindings_v2 b JOIN forecasts f\n",
    " ON f.id=json_extract(b.binding_json,'$.forecast_id')\n",
    "WHERE b.binding_id=? AND f.specification_hash=json_extract(b.binding_json,'$.specification_hash')\n",
    " AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?\n",
    " AND json_extract(b.binding_json,'$.authorization_valid_from_ms')<=?\n",
    " AND json_extract(b.binding_json,'$.authorization_valid_until_ms')>?\n",
    " AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id)\n",
    " AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)\n",
    " AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.id\n",
    "  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id))\n",
);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RefreshError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl RefreshError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

fn not_current() -> RefreshError {
    RefreshError::new(
        409,
        "risk_binding_not_current",
        "The approved risk question is not available for refresh.",
    )
}

fn conflict() -> RefreshError {
    RefreshError::new(
        409,
        "risk_refresh_conflict",
        "The risk question changed while the estimate was being prepared.",
    )
}

fn from_ai(error: &RefreshFailure) -> RefreshError {
    let mapped = ai_error(&error.error, error.source_failure);
    RefreshError::new(mapped.status, mapped.code, mapped.message)
}

fn from_timeout(message: &(u16, &'static str, &'static str)) -> RefreshError {
    RefreshError::new(message.0, message.1, message.2)
}

/// `_clock_statements`: retain every clock role of one refresh.
///
/// The estimate artifact itself is never edited — that is the whole of the v2 clock design, and
/// the retained `ai-forecast` keeps saying when *it* was as-of. The clock records when the source
/// was captured and when the evaluation finished, which are different instants from that.
pub fn clock_statements(
    artifacts: &[Artifact],
    forecast_id: &str,
    specification_hash: &str,
    started_ms: i64,
    completed_ms: i64,
) -> Result<Vec<(String, Vec<Value>)>, RefreshError> {
    let kind = |wanted: &str| artifacts.iter().find(|artifact| artifact.kind == wanted);
    let (Some(estimate), Some(sources)) = (kind("ai-forecast"), kind("risk-prediction-sources")) else {
        return Ok(Vec::new());
    };
    let as_of = serde_json::from_str::<Value>(&estimate.body)
        .ok()
        .and_then(|body| body["as_of_ms"].as_i64())
        .unwrap_or(0);
    let clock = json!({
        "version": "risk-prediction-clock-v1",
        "specification_hash": specification_hash,
        "estimate_artifact_hash": estimate.hash,
        "source_bundle_hash": sources.hash,
        "forecast_as_of_ms": as_of,
        "information_cutoff_ms": as_of,
        "source_capture_started_at_ms": started_ms,
        "source_capture_completed_at_ms": as_of,
        "evaluation_started_at_ms": as_of,
        "evaluation_completed_at_ms": completed_ms,
        "source_watermark_ms": null,
    });
    let digest = content_hash(&clock).map_err(|_| conflict())?;
    Ok(vec![
        (
            "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)".to_string(),
            vec![
                json!(digest),
                json!("risk-prediction-clock"),
                json!(String::from_utf8(canonical_bytes(&clock).unwrap_or_default()).unwrap_or_default()),
                json!("application/json"),
                json!(completed_ms),
            ],
        ),
        (
            concat!(
                "INSERT OR IGNORE INTO risk_prediction_clocks_v2(estimate_artifact_hash,forecast_id,clock_artifact_hash,",
                "recorded_at) VALUES(?,?,?,?)",
            )
            .to_string(),
            vec![json!(estimate.hash), json!(forecast_id), json!(digest), json!(completed_ms)],
        ),
    ])
}

/// What a failed refresh carries: the coordinator's refusal, whether its *cause* was the source
/// rather than the provider, and the artifacts it retained. The reference reads the first and third
/// off the exception and the second off `exc.__cause__`, which only the caller can see.
#[derive(Debug, Clone)]
pub struct RefreshFailure {
    pub error: CoordinatorError,
    pub source_failure: bool,
}

/// `AiCoordinator.refresh_prediction`. Injected because it is the one effect here with a network
/// behind it.
pub type Refresher = dyn Fn(ForecastSpecification, i64) -> BoxFuture<Result<(Value, Vec<Artifact>), RefreshFailure>>;

/// The artifact statements, injected for the same reason `source_watch` injects them: they are the
/// application's, and reaching for them here would make this module untestable against a fixture.
pub type ArtifactSql = dyn Fn(&[Artifact], i64) -> Result<Vec<(String, Vec<Value>)>, RefreshError>;

/// `risk_refresh._refresh`.
#[allow(clippy::too_many_arguments)]
pub async fn refresh(
    db: &dyn Database,
    binding_id: &str,
    current: &str,
    v2: bool,
    refresh_prediction: &Refresher,
    artifact_sql: &ArtifactSql,
    now_ms: &dyn Fn() -> i64,
    lease: &dyn Fn() -> BoxFuture<Result<String, ()>>,
    release: &dyn Fn(String) -> BoxFuture<Result<(), ()>>,
) -> Result<Value, RefreshError> {
    let started = now_ms();
    let row = db
        .first(
            current,
            &[
                json!(binding_id),
                json!(started),
                json!(started),
                json!(started),
                json!(started),
            ],
        )
        .await
        .map_err(|_| conflict())?;
    let Some(row) = row else {
        return Err(not_current());
    };
    let binding_json = db::text(&row, "binding_json").unwrap_or("").to_string();
    let forecast_id = if v2 {
        RiskFeedBindingV2::from_json(&binding_json)
            .map_err(|_| conflict())?
            .forecast_id
    } else {
        RiskFeedBinding::from_json(&binding_json)
            .map_err(|_| conflict())?
            .forecast_id
    };
    let specification_hash = db::text(&row, "specification_hash").unwrap_or("").to_string();
    let forecast = Forecast::from_json(db::text(&row, "snapshot").unwrap_or("")).map_err(|_| conflict())?;
    let specification = forecast.specification.clone();
    let owner = format!("risk-prediction:{forecast_id}");
    let token = lease().await.map_err(|_| conflict())?;
    let outcome = prepare(
        db,
        &row,
        &specification,
        &forecast_id,
        &specification_hash,
        started,
        refresh_prediction,
        artifact_sql,
        now_ms,
        current,
        binding_id,
        &owner,
        &token,
    )
    .await;
    release(token).await.map_err(|_| conflict())?;
    outcome
}

/// The body of `_refresh`, split out so the lease is released on every path.
#[allow(clippy::too_many_arguments)]
async fn prepare(
    db: &dyn Database,
    row: &Row,
    specification: &ForecastSpecification,
    forecast_id: &str,
    specification_hash: &str,
    started: i64,
    refresh_prediction: &Refresher,
    artifact_sql: &ArtifactSql,
    now_ms: &dyn Fn() -> i64,
    current: &str,
    binding_id: &str,
    owner: &str,
    token: &str,
) -> Result<Value, RefreshError> {
    let (estimate, artifacts) = match refresh_prediction(specification.clone(), started).await {
        Ok(value) => value,
        Err(failure) => {
            // The refusal is retained whichever kind it is: those artifacts are what a later
            // reviewer reads to see what the model proposed, and dropping them on the failure path
            // would leave every rejection with only a code.
            retain_rejected(db, failure.error.artifacts())
                .await
                .map_err(|_| conflict())?;
            return Err(from_ai(&failure));
        }
    };
    let now = now_ms();
    if workflow_deadline_passed(started, now) {
        return Err(from_timeout(&workflow_timeout()));
    }
    let previous: Option<Value> = db::text(row, "ai_forecast")
        .filter(|text| !text.is_empty())
        .and_then(|text| serde_json::from_str(text).ok());
    let fresh = estimate["specificationHash"] == json!(specification_hash)
        && estimate["asOf"]
            .as_i64()
            .is_some_and(|as_of| started <= as_of && as_of <= now);
    let advanced = previous
        .as_ref()
        .and_then(|previous| previous["asOf"].as_i64())
        .is_none_or(|before| estimate["asOf"].as_i64().unwrap_or(0) > before);
    if !fresh || !advanced {
        return Err(conflict());
    }
    // The binding is re-checked *after* the await: that is the instant a revocation or a hold can
    // have landed, and the guard below re-runs the same query a third time inside the batch.
    if db
        .first(
            current,
            &[json!(binding_id), json!(now), json!(now), json!(now), json!(now)],
        )
        .await
        .map_err(|_| conflict())?
        .is_none()
    {
        return Err(conflict());
    }
    // `compact`, not `canonical_bytes`: the estimate is a stored summary, and `canonical_bytes`
    // enforces the *commitment* rule of integers only — a probability of `1.8` would serialize to
    // nothing at all. The reference's `json.dumps(..., allow_nan=False)` allows every finite float.
    let serialized = crate::source_watch::compact(&estimate);
    let guard = format!("risk-refresh:{token}");
    let condition =
        format!("SELECT 1 FROM ({current}) f WHERE f.revision=? AND f.ai_forecast IS ? AND f.binding_json=?");
    let mut statements: Vec<(String, Vec<Value>)> = vec![(
        format!(
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS({condition}) \
             AND EXISTS(SELECT 1 FROM ai_leases WHERE owner=? AND token=? AND expires_at>?) THEN 1 ELSE 0 END"
        ),
        vec![
            json!(guard),
            json!(binding_id),
            json!(now),
            json!(now),
            json!(now),
            json!(now),
            db::get(row, "revision").clone(),
            db::get(row, "ai_forecast").clone(),
            json!(binding_json_of(row)),
            json!(owner),
            json!(token),
            json!(now),
        ],
    )];
    statements.extend(artifact_sql(&artifacts, now)?);
    statements.extend(clock_statements(
        &artifacts,
        forecast_id,
        specification_hash,
        started,
        now,
    )?);
    statements.push((
        "UPDATE forecasts SET ai_forecast=? WHERE id=?".to_string(),
        vec![json!(serialized), json!(forecast_id)],
    ));
    statements.push((
        "DELETE FROM mutation_guards WHERE token=?".to_string(),
        vec![json!(guard)],
    ));
    db.batch(&statements).await.map_err(|_| conflict())?;
    Ok(json!({
        "status": "refreshed",
        "bindingId": binding_id,
        "forecastId": forecast_id,
        "aiForecast": estimate,
    }))
}

fn binding_json_of(row: &Row) -> Value {
    db::get(row, "binding_json").clone()
}

/// A `Map` from a JSON object, for callers that build one.
pub fn object(value: Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/risk-refresh-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("risk refresh golden")).expect("json")
    }

    /// The clock statements, compared as SQL *and* as parameters: a port that retained the right
    /// roles under the wrong statement would record a clock nothing reads.
    #[test]
    fn the_reference_clock_roles_are_reproduced() {
        let document = golden();
        // The inputs come out of the vector, hashes and all: a clock is a record of four hashes and
        // five instants, and a port reproducing the statements but not the values would be
        // reproducing the shape rather than the clock.
        let input = &document["calls"][0]["input"];
        let field = |name: &str| input[name].as_str().unwrap_or("").to_string();
        let instant = |name: &str| input[name].as_i64().unwrap_or(0);
        let estimate = Artifact {
            hash: field("estimateHash"),
            kind: "ai-forecast",
            body: document["clockArtifacts"]["estimate"]
                .as_str()
                .unwrap_or("")
                .to_string(),
        };
        let sources = Artifact {
            hash: field("sourceHash"),
            kind: "risk-prediction-sources",
            body: document["clockArtifacts"]["sources"].as_str().unwrap_or("").to_string(),
        };
        let produced = clock_statements(
            &[estimate.clone(), sources],
            &field("forecastId"),
            &field("specificationHash"),
            instant("startedMs"),
            instant("completedMs"),
        )
        .expect("clock statements");
        let expected = document["calls"][0]["result"]
            .as_array()
            .expect("the vector's statements");
        assert_eq!(produced.len(), expected.len());
        for (index, statement) in produced.iter().enumerate() {
            assert_eq!(
                statement.0,
                expected[index][0].as_str().unwrap(),
                "statement {index}: SQL"
            );
            // The parameters are compared as a sequence, because the clock is a *record*: the same
            // values in a different order are a different row.
            assert_eq!(
                json!(statement.1),
                json!(expected[index][1]),
                "statement {index}: parameters"
            );
        }
        // An estimate with no source bundle retains no clock at all, rather than half of one.
        assert!(
            clock_statements(&[estimate], "f", "a", 0, 0)
                .expect("clock statements")
                .is_empty(),
            "a clock needs both the estimate and the source bundle"
        );
    }

    /// The successful refresh, replayed end to end: the currency query, the await, the re-check,
    /// the guard batch, and the clock artifact it retains.
    ///
    /// The estimate comes out of the vector rather than from a live model, because a refresh is
    /// about what happens *around* the AI call — and the vector's estimate is the one the reference
    /// actually wrote, `artifactHash` and all.
    #[test]
    fn the_reference_refresh_is_reproduced() {
        let document = golden();
        let db = Sqlite::from_migrations();
        restore(&db, &document);
        let binding_id = document["calls"][3]["input"]["bindingId"].as_str().unwrap().to_string();
        let expected = &document["calls"][3]["result"];
        // Owned, so the seam that returns it is `'static` like the future it produces.
        let answer = std::rc::Rc::new(expected["aiForecast"].clone());
        let retained = std::rc::Rc::new(
            document["rows"]["artifacts"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .filter(|row| matches!(row["kind"].as_str(), Some("ai-forecast" | "risk-prediction-sources")))
                .map(|row| Artifact {
                    hash: row["hash"].as_str().unwrap_or("").to_string(),
                    // The two kinds are the vector's own literals, so the `'static` the artifact
                    // type asks for is satisfiable without leaking.
                    kind: match row["kind"].as_str() {
                        Some("ai-forecast") => "ai-forecast",
                        _ => "risk-prediction-sources",
                    },
                    body: row["body"].as_str().unwrap_or("").to_string(),
                })
                .collect::<Vec<Artifact>>(),
        );
        let now = answer["asOf"].as_i64().unwrap();
        let clock = move || now;
        // The guard checks the lease *inside the batch*, so the seam has to take one the way the
        // reference's `_ai_lease` does — a token with no row behind it refuses every refresh, which
        // is the guard doing its job.
        let lease = || -> BoxFuture<Result<String, ()>> {
            let token = "lease-token".to_string();
            db.run(
                "INSERT INTO ai_leases(owner,token,expires_at) VALUES(?,?,?)",
                &[
                    json!("risk-prediction:f_19581e27de7ced00ff1ce50b"),
                    json!(token),
                    json!(now + 300_000),
                ],
            )
            .expect("the lease");
            Box::pin(async move { Ok(token) })
        };
        let release = |_token: String| -> BoxFuture<Result<(), ()>> { Box::pin(async { Ok(()) }) };
        // The artifacts the reference retained alongside this estimate are read back from the store
        // it wrote them to, so the clock the port derives is the clock that was recorded.
        let refresh_prediction = move |_specification: ForecastSpecification,
                                       _started: i64|
              -> BoxFuture<Result<(Value, Vec<Artifact>), RefreshFailure>> {
            let answer = std::rc::Rc::clone(&answer);
            let retained = std::rc::Rc::clone(&retained);
            Box::pin(async move { Ok(((*answer).clone(), (*retained).clone())) })
        };
        // The reference's `_artifact_sql` is the application's, so the test supplies *its* output
        // rather than a stub of it: the rows the refresh wrote, which are the difference between
        // the state it started from and the state it left. A stub that inserted only the two
        // artifacts the seam was handed would compare a different store for no reason.
        let before: Vec<String> = document["initial"]["artifacts"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(|row| row["hash"].as_str().map(str::to_string))
            .collect();
        let written: Vec<Value> = document["rows"]["artifacts"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|row| {
                row["hash"]
                    .as_str()
                    .is_some_and(|hash| !before.iter().any(|seen| seen == hash))
                    && row["kind"] != json!("risk-prediction-clock")
            })
            .collect();
        let artifact_sql =
            move |_artifacts: &[Artifact], _at: i64| -> Result<Vec<(String, Vec<Value>)>, RefreshError> {
                Ok(written
                    .iter()
                    .map(|row| {
                        (
                            "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)"
                                .to_string(),
                            vec![
                                row["hash"].clone(),
                                row["kind"].clone(),
                                row["body"].clone(),
                                row["media_type"].clone(),
                                row["created_at"].clone(),
                            ],
                        )
                    })
                    .collect())
            };
        let produced = block(refresh(
            &db,
            &binding_id,
            CURRENT_V2,
            true,
            &refresh_prediction,
            &artifact_sql,
            &clock,
            &lease,
            &release,
        ));
        assert!(
            produced.is_ok(),
            "the reference's own refresh was refused: {produced:?}"
        );
        assert_eq!(produced.unwrap()["aiForecast"], expected["aiForecast"]);
        // And the store it wrote: the artifacts, the forecast's summary, and the clock. Only the
        // tables *this* call writes are compared — the vector goes on to revoke the binding, and a
        // replay of one call cannot be held to the state a later one left.
        for table in ["artifacts", "forecasts", "risk_prediction_clocks_v2"] {
            let (rows, _) = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .expect("rows");
            assert_eq!(json!(rows), document["rows"][table], "{table}: different rows");
        }
        // The clock is the point of the v2 design: the estimate artifact is never edited, and the
        // clock records when the source was captured against when the evaluation finished.
        let (clocks, _) = db.run("SELECT * FROM risk_prediction_clocks_v2", &[]).expect("clocks");
        assert_eq!(clocks.len(), 1, "one refresh retains exactly one clock");
        // The rows the reference wrote through its own artifact writer are the ones the refresh
        // reported, so a port that wrote a different set is caught above rather than here.
    }

    /// The tables the currency query reads, in dependency order: `forecasts.creator_id` references
    /// `users`, so restoring the query's tables alone fails a foreign key.
    ///
    /// The rows come from `initial`, not `rows`: the latter is the state *after* the sequence, and
    /// restoring it would replay a refresh against an estimate the refresh itself wrote.
    fn restore(db: &Sqlite, document: &Value) {
        for table in [
            "users",
            "artifacts",
            "forecasts",
            "risk_feed_definitions_v2",
            "risk_feed_profiles_v2",
            "risk_feed_bindings_v2",
        ] {
            for row in document["initial"][table].as_array().cloned().unwrap_or_default() {
                let fields = row.as_object().expect("a fixture row");
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
    }

    /// The currency query, for the reasons that are reachable without the AI seam: an unknown
    /// binding, a paused forecast, and a revoked binding.
    #[test]
    fn a_refresh_is_refused_when_the_binding_is_not_current() {
        let document = golden();
        let db = Sqlite::from_migrations();
        for table in [
            "users",
            "artifacts",
            "forecasts",
            "risk_feed_bindings_v2",
            "risk_feed_definitions_v2",
            "risk_feed_profiles_v2",
        ] {
            for row in document["rows"][table].as_array().cloned().unwrap_or_default() {
                let fields = row.as_object().expect("a fixture row");
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
        let binding = document["rows"]["risk_feed_bindings_v2"][0]["binding_json"]
            .as_str()
            .unwrap()
            .to_string();
        let binding_id = db::text(
            &document["rows"]["risk_feed_bindings_v2"][0]
                .as_object()
                .cloned()
                .expect("a binding row"),
            "binding_id",
        )
        .unwrap()
        .to_string();
        let now = document["rows"]["forecasts"][0]["open_at"].as_i64().unwrap() + 1000;
        let probe = |db: &Sqlite, at: i64| -> bool {
            block(db.first(
                CURRENT_V2,
                &[json!(binding_id), json!(at), json!(at), json!(at), json!(at)],
            ))
            .expect("the currency query")
            .is_some()
        };
        assert!(probe(&db, now), "the fixture's binding is current to begin with");
        let _ = binding;
        // Paused, and then revoked: both stop being current, and the same query says so.
        db.run("UPDATE forecasts SET state='PAUSED' WHERE id=(SELECT json_extract(binding_json,'$.forecast_id') FROM risk_feed_bindings_v2)", &[])
            .expect("pause");
        assert!(!probe(&db, now), "a paused forecast is not current");
        db.run("UPDATE forecasts SET state='OPEN'", &[]).expect("resume");
        db.run(
            "INSERT INTO risk_feed_binding_revocations_v2(binding_id,revoked_by,revoked_at,reason) VALUES(?,?,?,?)",
            &[
                json!(binding_id),
                json!("admin"),
                json!(now),
                json!("operator withdrew it"),
            ],
        )
        .expect("revoke");
        assert!(!probe(&db, now), "a revoked binding is not current");
    }
}
