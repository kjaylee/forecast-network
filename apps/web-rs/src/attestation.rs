//! Wallet-signed Devnet memo attestations of a forecaster's own receipt.
//!
//! The Worker cannot reach public Devnet RPC — Cloudflare egress is refused — so the *phone* does
//! the network work: it fetches a recent blockhash, asks this service for a relayer-fee-paid
//! transaction, has the Seed Vault wallet co-sign it, submits it, and reports the signature. This
//! module only builds messages, signs as the relayer, and records what the device reports.
//!
//! Memo v2 requires every listed account to sign, which is exactly the proof wanted: the
//! forecaster's key vouches for the memo, not ours. So the transaction this hands back is
//! deliberately *not* complete — one signature slot is real and the rest are zeroes for the wallet
//! to fill.

use crate::db::{self, Database};
use crate::solana::{base58_decode, base58_encode, compile_message, shortvec, AccountMeta, Instruction};
use crate::wallets::BoxFuture;
use forecast_domain::content_hash;
use serde_json::{json, Value};

/// `MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr`.
pub const MEMO_PROGRAM: &str = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr";
pub const MEMO_VERSION: &str = "forecast-attest-v1";
pub const DAY_MS: i64 = 86_400_000;
/// `^[1-9A-HJ-NP-Za-km-z]{86,88}$` — base58, never the padded or all-zero form.
pub fn valid_signature_text(value: &str) -> bool {
    (86..=88).contains(&value.len())
        && value
            .bytes()
            .all(|byte| b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz".contains(&byte))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttestationError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl AttestationError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

fn requires_forecast() -> AttestationError {
    AttestationError::new(
        409,
        "attestation_requires_forecast",
        "Record a forecast before stamping it on-chain.",
    )
}

fn requires_wallet() -> AttestationError {
    AttestationError::new(
        409,
        "attestation_requires_wallet",
        "Sign in with your wallet to stamp a forecast on-chain.",
    )
}

fn bad_blockhash() -> AttestationError {
    AttestationError::new(400, "attestation_blockhash", "A recent Devnet blockhash is required.")
}

fn confirm_invalid() -> AttestationError {
    AttestationError::new(
        400,
        "attestation_confirm_invalid",
        "A transaction signature is required.",
    )
}

fn not_found() -> AttestationError {
    AttestationError::new(404, "attestation_not_found", "This attestation was not prepared here.")
}

fn unavailable() -> AttestationError {
    AttestationError::new(503, "attestation_unavailable", "On-chain stamping is not configured.")
}

fn storage() -> AttestationError {
    AttestationError::new(503, "attestation_unavailable", "On-chain stamping is not configured.")
}

/// `memo_text`.
pub fn memo_text(forecast_id: &str, receipt_hash: &str) -> String {
    format!("{MEMO_VERSION} forecast={forecast_id} receipt={receipt_hash}")
}

/// `partially_signed_transaction`: real signatures where they are known, zeroed slots otherwise.
///
/// The zeroed slot is the point — the wallet fills it — so a signature of the wrong width is a
/// refusal rather than something padded into place.
pub fn partially_signed_transaction(
    message: &[u8],
    signers: &[[u8; 32]],
    signatures: &[[u8; 64]],
) -> Result<Vec<u8>, String> {
    let mut wire = shortvec(signers.len() as i64)?;
    for (index, _) in signers.iter().enumerate() {
        match signatures.get(index) {
            Some(signature) if signature != &[0u8; 64] => wire.extend_from_slice(signature),
            _ => wire.extend_from_slice(&[0u8; 64]),
        }
    }
    wire.extend_from_slice(message);
    Ok(wire)
}

/// A base58 account key of exactly the width the runtime requires.
fn account(value: &str) -> Result<[u8; 32], AttestationError> {
    let raw = base58_decode(value, 32).map_err(|_| requires_wallet())?;
    let mut key = [0u8; 32];
    key.copy_from_slice(&raw);
    Ok(key)
}

/// `sign(message)` as the relayer, injected so the key never enters this crate.
pub type RelayerSigner = dyn Fn(Vec<u8>) -> BoxFuture<Result<[u8; 64], ()>>;
/// `rate_limit(scope, limit, window_ms)`, injected because the limiter is storage.
pub type RateLimit = dyn Fn(String, i64, i64) -> BoxFuture<Result<(), ()>>;

/// `Attestations`.
pub struct Attestations<'a> {
    pub db: &'a dyn Database,
    /// `None` when the relayer is not configured, which is `available == false`.
    pub relayer: Option<[u8; 32]>,
    pub sign: Option<&'a RelayerSigner>,
    pub now_ms: &'a dyn Fn() -> i64,
    pub random_token: &'a dyn Fn() -> String,
    pub rate_limit: &'a RateLimit,
}

impl Attestations<'_> {
    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    pub fn available(&self) -> bool {
        self.relayer.is_some() && self.sign.is_some()
    }

    /// `_receipt`: the hash of the *retained* submission, not of anything re-sent, and the
    /// address the user's active wallet identity carries.
    async fn receipt(&self, user_id: &str, forecast_id: &str) -> Result<(String, String), AttestationError> {
        let row = self
            .db
            .first(
                "SELECT body FROM eligible_user_forecasts WHERE forecast_id=? AND user_id=?",
                &[json!(forecast_id), json!(user_id)],
            )
            .await
            .map_err(|_| storage())?;
        let Some(row) = row else {
            return Err(requires_forecast());
        };
        let wallet = self
            .db
            .first(
                "SELECT address FROM wallet_identities WHERE user_id=? AND status='active'",
                &[json!(user_id)],
            )
            .await
            .map_err(|_| storage())?;
        let Some(wallet) = wallet else {
            return Err(requires_wallet());
        };
        let address = db::text(&wallet, "address").unwrap_or("").to_string();
        let hash = hex::encode(sha256(db::text(&row, "body").unwrap_or("").as_bytes()));
        Ok((hash, address))
    }

    /// `prepare`.
    pub async fn prepare(&self, user_id: &str, forecast_id: &str, body: &Value) -> Result<Value, AttestationError> {
        let (Some(relayer), Some(sign)) = (self.relayer, self.sign) else {
            return Err(unavailable());
        };
        let Some(blockhash) = body.get("blockhash").and_then(Value::as_str) else {
            return Err(bad_blockhash());
        };
        let Ok(blockhash_bytes) = base58_decode(blockhash, 32) else {
            return Err(bad_blockhash());
        };
        // The limit is per user per day, and it is taken *before* the receipt is read: a stamp is
        // a chain write paid for by the relayer, so the cost is bounded whether or not the request
        // turns out to be valid.
        (self.rate_limit)(format!("attest:{user_id}"), 20, DAY_MS)
            .await
            .map_err(|_| storage())?;
        let (receipt_hash, address) = self.receipt(user_id, forecast_id).await?;
        let signer = account(&address)?;
        let memo = memo_text(forecast_id, &receipt_hash);
        let mut program = [0u8; 32];
        program.copy_from_slice(&base58_decode(MEMO_PROGRAM, 32).map_err(|_| unavailable())?);
        let instruction = Instruction {
            program_id: program,
            accounts: vec![AccountMeta {
                pubkey: signer,
                is_signer: true,
                is_writable: false,
            }],
            data: memo.as_bytes().to_vec(),
        };
        let compiled = compile_message(&relayer, &blockhash_bytes, &[instruction]).map_err(|_| unavailable())?;
        let relayer_signature = sign(compiled.data.clone()).await.map_err(|_| storage())?;
        // Only the relayer's slot is filled. The rest are the wallet's, and zeroes are what the
        // device expects to replace.
        let mut signatures = vec![[0u8; 64]; compiled.signer_keys.len()];
        if let Some(index) = compiled.signer_keys.iter().position(|key| key == &relayer) {
            signatures[index] = relayer_signature;
        }
        let transaction = partially_signed_transaction(&compiled.data, &compiled.signer_keys, &signatures)
            .map_err(|_| unavailable())?;
        let message_hash = hex::encode(sha256(&compiled.data));
        let token = (self.random_token)();
        let attestation_id = format!("at_{}", &token[..24.min(token.len())]);
        let now = self.now();
        self.db
            .execute(
                concat!(
                    "INSERT INTO forecast_attestations(id,forecast_id,user_id,address,receipt_hash,memo,blockhash,",
                    "message_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)",
                ),
                &[
                    json!(attestation_id),
                    json!(forecast_id),
                    json!(user_id),
                    json!(address),
                    json!(receipt_hash),
                    json!(memo),
                    json!(blockhash),
                    json!(message_hash),
                    json!(now),
                    json!(now),
                ],
            )
            .await
            .map_err(|_| storage())?;
        Ok(json!({
            "attestationId": attestation_id,
            "transaction": base64_encode(&transaction),
            "memo": memo,
            "signerAddress": address,
            "feePayer": base58_encode(&relayer),
            "cluster": "devnet",
            "expiresAt": now + 90_000,
        }))
    }

    /// `confirm`. A retry with the *same* signature is the device reporting again; a different one
    /// is a conflict, because the attestation is a statement about one transaction.
    pub async fn confirm(&self, user_id: &str, forecast_id: &str, body: &Value) -> Result<Value, AttestationError> {
        let (Some(attestation_id), Some(signature)) = (
            body.get("attestationId").and_then(Value::as_str),
            body.get("signature").and_then(Value::as_str),
        ) else {
            return Err(confirm_invalid());
        };
        if !valid_signature_text(signature) {
            return Err(confirm_invalid());
        }
        if body
            .get("slot")
            .is_some_and(|slot| slot.as_i64().is_none_or(|slot| slot < 0))
        {
            return Err(confirm_invalid());
        }
        let slot = body.get("slot").and_then(Value::as_i64);
        let row = self
            .db
            .first(
                "SELECT * FROM forecast_attestations WHERE id=? AND user_id=? AND forecast_id=?",
                &[json!(attestation_id), json!(user_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| storage())?;
        let Some(row) = row else {
            return Err(not_found());
        };
        if db::text(&row, "status") == Some("prepared") {
            self.db
                .execute(
                    concat!(
                        "UPDATE forecast_attestations SET status='submitted',signature=?,reported_slot=?,updated_at=? ",
                        "WHERE id=? AND status='prepared'",
                    ),
                    &[json!(signature), json!(slot), json!(self.now()), json!(attestation_id)],
                )
                .await
                .map_err(|_| storage())?;
        } else if db::text(&row, "signature") != Some(signature) {
            return Err(AttestationError::new(
                409,
                "attestation_conflict",
                "This attestation already has a different signature.",
            ));
        }
        match self.status(Some(user_id), forecast_id).await? {
            Some(status) => Ok(status),
            None => Err(not_found()),
        }
    }

    /// `status`. No user is not "nothing stamped" — it is nothing to report, which is why the
    /// reference answers `None` rather than a status.
    pub async fn status(&self, user_id: Option<&str>, forecast_id: &str) -> Result<Option<Value>, AttestationError> {
        let Some(user_id) = user_id else {
            return Ok(None);
        };
        let row = self
            .db
            .first(
                concat!(
                    "SELECT id,status,signature,memo,reported_slot,created_at FROM forecast_attestations ",
                    "WHERE user_id=? AND forecast_id=? AND status IN ('submitted','verified') ",
                    "ORDER BY created_at DESC LIMIT 1",
                ),
                &[json!(user_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| storage())?;
        let Some(row) = row else {
            return Ok(Some(json!({"status": "none", "available": self.available()})));
        };
        let signature = db::text(&row, "signature").unwrap_or("");
        let memo = db::text(&row, "memo").unwrap_or("");
        Ok(Some(json!({
            "status": db::text(&row, "status"),
            "signature": signature,
            "memo": memo,
            "slot": db::int(&row, "reported_slot"),
            "at": db::int(&row, "created_at"),
            "cluster": "devnet",
            "explorer": format!("https://explorer.solana.com/tx/{signature}?cluster=devnet"),
            "available": self.available(),
            "commitment": content_hash(&json!({"memo": memo})).unwrap_or_default(),
        })))
    }
}

fn sha256(bytes: &[u8]) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes).to_vec()
}

/// `base64.b64encode(...).decode("ascii")`.
fn base64_encode(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use std::cell::RefCell;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/attestation-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("attestation golden")).expect("json")
    }

    /// The same synthetic relayer signature the vector used: sixty-four bytes that depend on every
    /// byte of the message, so the recorded transaction is a check on the compiled message.
    fn sign(message: &[u8]) -> [u8; 64] {
        let mut out = [0u8; 64];
        out[..32].copy_from_slice(&sha256(message));
        let mut second = Vec::from(&b"second:"[..]);
        second.extend_from_slice(message);
        out[32..].copy_from_slice(&sha256(&second));
        out
    }

    fn key(value: &str) -> [u8; 32] {
        let raw = base58_decode(value, 32).expect("a fixture key");
        let mut out = [0u8; 32];
        out.copy_from_slice(&raw);
        out
    }

    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, AttestationError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
        match produced {
            Ok(value) => {
                assert!(
                    entry["error"].is_null(),
                    "{name}: succeeded where the reference refused"
                );
                assert_eq!(value, entry["result"], "{name}: a different result");
            }
            Err(error) => {
                assert!(
                    !entry["error"].is_null(),
                    "{name}: refused with {error:?} where the reference succeeded"
                );
                assert_eq!(
                    error.status as i64,
                    entry["error"]["status"].as_i64().unwrap(),
                    "{name}: status"
                );
                assert_eq!(Some(error.code), entry["error"]["code"].as_str(), "{name}: code");
                assert_eq!(
                    Some(error.message),
                    entry["error"]["message"].as_str(),
                    "{name}: message"
                );
            }
        }
        *index += 1;
    }

    /// Seed the three forecasters the vector's fixture has: one with a wallet and a receipt, one
    /// with neither, and one with a receipt and no wallet.
    fn seed(db: &Sqlite, document: &Value) {
        for user in ["u-wallet", "u-nowallet", "u-noreceipt", "u-receipt-nowallet"] {
            db.run(
                "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
                &[
                    json!(user),
                    json!(user),
                    json!(user),
                    json!(format!("recovery:{user}")),
                    json!(document["now"]),
                ],
            )
            .expect("user");
        }
        db.run(
            concat!(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,",
                "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) ",
                "VALUES('f-att','u-wallet','d-att','{}',1,'OPEN','CRYPTO','t','q','q',?,0,?,1,1,'k')",
            ),
            &[
                json!("a".repeat(64)),
                json!(document["now"].as_i64().unwrap() + 86_400_000),
            ],
        )
        .expect("forecast");
        for (user, at) in [("u-wallet", -500i64), ("u-receipt-nowallet", -400)] {
            db.run(
                concat!(
                    "INSERT INTO user_forecasts(forecast_id,user_id,outcome,confidence,yes_probability,submitted_at,",
                    "revision,body) VALUES('f-att',?,'YES',80,80,?,1,'{\"outcome\":\"YES\"}')",
                ),
                &[json!(user), json!(document["now"].as_i64().unwrap() + at)],
            )
            .expect("submission");
        }
        db.run(
            "INSERT INTO wallet_identities(address,user_id,status,created_at) VALUES(?,?,'active',?)",
            &[document["wallet"].clone(), json!("u-wallet"), json!(document["now"])],
        )
        .expect("wallet");
    }

    #[test]
    fn the_reference_attestation_service_is_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let db = Sqlite::from_migrations();
        seed(&db, &document);
        let now = document["now"].as_i64().unwrap();
        let clock = || now;
        // Owned rather than borrowed, so the closures are `'static` whatever they capture: the
        // futures they return have to be.
        let counter = std::rc::Rc::new(RefCell::new(0));
        let token = {
            let counter = std::rc::Rc::clone(&counter);
            move || {
                *counter.borrow_mut() += 1;
                format!("{:0<32}", format!("attestationtoken{}", counter.borrow()))
            }
        };
        let limited = std::rc::Rc::new(RefCell::new(Vec::<Value>::new()));
        let seen = std::rc::Rc::clone(&limited);
        let rate_limit = move |scope: String, limit: i64, window_ms: i64| -> BoxFuture<Result<(), ()>> {
            limited
                .borrow_mut()
                .push(json!({"scope": scope, "limit": limit, "windowMs": window_ms}));
            Box::pin(async { Ok(()) })
        };
        let relayer = key(document["relayer"].as_str().unwrap());
        let signer = |message: Vec<u8>| -> BoxFuture<Result<[u8; 64], ()>> {
            let signature = sign(&message);
            Box::pin(async move { Ok(signature) })
        };
        let service = Attestations {
            db: &db,
            relayer: Some(relayer),
            sign: Some(&signer),
            now_ms: &clock,
            random_token: &token,
            rate_limit: &rate_limit,
        };
        let blockhash = document["blockhash"].clone();
        let mut index = 0usize;

        // --- prepare: everything that has to be true before the relayer signs anything.
        check(
            &calls,
            &mut index,
            block(service.prepare("u-wallet", "f-att", &json!({}))),
        );
        check(
            &calls,
            &mut index,
            block(service.prepare("u-wallet", "f-att", &json!({"blockhash": 7}))),
        );
        check(
            &calls,
            &mut index,
            block(service.prepare("u-wallet", "f-att", &json!({"blockhash": "0OIl"}))),
        );
        check(
            &calls,
            &mut index,
            block(service.prepare("u-noreceipt", "f-att", &json!({"blockhash": blockhash}))),
        );
        check(
            &calls,
            &mut index,
            block(service.prepare("u-receipt-nowallet", "f-att", &json!({"blockhash": blockhash}))),
        );
        let first = block(service.prepare("u-wallet", "f-att", &json!({"blockhash": blockhash})));
        let attestation_id = first.as_ref().unwrap()["attestationId"].as_str().unwrap().to_string();
        check(&calls, &mut index, first);

        // --- confirm: idempotent for the same signature, a conflict for a different one.
        let signature = base58_encode(&(1u8..=64).collect::<Vec<u8>>());
        let other = base58_encode(&(2u8..=66).collect::<Vec<u8>>());
        check(
            &calls,
            &mut index,
            block(service.confirm(
                "u-wallet",
                "f-att",
                &json!({"attestationId": attestation_id, "signature": "too-short"}),
            )),
        );
        check(
            &calls,
            &mut index,
            block(service.confirm(
                "u-wallet",
                "f-att",
                &json!({"attestationId": "at_unknown", "signature": signature}),
            )),
        );
        check(
            &calls,
            &mut index,
            block(service.confirm(
                "u-wallet",
                "f-att",
                &json!({"attestationId": attestation_id, "signature": signature, "slot": -1}),
            )),
        );
        check(
            &calls,
            &mut index,
            block(service.confirm(
                "u-wallet",
                "f-att",
                &json!({"attestationId": attestation_id, "signature": signature, "slot": 12345}),
            )),
        );
        check(
            &calls,
            &mut index,
            block(service.confirm(
                "u-wallet",
                "f-att",
                &json!({"attestationId": attestation_id, "signature": signature, "slot": 12345}),
            )),
        );
        check(
            &calls,
            &mut index,
            block(service.confirm(
                "u-wallet",
                "f-att",
                &json!({"attestationId": attestation_id, "signature": other, "slot": 12346}),
            )),
        );

        // --- status: no user is nothing to report; a user with nothing stamped says so.
        check(
            &calls,
            &mut index,
            block(service.status(None, "f-att")).map(|value| value.unwrap_or(Value::Null)),
        );
        check(
            &calls,
            &mut index,
            block(service.status(Some("u-wallet"), "f-att")).map(|value| value.unwrap_or(Value::Null)),
        );
        check(
            &calls,
            &mut index,
            block(service.status(Some("u-nowallet"), "f-att")).map(|value| value.unwrap_or(Value::Null)),
        );

        // --- unavailable: a service with no relayer refuses rather than building a transaction
        // nobody can pay for.
        let offline_db = Sqlite::from_migrations();
        seed(&offline_db, &document);
        let offline = Attestations {
            db: &offline_db,
            relayer: None,
            sign: None,
            now_ms: &clock,
            random_token: &token,
            rate_limit: &rate_limit,
        };
        let offline_calls = document["unavailableCalls"].as_array().expect("unavailableCalls");
        let mut offline_index = 0usize;
        check(
            offline_calls,
            &mut offline_index,
            block(offline.prepare("u-wallet", "f-att", &json!({"blockhash": blockhash}))),
        );
        check(
            offline_calls,
            &mut offline_index,
            block(offline.status(Some("u-wallet"), "f-att")).map(|value| value.unwrap_or(Value::Null)),
        );
        assert_eq!(offline_index, offline_calls.len());

        assert_eq!(index, calls.len(), "every recorded call is replayed");
        assert_eq!(
            json!(seen.borrow().clone()),
            document["rateLimited"],
            "the rate limiter saw different scopes"
        );
        for (table, expected) in document["rows"].as_object().expect("rows") {
            let (rows, _) = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .expect("rows");
            assert_eq!(json!(rows), *expected, "{table}: different rows");
        }
    }
}
