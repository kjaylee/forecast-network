//! Devnet registry status for a forecast (`SolanaRegistry.status`) and attestation status.

use curve25519_dalek::edwards::CompressedEdwardsY;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use worker::*;

use forecast_domain::content_hash;

use crate::db::{batch, first, get, int, text};

pub const DEVNET_GENESIS: &str = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";

pub fn identity_hash(kind: &str, value: &str) -> [u8; 32] {
    Sha256::digest(format!("forecast-network:registry:{kind}:v1\n{value}").as_bytes()).into()
}

pub fn find_program_address(program: &[u8; 32], seeds: &[&[u8]]) -> Option<[u8; 32]> {
    for bump in (0..=255u8).rev() {
        let mut hasher = Sha256::new();
        for seed in seeds {
            hasher.update(seed);
        }
        hasher.update([bump]);
        hasher.update(program);
        hasher.update(b"ProgramDerivedAddress");
        let candidate: [u8; 32] = hasher.finalize().into();
        if CompressedEdwardsY(candidate).decompress().is_none() {
            return Some(candidate);
        }
    }
    None
}

pub async fn status(session: &D1DatabaseSession, program_id: &[u8; 32], forecast_id: &str) -> Result<Value> {
    let id = json!(forecast_id);
    let results = batch(
        session,
        vec![
            ("SELECT enabled FROM registry_forecasts WHERE forecast_id=?".to_string(), vec![id.clone()]),
            ("SELECT revision,state FROM forecasts WHERE id=?".to_string(), vec![id.clone()]),
            ("SELECT revision,confirmed_slot,signature FROM registry_delivery WHERE forecast_id=? AND status='confirmed' ORDER BY revision DESC LIMIT 1".to_string(), vec![id.clone()]),
            ("SELECT revision,status,error_code FROM registry_delivery WHERE forecast_id=? AND status!='confirmed' ORDER BY revision LIMIT 1".to_string(), vec![id]),
            ("SELECT program_id,genesis_hash FROM registry_deployment WHERE singleton=1".to_string(), vec![]),
        ],
    )
    .await?;
    let enabled = results[0].first();
    let current = results[1].first();
    let mut confirmed = results[2].first();
    let pending = results[3].first();
    let pin = results[4].first();
    let address =
        find_program_address(program_id, &[b"forecast", &identity_hash("forecast", forecast_id)]).ok_or("pda")?;
    let deployment_matches = pin.is_some_and(|pin| {
        text(pin, "program_id") == Some(hex::encode(program_id).as_str())
            && text(pin, "genesis_hash") == Some(DEVNET_GENESIS)
    });
    if !deployment_matches {
        confirmed = None;
    }
    let status = if enabled.is_none_or(|row| int(row, "enabled").unwrap_or(0) == 0) {
        "disabled"
    } else if pending.is_some_and(|p| text(p, "status") == Some("blocked")) {
        "blocked"
    } else if let (Some(current), Some(confirmed)) = (current, confirmed) {
        if get(current, "revision") == get(confirmed, "revision") {
            "confirmed"
        } else {
            "pending"
        }
    } else {
        "pending"
    };
    Ok(json!({
        "cluster": "devnet", "programId": bs58::encode(program_id).into_string(), "account": bs58::encode(address).into_string(),
        "localRevision": current.map_or(Value::Null, |c| get(c, "revision").clone()),
        "localState": current.map_or(Value::Null, |c| get(c, "state").clone()),
        "confirmedRevision": confirmed.map_or(Value::Null, |c| get(c, "revision").clone()),
        "confirmedSlot": confirmed.map_or(Value::Null, |c| get(c, "confirmed_slot").clone()),
        "signature": confirmed.map_or(Value::Null, |c| get(c, "signature").clone()),
        "status": status,
        "pendingReason": pending.map_or(Value::Null, |p| get(p, "error_code").clone()),
        "trust": "authority-attested commitments; upgradeable program",
    }))
}

pub async fn attestation(
    session: &D1DatabaseSession,
    user_id: Option<&str>,
    forecast_id: &str,
    available: bool,
) -> Result<Value> {
    let Some(user_id) = user_id else { return Ok(Value::Null) };
    let row = first(
        session,
        "SELECT id,status,signature,memo,reported_slot,created_at FROM forecast_attestations \
         WHERE user_id=? AND forecast_id=? AND status IN ('submitted','verified') ORDER BY created_at DESC LIMIT 1",
        &[json!(user_id), json!(forecast_id)],
    )
    .await?;
    let Some(row) = row else {
        return Ok(json!({"status": "none", "available": available}));
    };
    let signature = text(&row, "signature").unwrap_or("");
    Ok(json!({
        "status": get(&row, "status"), "signature": get(&row, "signature"), "memo": get(&row, "memo"), "slot": get(&row, "reported_slot"),
        "at": get(&row, "created_at"), "cluster": "devnet", "explorer": format!("https://explorer.solana.com/tx/{signature}?cluster=devnet"),
        "available": available, "commitment": content_hash(&json!({"memo": get(&row, "memo")})).map_err(|e| worker::Error::from(e.to_string()))?,
    }))
}
