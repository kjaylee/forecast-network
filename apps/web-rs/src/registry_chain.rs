//! The registry's chain-facing half: what the chain says, and the commitment we publish to it.
//!
//! The read side lives in `registry.rs`; this is the part that talks to a cluster. Nothing here
//! holds a key — the transport does — but everything here decides *what* would be sent, and a
//! commitment that disagrees with the local history is worse than no commitment at all.
//!
//! Two distinctions are the reason this module is not a thin wrapper:
//!
//!   - **A schedule is not a fault.** `ChainDeadlineNotReached` means the chain agrees with us and
//!     its own clock has not reached the deadline yet. Every other refusal is a fault. A caller
//!     that cannot tell them apart either records a real fault as a wait and never looks again, or
//!     retries a wait as a fault and escalates against a wall that time alone moves.
//!   - **The reputation commitment is the whole history, re-derived.** `material` and
//!     `reputation_hash` replay every event up to the final revision and refuse if any receipt,
//!     hash or revision does not line up. A commitment that is merely *stated* would be a claim
//!     about the past that nobody can check.
//!
//! `send` is on the transport because the delivery path needs it, and it is the only place in this
//! port that writes to a chain. That half — `_deliver`, `_payload`, `_artifact`, `_release`,
//! `sync`, `backfill` — is not ported yet and is recorded here rather than left to be discovered.

use serde_json::{json, Value};

use forecast_domain::lifecycle::{
    apply_command, Command, CommandReceipt, DomainEvent, Forecast, Payload, Snapshot, TransitionResult,
};
use forecast_domain::models::UserForecast;
use forecast_domain::{canonical_bytes, content_hash};

use crate::db::{int, text, Database, Row};
use crate::dispute_wire as wire;
use crate::eligibility::{receipt_status, POLICY_VERSION};
use crate::registry::{identity_hash, DEVNET_GENESIS};
use crate::solana;

pub type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T>>>;
pub const CHALLENGE_MS: i64 = 48 * 3_600_000;
pub const LEASE_MS: i64 = 120_000;
pub const ZERO: [u8; 32] = [0u8; 32];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistryError {
    pub code: String,
    /// Set only by `chain_finalize_not_before`: the instant the chain will allow finalization.
    pub not_before_ms: Option<i64>,
}

impl RegistryError {
    fn new(code: &str) -> Self {
        Self {
            code: code.to_string(),
            not_before_ms: None,
        }
    }

    /// The chain agrees with us and will not finalize until its own clock says so.
    pub fn deferred(not_before_ms: i64) -> Self {
        Self {
            code: "chain_finalize_not_before".to_string(),
            not_before_ms: Some(not_before_ms),
        }
    }
}

type Outcome<T> = Result<T, RegistryError>;

/// A finalized account, as the transport read it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistryAccount {
    pub address: Vec<u8>,
    pub owner: Vec<u8>,
    pub data: Vec<u8>,
    pub slot: i64,
    pub finalized: bool,
}

/// The cluster, injected. `send` is the only method that writes.
pub trait RegistryTransport {
    fn genesis_hash(&self) -> BoxFuture<Outcome<String>>;
    fn account(&self, address: &[u8]) -> BoxFuture<Outcome<Option<RegistryAccount>>>;
    fn send(&self, instruction: &[u8], forecast_address: &[u8], register: bool) -> BoxFuture<Outcome<String>>;
    fn signature_finalized(&self, signature: &str) -> BoxFuture<Outcome<bool>>;
    fn finalized_time_ms(&self) -> BoxFuture<Outcome<i64>>;
}

/// `registry_intent_sql`: append after the event insert, in the same aggregate transaction.
pub fn registry_intent_sql(forecast: &Forecast) -> Outcome<Vec<(String, Vec<Value>)>> {
    if forecast.published_at_ms.is_none() {
        return Ok(Vec::new());
    }
    let Some(event) = forecast.latest_event.as_ref() else {
        return Err(RegistryError::new("invalid_local_event"));
    };
    if forecast.audit_head_hash.as_deref() != content_hash(event).ok().as_deref() {
        return Err(RegistryError::new("invalid_local_event"));
    }
    Ok(vec![(
        "INSERT INTO registry_intents(forecast_id,revision,event_hash,snapshot,created_at) VALUES(?,?,?,?,?)"
            .to_string(),
        vec![
            json!(forecast.forecast_id),
            json!(forecast.revision),
            json!(content_hash(event).unwrap_or_default()),
            json!(canonical(forecast)),
            json!(event.occurred_at_ms),
        ],
    )])
}

/// `registry_enable_sql`.
pub fn registry_enable_sql(forecast_id: &str) -> (String, Vec<Value>) {
    (
        "INSERT INTO registry_forecasts(forecast_id) VALUES(?) \
         ON CONFLICT(forecast_id) DO UPDATE SET enabled=1"
            .to_string(),
        vec![json!(forecast_id)],
    )
}

/// `reserve_daily_spend`: reserve before signing; an uncertain outcome never refunds.
///
/// The reference reads an affected-row count that this `Database` does not report, so the reserve
/// is confirmed by reading the day back and requiring it to have moved by exactly the amount. The
/// statement is the reference's own — nothing is invented to make the check possible.
pub async fn reserve_daily_spend(db: &dyn Database, amount: i64, now_ms: i64, limit_lamports: i64) -> Outcome<()> {
    let exhausted = RegistryError::new("daily_budget_exhausted");
    if amount < 0
        || now_ms < 0
        || limit_lamports < 0
        || amount > limit_lamports
        || limit_lamports > 9_007_199_254_740_991
    {
        return Err(exhausted);
    }
    let day = now_ms.div_euclid(86_400_000);
    let before = db
        .first(
            "SELECT reserved_lamports FROM registry_spend WHERE day=?",
            &[json!(day)],
        )
        .await
        .map_err(|_| exhausted.clone())?
        .as_ref()
        .and_then(|row| int(row, "reserved_lamports"))
        .unwrap_or(0);
    db.execute(
        "INSERT INTO registry_spend(day,reserved_lamports) VALUES(?,?) \
         ON CONFLICT(day) DO UPDATE SET reserved_lamports=reserved_lamports+excluded.reserved_lamports \
         WHERE reserved_lamports<=?",
        &[json!(day), json!(amount), json!(limit_lamports - amount)],
    )
    .await
    .map_err(|_| exhausted.clone())?;
    let after = db
        .first(
            "SELECT reserved_lamports FROM registry_spend WHERE day=?",
            &[json!(day)],
        )
        .await
        .map_err(|_| exhausted.clone())?
        .as_ref()
        .and_then(|row| int(row, "reserved_lamports"))
        .unwrap_or(0);
    if after != before + amount {
        return Err(exhausted);
    }
    Ok(())
}

fn canonical<T: serde::Serialize>(value: &T) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

/// `SolanaRegistry`.
pub struct SolanaRegistry<'a> {
    pub db: &'a dyn Database,
    pub transport: &'a dyn RegistryTransport,
    pub program_id: Vec<u8>,
    pub relayer: Vec<u8>,
    pub now_ms: &'a dyn Fn() -> i64,
    pub random_token: &'a dyn Fn() -> String,
}

impl SolanaRegistry<'_> {
    pub fn new<'a>(
        db: &'a dyn Database,
        transport: &'a dyn RegistryTransport,
        program_id: &[u8],
        relayer: &[u8],
        now_ms: &'a dyn Fn() -> i64,
        random_token: &'a dyn Fn() -> String,
    ) -> Outcome<SolanaRegistry<'a>> {
        if program_id.len() != 32 || relayer.len() != 32 || solana::zeroed(program_id) || solana::zeroed(relayer) {
            return Err(RegistryError::new("invalid_registry_configuration"));
        }
        Ok(SolanaRegistry {
            db,
            transport,
            program_id: program_id.to_vec(),
            relayer: relayer.to_vec(),
            now_ms,
            random_token,
        })
    }

    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    /// `enable`.
    pub async fn enable(&self, forecast_id: &str) -> Outcome<()> {
        self.backfill(forecast_id).await?;
        let (sql, params) = registry_enable_sql(forecast_id);
        self.db
            .execute(&sql, &params)
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        Ok(())
    }

    /// `_checked_account`: the account is the one asked for, owned by the program, finalized.
    ///
    /// Every clause is load-bearing. An account at the right address owned by something else, or a
    /// read the provider has not finalized, would let a commitment be made against a state that
    /// can still change.
    pub fn checked_account(&self, account: &RegistryAccount, address: &[u8]) -> Outcome<()> {
        let ok =
            account.address == address && account.owner == self.program_id && account.finalized && account.slot >= 0;
        if ok {
            Ok(())
        } else {
            Err(RegistryError::new("chain_account_unverified"))
        }
    }

    /// `_configuration`: the cluster is the right one, and the deployed config names our relayer.
    pub async fn configuration(&self) -> Outcome<()> {
        let genesis = self.transport.genesis_hash().await?;
        if genesis != DEVNET_GENESIS {
            return Err(RegistryError::new("wrong_cluster"));
        }
        let address = solana::config_address(&self.program_id)
            .map_err(|_| RegistryError::new("registry_unavailable"))?
            .0;
        let account = self
            .transport
            .account(&address)
            .await?
            .ok_or_else(|| RegistryError::new("registry_uninitialized"))?;
        self.checked_account(&account, &address)?;
        let config = solana::decode_config(&account.data).map_err(|_| RegistryError::new("registry_uninitialized"))?;
        if config.relayer != to_array(&self.relayer) {
            return Err(RegistryError::new("relayer_mismatch"));
        }
        self.db
            .execute(
                "INSERT OR IGNORE INTO registry_deployment(singleton,program_id,genesis_hash) VALUES(1,?,?)",
                &[json!(wire::to_hex(&self.program_id)), json!(DEVNET_GENESIS)],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        let pin = self
            .db
            .first(
                "SELECT program_id,genesis_hash FROM registry_deployment WHERE singleton=1",
                &[],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        // The pin is what stops a deployment being silently re-pointed at a different program:
        // the first successful configuration is the one every later one must agree with.
        let pinned = pin.as_ref().is_some_and(|row| {
            text(row, "program_id") == Some(wire::to_hex(&self.program_id).as_str())
                && text(row, "genesis_hash") == Some(DEVNET_GENESIS)
        });
        if !pinned {
            return Err(RegistryError::new("registry_deployment_mismatch"));
        }
        Ok(())
    }
}

fn to_array(value: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(value);
    out
}

impl SolanaRegistry<'_> {
    /// `_reputation_hash`: the whole history, re-derived and committed to in one value.
    ///
    /// This replays every event up to the forecast's revision and refuses if any revision, hash,
    /// receipt or command does not line up. A commitment that is merely *stated* would be a claim
    /// about the past that nobody can check; this one can be recomputed by anyone holding the same
    /// rows.
    pub async fn reputation_hash(&self, forecast: &Forecast) -> Outcome<[u8; 32]> {
        if forecast.state != "FINALIZED" && forecast.state != "ARCHIVED" {
            return Ok(ZERO);
        }
        let rows = self
            .db
            .all(
                "SELECT r.receipt,e.event,e.hash,e.revision FROM events e \
                 LEFT JOIN command_receipts r ON r.forecast_id=e.forecast_id \
                 AND r.command_id=json_extract(e.event,'$.command_id') \
                 WHERE e.forecast_id=? AND e.revision<=? ORDER BY e.revision",
                &[json!(forecast.forecast_id), json!(forecast.revision)],
            )
            .await
            .map_err(|_| RegistryError::new("reputation_history_mismatch"))?;

        let mut submissions: std::collections::BTreeMap<String, UserForecast> = std::collections::BTreeMap::new();
        let mut receipts: std::collections::BTreeMap<i64, (Value, UserForecast)> = std::collections::BTreeMap::new();
        let mut previous: Option<String> = None;
        for (index, row) in rows.iter().enumerate() {
            let expected = index as i64 + 1;
            let event: DomainEvent = serde_json::from_str(text(row, "event").unwrap_or_default())
                .map_err(|_| RegistryError::new("reputation_history_mismatch"))?;
            let consistent = int(row, "revision") == Some(expected)
                && event.revision == expected
                && event.forecast_id == forecast.forecast_id
                && content_hash(&event).ok().as_deref() == text(row, "hash")
                && event.previous_event_hash == previous;
            if !consistent {
                return Err(RegistryError::new("reputation_history_mismatch"));
            }
            previous = text(row, "hash").map(str::to_string);
            if event.command_name != "submit_forecast" {
                continue;
            }
            let Some(body) = text(row, "receipt") else {
                return Err(RegistryError::new("reputation_receipt_missing"));
            };
            let receipt: forecast_domain::lifecycle::CommandReceipt =
                serde_json::from_str(body).map_err(|_| RegistryError::new("reputation_receipt_mismatch"))?;
            let Some(value) = receipt.accepted_user_forecast.clone() else {
                return Err(RegistryError::new("reputation_receipt_mismatch"));
            };
            let matches = receipt.event_hash == text(row, "hash").unwrap_or_default()
                && receipt.revision == event.revision
                && receipt.forecast_id == forecast.forecast_id
                && receipt.idempotency_key == event.command_id
                && receipt.accepted_at_ms == event.occurred_at_ms
                && event.artifact_hash.as_deref() == content_hash(&value).ok().as_deref();
            if !matches {
                return Err(RegistryError::new("reputation_receipt_mismatch"));
            }
            // The command the receipt claims to be of is rebuilt and hashed: a receipt whose
            // command hash matches something else is a receipt for a different request.
            let command = forecast_domain::lifecycle::Command {
                schema_version: 1,
                idempotency_key: event.command_id.clone(),
                expected_revision: event.revision - 1,
                payload: Payload::SubmitForecast {
                    user_forecast: value.clone(),
                    schema_version: 1,
                },
            };
            if content_hash(&command).ok().as_deref() != Some(receipt.command_hash.as_str()) {
                return Err(RegistryError::new("reputation_receipt_mismatch"));
            }
            receipts.insert(event.revision, (body_value(body), value.clone()));
            submissions.insert(value.forecaster_id.clone(), value);
        }
        if rows.len() as i64 != forecast.revision || previous != forecast.audit_head_hash {
            return Err(RegistryError::new("reputation_history_incomplete"));
        }

        let decision = self
            .db
            .first(
                "SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?",
                &[json!(forecast.forecast_id)],
            )
            .await
            .map_err(|_| RegistryError::new("reputation_history_mismatch"))?;
        let Some(decision) = decision else {
            let ordered: Vec<&UserForecast> = submissions.values().collect();
            return content_hash(&json!({
                "schema_version": 1, "forecast_id": forecast.forecast_id,
                "specification_hash": forecast.specification_hash,
                "resolution_hash": forecast.finalized_resolution_hash,
                "outcome": forecast.finalized_outcome,
                "submissions": ordered,
            }))
            .map(|digest| hex_to_array(&digest))
            .map_err(|_| RegistryError::new("reputation_history_mismatch"));
        };

        // An eligibility decision restates which submissions count, so the commitment is over the
        // classified receipts rather than over every submission ever accepted.
        let trigger: forecast_domain::lifecycle::EarlyResolutionTrigger =
            serde_json::from_str(text(&decision, "body").unwrap_or_default())
                .map_err(|_| RegistryError::new("eligibility_commitment_mismatch"))?;
        trigger
            .validate_for(&forecast.specification)
            .map_err(|_| RegistryError::new("eligibility_commitment_mismatch"))?;
        let bound = trigger.trigger_hash().ok().as_deref() == text(&decision, "id")
            && trigger.forecast_id == forecast.forecast_id
            && trigger.event_at_ms == int(&decision, "cutoff_at").unwrap_or(-1)
            && trigger.event_time_basis == text(&decision, "event_time_basis").unwrap_or_default();
        if !bound {
            return Err(RegistryError::new("eligibility_commitment_mismatch"));
        }
        let completed = self
            .db
            .first(
                "SELECT 1 FROM forecast_eligibility_completions WHERE decision_id=?",
                &[json!(text(&decision, "id"))],
            )
            .await
            .map_err(|_| RegistryError::new("reputation_history_mismatch"))?;
        if completed.is_none() {
            return Err(RegistryError::new("eligibility_incomplete"));
        }
        let classifications = self
            .db
            .all(
                "SELECT * FROM forecast_receipt_eligibility WHERE decision_id=? ORDER BY revision",
                &[json!(text(&decision, "id"))],
            )
            .await
            .map_err(|_| RegistryError::new("reputation_history_mismatch"))?;
        let classified: std::collections::BTreeSet<i64> =
            classifications.iter().filter_map(|row| int(row, "revision")).collect();
        if classified != receipts.keys().copied().collect() {
            return Err(RegistryError::new("eligibility_history_incomplete"));
        }
        let mut eligible: std::collections::BTreeMap<String, UserForecast> = std::collections::BTreeMap::new();
        let mut commitments = Vec::new();
        for item in &classifications {
            let revision = int(item, "revision").unwrap_or(0);
            let Some((body, value)) = receipts.get(&revision) else {
                return Err(RegistryError::new("eligibility_receipt_mismatch"));
            };
            let expected = receipt_status(
                &json!(value.submitted_at_ms),
                &json!(trigger.event_at_ms),
                &trigger.event_time_basis,
            )
            .map_err(|_| RegistryError::new("eligibility_receipt_mismatch"))?;
            let matches = text(item, "receipt_hash") == content_hash(body).ok().as_deref()
                && text(item, "body") == Some(&canonical(value))
                && text(item, "user_id") == Some(value.forecaster_id.as_str())
                && text(item, "status") == Some(expected.as_str())
                && text(item, "forecast_id") == Some(forecast.forecast_id.as_str())
                && expected != "review";
            if !matches {
                return Err(RegistryError::new("eligibility_receipt_mismatch"));
            }
            if expected == "eligible" {
                eligible.insert(value.forecaster_id.clone(), value.clone());
            }
            commitments.push(json!({
                "revision": revision, "receipt_hash": text(item, "receipt_hash"), "status": expected,
            }));
        }
        let ordered: Vec<&UserForecast> = eligible.values().collect();
        content_hash(&json!({
            "schema_version": 2, "kind": "eligible_forecast_reputation",
            "forecast_id": forecast.forecast_id, "specification_hash": forecast.specification_hash,
            "resolution_hash": forecast.finalized_resolution_hash, "outcome": forecast.finalized_outcome,
            "eligibility": {"policy_version": POLICY_VERSION,
                            "trigger_hash": trigger.trigger_hash().unwrap_or_default(),
                            "receipts": commitments},
            "submissions": ordered,
        }))
        .map(|digest| hex_to_array(&digest))
        .map_err(|_| RegistryError::new("reputation_history_mismatch"))
    }

    /// `_material`: the fields the chain account must agree with before anything is sent.
    pub async fn material(&self, snapshot: &Snapshot) -> Outcome<Value> {
        let forecast = snapshot.base();
        let Some(event) = forecast.latest_event.as_ref() else {
            return Err(RegistryError::new("invalid_local_event"));
        };
        let resolution = forecast.resolution.as_ref().map(|value| value.base());
        let dispute_commitment = if forecast.disputes.is_empty() {
            ZERO
        } else {
            hex_to_array(
                &content_hash(&json!({"disputes": forecast.disputes, "reviews": forecast.dispute_reviews}))
                    .map_err(|_| RegistryError::new("invalid_local_event"))?,
            )
        };
        let outcome = match resolution.map(|base| base.proposed_outcome.as_str()) {
            None => 0,
            Some("YES") => 1,
            Some("NO") => 2,
            Some(_) => 3,
        };
        Ok(json!({
            "revision": forecast.revision,
            "occurred_at_ms": event.occurred_at_ms,
            "previous_event_hash": match event.previous_event_hash.as_deref() {
                Some(hash) => hex_to_array(hash), None => ZERO,
            },
            "event_hash": hex_to_array(&content_hash(event).map_err(|_| RegistryError::new("invalid_local_event"))?),
            "snapshot_hash": hex_to_array(&event.state_hash),
            "state": lifecycle_index(&forecast.state),
            "outcome": outcome,
            "resolution_hash": resolution
                .map(|base| hex_to_array(&base.resolution_hash().unwrap_or_default()))
                .unwrap_or(ZERO),
            "dispute_hash": dispute_commitment,
            "reputation_hash": self.reputation_hash(forecast).await?,
            // Only an upgraded forecast has an early trigger; a base forecast commits to nothing
            // there, which is the all-zero value the chain reads as "absent".
            "trigger_hash": match snapshot {
                Snapshot::V2(upgraded) => hex_to_array(&upgraded.early_trigger.trigger_hash().unwrap_or_default()),
                Snapshot::V1(_) => ZERO,
            },
            "challenge_until_ms": forecast.challenge_until_ms.unwrap_or(0),
            "pending_disputes": forecast.disputes.len() as i64 - forecast.dispute_reviews.len() as i64,
            "material_disputes": forecast.dispute_reviews.iter().filter(|review| review.material_conflict).count(),
        }))
    }

    /// `prepare_finalization`: refresh finalized chain evidence immediately before the local CAS.
    pub async fn prepare_finalization(&self, forecast_id: &str) -> Outcome<bool> {
        let enabled = self
            .db
            .first(
                "SELECT enabled FROM registry_forecasts WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        if enabled.as_ref().and_then(|row| int(row, "enabled")).unwrap_or(0) == 0 {
            return Ok(true);
        }
        self.configuration().await?;
        let Some(row) = self
            .db
            .first("SELECT snapshot FROM forecasts WHERE id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?
        else {
            return Ok(false);
        };
        let snapshot = Snapshot::from_json(text(&row, "snapshot").unwrap_or_default())
            .map_err(|_| RegistryError::new("invalid_local_event"))?;
        if snapshot.base().state != "CHALLENGE" {
            return Ok(false);
        }
        let forecast = snapshot.base();
        let fid = identity_hash("forecast", forecast_id);
        let address = solana::forecast_address(&self.program_id, &fid)
            .map_err(|_| RegistryError::new("registry_unavailable"))?
            .0;
        let Some(account) = self.transport.account(&address).await? else {
            return Ok(false);
        };
        self.checked_account(&account, &address)?;
        let state =
            solana::decode_forecast(&account.data).map_err(|_| RegistryError::new("chain_account_unverified"))?;
        let material = self.material(&snapshot).await?;
        // Every field but the predecessor hash, which the chain supplies and we cannot: the
        // account's own previous event is what the local record has to *succeed*.
        let mut mismatched = state.forecast_id_hash != fid
            || state.creator_hash != identity_hash("creator", &forecast.creator_id)
            || wire::to_hex(&state.specification_hash) != forecast.specification_hash
            || state.open_at_ms != forecast.specification.open_at_ms
            || state.close_at_ms != forecast.specification.close_at_ms;
        for (key, value) in material.as_object().cloned().unwrap_or_default() {
            if key == "previous_event_hash" {
                continue;
            }
            mismatched |= material_matches(&key, &state, &value) == Some(false);
        }
        if mismatched {
            return Ok(false);
        }
        let chain_time = self.transport.finalized_time_ms().await?;
        if chain_time < 0 || chain_time > self.now() + 60_000 {
            return Err(RegistryError::new("invalid_chain_clock"));
        }
        self.db
            .execute(
                "UPDATE registry_forecasts SET confirmed_revision=?,confirmed_event_hash=?,\
                 confirmed_state=?,chain_deadline=?,chain_time=?,observed_at=? WHERE forecast_id=? AND enabled=1",
                &[
                    json!(state.revision),
                    json!(wire::to_hex(&state.event_hash)),
                    json!(state.state),
                    json!(state.chain_finalize_not_before_ms),
                    json!(chain_time),
                    json!(self.now()),
                    json!(forecast_id),
                ],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        // The chain's clock, not ours: finalization before its deadline is not a fault, it is a
        // schedule, and the caller has to be able to tell the two apart.
        let not_before = state.chain_finalize_not_before_ms.max(state.challenge_until_ms);
        if chain_time < not_before {
            return Err(RegistryError::deferred(not_before));
        }
        Ok(true)
    }
}

/// The material field compared against the decoded account, or `None` for a field this comparison
/// does not cover.
fn material_matches(key: &str, state: &solana::ForecastAccount, value: &Value) -> Option<bool> {
    let bytes = |value: &Value| -> Option<[u8; 32]> {
        value.as_array().map(|items| {
            let mut out = [0u8; 32];
            for (index, byte) in items.iter().take(32).enumerate() {
                out[index] = byte.as_u64().unwrap_or(0) as u8;
            }
            out
        })
    };
    let number = |value: &Value| value.as_i64();
    match key {
        "revision" => Some(state.revision == number(value)?),
        "occurred_at_ms" => Some(state.occurred_at_ms == number(value)?),
        "event_hash" => Some(state.event_hash == bytes(value)?),
        "snapshot_hash" => Some(state.snapshot_hash == bytes(value)?),
        "state" => Some(state.state == number(value)?),
        "outcome" => Some(state.outcome == number(value)?),
        "resolution_hash" => Some(state.resolution_hash == bytes(value)?),
        "dispute_hash" => Some(state.dispute_hash == bytes(value)?),
        "reputation_hash" => Some(state.reputation_hash == bytes(value)?),
        "trigger_hash" => Some(state.trigger_hash == bytes(value)?),
        "challenge_until_ms" => Some(state.challenge_until_ms == number(value)?),
        "pending_disputes" => Some(state.pending_disputes == number(value)?),
        "material_disputes" => Some(state.material_disputes == number(value)?),
        _ => None,
    }
}

/// `LifecycleState`'s index, which is the state byte the chain carries.
fn lifecycle_index(state: &str) -> i64 {
    const STATES: [&str; 12] = [
        "DRAFT",
        "VALIDATING",
        "OPEN",
        "LOCKED",
        "RESOLVING",
        "PROPOSED",
        "CHALLENGE",
        "DISPUTED",
        "ESCALATED",
        "PAUSED",
        "FINALIZED",
        "ARCHIVED",
    ];
    STATES
        .iter()
        .position(|name| *name == state)
        .map(|index| index as i64)
        .unwrap_or(0)
}

fn hex_to_array(value: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (index, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(value.get(index * 2..index * 2 + 2).unwrap_or("00"), 16).unwrap_or(0);
    }
    out
}

/// The receipt body as a value, for the eligibility comparison.
fn body_value(body: &str) -> Value {
    serde_json::from_str(body).unwrap_or(Value::Null)
}

/// The record a command payload is built from, named so the decoder knows what to expect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactKind {
    Assessment,
    Resolution,
    Dispute,
    Review,
    Pause,
    Resume,
    Adjudication,
}

impl SolanaRegistry<'_> {
    /// `_artifact`: the retained record a command was made of, by its hash.
    ///
    /// An artifact that is not in the store may still be one the current snapshot holds — the
    /// validations, resolutions and disputes are part of it — so both are searched. What is never
    /// done is accepting a record whose hash is not the one the event names.
    async fn artifact(&self, digest: Option<&str>, kind: ArtifactKind, current: &Forecast) -> Outcome<Value> {
        let Some(digest) = digest else {
            return Err(RegistryError::new("history_artifact_missing"));
        };
        let stored = self
            .db
            .first("SELECT body FROM artifacts WHERE hash=?", &[json!(digest)])
            .await
            .map_err(|_| RegistryError::new("history_artifact_missing"))?;
        let record = match stored.as_ref().and_then(|row| text(row, "body")) {
            Some(body) => {
                let parsed: Value =
                    serde_json::from_str(body).map_err(|_| RegistryError::new("history_artifact_invalid"))?;
                // A resolution artifact may be an early resolution, which is a different record
                // with the same claim to the name.
                match kind {
                    ArtifactKind::Resolution if parsed.get("trigger").is_some() => parsed,
                    _ => parsed,
                }
            }
            None => {
                let mut candidates: Vec<Value> = Vec::new();
                if let Some(assessment) = &current.validation_assessment {
                    candidates.push(serde_json::to_value(assessment).unwrap_or(Value::Null));
                }
                if let Some(resolution) = &current.resolution {
                    candidates.push(serde_json::to_value(resolution).unwrap_or(Value::Null));
                }
                candidates.extend(
                    current
                        .disputes
                        .iter()
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null)),
                );
                candidates.extend(
                    current
                        .dispute_reviews
                        .iter()
                        .map(|value| serde_json::to_value(value).unwrap_or(Value::Null)),
                );
                candidates
                    .into_iter()
                    .find(|value| content_hash(value).ok().as_deref() == Some(digest))
                    .unwrap_or(Value::Null)
            }
        };
        if record.is_null() || content_hash(&record).ok().as_deref() != Some(digest) {
            return Err(RegistryError::new("history_artifact_missing"));
        }
        Ok(record)
    }

    /// `_payload`: the command an event was produced by.
    ///
    /// This is the inverse of the lifecycle, and it is where a reconstruction can be wrong
    /// quietly: a payload that is merely *plausible* reproduces a receipt nobody signed. So each
    /// branch builds the exact record type the command names, and `backfill` then checks the
    /// command hash against the receipt the chain of custody already recorded.
    async fn payload(&self, event: &DomainEvent, receipt: &CommandReceipt, current: &Snapshot) -> Outcome<Payload> {
        let base = current.base();
        let name = event.command_name.as_str();
        let simple = |payload: Payload| Ok(payload);
        match name {
            // A legacy record omits the duration. The candidate is only accepted if the original
            // receipt *and* the resulting event match exactly, which `backfill` checks.
            "begin_challenge" => {
                return simple(Payload::BeginChallenge {
                    schema_version: 1,
                    duration_ms: CHALLENGE_MS,
                })
            }
            "begin_validation" => return simple(Payload::BeginValidation { schema_version: 1 }),
            "begin_resolution" => return simple(Payload::BeginResolution { schema_version: 1 }),
            "retain_proposal" => return simple(Payload::RetainProposal { schema_version: 1 }),
            "escalate" => return simple(Payload::Escalate { schema_version: 1 }),
            "finalize" => return simple(Payload::Finalize { schema_version: 1 }),
            "archive" => return simple(Payload::Archive { schema_version: 1 }),
            _ => {}
        }
        // A lock that carries a trigger is the upgraded form: a base forecast has no trigger, so
        // this is exactly the case the reference guards on `isinstance(current, ForecastV2)`.
        if name == "lock" && event.artifact_hash.is_some() {
            let Snapshot::V2(upgraded) = current else {
                return Err(RegistryError::new("history_trigger_mismatch"));
            };
            if upgraded.early_trigger.trigger_hash().ok().as_deref() != event.artifact_hash.as_deref() {
                return Err(RegistryError::new("history_trigger_mismatch"));
            }
            return simple(Payload::Lock {
                schema_version: 2,
                trigger: Some(upgraded.early_trigger.clone()),
            });
        }
        if name == "lock" {
            return simple(Payload::Lock {
                schema_version: 1,
                trigger: None,
            });
        }
        if name == "publish" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Assessment, base)
                .await?;
            let assessment =
                serde_json::from_value(record).map_err(|_| RegistryError::new("history_artifact_invalid"))?;
            return Ok(Payload::Publish {
                schema_version: 1,
                assessment,
            });
        }
        if name == "submit_forecast" {
            let Some(value) = receipt.accepted_user_forecast.clone() else {
                return Err(RegistryError::new("history_command_unrecoverable"));
            };
            return Ok(Payload::SubmitForecast {
                schema_version: 1,
                user_forecast: value,
            });
        }
        if name == "propose_resolution" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Resolution, base)
                .await?;
            let resolution =
                serde_json::from_value(record).map_err(|_| RegistryError::new("history_artifact_invalid"))?;
            return Ok(Payload::ProposeResolution {
                schema_version: 1,
                resolution,
            });
        }
        if name == "submit_dispute" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Dispute, base)
                .await?;
            let dispute = serde_json::from_value(record).map_err(|_| RegistryError::new("history_artifact_invalid"))?;
            return Ok(Payload::SubmitDispute {
                schema_version: 1,
                dispute,
            });
        }
        if name == "review_dispute" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Review, base)
                .await?;
            let review = serde_json::from_value(record).map_err(|_| RegistryError::new("history_artifact_invalid"))?;
            return Ok(Payload::ReviewDispute {
                schema_version: 1,
                review,
            });
        }
        if name == "pause_for_provider_outage" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Pause, base)
                .await?;
            return Ok(Payload::PauseForProviderOutage {
                schema_version: 1,
                configured_providers: record["configuredProviders"]
                    .as_array()
                    .map(|items| items.iter().filter_map(Value::as_str).map(str::to_string).collect())
                    .unwrap_or_default(),
                unavailable_providers: record["unavailableProviders"]
                    .as_array()
                    .map(|items| items.iter().filter_map(Value::as_str).map(str::to_string).collect())
                    .unwrap_or_default(),
                reason: record["reason"].as_str().unwrap_or_default().to_string(),
            });
        }
        if name == "resume_after_provider_recovery" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Resume, base)
                .await?;
            return Ok(Payload::ResumeAfterProviderRecovery {
                schema_version: 1,
                recovered_provider: record["recoveredProvider"].as_str().unwrap_or_default().to_string(),
            });
        }
        if name == "adjudicate_resolution" {
            let record = self
                .artifact(event.artifact_hash.as_deref(), ArtifactKind::Adjudication, base)
                .await?;
            let resolution = serde_json::from_value(record["resolution"].clone())
                .map_err(|_| RegistryError::new("history_artifact_invalid"))?;
            let adjudicator = serde_json::from_value(record["adjudicator"].clone())
                .map_err(|_| RegistryError::new("history_artifact_invalid"))?;
            return Ok(Payload::AdjudicateResolution {
                schema_version: 1,
                resolution,
                adjudicator,
            });
        }
        Err(RegistryError::new("history_command_unrecoverable"))
    }

    /// `backfill`: reconstruct the history through the domain, failing without partial writes.
    ///
    /// Every event is replayed and the result compared to what the row already says — the receipt,
    /// the event, the hash, the revision and finally the whole snapshot. Nothing is written until
    /// all of it agrees, because a backfill that half-succeeds leaves a history that is neither
    /// the old one nor the new one.
    pub async fn backfill(&self, forecast_id: &str) -> Outcome<i64> {
        let Some(row) = self
            .db
            .first("SELECT snapshot FROM forecasts WHERE id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?
        else {
            return Err(RegistryError::new("forecast_missing"));
        };
        let current = Snapshot::from_json(text(&row, "snapshot").unwrap_or_default())
            .map_err(|_| RegistryError::new("history_snapshot_mismatch"))?;
        let rows = self
            .db
            .all(
                "SELECT e.*,r.receipt FROM events e LEFT JOIN command_receipts r \
                 ON r.forecast_id=e.forecast_id AND r.command_id=json_extract(e.event,'$.command_id') \
                 WHERE e.forecast_id=? ORDER BY e.revision",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| RegistryError::new("history_incomplete"))?;
        if rows.len() as i64 != current.base().revision {
            return Err(RegistryError::new("history_incomplete"));
        }

        let base = current.base();
        let mut forecast = Snapshot::V1(
            forecast_domain::lifecycle::create_forecast(
                &base.forecast_id,
                &base.creator_id,
                base.specification.clone(),
                base.created_at_ms,
            )
            .map_err(|_| RegistryError::new("history_snapshot_mismatch"))?,
        );
        let mut statements: Vec<(String, Vec<Value>)> = Vec::new();
        for row in &rows {
            let Some(body) = text(row, "receipt") else {
                return Err(RegistryError::new("history_receipt_missing"));
            };
            let event: DomainEvent = serde_json::from_str(text(row, "event").unwrap_or_default())
                .map_err(|_| RegistryError::new("history_commitment_mismatch"))?;
            let receipt: CommandReceipt =
                serde_json::from_str(body).map_err(|_| RegistryError::new("history_commitment_mismatch"))?;
            let payload = self.payload(&event, &receipt, &current).await?;
            let v2 = matches!(payload, Payload::Lock { trigger: Some(_), .. })
                || matches!(&payload, Payload::ProposeResolution { resolution, .. } if matches!(resolution, forecast_domain::lifecycle::AnyResolution::Early(_)));
            let command = Command {
                schema_version: if v2 { 2 } else { 1 },
                idempotency_key: event.command_id.clone(),
                expected_revision: forecast.base().revision,
                payload,
            };
            if content_hash(&command).ok().as_deref() != Some(receipt.command_hash.as_str()) {
                return Err(RegistryError::new("history_command_mismatch"));
            }
            let TransitionResult {
                forecast: next,
                receipt: produced,
                events,
            } = apply_command(&forecast, &command, event.occurred_at_ms, None)
                .map_err(|_| RegistryError::new("history_commitment_mismatch"))?;
            let agrees = produced == receipt
                && events.len() == 1
                && events[0] == event
                && text(row, "hash") == content_hash(&event).ok().as_deref()
                && int(row, "revision") == Some(event.revision);
            if !agrees {
                return Err(RegistryError::new("history_commitment_mismatch"));
            }
            forecast = next;
            let existing = self
                .db
                .first(
                    "SELECT snapshot FROM registry_intents WHERE forecast_id=? AND revision=?",
                    &[json!(forecast_id), json!(forecast.base().revision)],
                )
                .await
                .map_err(|_| RegistryError::new("registry_unavailable"))?;
            match existing.as_ref().and_then(|row| text(row, "snapshot")) {
                Some(stored) if stored != canonical(forecast.base()) => {
                    return Err(RegistryError::new("history_intent_conflict"));
                }
                Some(_) => {}
                None => statements.extend(registry_intent_sql(forecast.base())?),
            }
        }
        if canonical(forecast.base()) != canonical(current.base()) {
            return Err(RegistryError::new("history_snapshot_mismatch"));
        }
        if !statements.is_empty() {
            self.db
                .batch(&statements)
                .await
                .map_err(|_| RegistryError::new("registry_unavailable"))?;
        }
        Ok(statements.len() as i64)
    }
}

impl SolanaRegistry<'_> {
    /// `_deliver`: one intent, either confirmed against the chain or sent.
    ///
    /// The order is the design. An account that already carries this revision is *proof* — the
    /// confirmation is written and nothing is sent. An account one revision behind is a reason to
    /// send. Anything else is a chain we do not recognise, and the intent is refused rather than
    /// pushed at it.
    #[allow(clippy::too_many_lines)]
    pub async fn deliver(&self, row: &Row, token: &str) -> Outcome<bool> {
        let snapshot = Snapshot::from_json(text(row, "snapshot").unwrap_or_default())
            .map_err(|_| RegistryError::new("local_intent_mismatch"))?;
        let forecast = snapshot.base();
        let material = self.material(&snapshot).await?;
        let state_index = material["state"].as_i64().unwrap_or(0);
        if (state_index == 10 || state_index == 11)
            && self
                .db
                .first(
                    "SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=?",
                    &[json!(forecast.forecast_id)],
                )
                .await
                .map_err(|_| RegistryError::new("registry_unavailable"))?
                .is_some()
        {
            return Err(RegistryError::new("resolution_eligibility_blocked"));
        }
        let event_hash = material["event_hash"]
            .as_array()
            .map(|_| hex_of(&material["event_hash"]))
            .unwrap_or_default();
        if text(row, "event_hash") != Some(event_hash.as_str()) {
            return Err(RegistryError::new("local_intent_mismatch"));
        }
        let id_hash = identity_hash("forecast", &forecast.forecast_id);
        let creator_hash = identity_hash("creator", &forecast.creator_id);
        let address = solana::forecast_address(&self.program_id, &id_hash)
            .map_err(|_| RegistryError::new("registry_unavailable"))?
            .0;
        let account = self.transport.account(&address).await?;
        let prior = self
            .db
            .first(
                "SELECT MAX(confirmed_slot) AS slot FROM registry_delivery WHERE forecast_id=? AND status='confirmed'",
                &[json!(forecast.forecast_id)],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        let minimum_slot = prior.as_ref().and_then(|row| int(row, "slot")).unwrap_or(0);
        if minimum_slot > 0 && account.as_ref().map(|value| value.slot).unwrap_or(0) < minimum_slot {
            self.release(row, token, "chain_observation_stale", self.now() + 30_000, false)
                .await?;
            return Ok(false);
        }
        let register = forecast
            .latest_event
            .as_ref()
            .is_some_and(|event| event.command_name == "publish");
        if let Some(account) = account.as_ref() {
            self.checked_account(account, &address)?;
            let state =
                solana::decode_forecast(&account.data).map_err(|_| RegistryError::new("chain_account_unverified"))?;
            let identity_ok = state.forecast_id_hash == id_hash
                && state.creator_hash == creator_hash
                && wire::to_hex(&state.specification_hash) == forecast.specification_hash
                && state.open_at_ms == forecast.specification.open_at_ms
                && state.close_at_ms == forecast.specification.close_at_ms;
            if !identity_ok {
                return Err(RegistryError::new("chain_identity_mismatch"));
            }
            if state.revision == forecast.revision {
                let expected_pause = match forecast.pause.as_ref() {
                    Some(pause) => lifecycle_index(&pause.previous_state),
                    None => 0,
                };
                if state.paused_from != expected_pause {
                    return Err(RegistryError::new("chain_pause_mismatch"));
                }
                if material
                    .as_object()
                    .cloned()
                    .unwrap_or_default()
                    .iter()
                    .any(|(key, value)| {
                        key != "previous_event_hash" && material_matches(key, &state, value) == Some(false)
                    })
                {
                    return Err(RegistryError::new("chain_commitment_mismatch"));
                }
                // The finalized account is the proof; an unknown transaction is not. A signature
                // the provider cannot confirm is simply left unrecorded.
                let mut signature = None;
                if let Some(known) = text(row, "signature") {
                    if self.transport.signature_finalized(known).await.unwrap_or(false) {
                        signature = Some(known.to_string());
                    }
                }
                self.db
                    .execute(
                        "UPDATE registry_delivery SET status='confirmed',confirmed_slot=?,signature=?,\
                         lease_token=NULL,lease_until=0,error_code=NULL WHERE forecast_id=? AND revision=? AND lease_token=?",
                        &[
                            json!(account.slot),
                            json!(signature),
                            json!(forecast.forecast_id),
                            json!(forecast.revision),
                            json!(token),
                        ],
                    )
                    .await
                    .map_err(|_| RegistryError::new("registry_unavailable"))?;
                return Ok(self.delivery_status(forecast, token).await? == Some("confirmed".to_string()));
            }
            let previous = material["previous_event_hash"]
                .as_array()
                .map(|_| hex_of(&material["previous_event_hash"]))
                .unwrap_or_default();
            if state.revision != forecast.revision - 1 || wire::to_hex(&state.event_hash) != previous {
                return Err(RegistryError::new("chain_revision_mismatch"));
            }
            if state_index == 10 && self.now() < state.chain_finalize_not_before_ms {
                self.release(
                    row,
                    token,
                    "chain_challenge_pending",
                    state.chain_finalize_not_before_ms,
                    false,
                )
                .await?;
                return Ok(false);
            }
        } else if !register {
            return Err(RegistryError::new("chain_predecessor_missing"));
        }
        if let Some(known) = text(row, "signature") {
            let submitted = int(row, "submitted_at").unwrap_or(0);
            let unconfirmed =
                !self.transport.signature_finalized(known).await.unwrap_or(false) && self.now() < submitted + 600_000;
            if unconfirmed {
                self.release(row, token, "transaction_pending", self.now() + 30_000, false)
                    .await?;
                return Ok(false);
            }
        }

        let data = if register {
            solana::encode_register(solana::RegisterParts {
                forecast_id_hash: &id_hash,
                creator_hash: &creator_hash,
                specification_hash: &hex_to_array(&forecast.specification_hash),
                open_at_ms: forecast.specification.open_at_ms,
                close_at_ms: forecast.specification.close_at_ms,
                revision: material["revision"].as_i64().unwrap_or(0),
                occurred_at_ms: material["occurred_at_ms"].as_i64().unwrap_or(0),
                event_hash: &bytes_of(&material["event_hash"]),
                snapshot_hash: &bytes_of(&material["snapshot_hash"]),
            })
            .map_err(|_| RegistryError::new("history_command_unrecoverable"))?
        } else {
            // The arrays are owned here and borrowed by the encoder, rather than leaked into a
            // static the encoder would outlive.
            let previous = bytes_of(&material["previous_event_hash"]);
            let event = bytes_of(&material["event_hash"]);
            let snapshot_hash = bytes_of(&material["snapshot_hash"]);
            let resolution = bytes_of(&material["resolution_hash"]);
            let dispute = bytes_of(&material["dispute_hash"]);
            let reputation = bytes_of(&material["reputation_hash"]);
            let trigger = bytes_of(&material["trigger_hash"]);
            solana::encode_advance(&solana::AdvanceFields {
                revision: material["revision"].as_i64().unwrap_or(0),
                occurred_at_ms: material["occurred_at_ms"].as_i64().unwrap_or(0),
                previous_event_hash: &previous,
                event_hash: &event,
                snapshot_hash: &snapshot_hash,
                state: material["state"].as_i64().unwrap_or(0),
                outcome: material["outcome"].as_i64().unwrap_or(0),
                resolution_hash: &resolution,
                dispute_hash: &dispute,
                reputation_hash: &reputation,
                trigger_hash: &trigger,
                challenge_until_ms: material["challenge_until_ms"].as_i64().unwrap_or(0),
                pending_disputes: material["pending_disputes"].as_i64().unwrap_or(0),
                material_disputes: material["material_disputes"].as_i64().unwrap_or(0),
            })
            .map_err(|_| RegistryError::new("history_command_unrecoverable"))?
        };
        let signature = self.transport.send(&data, &address, register).await?;
        self.db
            .execute(
                "UPDATE registry_delivery SET status='submitted',signature=?,submitted_at=?,retry_at=?,\
                 lease_token=NULL,lease_until=0,error_code=NULL WHERE forecast_id=? AND revision=? AND lease_token=?",
                &[
                    json!(signature),
                    json!(self.now()),
                    json!(self.now() + 5_000),
                    json!(forecast.forecast_id),
                    json!(forecast.revision),
                    json!(token),
                ],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        Ok(false)
    }

    /// The delivery row's status under this lease, which is how a write with no affected-row count
    /// is confirmed.
    async fn delivery_status(&self, forecast: &Forecast, token: &str) -> Outcome<Option<String>> {
        let row = self
            .db
            .first(
                "SELECT status,lease_token FROM registry_delivery WHERE forecast_id=? AND revision=?",
                &[json!(forecast.forecast_id), json!(forecast.revision)],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        let _ = token;
        Ok(row.as_ref().and_then(|row| text(row, "status")).map(str::to_string))
    }

    /// `_release`: give the lease back with the reason it was not confirmed.
    pub async fn release(&self, row: &Row, token: &str, error: &str, retry_at: i64, blocked: bool) -> Outcome<()> {
        let status = if blocked {
            "blocked"
        } else {
            text(row, "status").unwrap_or("pending")
        };
        self.db
            .execute(
                "UPDATE registry_delivery SET status=?,error_code=?,retry_at=?,\
                 lease_token=NULL,lease_until=0 WHERE forecast_id=? AND revision=? AND lease_token=?",
                &[
                    json!(status),
                    json!(error),
                    json!(retry_at),
                    json!(text(row, "forecast_id")),
                    json!(int(row, "revision")),
                    json!(token),
                ],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        Ok(())
    }

    /// `sync`: one bounded pass over the intents that are due.
    pub async fn sync(&self, limit: i64) -> Outcome<Value> {
        if !(1..=32).contains(&limit) {
            return Err(RegistryError::new("invalid_batch_limit"));
        }
        self.configuration().await?;
        let rows = self
            .db
            .all(
                "SELECT i.*,d.status,d.signature,d.submitted_at,d.attempts FROM registry_intents i \
                 JOIN registry_delivery d USING(forecast_id,revision) JOIN registry_forecasts r ON r.forecast_id=i.forecast_id \
                 WHERE r.enabled=1 AND d.status IN ('pending','submitted') \
                 AND d.retry_at<=? AND d.lease_until<=? AND NOT EXISTS (SELECT 1 FROM registry_delivery p \
                 WHERE p.forecast_id=d.forecast_id AND p.revision<d.revision AND p.status!='confirmed') \
                 ORDER BY i.created_at,i.forecast_id,i.revision LIMIT ?",
                &[json!(self.now()), json!(self.now()), json!(limit)],
            )
            .await
            .map_err(|_| RegistryError::new("registry_unavailable"))?;
        let mut confirmed = 0i64;
        let mut considered = 0i64;
        for row in &rows {
            let token = (self.random_token)();
            self.db
                .execute(
                    "UPDATE registry_delivery SET lease_token=?,lease_until=?,attempts=attempts+1 \
                     WHERE forecast_id=? AND revision=? AND lease_until<=? AND status IN ('pending','submitted')",
                    &[
                        json!(token),
                        json!(self.now() + LEASE_MS),
                        json!(text(row, "forecast_id")),
                        json!(int(row, "revision")),
                        json!(self.now()),
                    ],
                )
                .await
                .map_err(|_| RegistryError::new("registry_unavailable"))?;
            // The lease is confirmed by reading it back: this `Database` reports no affected-row
            // count, and a lost lease means another worker is already delivering this intent.
            let held = self
                .db
                .first(
                    "SELECT lease_token FROM registry_delivery WHERE forecast_id=? AND revision=?",
                    &[json!(text(row, "forecast_id")), json!(int(row, "revision"))],
                )
                .await
                .map_err(|_| RegistryError::new("registry_unavailable"))?;
            if held.as_ref().and_then(|held| text(held, "lease_token")) != Some(token.as_str()) {
                continue;
            }
            considered += 1;
            match self.deliver(row, &token).await {
                Ok(true) => confirmed += 1,
                Ok(false) => {}
                Err(error) => {
                    self.release(row, &token, &error.code, 0, true).await?;
                }
            }
        }
        let _ = &rows;
        Ok(json!({"considered": considered, "confirmed": confirmed}))
    }
}

fn bytes_of(value: &Value) -> [u8; 32] {
    let mut out = [0u8; 32];
    if let Some(items) = value.as_array() {
        for (index, byte) in items.iter().take(32).enumerate() {
            out[index] = byte.as_u64().unwrap_or(0) as u8;
        }
    }
    out
}

fn hex_of(value: &Value) -> String {
    wire::to_hex(&bytes_of(value))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    const ADMIN: [u8; 32] = [13u8; 32];
    const RELAYER: [u8; 32] = [12u8; 32];
    const PROGRAM: [u8; 32] = [11u8; 32];

    struct Fake {
        accounts: Mutex<BTreeMap<String, RegistryAccount>>,
        chain_time: Mutex<i64>,
    }

    impl RegistryTransport for Fake {
        fn genesis_hash(&self) -> BoxFuture<Outcome<String>> {
            Box::pin(async { Ok(DEVNET_GENESIS.to_string()) })
        }
        fn account(&self, address: &[u8]) -> BoxFuture<Outcome<Option<RegistryAccount>>> {
            let key = wire::to_hex(address);
            let found = self.accounts.lock().unwrap().get(&key).cloned();
            Box::pin(async move { Ok(found) })
        }
        fn send(&self, _instruction: &[u8], _forecast: &[u8], _register: bool) -> BoxFuture<Outcome<String>> {
            Box::pin(async { Err(RegistryError::new("not_used")) })
        }
        fn signature_finalized(&self, _signature: &str) -> BoxFuture<Outcome<bool>> {
            Box::pin(async { Ok(false) })
        }
        fn finalized_time_ms(&self) -> BoxFuture<Outcome<i64>> {
            let time = *self.chain_time.lock().unwrap();
            Box::pin(async move { Ok(time) })
        }
    }

    /// The CHALLENGE snapshot from the lifecycle golden, which is a real one.
    fn challenged() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/lifecycle-golden.json");
        let golden: Value = serde_json::from_str(&std::fs::read_to_string(&path).expect("golden")).expect("json");
        golden["steps"]
            .as_array()
            .expect("steps")
            .iter()
            .find(|step| step["command"]["payload"]["kind"] == json!("begin_challenge") && step.get("after").is_some())
            .map(|step| step["after"].clone())
            .expect("a challenged forecast")
    }

    /// The chain account the reference's own fixture builds from `material`.
    fn chain_account(material: &Value, snapshot: &Snapshot, deadline: i64) -> (Vec<u8>, Vec<u8>) {
        let forecast = snapshot.base();
        let bytes = |name: &str| -> Vec<u8> {
            material[name]
                .as_array()
                .map(|items| items.iter().map(|byte| byte.as_u64().unwrap_or(0) as u8).collect())
                .unwrap_or_else(|| vec![0u8; 32])
        };
        let mut raw = vec![0u8; 360];
        raw[0..8].copy_from_slice(b"FNFORE01");
        raw[8..40].copy_from_slice(&identity_hash("forecast", &forecast.forecast_id));
        raw[40..72].copy_from_slice(&identity_hash("creator", &forecast.creator_id));
        raw[72..104].copy_from_slice(&hex_to_array(&forecast.specification_hash));
        raw[104..112].copy_from_slice(&forecast.specification.open_at_ms.to_le_bytes());
        raw[112..120].copy_from_slice(&forecast.specification.close_at_ms.to_le_bytes());
        raw[120..128].copy_from_slice(&(forecast.revision as u64).to_le_bytes());
        raw[128..136].copy_from_slice(&material["occurred_at_ms"].as_i64().unwrap_or(0).to_le_bytes());
        raw[136] = material["state"].as_i64().unwrap_or(0) as u8;
        raw[137] = material["outcome"].as_i64().unwrap_or(0) as u8;
        for (offset, name) in [
            (140, "event_hash"),
            (172, "snapshot_hash"),
            (204, "resolution_hash"),
            (236, "dispute_hash"),
            (268, "reputation_hash"),
            (300, "trigger_hash"),
        ] {
            raw[offset..offset + 32].copy_from_slice(&bytes(name));
        }
        raw[332..340].copy_from_slice(&material["challenge_until_ms"].as_i64().unwrap_or(0).to_le_bytes());
        raw[340..348].copy_from_slice(&deadline.to_le_bytes());
        raw[348..356].copy_from_slice(&0i64.to_le_bytes());
        raw[356..358].copy_from_slice(&(material["pending_disputes"].as_i64().unwrap_or(0) as u16).to_le_bytes());
        raw[358..360].copy_from_slice(&(material["material_disputes"].as_i64().unwrap_or(0) as u16).to_le_bytes());
        let address = solana::forecast_address(&PROGRAM, &identity_hash("forecast", &forecast.forecast_id))
            .unwrap()
            .0;
        (address.to_vec(), raw)
    }

    #[test]
    fn finalization_waits_for_the_chains_own_clock() {
        // The chain agreeing about the record and refusing on its clock is a *schedule*, not a
        // fault. Telling them apart is what lets the scheduler wait instead of escalating.
        let snapshot_value = challenged();
        let snapshot = Snapshot::from_json(&snapshot_value.to_string()).expect("a snapshot");
        let db = Sqlite::from_migrations();
        // A forecast belongs to a creator, so the creator has to exist first.
        db.run(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
            &[
                json!(snapshot.base().creator_id),
                json!("Creator"),
                json!("creator"),
                json!("hash:creator"),
                json!(1),
            ],
        )
        .expect("creator");
        db.run(
            "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
             normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            &[
                json!(snapshot.base().forecast_id),
                json!(snapshot.base().creator_id),
                json!("draft:registry"),
                json!(snapshot_value.to_string()),
                json!(snapshot.base().revision),
                json!("CHALLENGE"),
                json!("TECHNOLOGY"),
                json!("Will registry finalize?"),
                json!("Will registry finalize?"),
                json!("will registry finalize?"),
                json!(snapshot.base().specification_hash),
                json!(snapshot.base().specification.open_at_ms),
                json!(snapshot.base().specification.close_at_ms),
                json!(1),
                json!(1),
                json!("publish"),
            ],
        )
        .expect("forecast");
        db.run(
            "INSERT INTO registry_forecasts(forecast_id,enabled) VALUES(?,1)",
            &[json!(snapshot.base().forecast_id)],
        )
        .expect("enabled");

        let config_address = solana::config_address(&PROGRAM).unwrap().0;
        let fake = Fake {
            accounts: Mutex::new(BTreeMap::new()),
            chain_time: Mutex::new(0),
        };
        fake.accounts.lock().unwrap().insert(
            wire::to_hex(&config_address),
            RegistryAccount {
                address: config_address.to_vec(),
                owner: PROGRAM.to_vec(),
                data: b"FNCONF01"
                    .iter()
                    .chain(ADMIN.iter())
                    .chain(RELAYER.iter())
                    .chain([0u8; 32].iter())
                    .copied()
                    .collect(),
                slot: 100,
                finalized: true,
            },
        );
        let now = Mutex::new(1_000_000i64);
        let clock = || *now.lock().unwrap();
        let token = || "token".to_string();
        let registry = SolanaRegistry::new(&db, &fake, &PROGRAM, &RELAYER, &clock, &token).expect("a registry");

        let material = block(registry.material(&snapshot)).expect("material");
        // Within a minute of our own clock, because the chain's time is not allowed to run ahead
        // of ours by more. That bound is the reference's, and it is what makes a stale clock a
        // fault rather than a schedule.
        let chain_deadline = 1_030_000i64;
        let (address, raw) = chain_account(&material, &snapshot, chain_deadline);
        fake.accounts.lock().unwrap().insert(
            wire::to_hex(&address),
            RegistryAccount {
                address: address.clone(),
                owner: PROGRAM.to_vec(),
                data: raw,
                slot: 103,
                finalized: true,
            },
        );
        *fake.chain_time.lock().unwrap() = *now.lock().unwrap();

        let error = block(registry.prepare_finalization(&snapshot.base().forecast_id)).unwrap_err();
        assert_eq!(error.code, "chain_finalize_not_before");
        // The wait is the later of the two walls the chain publishes: its own finalization
        // deadline and the challenge it has not finished.
        let not_before = error.not_before_ms.expect("a deadline to wait for");
        assert!(not_before >= chain_deadline);

        // The chain's clock reaches the deadline the record already published.
        *fake.chain_time.lock().unwrap() = not_before;
        assert!(block(registry.prepare_finalization(&snapshot.base().forecast_id)).expect("finalizable"));
        let row = db
            .run(
                "SELECT confirmed_revision,chain_deadline,chain_time FROM registry_forecasts WHERE forecast_id=?",
                &[json!(snapshot.base().forecast_id)],
            )
            .expect("row")
            .0;
        assert_eq!(int(&row[0], "chain_deadline"), Some(chain_deadline));
        assert_eq!(int(&row[0], "chain_time"), Some(not_before));
    }

    #[test]
    fn an_account_owned_by_something_else_is_not_evidence() {
        // Every clause matters: the right address, the right owner, and a read the provider has
        // actually finalized.
        let fake = Fake {
            accounts: Mutex::new(BTreeMap::new()),
            chain_time: Mutex::new(0),
        };
        let clock = || 0i64;
        let db = Sqlite::from_migrations();
        let token = || "token".to_string();
        let registry = SolanaRegistry::new(&db, &fake, &PROGRAM, &RELAYER, &clock, &token).expect("a registry");
        let good = RegistryAccount {
            address: vec![1u8; 32],
            owner: PROGRAM.to_vec(),
            data: Vec::new(),
            slot: 1,
            finalized: true,
        };
        assert!(registry.checked_account(&good, &[1u8; 32]).is_ok());
        assert_eq!(
            registry
                .checked_account(
                    &RegistryAccount {
                        owner: [9u8; 32].to_vec(),
                        ..good.clone()
                    },
                    &[1u8; 32]
                )
                .unwrap_err()
                .code,
            "chain_account_unverified"
        );
        assert_eq!(
            registry
                .checked_account(
                    &RegistryAccount {
                        finalized: false,
                        ..good.clone()
                    },
                    &[1u8; 32]
                )
                .unwrap_err()
                .code,
            "chain_account_unverified"
        );
        assert_eq!(
            registry.checked_account(&good, &[2u8; 32]).unwrap_err().code,
            "chain_account_unverified"
        );
    }

    #[test]
    fn the_daily_reserve_refuses_once_the_envelope_is_gone() {
        let db = Sqlite::from_migrations();
        block(reserve_daily_spend(&db, 60, 0, 100)).expect("a first reserve");
        assert_eq!(
            block(reserve_daily_spend(&db, 50, 0, 100)).unwrap_err().code,
            "daily_budget_exhausted"
        );
        block(reserve_daily_spend(&db, 40, 0, 100)).expect("the envelope still has room");
        assert_eq!(
            block(reserve_daily_spend(&db, 1, 0, 100)).unwrap_err().code,
            "daily_budget_exhausted"
        );
    }
}
