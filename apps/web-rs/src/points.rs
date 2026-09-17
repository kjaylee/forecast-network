//! Participation points read models (`PointsService.summary` / `position`).

use serde_json::{json, Value};
use worker::*;

use crate::db::{all, first, get, int, text, Row};

pub const POLICY_VERSION: &str = "participation-points-v1";

fn position(row: Option<&Row>) -> Value {
    match row {
        Some(row) => json!({
            "amount": get(row, "amount"), "status": get(row, "status"), "policyVersion": get(row, "policy_version"),
            "returned": get(row, "returned"), "outcome": get(row, "outcome"), "forecastRevision": get(row, "forecast_revision"),
        }),
        None => {
            json!({"amount": 0, "status": "practice", "policyVersion": POLICY_VERSION, "returned": null, "outcome": null, "forecastRevision": null})
        }
    }
}

pub async fn position_for(session: &D1DatabaseSession, user_id: &str, forecast_id: &str) -> Result<Value> {
    let rows = all(
        session,
        "SELECT * FROM point_positions WHERE user_id=? AND forecast_id IN (SELECT value FROM json_each(?))",
        &[json!(user_id), json!(serde_json::to_string(&[forecast_id])?)],
    )
    .await?;
    Ok(position(
        rows.iter().find(|r| text(r, "forecast_id") == Some(forecast_id)),
    ))
}

pub async fn summary(session: &D1DatabaseSession, user_id: &str) -> Result<Option<Value>> {
    let installed: Vec<String> = all(
        session,
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN \
         ('market_account_ledger','point_eligibility_adjustments','market_fill_voids','point_evidence_rewards')",
        &[],
    )
    .await?
    .iter()
    .filter_map(|r| text(r, "name").map(str::to_string))
    .collect();
    let has = |name: &str| installed.iter().any(|n| n == name);
    let mut history = String::from(
        "SELECT id,kind,available_delta,committed_delta,available_after,committed_after,stake,returned,forecast_id,created_at \
         FROM point_ledger l WHERE l.user_id=a.user_id ",
    );
    let mut fraction = "0".to_string();
    if has("market_account_ledger") {
        history.push_str(
            "UNION ALL SELECT ml.id,ml.kind,ml.available_delta,ml.committed_delta,ml.available_after,ml.committed_after,\
             ABS(ml.committed_delta), ( CASE WHEN ml.kind='market_settlement' THEN ml.available_delta ELSE NULL END ) ,\
             ml.forecast_id,ml.created_at FROM market_account_ledger ml WHERE ml.user_id=a.user_id AND ml.mode='active' ",
        );
        fraction =
            "COALESCE((SELECT remainder_atomic FROM point_fractions WHERE user_id=a.user_id AND mode='active'),0)"
                .to_string();
    }
    if has("point_eligibility_adjustments") {
        history.push_str(
            "UNION ALL SELECT e.id, ( CASE WHEN e.available_delta>=0 THEN 'evidence_refund' ELSE 'evidence_restore' END ) ,\
             e.available_delta,e.committed_delta,e.available_after,e.committed_after,e.old_amount,\
             MAX(0,e.available_delta),e.forecast_id,e.created_at FROM point_eligibility_adjustments e \
             WHERE e.user_id=a.user_id AND e.available_delta!=0 ",
        );
    }
    if has("point_evidence_rewards") {
        history.push_str(
            "UNION ALL SELECT w.id,'evidence_reward',w.amount,0,w.available_after,w.committed_after,0,w.amount,\
             w.forecast_id,w.created_at FROM point_evidence_rewards w WHERE w.user_id=a.user_id ",
        );
    }
    if has("market_fill_voids") {
        history.push_str(
            "UNION ALL SELECT 'market-void:'||v.fill_id,'market_void_refund',v.spend,-v.spend,\
             v.available_before+v.spend,v.committed_before-v.spend,v.spend,v.spend,v.forecast_id,v.created_at \
             FROM market_fill_voids v WHERE v.user_id=a.user_id AND v.mode='active' ",
        );
    }
    history.push_str("ORDER BY created_at DESC,id DESC LIMIT 30");
    let sql = format!(
        "SELECT a.*,p.profile_grant,p.wallet_grant,p.max_stake,p.win_return_multiplier,p.invalid_return_multiplier,\
         profile.created_at AS profile_awarded_at,wallet.created_at AS wallet_awarded_at,\
         w.address AS linked_address,EXISTS(SELECT 1 FROM point_awards old WHERE old.wallet_address=w.address) AS address_rewarded,\
         (SELECT json_group_array(json_object('id',id,'kind',kind,'amount',available_delta,\
         'availableDelta',available_delta,'committedDelta',committed_delta,'availableAfter',available_after,\
         'committedAfter',committed_after,'stake',stake,'returned',returned,'forecastId',forecast_id,'at',created_at)) \
         FROM ({history})) AS entries_json, {fraction} AS fraction_atomic \
         FROM point_accounts a JOIN point_policies p ON p.version=? \
         LEFT JOIN point_awards profile ON profile.user_id=a.user_id AND profile.kind='profile' \
         LEFT JOIN point_awards wallet ON wallet.user_id=a.user_id AND wallet.kind='wallet' \
         LEFT JOIN wallet_links w ON w.user_id=a.user_id WHERE a.user_id=?"
    );
    let Some(row) = first(session, &sql, &[json!(POLICY_VERSION), json!(user_id)]).await? else {
        return Ok(None);
    };
    let rewarded = !get(&row, "wallet_awarded_at").is_null();
    let linked = !get(&row, "linked_address").is_null();
    let address_rewarded = int(&row, "address_rewarded").unwrap_or(0) != 0;
    let eligible = !rewarded && !address_rewarded;
    let reason = if rewarded {
        "awarded"
    } else if address_rewarded {
        "wallet_already_rewarded"
    } else if linked {
        "eligible"
    } else {
        "connect_wallet"
    };
    let available = int(&row, "available").unwrap_or(0);
    let committed = int(&row, "committed").unwrap_or(0);
    let entries: Value = serde_json::from_str(text(&row, "entries_json").unwrap_or("[]")).unwrap_or(json!([]));
    Ok(Some(json!({
        "userId": user_id, "available": available, "committed": committed, "total": available + committed,
        "fractionAtomic": get(&row, "fraction_atomic").as_i64().map(|v| v.to_string()).unwrap_or_else(|| get(&row, "fraction_atomic").to_string()),
        "atomicScale": 1_000_000,
        "policy": {"version": POLICY_VERSION, "profileGrant": get(&row, "profile_grant"), "walletGrant": get(&row, "wallet_grant"),
                   "minStake": 0, "maxStake": get(&row, "max_stake"), "winReturnMultiplier": get(&row, "win_return_multiplier"),
                   "invalidReturnMultiplier": get(&row, "invalid_return_multiplier"), "practiceAllowed": true, "purchasable": false,
                   "transferable": false, "redeemable": false, "reputationWeighted": false},
        "onboarding": {"profile": {"completed": !get(&row, "profile_awarded_at").is_null(), "reward": get(&row, "profile_grant"),
                                   "awardedAt": get(&row, "profile_awarded_at")},
                       "wallet": {"completed": rewarded, "linked": linked, "reward": get(&row, "wallet_grant"), "eligible": eligible,
                                  "reason": reason, "awardedAt": get(&row, "wallet_awarded_at")}},
        "entries": entries,
    })))
}

/// `PointsService.positions`: one entry per requested id (practice defaults when absent).
pub async fn positions(
    session: &D1DatabaseSession,
    user_id: &str,
    forecast_ids: &[String],
) -> Result<std::collections::BTreeMap<String, Value>> {
    let mut identifiers: Vec<String> = Vec::new();
    for id in forecast_ids {
        if !identifiers.contains(id) {
            identifiers.push(id.clone());
        }
    }
    let mut result = std::collections::BTreeMap::new();
    if identifiers.is_empty() {
        return Ok(result);
    }
    let rows = all(
        session,
        "SELECT * FROM point_positions WHERE user_id=? AND forecast_id IN (SELECT value FROM json_each(?))",
        &[json!(user_id), json!(serde_json::to_string(&identifiers)?)],
    )
    .await?;
    for id in identifiers {
        let row = rows.iter().find(|r| text(r, "forecast_id") == Some(id.as_str()));
        result.insert(id, position(row));
    }
    Ok(result)
}
