//! The risk v2 administrative routes: the operator's surface over the signed feed.
//!
//! Three rules shape every handler here.
//!
//! Each checks its body's key set **exactly**. That is not pedantry: these calls admit a definition,
//! approve a binding or publish a signed envelope, and a body that carried an extra field is one the
//! reference would have refused. Accepting it would mean this Worker and the Python Worker disagreed
//! about what an admission is.
//!
//! The whole surface is behind the operator credential, which the entry checks before this module
//! runs. The `actor` recorded is a fixed name rather than a user id, because an operator is not a
//! user of this application and nothing below the entry may decide differently.
//!
//! And a **domain refusal is a 503 here**, not a 400. The reference's `require(...)` raises a
//! `ValidationError`, which is a `ValueError` and *not* an `AppError` — so it falls through the
//! entry's `except Exception` and becomes `service_unavailable`. That is arguably a wart, and it is
//! the behaviour: a port that returned 400 would be answering a question the reference did not.
//! The one failure that keeps its own code is the missing relayer, which the entry raises itself.
//!
//! What is verified here, and what is not. The services underneath are replayed against the
//! reference's own output by the risk v2 and refresh vectors, so a handler is a key check, a decode
//! and a call. Those three are covered by this module's own tests; the *routes* are not, because a
//! handler needs an `Env` and a `D1DatabaseSession` and no natively-run test can build either. The
//! route table and `scripts/sql_parity.py` are what hold the dispatch to the reference.
//!
//! Two things in this module are derived rather than called, and both were wrong in a draft: the
//! feed's key id is the hash of the relayer's *key* rather than of the address that spells it, and
//! the relayer is optional so that a refresh or a tick still runs without one — the reference only
//! requires a signer where it signs, and it records a per-feed `publishFailure` rather than failing
//! the tick.

use serde_json::{json, Value};
use worker::*;

use crate::api_response;
use crate::application::DEVNET_GENESIS;
use crate::risk_feed_series::SeedCallback;
use crate::risk_feed_v2::FeedFault;
use crate::risk_feed_v2::{
    admit_definition, admit_profile, approve_binding_v2, configure_operation, operate_feeds_v2, operations_health,
    publish_feed_v2, revoke_binding_v2, training_export, FeedError, PublishCallback, RefreshCallback, Signer,
};
use crate::risk_refresh::{ArtifactSql, LeaseRelease, LeaseSource, RefreshFailure, Refresher};
use crate::routes::{Context, RouteError};
use forecast_domain::risk_feed::SignedRiskFeedV2;

/// `actor = "authenticated-operator"`.
pub const ACTOR: &str = "authenticated-operator";

/// The genesis the feed is pinned to: the same one the chain transport pins. A feed signed against
/// a different chain is one nobody can verify.
pub const FEED_GENESIS: &str = DEVNET_GENESIS;

/// A feed failure, as the reference's entry answers one.
///
/// Two shapes, because the reference has two. A domain `require` raises a `ValidationError`, which
/// is *not* an `AppError`, so it falls through the entry's `except Exception` and becomes
/// `service_unavailable`. A missing relayer is raised by the entry itself as an `AppError` with its
/// own code, and it keeps it.
pub fn feed_error(error: FeedError) -> RouteError {
    match error.fault {
        FeedFault::Signer => RouteError::Failed(503, "risk_signer_unavailable", "Risk feed signing is not configured."),
        FeedFault::Refused | FeedFault::Storage => {
            RouteError::Failed(503, "service_unavailable", "Please try again shortly.")
        }
    }
}

/// `training_export`'s default page: the reference's own, so an operator reading a truncated
/// export sees the same number of rows here as there.
const TRAINING_LIMIT: i64 = 500;

/// The exact key set a route accepts.
///
/// `set(body) != {...}` in the reference, and the *set* is the check: a body with a right key
/// missing and a wrong one added has the same length and is still refused.
fn exact(body: &serde_json::Map<String, Value>, keys: &[&str]) -> std::result::Result<(), RouteError> {
    let present: Vec<&str> = body.keys().map(String::as_str).collect();
    if present.len() != keys.len() || !keys.iter().all(|key| present.contains(key)) {
        return Err(feed_error(FeedError::refused("invalid operator body")));
    }
    Ok(())
}

/// The relayer's signature, as the feed publisher takes it, with the identity it signs as.
struct Relayer<'a, 'b> {
    inner: &'a crate::solana_rpc::Signer<'b>,
    identity: &'a Identity,
}

impl Signer for Relayer<'_, '_> {
    fn sign<'a>(&'a self, message: Vec<u8>) -> crate::risk_feed_v2::BorrowedFuture<'a, Result<Vec<u8>, ()>> {
        Box::pin(async move { (self.inner)(message).await.map(|signature| signature.to_vec()) })
    }
}

/// `publish_risk_v2`: sign and store one feed envelope.
///
/// The relayer is optional here rather than required at construction, because the reference checks
/// for it *inside* the publish: a tick over an unconfigured relayer records a `publishFailure` per
/// feed rather than failing, and a route that could not even build its publisher would answer
/// differently from the reference it is standing in for.
struct FeedPublisher<'a, 'b> {
    session: &'a D1DatabaseSession,
    relayer: Option<Relayer<'a, 'b>>,
    now_ms: i64,
}

impl PublishCallback for FeedPublisher<'_, '_> {
    fn publish<'a>(
        &'a self,
        feed_id: &'a str,
        weight_set_hash: &'a str,
        weight_set_version: &'a str,
        calibration_cohort_id: &'a str,
    ) -> crate::risk_feed_v2::BorrowedFuture<'a, Result<SignedRiskFeedV2, FeedError>> {
        Box::pin(async move {
            let Some(relayer) = self.relayer.as_ref() else {
                return Err(FeedError::signer());
            };
            publish_feed_v2(
                &crate::db::D1(self.session),
                feed_id,
                FEED_GENESIS,
                &relayer.identity.key_id,
                &relayer.identity.public_key_hex,
                relayer,
                self.now_ms,
                weight_set_hash,
                weight_set_version,
                calibration_cohort_id,
            )
            .await
            .inspect_err(|error| {
                // D1 reports constraint and statement failures by class and column, never row data.
                // The event is logged because a refused publish is otherwise indistinguishable from
                // a broken statement, and this route's failures have been mis-attributed before.
                console_log!(
                    "{}",
                    json!({"event": "risk_v2_publish_failed", "feedId": feed_id, "detail": error.message})
                );
            })
        })
    }
}

/// The AI's own prediction refresh.
struct Prediction<'a> {
    coordinator: &'a crate::ai::coordinator::Coordinator,
    fetch: &'a crate::ai::resolution::EvidenceFetcher,
    now_ms: i64,
}

impl Refresher for Prediction<'_> {
    fn refresh<'a>(
        &'a self,
        specification: forecast_domain::models::ForecastSpecification,
        started_ms: i64,
    ) -> crate::risk_refresh::BorrowedFuture<'a, Result<(Value, Vec<crate::ai::coordinator::Artifact>), RefreshFailure>>
    {
        Box::pin(async move {
            crate::ai::compile::refresh_prediction(self.coordinator, self.fetch, &specification, started_ms, &|| {
                self.now_ms
            })
            .await
            .map(|result| (result.ai_forecast, result.artifacts))
            .map_err(|error| RefreshFailure {
                error,
                // Whether the *source* failed is the caller's knowledge: this layer awaited the
                // model, and the transport's own artifacts are what say otherwise.
                source_failure: false,
            })
        })
    }
}

/// `_ai_lease`, over the owner the refresh derived. The owner is passed in rather than reconstructed
/// here: a lease taken under a name of this module's own choosing would be one nobody checked.
struct ProductionLease<'a> {
    session: &'a D1DatabaseSession,
    now_ms: i64,
    daily_limit: i64,
}

impl LeaseSource for ProductionLease<'_> {
    fn lease<'a>(&'a self, owner: &'a str) -> crate::risk_refresh::BorrowedFuture<'a, Result<String, ()>> {
        Box::pin(async move {
            let token = crate::mutate::random_token();
            crate::scheduler::ai_lease(
                &crate::db::D1(self.session),
                owner,
                &token,
                self.now_ms,
                self.daily_limit,
            )
            .await
            .map_err(|_| ())?;
            Ok(token)
        })
    }
}

/// `_release_ai`, which the refresh calls on every path.
struct ProductionRelease<'a> {
    session: &'a D1DatabaseSession,
}

impl LeaseRelease for ProductionRelease<'_> {
    fn release<'a>(&'a self, owner: &'a str, token: String) -> crate::risk_refresh::BorrowedFuture<'a, Result<(), ()>> {
        Box::pin(async move {
            crate::scheduler::release_ai(&crate::db::D1(self.session), owner, &token).await;
            Ok(())
        })
    }
}

/// `refresh_bound_prediction_v2`: bring one binding's forecast up to date.
struct BindingRefresh<'a> {
    session: &'a D1DatabaseSession,
    coordinator: &'a crate::ai::coordinator::Coordinator,
    fetch: &'a crate::ai::resolution::EvidenceFetcher,
    artifact_sql: &'a ArtifactSql,
    now_ms: i64,
    daily_limit: i64,
}

impl RefreshCallback for BindingRefresh<'_> {
    fn refresh<'a>(
        &'a self,
        binding_id: String,
    ) -> crate::risk_feed_v2::BorrowedFuture<'a, Result<Value, crate::risk_refresh::RefreshError>> {
        Box::pin(async move {
            crate::risk_refresh::refresh(
                &crate::db::D1(self.session),
                &binding_id,
                crate::risk_refresh::CURRENT_V2,
                true,
                &Prediction {
                    coordinator: self.coordinator,
                    fetch: self.fetch,
                    now_ms: self.now_ms,
                },
                self.artifact_sql,
                &|| self.now_ms,
                &ProductionLease {
                    session: self.session,
                    now_ms: self.now_ms,
                    daily_limit: self.daily_limit,
                },
                &ProductionRelease { session: self.session },
            )
            .await
        })
    }
}

/// The operator's seed, as the tick takes it.
struct Editorial<'a> {
    context: &'a Context<'a>,
}

impl SeedCallback for Editorial<'_> {
    fn seed<'a>(
        &'a self,
        question: String,
    ) -> crate::risk_feed_series::BorrowedFuture<'a, Result<Value, crate::risk_feed_series::AttemptError>> {
        Box::pin(async move {
            // A canonical risk question is a declared series, so the compiler may treat a shifted
            // explicit measurement interval as a distinct contract. The uncertainty band is *not*
            // applied: these questions are chosen to be open by construction, and the band exists
            // for editorial questions a person wrote.
            crate::writes::seed(self.context, &question, "Forecast Editorial", None, true)
                .await
                .map_err(|error| {
                    // The error travels with the reference's own type and message, because that is
                    // what its series log holds — a port that named its own would log a failure
                    // nobody can act on.
                    crate::risk_feed_series::AttemptError {
                        code: None,
                        kind: "AppError".to_string(),
                        message: format!("{error:?}"),
                    }
                })
        })
    }
}

// ---------------------------------------------------------------------------------------------
// The chain, and the seams over it.
// ---------------------------------------------------------------------------------------------
/// The relayer's identity: the bytes it signs as, and the key id the feed names it by.
///
/// One struct rather than two fields, because the key id is the hash of *those* bytes and a chain
/// that could pair one identity's name with another's key would be signing under a name nobody
/// verified.
struct Identity {
    public_key_hex: String,
    key_id: String,
}

impl Identity {
    /// `publish_risk_v2`'s key id: `"forecast-relayer-" + sha256(public_key).hexdigest()[:16]`.
    fn of(public_key: &[u8]) -> Self {
        use sha2::{Digest, Sha256};
        let digest = hex::encode(Sha256::digest(public_key));
        Self {
            public_key_hex: hex::encode(public_key),
            key_id: format!("forecast-relayer-{}", &digest[..16]),
        }
    }
}

/// The operator's chain adapter, over the session the request was served with.
///
/// Every seam below holds the *session* rather than a database value: a `D1` borrows a session that
/// lives in the request frame, and a struct that owned one would outlive it. Constructing `D1` at
/// each use is what keeps the seams `'a`-bounded rather than `'static`.
///
/// The signer is optional, and that is the reference's shape rather than a convenience: an operator
/// with no relayer can still refresh a binding — the refresh never signs — and a tick with no
/// relayer records a per-feed `publishFailure` instead of failing the whole tick. Only `publisher`
/// requires it, because only a publication does.
struct Chain<'a> {
    pub session: &'a D1DatabaseSession,
    signer: Option<Box<crate::solana_rpc::Signer<'a>>>,
    identity: Option<Identity>,
    pub now_ms: i64,
    pub daily_limit: i64,
}

impl<'a> Chain<'a> {
    /// `app.registry`'s signer and `app.relayer_public_key()`, as the publish path takes them.
    fn from(context: &'a Context<'_>) -> Self {
        let public_key = crate::application::relayer_public_key(context.env);
        Self {
            session: context.session,
            signer: crate::application::relayer_signer(context.env),
            identity: public_key.as_deref().map(Identity::of),
            now_ms: context.now_ms,
            daily_limit: crate::writes::AI_DAILY_LIMIT,
        }
    }

    /// A publisher over this chain, or `None` when there is nothing to sign with.
    fn publisher<'b>(&'b self) -> FeedPublisher<'b, 'a> {
        FeedPublisher {
            session: self.session,
            relayer: match (self.signer.as_deref(), self.identity.as_ref()) {
                (Some(signer), Some(identity)) => Some(Relayer {
                    inner: signer,
                    identity,
                }),
                _ => None,
            },
            now_ms: self.now_ms,
        }
    }

    /// A refresh over this chain, with the AI the caller supplies.
    fn refresher<'b>(
        &'b self,
        coordinator: &'b crate::ai::coordinator::Coordinator,
        fetch: &'b crate::ai::resolution::EvidenceFetcher,
        artifact_sql: &'b ArtifactSql,
    ) -> BindingRefresh<'b> {
        BindingRefresh {
            session: self.session,
            coordinator,
            fetch,
            artifact_sql,
            now_ms: self.now_ms,
            daily_limit: self.daily_limit,
        }
    }

    /// The tick's three callbacks, over this chain.
    fn tick<'b>(
        &'b self,
        context: &'b Context<'b>,
        coordinator: &'b crate::ai::coordinator::Coordinator,
        fetch: &'b crate::ai::resolution::EvidenceFetcher,
        artifact_sql: &'b ArtifactSql,
    ) -> Tick<'b, 'a> {
        Tick {
            chain: self,
            context,
            coordinator,
            fetch,
            artifact_sql,
        }
    }
}

/// What `operate_feeds_v2` is handed: the seed path, the refresh path and the publish path, all over
/// the same chain.
struct Tick<'a, 'b> {
    chain: &'a Chain<'b>,
    context: &'a Context<'a>,
    coordinator: &'a crate::ai::coordinator::Coordinator,
    fetch: &'a crate::ai::resolution::EvidenceFetcher,
    artifact_sql: &'a ArtifactSql,
}

impl Tick<'_, '_> {
    /// `operate_feeds_v2`, with this tick's three callbacks.
    async fn run(&self, now_ms: i64) -> std::result::Result<Vec<Value>, FeedError> {
        let refresh = self.chain.refresher(self.coordinator, self.fetch, self.artifact_sql);
        let publisher = self.chain.publisher();
        let seed = Editorial { context: self.context };
        operate_feeds_v2(
            &crate::db::D1(self.chain.session),
            now_ms,
            &refresh,
            &publisher,
            Some(&seed),
        )
        .await
    }
}

// ---------------------------------------------------------------------------------------------
// The handlers.
// ---------------------------------------------------------------------------------------------

/// The AI a refreshing or ticking handler needs, alongside the chain.
struct Services<'a> {
    chain: Chain<'a>,
    coordinator: crate::ai::coordinator::Coordinator,
    fetch: crate::ai::resolution::EvidenceFetcher,
    artifact_sql: Box<ArtifactSql>,
}

impl<'a> Services<'a> {
    fn build(context: &'a Context<'_>) -> Self {
        // The artifact statements are the application's own: a refresh retains what the model was
        // given, and the writing of that is not this module's to decide.
        let artifact_sql: Box<ArtifactSql> = Box::new(|artifacts, now_ms| {
            crate::source_watch::artifact_sql(&retained(artifacts), now_ms).map_err(|refusal| {
                crate::risk_refresh::RefreshError {
                    status: refusal.status(),
                    code: refusal.code(),
                    message: refusal.message(),
                }
            })
        });
        Self {
            chain: Chain::from(context),
            coordinator: crate::application::coordinator(context.env),
            fetch: crate::application::evidence_fetcher(),
            artifact_sql,
        }
    }
}

/// The AI's artifacts, as the retained-bytes form the artifact statements take.
fn retained(artifacts: &[crate::ai::coordinator::Artifact]) -> Vec<crate::source_watch::Retained> {
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

/// `POST /api/admin/risk/v2/definitions`.
pub async fn admit_definition_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["feedId", "definition"])?;
    let definition: forecast_domain::risk_feed::CanonicalRiskDefinitionV2 =
        serde_json::from_value(body["definition"].clone())
            .map_err(|_| feed_error(FeedError::refused("invalid canonical definition")))?;
    let digest = admit_definition(
        &crate::db::D1(context.session),
        body["feedId"].as_str().unwrap_or(""),
        &definition,
        ACTOR,
        context.now_ms,
    )
    .await
    .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "admitted", "definitionHash": digest}),
        201,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/profiles`.
pub async fn admit_profile_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["feedId", "profile"])?;
    let profile: forecast_domain::risk_feed::RiskMappingProfileV2 = serde_json::from_value(body["profile"].clone())
        .map_err(|_| feed_error(FeedError::refused("invalid mapping profile")))?;
    let digest = admit_profile(
        &crate::db::D1(context.session),
        body["feedId"].as_str().unwrap_or(""),
        &profile,
        ACTOR,
        context.now_ms,
    )
    .await
    .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "admitted", "profileHash": digest}),
        201,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/bindings`.
pub async fn approve_binding_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["feedId", "binding"])?;
    let binding: forecast_domain::risk_feed::RiskFeedBindingV2 = serde_json::from_value(body["binding"].clone())
        .map_err(|_| feed_error(FeedError::refused("invalid canonical binding")))?;
    let binding_id = binding.binding_id.clone();
    approve_binding_v2(
        &crate::db::D1(context.session),
        body["feedId"].as_str().unwrap_or(""),
        &binding,
        ACTOR,
        context.now_ms,
    )
    .await
    .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "approved", "bindingId": binding_id}),
        201,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/bindings/{id}/revoke`.
pub async fn revoke_binding_route(
    context: &Context<'_>,
    binding_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["reason"])?;
    let Some(reason) = body["reason"].as_str() else {
        return Err(feed_error(FeedError::refused("a revocation reason is required")));
    };
    revoke_binding_v2(
        &crate::db::D1(context.session),
        binding_id,
        ACTOR,
        context.now_ms,
        reason,
    )
    .await
    .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "revoked", "bindingId": binding_id}),
        200,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/bindings/{id}/refresh`.
///
/// The body must be *empty*: the refresh reads the approved, immutable question, and a caller that
/// could name one would be naming something other than what was approved.
pub async fn refresh_binding_route(
    context: &Context<'_>,
    binding_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    if !body.is_empty() {
        return Err(feed_error(FeedError::refused(
            "refresh uses the approved immutable question",
        )));
    }
    let services = Services::build(context);
    let result = services
        .chain
        .refresher(&services.coordinator, &services.fetch, &services.artifact_sql)
        .refresh(binding_id.to_string())
        .await
        .map_err(|error| RouteError::Failed(error.status, error.code, error.message))?;
    Ok(api_response(result, 200, false)?)
}

/// `POST /api/admin/risk/v2/feeds/{id}/publish`.
pub async fn publish_feed_route(
    context: &Context<'_>,
    feed_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["weightSetHash", "weightSetVersion", "calibrationCohortId"])?;
    let hash = body["weightSetHash"].as_str().unwrap_or("");
    let version = body["weightSetVersion"].as_str().unwrap_or("");
    let cohort = body["calibrationCohortId"].as_str().unwrap_or("");
    // The weight set has to be one this application *admitted*. Both the commitment and the named
    // version are checked, so a caller cannot cite a version the retained bytes do not carry.
    let db = crate::db::D1(context.session);
    let row = crate::db::Database::first(
        &db,
        "SELECT body FROM artifacts WHERE hash=? AND kind='risk-weight-set'",
        &[json!(hash)],
    )
    .await
    .map_err(RouteError::Worker)?;
    let admitted = row
        .as_ref()
        .and_then(|row| crate::db::text(row, "body"))
        .and_then(|body| serde_json::from_str::<Value>(body).ok())
        .filter(|value| {
            forecast_domain::content_hash(value).is_ok_and(|digest| digest == hash)
                && value.get("version").and_then(Value::as_str) == Some(version)
        });
    if admitted.is_none() {
        return Err(feed_error(FeedError::refused("weight reference is not admitted")));
    }
    let envelope = Chain::from(context)
        .publisher()
        .publish(feed_id, hash, version, cohort)
        .await
        .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "published", "envelope": serde_json::to_value(&envelope).map_err(|error| RouteError::Worker(error.into()))?}),
        201,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/feeds/{id}/operate`.
pub async fn operate_feed_route(
    context: &Context<'_>,
    feed_id: &str,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(
        body,
        &["weightSetHash", "weightSetVersion", "calibrationCohortId", "enabled"],
    )?;
    let Some(enabled) = body["enabled"].as_bool() else {
        return Err(feed_error(FeedError::refused(
            "operation needs an admitted weight reference, cohort and enabled flag",
        )));
    };
    configure_operation(
        &crate::db::D1(context.session),
        feed_id,
        body["weightSetHash"].as_str().unwrap_or(""),
        body["weightSetVersion"].as_str().unwrap_or(""),
        body["calibrationCohortId"].as_str().unwrap_or(""),
        enabled,
        ACTOR,
        context.now_ms,
    )
    .await
    .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "configured", "feedId": feed_id, "enabled": enabled}),
        200,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/series`.
pub async fn configure_series_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    exact(body, &["series", "enabled"])?;
    let Some(enabled) = body["enabled"].as_bool() else {
        return Err(feed_error(FeedError::refused(
            "a canonical series template and enabled flag are required",
        )));
    };
    let series: forecast_domain::risk_feed::RiskFeedSeriesV2 = serde_json::from_value(body["series"].clone())
        .map_err(|_| feed_error(FeedError::refused("invalid canonical series")))?;
    let series_id = series.series_id.clone();
    let digest = crate::risk_feed_series::configure_series(
        &crate::db::D1(context.session),
        &series,
        enabled,
        ACTOR,
        context.now_ms,
    )
    .await
    .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "configured", "seriesId": series_id, "seriesHash": digest, "enabled": enabled}),
        200,
        false,
    )?)
}

/// `POST /api/admin/risk/v2/operate`: the scheduled tick.
pub async fn operate_route(
    context: &Context<'_>,
    body: &serde_json::Map<String, Value>,
) -> Result<Response, RouteError> {
    if !body.is_empty() {
        return Err(feed_error(FeedError::refused("operation ticks take no parameters")));
    }
    let services = Services::build(context);
    let outcomes = services
        .chain
        .tick(context, &services.coordinator, &services.fetch, &services.artifact_sql)
        .run(context.now_ms)
        .await
        .map_err(feed_error)?;
    Ok(api_response(
        json!({"status": "ticked", "feeds": outcomes}),
        200,
        false,
    )?)
}

/// `GET /api/admin/risk/v2/health`.
pub async fn health_route(context: &Context<'_>) -> Result<Response, RouteError> {
    let health = operations_health(&crate::db::D1(context.session), context.now_ms)
        .await
        .map_err(feed_error)?;
    Ok(api_response(health, 200, false)?)
}

/// `GET /api/admin/risk/v2/feeds/{id}/training`.
pub async fn training_route(context: &Context<'_>, feed_id: &str) -> Result<Response, RouteError> {
    let export = training_export(&crate::db::D1(context.session), feed_id, context.now_ms, TRAINING_LIMIT)
        .await
        .map_err(feed_error)?;
    Ok(api_response(export, 200, false)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The key id is the hash of the *key*, not of the address that spells it.
    ///
    /// The reference writes `"forecast-relayer-" + sha256(relayer_public_key()).hexdigest()[:16]`,
    /// where `relayer_public_key()` is `base58_decode(SOLANA_RELAYER)`. Hashing the address string
    /// instead is a plausible-looking port and a different key id, which would name a key that no
    /// verifier can find. The expected values below are that expression, run:
    ///
    /// ```text
    /// >>> base58_encode(b"forecast-network-relayer-key32ab!")   # 32 bytes
    /// '7ts9fNsa3obSDTBSHexSZFGJeWKzFTcB6e8mXoGgxbjo'
    /// >>> hashlib.sha256(key).hexdigest()[:16]
    /// 'd14649b3c9d5bad6'
    /// ```
    #[test]
    fn the_key_id_is_the_hash_of_the_key_not_of_its_address() {
        let address = "7ts9fNsa3obSDTBSHexSZFGJeWKzFTcB6e8mXoGgxbjo";
        let public_key = bs58::decode(address).into_vec().expect("a base58 address");
        assert_eq!(public_key.len(), 32);
        let identity = Identity::of(&public_key);
        assert_eq!(
            identity.public_key_hex,
            "666f7265636173742d6e6574776f726b2d72656c617965722d6b657933326162"
        );
        assert_eq!(identity.key_id, "forecast-relayer-d14649b3c9d5bad6");
    }

    fn body(pairs: &[(&str, Value)]) -> serde_json::Map<String, Value> {
        pairs
            .iter()
            .map(|(key, value)| (key.to_string(), value.clone()))
            .collect()
    }

    /// `set(body) != {...}` is a *set* comparison, so a body with the right number of keys and one
    /// of them wrong is refused — as is a body that is missing one and carries a stranger in its
    /// place. A length check alone would accept that second body.
    #[test]
    fn an_operator_body_is_accepted_only_for_the_exact_key_set() {
        assert!(exact(
            &body(&[("feedId", json!("f")), ("series", json!({}))]),
            &["feedId", "series"]
        )
        .is_ok());
        assert!(exact(
            &body(&[("feedId", json!("f")), ("series", json!({}))]),
            &["feedId", "series", "enabled"]
        )
        .is_err());
        // Same length, different keys.
        assert!(exact(
            &body(&[("feedId", json!("f")), ("profile", json!({}))]),
            &["feedId", "series"]
        )
        .is_err());
        // Same key, an extra one.
        assert!(exact(&body(&[("reason", json!("x")), ("extra", json!(1))]), &["reason"]).is_err());
        assert!(exact(&body(&[]), &[]).is_ok());
    }

    /// The two shapes the entry answers a feed failure with, and why there are two: a domain
    /// `require` raises a `ValidationError`, which is not an `AppError`, so it falls through the
    /// entry's `except Exception` as `service_unavailable`; the missing relayer is raised by the
    /// entry itself and keeps its own code.
    #[test]
    fn a_refusal_is_a_server_fault_and_a_missing_relayer_names_itself() {
        for error in [
            FeedError::refused("any domain refusal"),
            FeedError::storage(),
            FeedError::storage_at("risk feed publication did not persist"),
        ] {
            match feed_error(error) {
                RouteError::Failed(503, "service_unavailable", "Please try again shortly.") => {}
                other => panic!("{other:?}"),
            }
        }
        match feed_error(FeedError::signer()) {
            RouteError::Failed(503, "risk_signer_unavailable", "Risk feed signing is not configured.") => {}
            other => panic!("{other:?}"),
        }
    }

    /// The class names the reference's tick records, which are the reference's own classes and not
    /// this port's.
    #[test]
    fn every_fault_names_the_reference_class() {
        assert_eq!(FeedError::refused("x").kind(), "ValidationError");
        assert_eq!(FeedError::signer().kind(), "AppError");
        assert_eq!(FeedError::storage().kind(), "Error");
        assert_eq!(
            crate::risk_refresh::RefreshError {
                status: 409,
                code: "risk_binding_not_current",
                message: "The approved risk question is not available for refresh.",
            }
            .kind(),
            "AppError"
        );
    }
}
