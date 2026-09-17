//! `Markets.get` view: LMSR state re-validated from the row, probability frozen at an evidence
//! cutoff when a decision exists.

use serde_json::{json, Value};
use worker::*;

use forecast_domain::pricing::{
    initialize_market, market_probability_bp, PricingReceipt, PricingState, ATOMIC_UNITS_PER_POINT,
};
use forecast_domain::{canonical_bytes, content_hash, Record};

use crate::db::{first, get, int, text, Row};

pub fn conflict() -> worker::Error {
    "market_conflict".into()
}

/// `Markets._state`: the stored state must hash to the row's commitments.
pub fn state_of(row: &Row) -> Result<PricingState> {
    let state = PricingState::from_json(text(row, "state").unwrap_or("")).map_err(|_| conflict())?;
    let policy_json = String::from_utf8(canonical_bytes(&state.policy).map_err(|_| conflict())?).unwrap_or_default();
    if content_hash(&state).ok().as_deref() != text(row, "state_hash")
        || Some(state.revision) != int(row, "revision")
        || Some(state.specification_hash.as_str()) != text(row, "specification_hash")
        || Some(state.market_id.as_str()) != text(row, "forecast_id")
        || content_hash(&state.policy).ok().as_deref() != text(row, "policy_hash")
        || Some(policy_json.as_str()) != text(row, "policy")
    {
        return Err(conflict());
    }
    Ok(state)
}

/// Read the unchanged receipt at the cutoff, never reprice earlier fills.
async fn eligible_prefix(session: &D1DatabaseSession, row: &Row, decision: &Row) -> Result<Option<PricingState>> {
    if text(decision, "event_time_basis") != Some("published_instant")
        || text(decision, "specification_hash") != text(row, "specification_hash")
    {
        return Ok(None);
    }
    let forecast_id = json!(text(row, "forecast_id"));
    let cutoff = get(decision, "cutoff_at").clone();
    let inversion = first(
        session,
        "SELECT 1 FROM market_fills late JOIN market_fills early ON early.forecast_id=late.forecast_id AND early.revision>late.revision \
         WHERE late.forecast_id=? AND late.created_at>=? AND early.created_at<?",
        &[forecast_id.clone(), cutoff.clone(), cutoff.clone()],
    )
    .await?;
    if inversion.is_some() {
        return Ok(None);
    }
    let before = first(
        session,
        "SELECT * FROM market_fills WHERE forecast_id=? AND created_at<? ORDER BY revision DESC LIMIT 1",
        &[forecast_id.clone(), cutoff],
    )
    .await?;
    let Some(before) = before else {
        let policy = state_of(row)?.policy;
        return Ok(initialize_market(
            policy,
            text(row, "forecast_id").unwrap_or(""),
            text(row, "specification_hash").unwrap_or(""),
        )
        .ok());
    };
    let Ok(receipt) = PricingReceipt::from_json(text(&before, "body").unwrap_or("")) else {
        return Ok(None);
    };
    let state = receipt.state;
    let count = first(
        session,
        "SELECT COUNT(*) n FROM market_fills WHERE forecast_id=? AND revision<=?",
        &[forecast_id, get(&before, "revision").clone()],
    )
    .await?;
    let consistent = Some(state.market_id.as_str()) == text(row, "forecast_id")
        && Some(state.specification_hash.as_str()) == text(row, "specification_hash")
        && content_hash(&state.policy).ok().as_deref() == text(row, "policy_hash")
        && Some(state.revision) == int(&before, "revision")
        && count.as_ref().and_then(|c| int(c, "n")) == Some(state.revision);
    Ok(consistent.then_some(state))
}

pub async fn view(session: &D1DatabaseSession, row: &Row, live_enabled: bool) -> Result<Value> {
    let state = state_of(row)?;
    let forecast_id = json!(text(row, "forecast_id"));
    let decision = first(
        session,
        "SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?",
        std::slice::from_ref(&forecast_id),
    )
    .await?;
    let mut probability = Some(market_probability_bp(&state).map_err(|_| conflict())?);
    let mut probability_revision = Some(state.revision);
    let mut probability_status = "current";
    if let Some(decision) = &decision {
        probability_status = "frozen_before_evidence";
        match eligible_prefix(session, row, decision).await? {
            None => {
                probability = None;
                probability_revision = None;
                probability_status = "eligibility_review";
            }
            Some(prefix) => {
                probability = Some(market_probability_bp(&prefix).map_err(|_| conflict())?);
                probability_revision = Some(prefix.revision);
            }
        }
    }
    let voids = first(
        session,
        "SELECT COUNT(*) n,COALESCE(SUM(spend),0) amount FROM market_fill_voids WHERE forecast_id=?",
        &[forecast_id],
    )
    .await?;
    Ok(json!({
        "forecastId": get(row, "forecast_id"), "mode": get(row, "mode"), "status": get(row, "status"),
        "yesProbabilityBps": probability, "probabilityStatus": probability_status,
        "probabilityRevision": probability_revision, "revision": get(row, "revision"),
        "voidedFillCount": voids.as_ref().map_or(json!(0), |v| get(v, "n").clone()),
        "refundedPoints": voids.as_ref().map_or(json!(0), |v| get(v, "amount").clone()),
        "atomicScale": ATOMIC_UNITS_PER_POINT,
        "maxSpendPoints": (state.policy.maximum_fill_atomic / ATOMIC_UNITS_PER_POINT).min(100),
        "policyHash": get(row, "policy_hash"), "specificationHash": get(row, "specification_hash"),
        "liveEnabled": live_enabled, "reserveAtomic": get(row, "reserve_atomic").as_i64().map(|v| v.to_string()),
    }))
}

pub async fn market(session: &D1DatabaseSession, forecast_id: &str, live_enabled: bool) -> Result<Value> {
    if forecast_id.is_empty() || forecast_id.len() > 128 || forecast_id.chars().any(|c| (c as u32) < 32) {
        return Err("invalid_input".into());
    }
    match first(
        session,
        "SELECT * FROM point_markets WHERE forecast_id=?",
        &[json!(forecast_id)],
    )
    .await?
    {
        Some(row) => view(session, &row, live_enabled).await,
        None => Ok(Value::Null),
    }
}

/// `Markets._account`: shadow accounts are created lazily; active accounts must exist.
async fn account(session: &D1DatabaseSession, user_id: &str, mode: &str) -> Result<Row> {
    if mode == "shadow" {
        crate::db::statement(
            session,
            "INSERT OR IGNORE INTO market_shadow_accounts(user_id,updated_at) SELECT id,? FROM users WHERE id=?",
            &[json!(worker::Date::now().as_millis() as i64), json!(user_id)],
        )?
        .run()
        .await?;
    }
    let table = if mode == "shadow" {
        "market_shadow_accounts"
    } else {
        "point_accounts"
    };
    let sql = format!(
        "SELECT a.*,COALESCE(f.remainder_atomic,0) fraction FROM {table} a LEFT JOIN point_fractions f ON f.user_id=a.user_id AND f.mode=? WHERE a.user_id=?"
    );
    first(session, &sql, &[json!(mode), json!(user_id)])
        .await?
        .ok_or_else(|| "market_unavailable".into())
}

pub async fn positions(session: &D1DatabaseSession, user_id: &str, forecast_id: &str) -> Result<Value> {
    let row = first(
        session,
        "SELECT * FROM point_markets WHERE forecast_id=?",
        &[json!(forecast_id)],
    )
    .await?
    .ok_or_else(|| worker::Error::from("market_missing"))?;
    let mode = text(&row, "mode").unwrap_or("").to_string();
    let account = account(session, user_id, &mode).await?;
    let position = first(
        session,
        "SELECT * FROM market_positions WHERE user_id=? AND forecast_id=?",
        &[json!(user_id), json!(forecast_id)],
    )
    .await?;
    let voids = first(
        session,
        "SELECT COUNT(*) n,COALESCE(SUM(spend),0) amount FROM market_fill_voids WHERE user_id=? AND forecast_id=?",
        &[json!(user_id), json!(forecast_id)],
    )
    .await?;
    let claims = |name: &str| position.as_ref().and_then(|p| int(p, name)).unwrap_or(0).to_string();
    Ok(json!({
        "forecastId": forecast_id, "mode": mode, "availablePoints": get(&account, "available"), "committedPoints": get(&account, "committed"),
        "fractionAtomic": int(&account, "fraction").unwrap_or(0).to_string(),
        "grossPoints": position.as_ref().map_or(json!(0), |p| get(p, "gross").clone()),
        "yesClaimsAtomic": claims("yes_claims_atomic"), "noClaimsAtomic": claims("no_claims_atomic"),
        "voidedFillCount": voids.as_ref().map_or(json!(0), |v| get(v, "n").clone()),
        "refundedPoints": voids.as_ref().map_or(json!(0), |v| get(v, "amount").clone()),
        "settled": position.as_ref().and_then(|p| int(p, "settled")).unwrap_or(0) != 0,
    }))
}
