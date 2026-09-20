//! Finalized RPC intake mirror, and the opt-in exact-candidate seal admission.
//!
//! RPC observations are trusted finalized-provider evidence, not light-client proofs. Nothing here
//! signs, writes to chain, or serializes a candidate for anyone else; all RPC awaits are
//! sequential, because the whole point of the module is that its records match a chain that only
//! moves forward.
//!
//! Three things shape it:
//!
//!   - **Every write is a compare-and-set against a generation.** The intake may be re-pointed at
//!     a different native epoch, and every statement is guarded on the generation it was read
//!     under. A mirror that lands under the wrong generation is worse than no mirror.
//!   - **Observations are immutable, heads move forward.** The observation rows are inserted
//!     `OR IGNORE` and never updated; the head is a separately guarded upsert that only advances —
//!     by context slot, by revision, and within an epoch never by phase or accepted count. A
//!     re-org that goes backwards is refused rather than mirrored.
//!   - **A seal is admitted for one candidate.** `verify_and_store_seal` stores the advance it was
//!     given together with the accumulator it was sealed against, so the local finalization that
//!     follows can be checked against a specific commitment rather than against the act of sealing.

use base64::Engine;
use serde_json::{json, Value};

use crate::db::{int, text, Database, Row};
use crate::dispute_wire as wire;
use crate::registry::{identity_hash, DEVNET_GENESIS};
use crate::solana;

pub type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T>>>;
/// The read-only transport. It answers `getGenesisHash` and `getAccountInfo` and nothing else, and
/// a failure carries no detail — a provider body is not something this module may retain.
pub type Rpc = Box<dyn Fn(String, Vec<Value>) -> BoxFuture<Result<Value, ()>>>;

const LOADERS: [&str; 3] = [
    "BPFLoaderUpgradeab1e11111111111111111111111",
    "BPFLoader2111111111111111111111111111111111",
    "BPFLoader1111111111111111111111111111111111",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IntakeError(pub String);

fn require(condition: bool, code: &str) -> Result<(), IntakeError> {
    if condition {
        Ok(())
    } else {
        Err(IntakeError(code.to_string()))
    }
}

fn integer(value: i64) -> Result<i64, IntakeError> {
    require((0..=wire::MAX).contains(&value), "intake_invalid_integer")?;
    Ok(value)
}

fn key(value: &[u8]) -> Result<Vec<u8>, IntakeError> {
    require(value.len() == 32 && !solana::zeroed(value), "intake_invalid_key")?;
    Ok(value.to_vec())
}

fn b64(value: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(value)
}

/// `_guard`: a row in `mutation_guards`, whose `CHECK(valid=1)` is what makes the statement a
/// compare-and-set rather than an assertion nobody reads.
fn guard(token: &str, query: &str, params: Vec<Value>) -> (String, Vec<Value>) {
    let mut bound = vec![json!(token)];
    bound.extend(params);
    (
        format!("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN {query} THEN 1 ELSE 0 END"),
        bound,
    )
}

/// `_immutable`: insert if absent, prove the row now exists, and release the guard.
///
/// The second statement is the interesting one. It does not check that *this* call inserted the
/// row; it checks that the row with these exact values is there, which is what makes a retry and a
/// first attempt the same thing.
fn immutable(table: &str, fields: &[&str], values: &[Value], token: &str) -> Vec<(String, Vec<Value>)> {
    let columns = fields.join(",");
    let placeholders = vec!["?"; fields.len()].join(",");
    let condition = fields
        .iter()
        .map(|name| format!("{name}=?"))
        .collect::<Vec<String>>()
        .join(" AND ");
    vec![
        (
            format!("INSERT OR IGNORE INTO {table}({columns}) VALUES({placeholders})"),
            values.to_vec(),
        ),
        guard(
            token,
            &format!("EXISTS(SELECT 1 FROM {table} WHERE {condition})"),
            values.to_vec(),
        ),
        (
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(token)],
        ),
    ]
}

/// One finalized account, as the mirror retained it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountEvidence {
    pub address: Vec<u8>,
    pub data: Vec<u8>,
    pub slot: i64,
}

/// One epoch's worth of chain state, read at a consistent slot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpochEvidence {
    pub accumulator: wire::Accumulator,
    pub forecast: solana::ForecastAccount,
    pub accumulator_account: AccountEvidence,
    pub forecast_account: AccountEvidence,
    pub receipt_account: Option<AccountEvidence>,
}

/// `_candidate`: the advance fields a seal candidate carries.
///
/// The leading tag-2 is the canonical preimage's own format and is never sent; the decoder is fed
/// a tag-12 window so the *seal* shape is what gets parsed.
pub fn candidate(data: &[u8]) -> Result<Value, IntakeError> {
    require(data.len() == 255 && data[0] == 2, "intake_candidate_format")?;
    let mut window = vec![0x0cu8];
    window.extend(&data[1..]);
    window.extend([0u8; 40]);
    let decoded = wire::decode_instruction(&window).map_err(|_| IntakeError("intake_candidate_format".to_string()))?;
    Ok(decoded["advance"].clone())
}

/// `admission_guard_sql`: the guard the root inserts before its forecast update, **inside the same
/// batch**. Root must delete this token in that batch. It does not acquire a closing-action lease
/// and does not serialize a candidate.
pub fn admission_guard_sql(
    forecast_id: &str,
    generation: i64,
    native_advance: &[u8],
    token: &str,
) -> Result<(String, Vec<Value>), IntakeError> {
    require(!forecast_id.is_empty(), "intake_forecast_id")?;
    require(integer(generation)? > 0, "intake_generation")?;
    require(!token.is_empty() && token.len() <= 128, "intake_guard_token")?;
    let value = candidate(native_advance)?;
    let advance_hash =
        wire::advance_hash(native_advance).map_err(|_| IntakeError("intake_candidate_format".to_string()))?;
    let query = "EXISTS(SELECT 1 FROM intake_bindings b \
         JOIN intake_heads h ON h.forecast_id=b.forecast_id AND h.generation=b.generation \
         JOIN intake_seals s ON s.forecast_id=b.forecast_id AND s.epoch=h.epoch \
         JOIN intake_seal_admissions a ON a.forecast_id=s.forecast_id AND a.epoch=s.epoch AND a.generation=b.generation \
         JOIN forecasts f ON f.id=b.forecast_id \
         JOIN events e ON e.forecast_id=f.id AND e.revision=f.revision \
         WHERE b.forecast_id=? AND b.generation=? AND f.state='CHALLENGE' AND h.phase=2 AND h.pending_count=0 AND h.material_count=0 \
         AND h.commitment=s.seal_commitment AND a.context_slot<=h.context_slot \
         AND s.advance_base64=? AND s.advance_hash=? \
         AND s.predecessor_revision=f.revision AND s.predecessor_event_hash=e.hash \
         AND s.candidate_revision=? AND s.candidate_event_hash=? AND s.candidate_snapshot_hash=? \
         AND NOT EXISTS(SELECT 1 FROM intake_receipts r JOIN intake_receipt_heads rh ON rh.address=r.address \
           WHERE r.forecast_id=b.forecast_id AND (rh.native_status=1 OR (r.epoch=h.epoch AND rh.native_status=3))))";
    Ok(guard(
        token,
        query,
        vec![
            json!(forecast_id),
            json!(generation),
            json!(b64(native_advance)),
            json!(wire::to_hex(&advance_hash)),
            json!(value["revision"]),
            json!(value["event_hash"]),
            json!(value["snapshot_hash"]),
        ],
    ))
}

/// `DisputeIntake`.
pub struct DisputeIntake<'a> {
    pub db: &'a dyn Database,
    pub rpc: &'a Rpc,
    pub program: Vec<u8>,
    pub now_ms: &'a dyn Fn() -> i64,
}

impl DisputeIntake<'_> {
    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    /// `_call`: the transport is read-only by construction, not by convention.
    async fn call(&self, method: &str, params: Vec<Value>) -> Result<Value, IntakeError> {
        require(
            method == "getGenesisHash" || method == "getAccountInfo",
            "intake_read_only_transport",
        )?;
        (self.rpc)(method.to_string(), params)
            .await
            .map_err(|_| IntakeError("intake_rpc_unavailable".to_string()))
    }

    /// `_network`: the cluster is the one the binding was made against, and the program is a
    /// program. Both are checked before any account is read.
    async fn network(&self) -> Result<(), IntakeError> {
        let genesis = self.call("getGenesisHash", Vec::new()).await?;
        require(genesis.as_str() == Some(DEVNET_GENESIS), "intake_wrong_genesis")?;
        let response = self
            .call(
                "getAccountInfo",
                vec![
                    json!(solana::base58_encode(&self.program)),
                    json!({"encoding": "base64", "commitment": "finalized"}),
                ],
            )
            .await?;
        let (_, value) = context(&response)?;
        let executable = value["executable"].as_bool() == Some(true);
        let owned = value["owner"].as_str().is_some_and(|owner| LOADERS.contains(&owner));
        require(
            executable && owned && value["lamports"].as_i64().unwrap_or(0) > 0,
            "intake_program_unverified",
        )
    }

    /// `_account`: one finalized account, with the encoding checked rather than assumed.
    async fn account(&self, address: &[u8], maximum: usize, minimum_slot: i64) -> Result<AccountEvidence, IntakeError> {
        let result = self
            .call(
                "getAccountInfo",
                vec![
                    json!(solana::base58_encode(address)),
                    json!({"encoding": "base64", "commitment": "finalized",
                           "minContextSlot": minimum_slot}),
                ],
            )
            .await?;
        let (slot, value) = context(&result)?;
        require(slot >= minimum_slot, "intake_stale_context")?;
        let owned = value["owner"].as_str() == Some(solana::base58_encode(&self.program).as_str());
        require(
            owned && value["executable"].as_bool() == Some(false) && value["lamports"].as_i64().unwrap_or(0) > 0,
            "intake_account_owner",
        )?;
        let encoded = &value["data"];
        let shaped = encoded.as_array().is_some_and(|pair| {
            pair.len() == 2
                && pair[1].as_str() == Some("base64")
                && pair[0]
                    .as_str()
                    .is_some_and(|text| text.len() <= 4 * maximum.div_ceil(3))
        });
        require(shaped, "intake_account_encoding")?;
        let text = encoded[0].as_str().unwrap_or_default();
        let raw = base64::engine::general_purpose::STANDARD
            .decode(text)
            .map_err(|_| IntakeError("intake_account_encoding".to_string()))?;
        // The re-encode is what makes this base64 rather than something that merely decodes: a
        // non-canonical encoding would give two different byte strings for one account.
        require(raw.len() <= maximum && b64(&raw) == text, "intake_account_encoding")?;
        Ok(AccountEvidence {
            address: address.to_vec(),
            data: raw,
            slot,
        })
    }

    /// `_local`: the forecast this mirror is being pointed at.
    async fn local(&self, forecast_id: &str) -> Result<Row, IntakeError> {
        require(
            !forecast_id.is_empty() && forecast_id.len() <= 128,
            "intake_forecast_id",
        )?;
        self.db
            .first(
                "SELECT id,specification_hash,creator_id FROM forecasts WHERE id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| IntakeError("intake_mirror_conflict".to_string()))?
            .ok_or_else(|| IntakeError("intake_forecast_missing".to_string()))
    }

    /// `_binding`: the stored binding still describes the chain this mirror reads.
    ///
    /// The two derived addresses are recomputed rather than trusted: a binding whose addresses do
    /// not follow from the program and the forecast identity is a binding to something else.
    async fn binding(&self, forecast_id: &str, generation: i64) -> Result<Row, IntakeError> {
        require(
            !forecast_id.is_empty() && forecast_id.len() <= 128,
            "intake_forecast_id",
        )?;
        require(integer(generation)? > 0, "intake_generation")?;
        let row = self
            .db
            .first(
                "SELECT * FROM intake_bindings WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| IntakeError("intake_mirror_conflict".to_string()))?
            .ok_or_else(|| IntakeError("intake_generation_changed".to_string()))?;
        require(int(&row, "generation") == Some(generation), "intake_generation_changed")?;
        let scope_ok = text(&row, "program_id") == Some(solana::base58_encode(&self.program).as_str())
            && text(&row, "genesis_hash") == Some(DEVNET_GENESIS);
        require(scope_ok, "intake_binding_mismatch")?;
        let expected = solana::forecast_address(&self.program, &identity_hash("forecast", forecast_id))
            .map_err(|_| IntakeError("intake_binding_mismatch".to_string()))?
            .0;
        let gate = wire::gate_address(&self.program, &expected)
            .map_err(|_| IntakeError("intake_binding_mismatch".to_string()))?
            .0;
        let addresses_ok = text(&row, "forecast_address") == Some(solana::base58_encode(&expected).as_str())
            && text(&row, "accumulator_address") == Some(solana::base58_encode(&gate).as_str());
        require(addresses_ok, "intake_binding_mismatch")?;
        Ok(row)
    }
}

/// `_context`: the slot an answer was observed at, and the answer.
fn context(response: &Value) -> Result<(i64, &Value), IntakeError> {
    let slot = response["context"]["slot"]
        .as_i64()
        .ok_or_else(|| IntakeError("intake_rpc_context".to_string()))?;
    require(
        response["context"].is_object() && response.get("value").is_some() && slot > 0,
        "intake_rpc_context",
    )?;
    Ok((slot, &response["value"]))
}

impl DisputeIntake<'_> {
    /// `_observe`: one consistent read of an epoch, retried once if the gate moved underneath.
    ///
    /// The accumulator is read on both sides of the forecast because the account that has to be
    /// the same is the *gate*: a resolution and a forecast read at different revisions is exactly
    /// the inconsistency this whole mirror exists to detect.
    pub async fn observe(
        &self,
        forecast_id: &str,
        receipt_address: Option<&[u8]>,
    ) -> Result<EpochEvidence, IntakeError> {
        let local = self.local(forecast_id).await?;
        self.network().await?;
        let fid = identity_hash("forecast", forecast_id);
        let forecast_address = solana::forecast_address(&self.program, &fid)
            .map_err(|_| IntakeError("intake_binding_mismatch".to_string()))?
            .0;
        let accumulator_address = wire::gate_address(&self.program, &forecast_address)
            .map_err(|_| IntakeError("intake_binding_mismatch".to_string()))?
            .0;
        let head = self
            .db
            .first(
                "SELECT context_slot FROM intake_heads WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| IntakeError("intake_mirror_conflict".to_string()))?;
        let mut minimum = head.as_ref().and_then(|row| int(row, "context_slot")).unwrap_or(1);
        for _ in 0..2 {
            let first = self.account(&accumulator_address, wire::GATE_SIZE, minimum).await?;
            let mut slot = first.slot;
            let mut receipt = None;
            if let Some(address) = receipt_address {
                let evidence = self
                    .account(address, wire::RECEIPT_HEADER + wire::MAX_BODY, slot)
                    .await?;
                slot = evidence.slot;
                receipt = Some(evidence);
            }
            let forecast = self.account(&forecast_address, 360, slot).await?;
            let last = self
                .account(&accumulator_address, wire::GATE_SIZE, forecast.slot)
                .await?;
            minimum = last.slot;
            if first.data != last.data {
                continue;
            }
            let accumulator =
                wire::Accumulator::decode(&last.data).map_err(|_| IntakeError("intake_wire_invalid".to_string()))?;
            let decoded =
                solana::decode_forecast(&forecast.data).map_err(|_| IntakeError("intake_wire_invalid".to_string()))?;
            let bound = accumulator.forecast == to_array(&forecast_address)
                && accumulator.specification == decoded.specification_hash
                && decoded.forecast_id_hash == fid
                && decoded.creator_hash == identity_hash("creator", text(&local, "creator_id").unwrap_or_default())
                && wire::to_hex(&decoded.specification_hash) == text(&local, "specification_hash").unwrap_or_default();
            require(bound, "intake_forecast_binding")?;
            let proposal_ok = accumulator.phase == 0
                || (accumulator.resolution == decoded.resolution_hash
                    && accumulator.proposal_revision <= decoded.revision);
            require(proposal_ok, "intake_proposal_binding")?;
            return Ok(EpochEvidence {
                accumulator,
                forecast: decoded,
                accumulator_account: last,
                forecast_account: forecast,
                receipt_account: receipt,
            });
        }
        Err(IntakeError("intake_observation_raced".to_string()))
    }

    /// `_generation_guard`.
    fn generation_guard(forecast_id: &str, generation: i64, token: &str) -> (String, Vec<Value>) {
        guard(
            token,
            "EXISTS(SELECT 1 FROM intake_bindings WHERE forecast_id=? AND generation=?)",
            vec![json!(forecast_id), json!(generation)],
        )
    }

    /// `_epoch_sql`: the observation, then the head.
    ///
    /// `observed_at` is deliberately outside the equality test, so an exact retry at a later wall
    /// clock does not write a second observation of the same chain state.
    fn epoch_sql(
        &self,
        forecast_id: &str,
        generation: i64,
        observation: &EpochEvidence,
        token: &str,
    ) -> Result<Vec<(String, Vec<Value>)>, IntakeError> {
        let accumulator = &observation.accumulator;
        let account = &observation.accumulator_account;
        let forecast = &observation.forecast_account;
        let now = integer(self.now())?;
        let fields = [
            "forecast_id",
            "epoch",
            "revision",
            "context_slot",
            "commitment",
            "account_base64",
            "forecast_base64",
        ];
        let commitment = wire::to_hex(&accumulator.commitment());
        let values = vec![
            json!(forecast_id),
            json!(accumulator.epoch),
            json!(accumulator.revision),
            json!(account.slot),
            json!(commitment),
            json!(b64(&account.data)),
            json!(b64(&forecast.data)),
        ];
        let condition = fields
            .iter()
            .map(|name| format!("{name}=?"))
            .collect::<Vec<String>>()
            .join(" AND ");
        let mut statements = vec![
            (
                format!(
                    "INSERT OR IGNORE INTO intake_epoch_observations({},observed_at) VALUES(?,?,?,?,?,?,?,?)",
                    fields.join(",")
                ),
                {
                    let mut bound = values.clone();
                    bound.push(json!(now));
                    bound
                },
            ),
            guard(
                &format!("{token}o"),
                &format!("EXISTS(SELECT 1 FROM intake_epoch_observations WHERE {condition})"),
                values.clone(),
            ),
            (
                "DELETE FROM mutation_guards WHERE token=?".to_string(),
                vec![json!(format!("{token}o"))],
            ),
        ];
        statements.push((
            "INSERT INTO intake_heads(forecast_id,generation,epoch,revision,context_slot,commitment,phase,pending_count,material_count,accepted_count,deadline) \
             VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(forecast_id) DO UPDATE SET generation=excluded.generation,epoch=excluded.epoch,\
             revision=excluded.revision,context_slot=excluded.context_slot,commitment=excluded.commitment,phase=excluded.phase,\
             pending_count=excluded.pending_count,material_count=excluded.material_count,accepted_count=excluded.accepted_count,deadline=excluded.deadline \
             WHERE excluded.generation>=intake_heads.generation AND excluded.revision>=intake_heads.revision \
             AND excluded.context_slot>=intake_heads.context_slot AND excluded.epoch>=intake_heads.epoch \
             AND ((excluded.epoch=intake_heads.epoch AND excluded.phase>=intake_heads.phase \
             AND excluded.accepted_count>=intake_heads.accepted_count AND excluded.deadline>=intake_heads.deadline) \
             OR (excluded.epoch>intake_heads.epoch AND intake_heads.phase<2 AND excluded.phase>=1)) \
             AND (excluded.revision!=intake_heads.revision OR excluded.commitment=intake_heads.commitment)"
                .to_string(),
            vec![
                json!(forecast_id),
                json!(generation),
                json!(accumulator.epoch),
                json!(accumulator.revision),
                json!(account.slot),
                json!(commitment),
                json!(accumulator.phase),
                json!(accumulator.pending),
                json!(accumulator.material),
                json!(accumulator.accepted),
                json!(accumulator.deadline),
            ],
        ));
        statements.push(guard(
            &format!("{token}h"),
            "EXISTS(SELECT 1 FROM intake_heads WHERE forecast_id=? AND generation=? AND revision=? AND context_slot=? AND commitment=?)",
            vec![
                json!(forecast_id),
                json!(generation),
                json!(accumulator.revision),
                json!(account.slot),
                json!(commitment),
            ],
        ));
        statements.push((
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(format!("{token}h"))],
        ));
        Ok(statements)
    }

    /// `_batch`: one transaction, with the generation guard on the outside.
    ///
    /// Failure is diagnosed rather than guessed at: if the binding moved, that is the answer; if it
    /// did not, the mirror conflicted with itself and the caller is told so.
    async fn batch(
        &self,
        forecast_id: &str,
        generation: i64,
        statements: Vec<(String, Vec<Value>)>,
        token: &str,
    ) -> Result<(), IntakeError> {
        let mut all = vec![Self::generation_guard(forecast_id, generation, token)];
        all.extend(statements);
        all.push((
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(token)],
        ));
        if self.db.batch(&all).await.is_ok() {
            return Ok(());
        }
        let row = self
            .db
            .first(
                "SELECT generation FROM intake_bindings WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| IntakeError("intake_mirror_conflict".to_string()))?;
        if row.as_ref().and_then(|row| int(row, "generation")) != Some(generation) {
            return Err(IntakeError("intake_generation_changed".to_string()));
        }
        Err(IntakeError("intake_mirror_conflict".to_string()))
    }

    /// `_token`: a name for this exact chain state, so a retry of the same observation is the same
    /// request rather than a second one.
    fn token(forecast_id: &str, generation: i64, observation: &EpochEvidence) -> String {
        let mut hasher = sha2::Sha256::new();
        use sha2::Digest;
        hasher.update(format!(
            "{forecast_id}:{generation}:{}:",
            observation.accumulator_account.slot
        ));
        hasher.update(&observation.accumulator_account.data);
        format!("intake:{}", wire::to_hex(&hasher.finalize()))
    }
}

fn to_array(value: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(value);
    out
}

impl DisputeIntake<'_> {
    /// `activate`: point the mirror at a forecast that is already native-activated.
    pub async fn activate(&self, forecast_id: &str) -> Result<i64, IntakeError> {
        if let Some(previous) = self
            .db
            .first(
                "SELECT * FROM intake_bindings WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| IntakeError("intake_mirror_conflict".to_string()))?
        {
            let generation = int(&previous, "generation").unwrap_or(0);
            self.refresh(forecast_id, generation).await?;
            return Ok(generation);
        }
        let observation = self.observe(forecast_id, None).await?;
        let accumulator = &observation.accumulator;
        let token = Self::token(forecast_id, 1, &observation);
        let fields = [
            "forecast_id",
            "program_id",
            "genesis_hash",
            "forecast_address",
            "accumulator_address",
            "specification_hash",
            "generation",
        ];
        let values = vec![
            json!(forecast_id),
            json!(solana::base58_encode(&self.program)),
            json!(DEVNET_GENESIS),
            json!(solana::base58_encode(&accumulator.forecast)),
            json!(solana::base58_encode(&observation.accumulator_account.address)),
            json!(wire::to_hex(&accumulator.specification)),
            json!(1),
        ];
        let condition = fields
            .iter()
            .map(|name| format!("{name}=?"))
            .collect::<Vec<String>>()
            .join(" AND ");
        let mut sql: Vec<(String, Vec<Value>)> = vec![
            (
                format!(
                    "INSERT OR IGNORE INTO intake_bindings({},activated_at) VALUES(?,?,?,?,?,?,?,?)",
                    fields.join(",")
                ),
                {
                    let mut bound = values.clone();
                    bound.push(json!(integer(self.now())?));
                    bound
                },
            ),
            Self::generation_guard(forecast_id, 1, &token),
            guard(
                &format!("{token}b"),
                &format!("EXISTS(SELECT 1 FROM intake_bindings WHERE {condition})"),
                values.clone(),
            ),
            (
                "DELETE FROM mutation_guards WHERE token=?".to_string(),
                vec![json!(format!("{token}b"))],
            ),
        ];
        sql.extend(self.epoch_sql(forecast_id, 1, &observation, &token)?);
        sql.push((
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(token)],
        ));
        if self.db.batch(&sql).await.is_err() {
            return Err(IntakeError("intake_activation_raced".to_string()));
        }
        Ok(1)
    }

    /// `advance_generation`: re-point the mirror, once the previous one is settled.
    pub async fn advance_generation(&self, forecast_id: &str, expected_generation: i64) -> Result<i64, IntakeError> {
        self.binding(forecast_id, expected_generation).await?;
        let next = integer(expected_generation + 1)?;
        self.db
            .execute(
                "UPDATE intake_bindings SET generation=? WHERE forecast_id=? AND generation=?",
                &[json!(next), json!(forecast_id), json!(expected_generation)],
            )
            .await
            .map_err(|_| IntakeError("intake_generation_changed".to_string()))?;
        // The D1 client does not report a changed-row count here, so the write is read back. The
        // guard is the WHERE clause; this only confirms it held.
        let row = self
            .db
            .first(
                "SELECT generation FROM intake_bindings WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| IntakeError("intake_generation_changed".to_string()))?;
        require(
            row.as_ref().and_then(|row| int(row, "generation")) == Some(next),
            "intake_generation_changed",
        )?;
        Ok(next)
    }

    /// `refresh`: re-read the epoch and mirror it under the current generation.
    pub async fn refresh(&self, forecast_id: &str, generation: i64) -> Result<EpochEvidence, IntakeError> {
        self.binding(forecast_id, generation).await?;
        let observation = self.observe(forecast_id, None).await?;
        let token = Self::token(forecast_id, generation, &observation);
        let statements = self.epoch_sql(forecast_id, generation, &observation, &token)?;
        self.batch(forecast_id, generation, statements, &token).await?;
        Ok(observation)
    }

    /// `import_receipt`: mirror one dispute receipt, with its counter-binding re-checked.
    pub async fn import_receipt(
        &self,
        forecast_id: &str,
        receipt_address: &[u8],
        generation: i64,
    ) -> Result<Value, IntakeError> {
        self.binding(forecast_id, generation).await?;
        let receipt_address = key(receipt_address)?;
        let observation = self.observe(forecast_id, Some(&receipt_address)).await?;
        let evidence = observation
            .receipt_account
            .clone()
            .ok_or_else(|| IntakeError("intake_receipt_missing".to_string()))?;
        let accumulator = &observation.accumulator;
        let receipt =
            wire::Receipt::decode(&evidence.data).map_err(|_| IntakeError("intake_receipt_invalid".to_string()))?;
        let bound = to_array(&receipt.program) == to_array(&self.program)
            && receipt.forecast == accumulator.forecast
            && receipt.specification == accumulator.specification
            && wire::receipt_address(&self.program, &receipt.forecast, receipt.epoch, &receipt.user)
                .map(|(address, _)| address == to_array(&receipt_address))
                .unwrap_or(false);
        require(bound, "intake_receipt_binding")?;
        require(
            receipt.status != 0 && receipt.accepted_slot > 0 && receipt.accepted_slot <= evidence.slot,
            "intake_not_submitted",
        )?;
        require(receipt.epoch <= accumulator.epoch, "intake_future_epoch")?;
        if receipt.epoch == accumulator.epoch {
            let counters = receipt.proposal_revision == accumulator.proposal_revision
                && receipt.proposal_event == accumulator.proposal_event
                && receipt.resolution == accumulator.resolution
                && receipt.accepted_at >= accumulator.opened
                && accumulator.accepted > 0
                && (observation.forecast.state == 9 || receipt.deadline <= accumulator.deadline)
                && (receipt.status != 1 || accumulator.pending > 0)
                && (receipt.status != 3 || accumulator.material > 0);
            require(counters, "intake_receipt_counter_binding")?;
        } else {
            // Under this native version, replacement requires every pending receipt to be
            // reviewed; the immutable receipt keeps the old anchor and proposal.
            require(receipt.status == 2 || receipt.status == 3, "intake_historical_pending")?;
        }
        // Whether the retained body is canonical is recorded, not enforced: a receipt whose
        // evidence is unreadable is still a receipt the chain accepted.
        let canonical = wire::decode_evidence(&receipt.body).is_ok_and(|envelope| {
            envelope["sources"].as_array().is_some_and(|sources| {
                sources
                    .iter()
                    .all(|source| source["captured_at_ms"].as_i64().unwrap_or(i64::MAX) <= receipt.accepted_at)
            })
        });

        let token = Self::token(forecast_id, generation, &observation);
        let address_text = solana::base58_encode(&receipt_address);
        let fields = [
            "address",
            "forecast_id",
            "epoch",
            "proposal_revision",
            "proposal_event_hash",
            "resolution_hash",
            "user_signer",
            "nonce",
            "evidence_hash",
            "body_base64",
            "body_length",
            "accepted_at",
            "accepted_slot",
            "accepted_deadline",
            "evidence_valid",
        ];
        let values = vec![
            json!(address_text),
            json!(forecast_id),
            json!(receipt.epoch),
            json!(receipt.proposal_revision),
            json!(wire::to_hex(&receipt.proposal_event)),
            json!(wire::to_hex(&receipt.resolution)),
            json!(solana::base58_encode(&receipt.user)),
            json!(wire::to_hex(&receipt.nonce)),
            json!(wire::to_hex(&receipt.evidence)),
            json!(b64(&receipt.body)),
            json!(receipt.body_length),
            json!(receipt.accepted_at),
            json!(receipt.accepted_slot),
            json!(receipt.deadline),
            json!(i64::from(canonical)),
        ];
        let condition = fields
            .iter()
            .map(|name| format!("{name}=?"))
            .collect::<Vec<String>>()
            .join(" AND ");
        let mut statements = self.epoch_sql(forecast_id, generation, &observation, &token)?;
        statements.push((
            format!(
                "INSERT OR IGNORE INTO intake_receipts({},imported_at) VALUES({})",
                fields.join(","),
                vec!["?"; values.len() + 1].join(",")
            ),
            {
                let mut bound = values.clone();
                bound.push(json!(integer(self.now())?));
                bound
            },
        ));
        statements.push(guard(
            &format!("{token}r"),
            &format!("EXISTS(SELECT 1 FROM intake_receipts WHERE {condition})"),
            values.clone(),
        ));
        statements.push((
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(format!("{token}r"))],
        ));

        let commitment = wire::to_hex(&receipt.commitment());
        let observation_fields = [
            "address",
            "commitment",
            "context_slot",
            "account_base64",
            "native_status",
            "review_hash",
            "reviewer",
            "reviewed_at",
        ];
        let observation_values = vec![
            json!(address_text),
            json!(commitment),
            json!(evidence.slot),
            json!(b64(&evidence.data)),
            json!(receipt.status),
            json!(wire::to_hex(&receipt.review)),
            json!(solana::base58_encode(&receipt.reviewer)),
            json!(receipt.reviewed_at),
        ];
        statements.extend(immutable(
            "intake_receipt_observations",
            &observation_fields,
            &observation_values,
            &format!("{token}v"),
        ));
        statements.push((
            "INSERT INTO intake_receipt_heads(address,commitment,context_slot,native_status) VALUES(?,?,?,?) \
             ON CONFLICT(address) DO UPDATE SET commitment=excluded.commitment,context_slot=excluded.context_slot,native_status=excluded.native_status \
             WHERE excluded.context_slot>=intake_receipt_heads.context_slot AND \
             ((excluded.native_status=intake_receipt_heads.native_status AND excluded.commitment=intake_receipt_heads.commitment) \
             OR (intake_receipt_heads.native_status=1 AND excluded.native_status IN (2,3)))"
                .to_string(),
            vec![
                json!(address_text),
                json!(commitment),
                json!(evidence.slot),
                json!(receipt.status),
            ],
        ));
        statements.push(guard(
            &format!("{token}p"),
            "EXISTS(SELECT 1 FROM intake_receipt_heads WHERE address=? AND commitment=? AND context_slot=? AND native_status=?)",
            vec![
                json!(address_text),
                json!(commitment),
                json!(evidence.slot),
                json!(receipt.status),
            ],
        ));
        statements.push((
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(format!("{token}p"))],
        ));
        self.batch(forecast_id, generation, statements, &token).await?;

        let now = integer(self.now())?;
        Ok(json!({
            "address": address_text, "epoch": receipt.epoch, "nativeStatus": receipt.status,
            "acceptedAt": receipt.accepted_at, "acceptedSlot": receipt.accepted_slot,
            "importedAt": now, "lateImport": now > receipt.deadline,
            "canonicalEvidence": canonical, "historical": receipt.epoch < accumulator.epoch,
        }))
    }

    /// `seal_admission`: a read-only preflight, **not** authority for local finalization.
    pub async fn seal_admission(&self, forecast_id: &str, generation: i64) -> Result<EpochEvidence, IntakeError> {
        let observation = self.refresh(forecast_id, generation).await?;
        let accumulator = &observation.accumulator;
        require(
            accumulator.phase == 1 && accumulator.pending == 0 && accumulator.material == 0,
            "intake_unresolved",
        )?;
        // A pending mirror is a receipt the chain has not settled. Sealing over one would make the
        // local record disagree with the chain it was taken from.
        let pending = self
            .db
            .first(
                "SELECT r.address FROM intake_receipts r JOIN intake_receipt_heads h ON h.address=r.address \
                 WHERE r.forecast_id=? AND (h.native_status=1 OR (r.epoch=? AND h.native_status=3)) LIMIT 1",
                &[json!(forecast_id), json!(accumulator.epoch)],
            )
            .await
            .map_err(|_| IntakeError("intake_mirror_conflict".to_string()))?;
        require(pending.is_none(), "intake_mirror_unresolved")?;
        Ok(observation)
    }

    /// `verify_and_store_seal`: admit one exact candidate, bound to the accumulator it sealed.
    #[allow(clippy::too_many_lines)]
    pub async fn verify_and_store_seal(
        &self,
        forecast_id: &str,
        generation: i64,
        native_advance: &[u8],
    ) -> Result<Value, IntakeError> {
        self.binding(forecast_id, generation).await?;
        let value = candidate(native_advance)?;
        let observation = self.observe(forecast_id, None).await?;
        let accumulator = &observation.accumulator;
        let forecast = &observation.forecast;
        wire::encode_finalize(accumulator, native_advance)
            .map_err(|_| IntakeError("intake_seal_candidate_mismatch".to_string()))?;
        let slot = observation.accumulator_account.slot;
        let consistent = accumulator.phase == 2
            && accumulator.pending == 0
            && accumulator.material == 0
            && accumulator.sealed_slot <= slot
            && forecast.state == 6
            && forecast.pending_disputes == 0
            && forecast.material_disputes == 0
            && value["revision"].as_i64() == Some(forecast.revision + 1)
            && value["previous_event_hash"].as_str() == Some(&wire::to_hex(&forecast.event_hash))
            && value["resolution_hash"].as_str() == Some(&wire::to_hex(&forecast.resolution_hash))
            && value["outcome"].as_i64() == Some(forecast.outcome)
            && value["dispute_hash"].as_str() == Some(&wire::to_hex(&forecast.dispute_hash))
            && value["trigger_hash"].as_str() == Some(&wire::to_hex(&forecast.trigger_hash))
            && value["challenge_until_ms"].as_i64() == Some(forecast.challenge_until_ms)
            && value["occurred_at_ms"].as_i64().unwrap_or(i64::MIN) >= forecast.challenge_until_ms
            && value["occurred_at_ms"].as_i64().unwrap_or(i64::MAX) <= accumulator.sealed_at;
        require(consistent, "intake_seal_predecessor_mismatch")?;

        let token = Self::token(forecast_id, generation, &observation);
        let commitment = wire::to_hex(&accumulator.commitment());
        let advance_hash = wire::to_hex(
            &wire::advance_hash(native_advance).map_err(|_| IntakeError("intake_candidate_format".to_string()))?,
        );
        let fields = [
            "forecast_id",
            "epoch",
            "advance_base64",
            "advance_hash",
            "predecessor_revision",
            "predecessor_event_hash",
            "candidate_revision",
            "candidate_event_hash",
            "candidate_snapshot_hash",
            "seal_commitment",
            "account_base64",
            "sealed_at",
            "sealed_slot",
        ];
        let values = vec![
            json!(forecast_id),
            json!(accumulator.epoch),
            json!(b64(native_advance)),
            json!(advance_hash),
            json!(forecast.revision),
            json!(wire::to_hex(&forecast.event_hash)),
            json!(value["revision"]),
            json!(value["event_hash"]),
            json!(value["snapshot_hash"]),
            json!(commitment),
            json!(b64(&observation.accumulator_account.data)),
            json!(accumulator.sealed_at),
            json!(accumulator.sealed_slot),
        ];
        let mut statements = self.epoch_sql(forecast_id, generation, &observation, &token)?;
        statements.extend(immutable("intake_seals", &fields, &values, &format!("{token}s")));
        statements.push((
            "INSERT INTO intake_seal_admissions(forecast_id,epoch,generation,context_slot) VALUES(?,?,?,?) \
             ON CONFLICT(forecast_id,epoch,generation) DO UPDATE SET context_slot=excluded.context_slot \
             WHERE excluded.context_slot>=intake_seal_admissions.context_slot"
                .to_string(),
            vec![
                json!(forecast_id),
                json!(accumulator.epoch),
                json!(generation),
                json!(slot),
            ],
        ));
        // The mirrored intake status is guarded atomically with the admission: a pending mirror
        // must be refreshed, never silently discounted.
        statements.push(guard(
            &format!("{token}u"),
            "NOT EXISTS(SELECT 1 FROM intake_receipts r JOIN intake_receipt_heads h ON h.address=r.address \
             WHERE r.forecast_id=? AND (h.native_status=1 OR (r.epoch=? AND h.native_status=3)))",
            vec![json!(forecast_id), json!(accumulator.epoch)],
        ));
        statements.push((
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(format!("{token}u"))],
        ));
        self.batch(forecast_id, generation, statements, &token).await?;

        Ok(json!({
            "epoch": accumulator.epoch, "generation": generation,
            "candidateEventHash": value["event_hash"], "candidateSnapshotHash": value["snapshot_hash"],
            "advanceHash": advance_hash, "sealCommitment": commitment, "finalizedContextSlot": slot,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    const FORECAST_ID: &str = "forecast-intake";
    const CREATOR: &str = "creator-1";

    struct Fixture {
        db: Sqlite,
        accounts: BTreeMap<String, (Vec<u8>, i64)>,
        /// The addresses that answer as programs. The on-chain program is one; every account it
        /// owns is not.
        programs: BTreeMap<String, String>,
        calls: Arc<Mutex<Vec<Value>>>,
    }

    impl Fixture {
        fn new(program: &[u8]) -> Self {
            let db = Sqlite::from_migrations();
            db.run(
                "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                &[
                    json!(CREATOR),
                    json!("Creator"),
                    json!("creator"),
                    json!("hash:creator"),
                    json!(1),
                ],
            )
            .expect("user");
            let _ = program;
            Self {
                db,
                accounts: BTreeMap::new(),
                programs: BTreeMap::new(),
                calls: Arc::new(Mutex::new(Vec::new())),
            }
        }

        fn serve(&mut self, address: &[u8], data: Vec<u8>, slot: i64) {
            self.accounts.insert(solana::base58_encode(address), (data, slot));
        }

        /// Register an address as an executable program owned by an upgradeable loader.
        fn serve_program(&mut self, address: &[u8]) {
            self.programs.insert(
                solana::base58_encode(address),
                "BPFLoaderUpgradeab1e11111111111111111111111".to_string(),
            );
        }

        /// The transport every account is served from, reporting `owner` as the program that owns
        /// them. The owner is a parameter rather than a constant so a test can serve an account
        /// that belongs to something else and watch the mirror refuse it.
        fn rpc(&self, owner: String) -> Rpc {
            let accounts = self.accounts.clone();
            let programs = self.programs.clone();
            let calls = self.calls.clone();
            Box::new(move |method: String, params: Vec<Value>| {
                calls.lock().unwrap().push(json!({"method": method, "params": params}));
                let accounts = accounts.clone();
                let programs = programs.clone();
                let owner = owner.clone();
                Box::pin(async move {
                    match method.as_str() {
                        "getGenesisHash" => Ok(json!(DEVNET_GENESIS)),
                        _ => {
                            let address = params[0].as_str().unwrap_or_default().to_string();
                            if let Some(program_owner) = programs.get(&address) {
                                return Ok(json!({
                                    "context": {"slot": 1},
                                    "value": {"owner": program_owner, "executable": true,
                                              "lamports": 1, "data": ["", "base64"]},
                                }));
                            }
                            let Some((data, slot)) = accounts.get(&address) else {
                                return Ok(json!({"context": {"slot": 1}, "value": Value::Null}));
                            };
                            Ok(json!({
                                "context": {"slot": slot},
                                "value": {"owner": owner, "executable": false, "lamports": 1,
                                          "data": [b64(data), "base64"]},
                            }))
                        }
                    }
                })
            })
        }
    }

    #[test]
    fn the_mirror_activates_mirrors_and_imports() {
        let program = [7u8; 32];
        let mut fixture = Fixture::new(&program);
        let fid = identity_hash("forecast", FORECAST_ID);
        let forecast_address = solana::forecast_address(&program, &fid).unwrap().0;
        let gate_address = wire::gate_address(&program, &forecast_address).unwrap().0;
        let specification = [3u8; 32];
        let creator = identity_hash("creator", CREATOR);
        let event = [13u8; 32];

        // A live epoch: one accepted dispute is outstanding, which is what a receipt must bind to.
        let accumulator = wire::Accumulator::new(wire::AccumulatorParts {
            forecast: &forecast_address,
            specification: &specification,
            epoch: 1,
            revision: 1,
            proposal_revision: 1,
            resolution: &[6u8; 32],
            proposal_event: &event,
            opened: 10,
            deadline: 100,
            pending: 1,
            material: 0,
            accepted: 1,
            head: &[9u8; 32],
            phase: 1,
        })
        .expect("an open accumulator");

        let forecast_bytes = forecast_account(&fid, &specification, &creator);
        let user = [11u8; 32];
        let nonce = [12u8; 32];
        let body = b"forecast evidence body";
        let receipt_address = wire::receipt_address(&program, &forecast_address, 1, &user).unwrap().0;
        let receipt = wire::Receipt::new(wire::ReceiptParts {
            forecast: &forecast_address,
            specification: &specification,
            program: &program,
            epoch: 1,
            proposal_revision: 1,
            resolution: &[6u8; 32],
            proposal_event: &event,
            user: &user,
            nonce: &nonce,
            body,
        })
        .expect("a draft receipt")
        .accepted(50, 9, 100)
        .expect("an accepted receipt");

        // One finalized slot for all three: the observation reads the gate, then the receipt, then
        // the forecast, then the gate again, and each read must be at or after the last.
        fixture.serve(&gate_address, accumulator.encode(), 20);
        fixture.serve(&forecast_address, forecast_bytes, 20);
        fixture.serve(&receipt_address, receipt.encode(), 20);
        fixture
            .db
            .run(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
                 normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                &[
                    json!(FORECAST_ID),
                    json!(CREATOR),
                    json!("draft:intake"),
                    json!("{}"),
                    json!(1),
                    json!("OPEN"),
                    json!("TECHNOLOGY"),
                    json!("Will intake work?"),
                    json!("Will intake work?"),
                    json!("will intake work?"),
                    json!(wire::to_hex(&specification)),
                    json!(1),
                    json!(100),
                    json!(1),
                    json!(1),
                    json!("publish"),
                ],
            )
            .expect("forecast");

        fixture.serve_program(&program);
        let rpc = fixture.rpc(solana::base58_encode(&program));
        // After the acceptance and before the deadline, so the import is on time. `lateImport` is
        // a real distinction and the fixture has to sit on one side of it deliberately.
        let now = || 60i64;
        let intake = DisputeIntake {
            db: &fixture.db,
            rpc: &rpc,
            program: program.to_vec(),
            now_ms: &now,
        };

        assert_eq!(block(intake.activate(FORECAST_ID)).expect("an activation"), 1);
        let binding = fixture
            .db
            .run(
                "SELECT * FROM intake_bindings WHERE forecast_id=?",
                &[json!(FORECAST_ID)],
            )
            .expect("binding")
            .0;
        assert_eq!(int(&binding[0], "generation"), Some(1));
        assert_eq!(
            text(&binding[0], "accumulator_address"),
            Some(solana::base58_encode(&gate_address).as_str())
        );

        let refreshed = block(intake.refresh(FORECAST_ID, 1)).expect("a refresh");
        assert_eq!(refreshed.accumulator.epoch, 1);
        let head = fixture
            .db
            .run("SELECT * FROM intake_heads WHERE forecast_id=?", &[json!(FORECAST_ID)])
            .expect("head")
            .0;
        assert_eq!(int(&head[0], "epoch"), Some(1));
        assert_eq!(int(&head[0], "pending_count"), Some(1));
        assert_eq!(int(&head[0], "context_slot"), Some(20));

        let imported = block(intake.import_receipt(FORECAST_ID, &receipt_address, 1)).expect("an import");
        assert_eq!(imported["nativeStatus"], 1);
        assert_eq!(imported["historical"], false);
        assert_eq!(imported["lateImport"], false);
        let receipt_row = fixture
            .db
            .run(
                "SELECT * FROM intake_receipts WHERE address=?",
                &[json!(solana::base58_encode(&receipt_address))],
            )
            .expect("receipt")
            .0;
        assert_eq!(int(&receipt_row[0], "accepted_slot"), Some(9));

        // A repeated import of the same chain state is the same request, not a second one.
        let again = block(intake.import_receipt(FORECAST_ID, &receipt_address, 1)).expect("a retry");
        assert_eq!(again, imported);
        let count = fixture
            .db
            .run("SELECT COUNT(*) n FROM intake_receipt_observations", &[])
            .expect("count")
            .0;
        assert_eq!(int(&count[0], "n"), Some(1), "the observation is immutable");

        // Advancing the generation is once, and the old one can no longer write.
        assert_eq!(block(intake.advance_generation(FORECAST_ID, 1)).expect("an advance"), 2);
        let stale = block(intake.refresh(FORECAST_ID, 1)).unwrap_err();
        assert_eq!(stale.0, "intake_generation_changed");
    }

    /// An OPEN forecast account: the state a receipt is imported against.
    fn forecast_account(fid: &[u8; 32], specification: &[u8; 32], creator: &[u8; 32]) -> Vec<u8> {
        let mut out = b"FNFORE01".to_vec();
        out.extend(fid);
        out.extend(creator);
        out.extend(specification);
        out.extend(10i64.to_le_bytes());
        out.extend(100i64.to_le_bytes());
        out.extend(5u64.to_le_bytes());
        out.extend(150i64.to_le_bytes());
        out.extend([6u8, 1, 0, 0]);
        out.extend([13u8; 32]);
        out.extend([14u8; 32]);
        out.extend([6u8; 32]);
        out.extend([0u8; 32]);
        out.extend([0u8; 32]);
        out.extend([0u8; 32]);
        out.extend(100i64.to_le_bytes());
        out.extend(100i64.to_le_bytes());
        out.extend(0i64.to_le_bytes());
        out.extend(0u16.to_le_bytes());
        out.extend(0u16.to_le_bytes());
        assert_eq!(out.len(), solana::FORECAST_SIZE);
        out
    }
}
