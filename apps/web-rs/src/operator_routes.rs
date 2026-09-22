//! The operator's own writes: the editorial seed, the registry attestation, the automation run
//! and the five-minute sweep the schedule calls. Split from `writes.rs`, whose helpers it still
//! uses.

use serde_json::{json, Map, Value};
use worker::*;

use crate::api_response;
use crate::auth_routes::session_secret;
use crate::db::text;
use crate::mutate::random_token;
use crate::routes::var;
use crate::routes::{Context, RouteError};
use crate::writes::*;

/// `POST /api/admin/seed`: create a genuine, compiler-reviewed question with no votes.
///
/// Editorial questions must be genuinely *open*: when the compiler's own forecast falls outside the
/// uncertainty band the draft is discarded rather than published, so a question like "will there be
/// a new version" never reaches the feed looking like a market. That check is the whole reason this
/// is not merely compile-then-publish.
///
/// The seed key is derived from the question, so seeding the same question twice returns the first
/// one: the operations row is the record that it was already asked.
pub async fn seed(
    context: &Context<'_>,
    question: &str,
    creator_name: &str,
    uncertainty_band: Option<(i64, i64)>,
    canonical_risk: bool,
) -> std::result::Result<Value, RouteError> {
    let db = crate::db::D1(context.session);
    let editorial = crate::db::Database::first(&db, "SELECT id FROM users WHERE id='system_editorial'", &[]).await?;
    if editorial.is_none() {
        let secret = session_secret(context)?;
        let recovery = crate::auth::token_hash(&secret, &format!("recovery:{}", random_token()));
        crate::db::Database::execute(
            &db,
            "INSERT OR IGNORE INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
            &[
                json!("system_editorial"),
                json!(creator_name.chars().take(40).collect::<String>()),
                json!("forecast_editorial"),
                json!(recovery),
                json!(context.now_ms),
            ],
        )
        .await?;
    }
    let seed_key = format!(
        "seed:{}",
        forecast_domain::content_hash(&json!({"question": question}))
            .map_err(|error| RouteError::Worker(error.to_string().into()))?
    );
    let previous = crate::db::Database::first(
        &db,
        "SELECT forecast_id FROM operations WHERE user_id=? AND operation_key=?",
        &[json!("system_editorial"), json!(seed_key)],
    )
    .await?;
    if let Some(previous) = previous {
        let forecast_id = text(&previous, "forecast_id").unwrap_or("").to_string();
        return Ok(json!({"forecast": card_row(&db, &forecast_id, context.now_ms).await?}));
    }
    let draft = compile(
        context.env,
        &db,
        "system_editorial",
        question,
        context.now_ms,
        canonical_risk,
    )
    .await?;
    if let Some((low, high)) = uncertainty_band {
        if let Some(probability) = draft.get("aiForecast").and_then(|forecast| forecast.get("probability")) {
            if let Some(probability) = probability.as_f64() {
                if !(low as f64..=high as f64).contains(&probability) {
                    return Err(RouteError::Failed(
                        409,
                        "seed_not_uncertain",
                        Box::leak(
                            format!(
                                "The compiler already expects this outcome ({probability:.0}% YES); editorial questions must be genuinely open."
                            )
                            .into_boxed_str(),
                        ),
                    ));
                }
            }
        }
    }
    let draft_id = draft["draftId"].as_str().unwrap_or("");
    publish(context, &db, "system_editorial", draft_id, &seed_key).await
}

/// `POST /api/forecasts/{id}/attest/{prepare|confirm}`: a phone signs a Devnet memo.
///
/// `prepare` returns an *incomplete* transaction — one real signature and one zeroed slot — which is
/// the whole contract: the relayer pays the fee and the wallet fills the slot, so neither side can
/// produce the other's signature. `confirm` is what makes the record final, and it is separate
/// because a phone that never came back must not leave a half-signed transaction looking sent.
pub async fn attest(
    context: &Context<'_>,
    user_id: &str,
    forecast_id: &str,
    action: &str,
    body: &Map<String, Value>,
) -> Handler {
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let signer = crate::application::relayer_signer(context.env);
    let relayer = relayer_public_key(context.env);
    let limiter = AttestationLimit {
        session: context.session,
        now_ms: context.now_ms,
    };
    // The relayer is only available when both its address and its seed are deployed: an address
    // with no seed cannot sign, and a seed with no address cannot be checked against.
    let attestations = crate::attestation::Attestations {
        db: &db,
        relayer,
        sign: signer.as_deref(),
        now_ms: &now,
        random_token: &random_token,
        rate_limit: &limiter,
    };
    let payload = Value::Object(body.clone());
    if action == "prepare" {
        let result = attestations
            .prepare(user_id, forecast_id, &payload)
            .await
            .map_err(refused_from_attestation)?;
        return Ok(api_response(result, 201, false)?);
    }
    let result = attestations
        .confirm(user_id, forecast_id, &payload)
        .await
        .map_err(refused_from_attestation)?;
    Ok(api_response(result, 200, false)?)
}

/// `relayer_public_key`: the hot relayer's address, only when its seed is deployed too.
fn relayer_public_key(env: &Env) -> Option<[u8; 32]> {
    let address = var(env, "SOLANA_RELAYER");
    let seed = env.secret("SOLANA_RELAYER_SEED").ok().map(|value| value.to_string())?;
    if address.is_empty() || seed.is_empty() {
        return None;
    }
    let decoded = bs58::decode(address).into_vec().ok()?;
    <[u8; 32]>::try_from(decoded.as_slice()).ok()
}

/// The `attest` limiter, over the session the request was served with.
struct AttestationLimit<'a> {
    session: &'a D1DatabaseSession,
    now_ms: i64,
}

impl crate::attestation::RateLimit for AttestationLimit<'_> {
    fn check<'a>(
        &'a self,
        scope: String,
        limit: i64,
        window_ms: i64,
    ) -> crate::attestation::BorrowedFuture<'a, Result<(), ()>> {
        Box::pin(async move {
            rate_limit(self.session, self.now_ms, &scope, limit, window_ms)
                .await
                .map_err(|_| ())
        })
    }
}

fn refused_from_attestation(error: crate::attestation::AttestationError) -> RouteError {
    RouteError::Failed(error.status, error.code, error.message)
}

/// `POST /api/admin/automation/run`: one automation pass, on demand.
///
/// This is the path that *needs* the chain adapter: `run_automation` sweeps the lifecycle, and a
/// finalize consults the chain before it commits. Wiring it without an adapter would finalize
/// locally and differ from the reference in exactly the way that is hardest to notice.
pub async fn run_automation(context: &Context<'_>) -> Handler {
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let parts = crate::application::chain_parts(context.env, &db, &now);
    let program = bs58::decode(var(context.env, "SOLANA_PROGRAM_ID"))
        .into_vec()
        .unwrap_or_default();
    let relayer = bs58::decode(var(context.env, "SOLANA_RELAYER"))
        .into_vec()
        .unwrap_or_default();
    let transport = match &parts {
        Some(parts) => Some(
            parts
                .transport(&program, &relayer)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.0)))?,
        ),
        None => None,
    };
    let registry = match (&transport, &parts) {
        (Some(transport), Some(_)) => Some(
            crate::registry_chain::SolanaRegistry::new(&db, transport, &program, &relayer, &now, &random_token)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.code)))?,
        ),
        _ => None,
    };
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
        registry: registry
            .as_ref()
            .map(|registry| registry as &dyn crate::mutate::FinalizationGate),
    };
    let result = application
        .run_automation(1)
        .await
        .map_err(|detail| RouteError::Worker(worker::Error::from(detail)))?;
    Ok(api_response(result, 200, false)?)
}

/// `POST /api/admin/sweep`: the five-minute pass, and the chain's delivery half.
///
/// Two jobs in one call, and the order is the reference's: the local sweep first, then whatever the
/// chain owes. The phase timings are returned because this route was once failing with a platform
/// error and nothing recorded where the time went — the failure was being attributed to whatever
/// seemed likeliest, which is the same reason the operate tick got them.
pub async fn sweep(context: &Context<'_>) -> Handler {
    let db = crate::db::D1(context.session);
    let now = || context.now_ms;
    let started = clock_ms();
    let opened = crate::db::usage();
    let parts = crate::application::chain_parts(context.env, &db, &now);
    let program = bs58::decode(var(context.env, "SOLANA_PROGRAM_ID"))
        .into_vec()
        .unwrap_or_default();
    let relayer = bs58::decode(var(context.env, "SOLANA_RELAYER"))
        .into_vec()
        .unwrap_or_default();
    let transport = match &parts {
        Some(parts) => Some(
            parts
                .transport(&program, &relayer)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.0)))?,
        ),
        None => None,
    };
    let registry = match (&transport, &parts) {
        (Some(transport), Some(_)) => Some(
            crate::registry_chain::SolanaRegistry::new(&db, transport, &program, &relayer, &now, &random_token)
                .map_err(|error| RouteError::Worker(worker::Error::from(error.code)))?,
        ),
        _ => None,
    };
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
        registry: registry
            .as_ref()
            .map(|registry| registry as &dyn crate::mutate::FinalizationGate),
    };
    // Four source polls per five-minute sweep keeps every watched publisher and article current;
    // one per tick starved the market source gate.
    let mut result = application
        .run_automation(4)
        .await
        .map_err(|detail| RouteError::Worker(worker::Error::from(detail)))?;
    let automation_ms = clock_ms() - started;
    let automation_rows = crate::db::usage().0 - opened.0;
    if let Some(registry) = &registry {
        if crate::admin::flag(context.env, "SOLANA_REGISTRY_RELAY_ENABLED") {
            let registry_started = clock_ms();
            let registry_read = crate::db::usage().0;
            // A delivery that fails is a retry, not a failed sweep: the local half has already
            // completed, and reporting the whole pass as failed would re-run it.
            result["registry"] = match registry.sync(3).await {
                Ok(value) => value,
                Err(_) => json!({"status": "retry_pending"}),
            };
            result["phaseMs"]["registry"] = json!(clock_ms() - registry_started);
            result["rowsRead"]["registry"] = json!(crate::db::usage().0 - registry_read);
        } else {
            result["registry"] = json!({"status": "relay_paused"});
        }
    }
    result["phaseMs"]["automation"] = json!(automation_ms);
    result["phaseMs"]["total"] = json!(clock_ms() - started);
    // The sweep runs four source polls and the registry pass; the same budget question as the
    // tick's, asked of the job that runs alongside it.
    let (rows, queries) = crate::db::usage();
    result["rowsRead"]["automation"] = json!(automation_rows);
    result["rowsRead"]["total"] = json!(rows - opened.0);
    result["queries"] = json!(queries - opened.1);
    Ok(api_response(result, 200, false)?)
}

/// A monotonic-enough millisecond clock for the phase timings. It is the wall clock, which is what
/// this runtime offers; a phase that takes a negative number of milliseconds would mean the clock
/// moved, and the reference's `time.monotonic` is the only thing that would have hidden it.
fn clock_ms() -> i64 {
    #[cfg(target_arch = "wasm32")]
    {
        worker::js_sys::Date::now() as i64
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        0
    }
}
