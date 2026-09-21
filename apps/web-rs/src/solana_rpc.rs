//! The fail-closed Solana RPC transport.
//!
//! Every method here is a sequence of refusals, and the refusals are the point: this is the only
//! code in the port that spends money and the only code that writes to a chain. So each step
//! re-derives what it is about to trust — the genesis hash before anything else, the program's own
//! executability before signing, the relayer's authority from the *program's* configuration rather
//! than from local belief, and the blockhash's validity both before and after simulation, because
//! a simulated transaction is not a landed one.
//!
//! `assemble_transaction` is not a formality either: it re-reads the message it was handed and
//! refuses to assemble one whose signer list is not its account list's prefix.

use crate::registry_chain::RegistryAccount;
use crate::solana::{
    assemble_transaction, base58_decode, base58_encode, compile_message, config_address, decode_config,
    decode_forecast, encode_advance, encode_register, forecast_address, AccountMeta, Instruction, RegisterParts,
};
use serde_json::{json, Value};

/// `_MAX_INTEGER`.
pub const MAX_INTEGER: i64 = 9_007_199_254_740_991;
/// The System program, whose all-zero key is also the owner of an unallocated PDA.
pub const SYSTEM_PROGRAM: [u8; 32] = [0u8; 32];
pub const LOADER: &str = "BPFLoaderUpgradeab1e11111111111111111111111";
pub const CLOCK_SYSVAR: &str = "SysvarC1ock11111111111111111111111111111111";
pub const SYSVAR_OWNER: &str = "Sysvar1111111111111111111111111111111111111";
/// The committed Clock sysvar layout is 40 bytes: slot, epoch start, epoch, leader schedule epoch,
/// unix timestamp.
const CLOCK_SIZE: usize = 40;
/// A registry account's data is bounded because the RPC reply is what an attacker controls.
const MAX_REGISTRY_DATA: usize = 480;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SolanaRpcError(pub String);

impl std::fmt::Display for SolanaRpcError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

fn require(condition: bool, message: &str) -> Result<(), SolanaRpcError> {
    if condition {
        Ok(())
    } else {
        Err(SolanaRpcError(message.to_string()))
    }
}

fn integer(value: Option<&Value>, name: &str) -> Result<i64, SolanaRpcError> {
    match value.and_then(Value::as_i64) {
        Some(number) if (0..=MAX_INTEGER).contains(&number) => Ok(number),
        _ => Err(SolanaRpcError(format!("Invalid {name}"))),
    }
}

fn public_key(value: &[u8], name: &str) -> Result<[u8; 32], SolanaRpcError> {
    require(
        value.len() == 32 && value.iter().any(|byte| *byte != 0),
        &format!("Invalid {name}"),
    )
    .map_err(|_| SolanaRpcError("Invalid public key".to_string()))?;
    let mut key = [0u8; 32];
    key.copy_from_slice(value);
    Ok(key)
}

/// `_context`: every account-shaped reply carries the slot it was observed at, and the slot is
/// what makes the observation usable — a value without one cannot be compared against anything.
fn context(response: &Value) -> Result<(i64, Value), SolanaRpcError> {
    require(
        response.is_object() && response.get("value").is_some(),
        "Invalid RPC response",
    )?;
    let context = response.get("context");
    require(context.is_some_and(Value::is_object), "Missing RPC context")?;
    let slot = integer(context.and_then(|context| context.get("slot")), "context slot")?;
    Ok((slot, response["value"].clone()))
}

/// A boxed future that borrows for as long as its caller does. Distinct from `wallets`' own alias,
/// which is `'static`: this module's seams reach a database handle that belongs to the request.
pub type BoxFuture<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + 'a>>;

/// The three seams the transport is built from, each carrying the lifetime of what it borrows.
///
/// They are *not* `'static`, and cannot be: the spend authorizer reads the database it reserves
/// against, and that handle belongs to the request. A `'static` alias would force every caller to
/// own a database it does not own — or to leak one per request.
pub type Rpc<'a> = dyn Fn(String, Value) -> BoxFuture<'a, Result<Value, ()>> + 'a;
pub type Signer<'a> = dyn Fn(Vec<u8>) -> BoxFuture<'a, Result<[u8; 64], ()>> + 'a;
/// The authorizer's refusal travels with its own message: in the reference it is an exception the
/// transport does not catch, and `daily spend limit` is what the caller reads.
pub type SpendAuthorizer<'a> = dyn Fn(i64) -> BoxFuture<'a, Result<(), String>> + 'a;

/// `SolanaRpcTransport`.
pub struct SolanaRpcTransport<'a, 'b> {
    program_id: [u8; 32],
    relayer: [u8; 32],
    expected_genesis_hash: String,
    rpc: &'a Rpc<'b>,
    sign: &'a Signer<'b>,
    authorize_spend: &'a SpendAuthorizer<'b>,
    max_fee_lamports: i64,
    max_rent_lamports: i64,
    balance_floor_lamports: i64,
    config: [u8; 32],
}

impl<'a, 'b> SolanaRpcTransport<'a, 'b> {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        rpc: &'a Rpc<'b>,
        sign: &'a Signer<'b>,
        program_id: &[u8],
        relayer: &[u8],
        expected_genesis_hash: &str,
        authorize_spend: &'a SpendAuthorizer<'b>,
        max_fee_lamports: i64,
        max_rent_lamports: i64,
        balance_floor_lamports: i64,
    ) -> Result<Self, SolanaRpcError> {
        let program_id = public_key(program_id, "program ID")?;
        let relayer = public_key(relayer, "relayer")?;
        require(program_id != relayer, "Program and relayer must differ")?;
        base58_decode(expected_genesis_hash, 32).map_err(SolanaRpcError)?;
        let max_fee_lamports = integer(Some(&json!(max_fee_lamports)), "fee cap")?;
        let max_rent_lamports = integer(Some(&json!(max_rent_lamports)), "rent cap")?;
        let balance_floor_lamports = integer(Some(&json!(balance_floor_lamports)), "balance floor")?;
        require(
            max_fee_lamports > 0 && max_rent_lamports > 0,
            "Positive fee and rent caps required",
        )?;
        let config = config_address(&program_id).map_err(SolanaRpcError)?.0;
        Ok(Self {
            program_id,
            relayer,
            expected_genesis_hash: expected_genesis_hash.to_string(),
            rpc,
            sign,
            authorize_spend,
            max_fee_lamports,
            max_rent_lamports,
            balance_floor_lamports,
            config,
        })
    }

    /// `_call`. A provider's error message may contain request headers, tokens or raw bodies, so
    /// what is *logged* is the method and the frames and what is *raised* is this crate's own
    /// sentence — never the provider's.
    async fn call(&self, method: &str, params: Value) -> Result<Value, SolanaRpcError> {
        match (self.rpc)(method.to_string(), params).await {
            Ok(value) => Ok(value),
            Err(()) => {
                log_event(json!({"event": "registry_rpc_transport_error", "method": method}));
                Err(SolanaRpcError(format!("Solana RPC unavailable: {method}")))
            }
        }
    }

    pub async fn genesis_hash(&self) -> Result<String, SolanaRpcError> {
        let value = self.call("getGenesisHash", json!([])).await?;
        require(
            value.as_str() == Some(self.expected_genesis_hash.as_str()),
            "Solana cluster does not match the pinned genesis",
        )?;
        Ok(self.expected_genesis_hash.clone())
    }

    /// `finalized_time_ms`. `getBlockTime` is a separate block-time estimate and cannot attest that
    /// the registry's own `Clock::get().unix_timestamp` has reached a challenge deadline — so this
    /// reads the sysvar the program actually reads.
    pub async fn finalized_time_ms(&self) -> Result<i64, SolanaRpcError> {
        self.genesis_hash().await?;
        let response = self
            .call(
                "getAccountInfo",
                json!([CLOCK_SYSVAR, {"encoding": "base64", "commitment": "finalized"}]),
            )
            .await?;
        let (slot, value) = context(&response)?;
        require(
            value.is_object() && value["owner"] == json!(SYSVAR_OWNER) && value["executable"] == json!(false),
            "Invalid Clock sysvar account",
        )?;
        require(
            integer(value.get("lamports"), "Clock account balance")? > 0,
            "Clock sysvar account is not allocated",
        )?;
        let decoded = self.sysvar_bytes(&value, CLOCK_SIZE, "Clock sysvar")?;
        // `<QqQQq`: the slot the sysvar reports has to be the slot the RPC context reported, or the
        // two observations are about different chain states.
        let mut slot_bytes = [0u8; 8];
        slot_bytes.copy_from_slice(&decoded[0..8]);
        require(
            i64::try_from(u64::from_le_bytes(slot_bytes)).ok() == Some(slot),
            "Clock sysvar does not match the finalized context",
        )?;
        let mut seconds = [0u8; 8];
        seconds.copy_from_slice(&decoded[32..40]);
        integer(
            Some(&json!(i64::from_le_bytes(seconds) * 1000)),
            "finalized Clock time in milliseconds",
        )
    }

    /// A base64 account's bytes, with the canonicality the reference demands: re-encoding has to
    /// give back exactly what was sent, because two encodings of one account is a confusion.
    fn sysvar_bytes(&self, value: &Value, size: usize, what: &str) -> Result<Vec<u8>, SolanaRpcError> {
        let encoded = value.get("data");
        require(
            encoded.is_some_and(|encoded| {
                encoded.as_array().is_some_and(|parts| {
                    parts.len() == 2
                        && parts[1] == json!("base64")
                        && parts[0].as_str().is_some_and(|text| text.len() == base64_len(size))
                })
            }),
            &format!("Invalid {what} encoding"),
        )?;
        let text = encoded.unwrap()[0].as_str().unwrap_or("");
        let Ok(data) = base64_decode(text) else {
            return Err(SolanaRpcError(format!("Invalid {what} base64")));
        };
        require(data.len() == size, &format!("Invalid {what} layout"))?;
        require(base64_encode(&data) == text, &format!("Invalid {what} layout"))?;
        Ok(data)
    }

    /// `_valid_blockhash`. Called *before* signing and again after simulation, because a blockhash
    /// that expired during the round trip is a transaction that will be rejected on arrival.
    async fn valid_blockhash(&self, blockhash: &[u8], last_height: i64, min_slot: i64) -> Result<(), SolanaRpcError> {
        let response = self
            .call(
                "isBlockhashValid",
                json!([base58_encode(blockhash), {"commitment": "finalized", "minContextSlot": min_slot}]),
            )
            .await?;
        let (slot, valid) = context(&response)?;
        require(
            slot >= min_slot && valid == json!(true),
            "Transaction blockhash expired or RPC is stale",
        )?;
        let height = integer(
            Some(
                &self
                    .call(
                        "getBlockHeight",
                        json!([{"commitment": "finalized", "minContextSlot": min_slot}]),
                    )
                    .await?,
            ),
            "current block height",
        )?;
        require(height <= last_height, "Transaction block height expired")
    }

    /// `account`. A System-owned target with no data is `None` rather than registry state: anyone
    /// can transfer lamports to a predictable PDA, and prefunding must not deny publication.
    pub async fn account(&self, address: &[u8]) -> Result<Option<RegistryAccount>, SolanaRpcError> {
        let address = public_key(address, "address")?;
        self.genesis_hash().await?;
        let response = self
            .call(
                "getAccountInfo",
                json!([base58_encode(&address), {"encoding": "base64", "commitment": "finalized"}]),
            )
            .await?;
        let (slot, value) = context(&response)?;
        if value.is_null() {
            return Ok(None);
        }
        require(
            value.is_object() && value["executable"] == json!(false),
            "Invalid registry account",
        )?;
        let owner = base58_decode(value["owner"].as_str().unwrap_or(""), 32).map_err(SolanaRpcError)?;
        require(
            owner == self.program_id.to_vec() || owner == SYSTEM_PROGRAM.to_vec(),
            "Registry account has the wrong owner",
        )?;
        integer(value.get("lamports"), "account balance")?;
        let encoded = value.get("data");
        require(
            encoded.is_some_and(|encoded| {
                encoded.as_array().is_some_and(|parts| {
                    parts.len() == 2
                        && parts[1] == json!("base64")
                        && parts[0].as_str().is_some_and(|text| text.len() <= MAX_REGISTRY_DATA)
                })
            }),
            "Invalid or oversized registry account data",
        )?;
        let text = encoded.unwrap()[0].as_str().unwrap_or("");
        let Ok(data) = base64_decode(text) else {
            return Err(SolanaRpcError("Invalid registry base64 data".to_string()));
        };
        require(base64_encode(&data) == text, "Noncanonical registry data")?;
        if owner == SYSTEM_PROGRAM.to_vec() {
            require(data.is_empty(), "System-owned registry target contains unexpected data")?;
            return Ok(None);
        }
        if address == self.config {
            decode_config(&data).map_err(SolanaRpcError)?;
        } else {
            let decoded = decode_forecast(&data).map_err(SolanaRpcError)?;
            require(
                forecast_address(&self.program_id, &decoded.forecast_id_hash)
                    .map_err(SolanaRpcError)?
                    .0
                    == address,
                "Registry account PDA mismatch",
            )?;
        }
        Ok(Some(RegistryAccount {
            address: address.to_vec(),
            owner,
            data,
            slot,
            finalized: true,
        }))
    }
}

/// One structured line, on whichever console exists.
///
/// `worker::console_log!` panics off the wasm target rather than doing nothing, which the port's
/// own tests would meet the moment a refusal path was exercised natively.
pub(crate) fn log_event(event: Value) {
    #[cfg(target_arch = "wasm32")]
    worker::console_log!("{}", event);
    #[cfg(not(target_arch = "wasm32"))]
    eprintln!("{event}");
}

fn base64_len(size: usize) -> usize {
    size.div_ceil(3) * 4
}

fn base64_encode(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

fn base64_decode(text: &str) -> Result<Vec<u8>, ()> {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.decode(text).map_err(|_| ())
}

/// `_instruction`. Only the two exact registry instructions may be relayed, and the bytes have to
/// *re-encode* to themselves: a noncanonical encoding of a valid instruction is a second byte
/// string for one operation, and a relayer that forwards both is a relayer with a replay surface.
pub fn check_instruction(
    data: &[u8],
    address: &[u8; 32],
    program_id: &[u8; 32],
    register: bool,
) -> Result<(), SolanaRpcError> {
    let expected = if register { 193 } else { 255 };
    require(
        data.len() == expected && data[0] == if register { 1 } else { 2 },
        "Only exact registry register/advance instructions may be relayed",
    )?;
    // The register layout is `<qqQq` at 97 — two signed, one unsigned, one signed — so the fields
    // are read at their own widths rather than as four of one kind.
    let canonical = if register {
        encode_register(RegisterParts {
            forecast_id_hash: &data[1..33],
            creator_hash: &data[33..65],
            specification_hash: &data[65..97],
            open_at_ms: i64::from_le_bytes(data[97..105].try_into().unwrap()),
            close_at_ms: i64::from_le_bytes(data[105..113].try_into().unwrap()),
            revision: i64::from_le_bytes(data[113..121].try_into().unwrap()),
            occurred_at_ms: i64::from_le_bytes(data[121..129].try_into().unwrap()),
            event_hash: &data[129..161],
            snapshot_hash: &data[161..193],
        })
        .map_err(SolanaRpcError)?
    } else {
        // `<Qq` at 1 and `<qHH` at 243, which is where the advance layout puts its tail.
        encode_advance(&crate::solana::AdvanceFields {
            revision: i64::from_le_bytes(data[1..9].try_into().unwrap()),
            occurred_at_ms: i64::from_le_bytes(data[9..17].try_into().unwrap()),
            previous_event_hash: &data[17..49],
            event_hash: &data[49..81],
            snapshot_hash: &data[81..113],
            state: i64::from(data[113]),
            outcome: i64::from(data[114]),
            resolution_hash: &data[115..147],
            dispute_hash: &data[147..179],
            reputation_hash: &data[179..211],
            trigger_hash: &data[211..243],
            challenge_until_ms: i64::from_le_bytes(data[243..251].try_into().unwrap()),
            pending_disputes: u16::from_le_bytes(data[251..253].try_into().unwrap()) as i64,
            material_disputes: u16::from_le_bytes(data[253..255].try_into().unwrap()) as i64,
        })
        .map_err(SolanaRpcError)?
    };
    require(canonical == data, "Noncanonical instruction")?;
    if register {
        require(
            forecast_address(program_id, &data[1..33]).map_err(SolanaRpcError)?.0 == *address,
            "Publication instruction targets the wrong PDA",
        )?;
    }
    Ok(())
}

impl SolanaRpcTransport<'_, '_> {
    /// `send`. The whole method is an ordering, and the ordering is the safety property: nothing is
    /// signed until the program is known to be executable, the relayer's authority has been read
    /// out of the *program's* configuration, the fee and rent are inside their caps, the relayer's
    /// balance covers them plus its floor, and the blockhash is still valid. Only then is the spend
    /// authorized.
    #[allow(clippy::too_many_lines)]
    pub async fn send(
        &self,
        instruction: &[u8],
        forecast_address: &[u8],
        register: bool,
    ) -> Result<String, SolanaRpcError> {
        let target_address = public_key(forecast_address, "target")?;
        check_instruction(instruction, &target_address, &self.program_id, register)?;
        self.genesis_hash().await?;
        // A misconfigured RPC or program has to fail before any operational signing.
        let program_response = self
            .call(
                "getAccountInfo",
                json!([base58_encode(&self.program_id), {"encoding": "base64", "commitment": "finalized",
                                                         "dataSlice": {"offset": 0, "length": 0}}]),
            )
            .await?;
        let (_, program) = context(&program_response)?;
        require(
            program.is_object() && program["executable"] == json!(true) && program["owner"] == json!(LOADER),
            "Pinned registry program is not executable",
        )?;
        let Some(config) = self.account(&self.config).await? else {
            return Err(SolanaRpcError("Registry configuration is missing".to_string()));
        };
        let decoded_config = decode_config(&config.data).map_err(SolanaRpcError)?;
        require(decoded_config.relayer == self.relayer, "Relayer is not authorized")?;
        let target = self.account(&target_address).await?;
        require(
            if register { target.is_none() } else { target.is_some() },
            "Registry target does not match the requested operation",
        )?;
        if let Some(target) = &target {
            if !register {
                let current = decode_forecast(&target.data).map_err(SolanaRpcError)?;
                // An advance has to *extend* the finalized predecessor: the same revision or a
                // different previous event is a fork, and the program would reject it anyway.
                let revision = i64::from_le_bytes(instruction[1..9].try_into().unwrap());
                require(
                    revision == current.revision + 1 && instruction[17..49] == current.event_hash,
                    "Advance does not extend the finalized predecessor",
                )?;
            }
        }
        let block_response = self
            .call("getLatestBlockhash", json!([{"commitment": "finalized"}]))
            .await?;
        let (context_slot, block) = context(&block_response)?;
        require(
            context_slot >= config.slot.max(target.as_ref().map_or(0, |target| target.slot)),
            "Blockhash RPC is behind the observed registry state",
        )?;
        require(block.is_object(), "Invalid blockhash response")?;
        let blockhash = base58_decode(block["blockhash"].as_str().unwrap_or(""), 32).map_err(SolanaRpcError)?;
        let last_height = integer(block.get("lastValidBlockHeight"), "blockhash expiry")?;
        let mut accounts = vec![
            AccountMeta {
                pubkey: self.relayer,
                is_signer: true,
                is_writable: true,
            },
            AccountMeta {
                pubkey: self.config,
                is_signer: false,
                is_writable: false,
            },
            AccountMeta {
                pubkey: target_address,
                is_signer: false,
                is_writable: true,
            },
        ];
        if register {
            accounts.push(AccountMeta {
                pubkey: SYSTEM_PROGRAM,
                is_signer: false,
                is_writable: false,
            });
        }
        let message = compile_message(
            &self.relayer,
            &blockhash,
            &[Instruction {
                program_id: self.program_id,
                accounts,
                data: instruction.to_vec(),
            }],
        )
        .map_err(SolanaRpcError)?;
        require(
            message.signer_keys.len() == 1 && message.signer_keys[0] == self.relayer,
            "Unexpected transaction signer",
        )?;
        let fee_response = self
            .call(
                "getFeeForMessage",
                json!([base64_encode(&message.data), {"commitment": "finalized", "minContextSlot": context_slot}]),
            )
            .await?;
        let (fee_slot, fee) = context(&fee_response)?;
        require(fee_slot >= context_slot, "Fee RPC is behind the observed blockhash")?;
        let fee = integer(Some(&fee), "transaction fee")?;
        require(fee > 0 && fee <= self.max_fee_lamports, "Transaction fee exceeds cap")?;
        let rent = if register {
            integer(
                Some(
                    &self
                        .call(
                            "getMinimumBalanceForRentExemption",
                            json!([360, {"commitment": "finalized"}]),
                        )
                        .await?,
                ),
                "account rent",
            )?
        } else {
            0
        };
        require(
            rent <= self.max_rent_lamports && (!register || rent > 0),
            "Account rent exceeds cap",
        )?;
        let balance_response = self
            .call(
                "getBalance",
                json!([base58_encode(&self.relayer), {"commitment": "finalized", "minContextSlot": context_slot}]),
            )
            .await?;
        let (balance_slot, balance) = context(&balance_response)?;
        require(
            balance_slot >= context_slot,
            "Balance RPC is behind the observed blockhash",
        )?;
        require(
            integer(Some(&balance), "relayer balance")? >= fee + rent + self.balance_floor_lamports,
            "Relayer balance is below the protected floor",
        )?;
        self.valid_blockhash(&blockhash, last_height, context_slot).await?;
        (self.authorize_spend)(fee + rent).await.map_err(SolanaRpcError)?;
        let signature = (self.sign)(message.data.clone())
            .await
            .map_err(|_| SolanaRpcError("Signer returned an invalid signature".to_string()))?;
        require(
            signature.iter().any(|byte| *byte != 0),
            "Signer returned an invalid signature",
        )?;
        let transaction = assemble_transaction(&message, &[(self.relayer, signature)]).map_err(SolanaRpcError)?;
        let encoded = base64_encode(&transaction);
        let simulation_response = self
            .call(
                "simulateTransaction",
                json!([encoded, {"encoding": "base64", "sigVerify": true, "replaceRecentBlockhash": false,
                                 "commitment": "confirmed", "minContextSlot": context_slot}]),
            )
            .await?;
        let (simulation_slot, simulation) = context(&simulation_response)?;
        require(
            simulation_slot >= context_slot
                && simulation.is_object()
                && simulation.get("err").is_some()
                && simulation["err"].is_null(),
            "Transaction simulation failed",
        )?;
        // Again: simulation takes time, and a blockhash that expired during it is a transaction the
        // cluster will reject on arrival.
        self.valid_blockhash(&blockhash, last_height, context_slot).await?;
        let expected_signature = base58_encode(&signature);
        let returned = self
            .call(
                "sendTransaction",
                json!([encoded, {"encoding": "base64", "skipPreflight": false, "preflightCommitment": "confirmed",
                                 "maxRetries": 0, "minContextSlot": context_slot}]),
            )
            .await?;
        // A node that returns a different signature has sent a *different* transaction, and the
        // reference refuses to reconcile that silently: retrying would be a second spend.
        require(
            returned.as_str() == Some(expected_signature.as_str()),
            "RPC returned a different transaction signature; reconcile before retry",
        )?;
        Ok(expected_signature)
    }

    /// `signature_finalized`. `processed` and `confirmed` are not final: a confirmation that can
    /// still be rolled back is not evidence a memo landed.
    pub async fn signature_finalized(&self, signature: &str) -> Result<bool, SolanaRpcError> {
        // Unwrapped in the reference too: the decoder's own message is what a caller reads.
        base58_decode(signature, 64).map_err(SolanaRpcError)?;
        self.genesis_hash().await?;
        let response = self
            .call(
                "getSignatureStatuses",
                json!([[signature], {"searchTransactionHistory": true}]),
            )
            .await?;
        let (_, values) = context(&response)?;
        let values = values.as_array().cloned().unwrap_or_default();
        require(values.len() == 1, "Invalid transaction status response")?;
        let status = &values[0];
        if status.is_null() {
            return Ok(false);
        }
        require(
            status.is_object() && status.get("err").is_some(),
            "Invalid transaction status",
        )?;
        require(status["err"].is_null(), "Transaction failed on chain")?;
        integer(status.get("slot"), "transaction slot")?;
        let confirmation = status.get("confirmationStatus").and_then(Value::as_str);
        require(
            matches!(confirmation, Some("processed" | "confirmed" | "finalized")),
            "Unknown transaction confirmation state",
        )?;
        if confirmation == Some("finalized") {
            require(
                status.get("confirmations").is_some() && status["confirmations"].is_null(),
                "Inconsistent finalized transaction status",
            )?;
            return Ok(true);
        }
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::solana::{base58_encode, config_address, forecast_address};
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    const PROGRAM: [u8; 32] = [10u8; 32];
    const RELAYER: [u8; 32] = [20u8; 32];

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/solana-rpc-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("solana rpc golden")).expect("json")
    }

    fn unbase64(text: &str) -> Vec<u8> {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD.decode(text).expect("base64")
    }

    /// The genesis the fixture pins, taken from the vector rather than restated: a port that
    /// disagreed about it would fail every case for the same reason and the vector would say so.
    fn genesis(document: &Value) -> String {
        document["genesis"].as_str().unwrap().to_string()
    }

    /// Replay one case: the recorded RPC replies, in order, with the method and parameters
    /// asserted as they are consumed.
    fn replay(document: &Value, entry: &Value) -> (Result<Value, SolanaRpcError>, usize) {
        let log = Rc::new(RefCell::new(entry["rpc"].as_array().cloned().unwrap_or_default()));
        let cursor = Rc::new(Cell::new(0usize));
        let name = entry["call"].as_str().unwrap().to_string();
        // Each closure takes its own handle on the shared state, so the case name and the cursor
        // stay usable after the moves.
        let cursor_for_rpc = Rc::clone(&cursor);
        let name_for_rpc = name.clone();
        let rpc = move |method: String, params: Value| -> BoxFuture<'static, Result<Value, ()>> {
            let index = cursor_for_rpc.get();
            cursor_for_rpc.set(index + 1);
            let name = &name_for_rpc;
            let recorded = log.borrow();
            let Some(expected) = recorded.get(index).cloned() else {
                panic!(
                    "{name}: the port asked for RPC call {index}, the vector recorded {}",
                    recorded.len()
                );
            };
            assert_eq!(expected["method"], json!(method), "{name}: RPC order");
            assert_eq!(expected["params"], params, "{name}: RPC parameters");
            let failed = !expected["errorType"].is_null();
            let response = expected["response"].clone();
            Box::pin(async move {
                if failed {
                    Err(())
                } else {
                    Ok(response)
                }
            })
        };
        let configured = Rc::new(RefCell::new(Vec::<Value>::new()));
        // What the signer returns comes out of the vector, so `send:zero-signature` — a slot
        // nobody signed — is replayed rather than assumed away.
        let returned: [u8; 64] = {
            let raw = unbase64(entry["signature"].as_str().unwrap_or(""));
            let mut signature = [0u8; 64];
            signature.copy_from_slice(&raw);
            signature
        };
        let signer = {
            let configured = Rc::clone(&configured);
            move |message: Vec<u8>| -> BoxFuture<'static, Result<[u8; 64], ()>> {
                configured.borrow_mut().push(json!(base64_encode(&message)));
                Box::pin(async move { Ok(returned) })
            }
        };
        let spend = Rc::new(RefCell::new(Vec::<Value>::new()));
        let authorize = {
            let spend = Rc::clone(&spend);
            let name = name.clone();
            move |amount: i64| -> BoxFuture<'static, Result<(), String>> {
                spend.borrow_mut().push(json!(amount));
                // `send:spend-rejected` is the one case whose authorizer refuses, and its message
                // is the authorizer's own rather than something this layer chose.
                let rejected = name == "send:spend-rejected";
                Box::pin(async move {
                    if rejected {
                        Err("daily spend limit".to_string())
                    } else {
                        Ok(())
                    }
                })
            }
        };
        let transport = SolanaRpcTransport::new(
            &rpc,
            &signer,
            &PROGRAM,
            &RELAYER,
            &genesis(document),
            &authorize,
            20_000,
            10_000_000,
            10_000_000,
        )
        .expect("the reference's own configuration");
        let input = &entry["input"];
        let kind = input["kind"].as_str().unwrap();
        let address = |field: &str| -> [u8; 32] {
            let raw = base58_decode(input[field].as_str().unwrap_or(""), 32).expect("a key");
            let mut key = [0u8; 32];
            key.copy_from_slice(&raw);
            key
        };
        let produced = match kind {
            "send" => block(transport.send(
                &unbase64(input["instruction"].as_str().unwrap()),
                &address("target"),
                input["register"].as_bool().unwrap_or(false),
            ))
            .map(|signature| json!(signature)),
            "account" => block(transport.account(&address("address"))).map(|value| match value {
                None => Value::Null,
                Some(account) => json!({
                    "address": base58_encode(&account.address),
                    "owner": base58_encode(&account.owner),
                    "data": base64_encode(&account.data),
                    "slot": account.slot,
                    "finalized": account.finalized,
                }),
            }),
            "finalized_time_ms" => block(transport.finalized_time_ms()).map(|value| json!(value)),
            "signature_finalized" => {
                block(transport.signature_finalized(input["signature"].as_str().unwrap())).map(|value| json!(value))
            }
            other => panic!("{name}: unknown kind {other}"),
        };
        // The signer was called exactly as often as the vector says, and the spend was authorized
        // for exactly the amount it says: both are effects, so both are compared.
        assert_eq!(
            json!(configured.borrow().clone()),
            entry["signed"],
            "{name}: the signer was handed different bytes"
        );
        assert_eq!(
            json!(spend.borrow().clone()),
            entry["spend"],
            "{name}: a different spend was authorized"
        );
        (produced, cursor.get())
    }

    #[test]
    fn the_reference_rpc_transport_is_reproduced_case_for_case() {
        let document = golden();
        for entry in document["cases"].as_array().expect("cases") {
            let name = entry["call"].as_str().unwrap();
            let (produced, consumed) = replay(&document, entry);
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
                    assert_eq!(error.0, entry["error"].as_str().unwrap(), "{name}: a different refusal");
                }
            }
            // Every recorded RPC call was consumed, in order, with its parameters compared as it
            // went: a port that reached the same answer through a different sequence of gates has
            // not reproduced the sequence, and the sequence is the safety property.
            assert_eq!(
                Some(consumed),
                entry["rpc"].as_array().map(Vec::len),
                "{name}: a different number of RPC calls"
            );
        }
        assert_eq!(config_address(&PROGRAM).unwrap().0.len(), 32);
        assert_eq!(forecast_address(&PROGRAM, &[1u8; 32]).unwrap().0.len(), 32);
    }
}
