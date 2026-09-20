//! The Solana wire primitives: base58, PDA derivation, instruction encoding, message compilation.
//!
//! Everything here produces bytes that either verify on chain or do not, so the whole module is
//! written to be compared byte for byte rather than reasoned about.
//!
//! Two details are not ordinary serialisation and are the reason this is a port rather than a
//! reimplementation:
//!
//!   - **The PDA bump search matches `curve25519-dalek` decompression, not strict Ed25519
//!     verification.** Small-order points and noncanonical field encodings are decompressible
//!     too, so treating them as off-curve finds a different address than Solana does. The check
//!     below is decompression, deliberately.
//!   - **Message compilation orders accounts by privilege class and then by raw public-key
//!     bytes**, with the writable fee payer first — the official SDK's order, which the runtime
//!     will not accept an alternative to.

use curve25519_dalek::edwards::CompressedEdwardsY;
use sha2::{Digest, Sha256};

pub const MAX_INTEGER: i64 = 9_007_199_254_740_991;
pub const MAX_TRANSACTION_BYTES: usize = 1232;

const ALPHABET: &[u8; 58] = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
pub const ZERO: [u8; 32] = [0u8; 32];

pub fn require(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message.to_string())
    }
}

/// `_raw`: exactly `size` bytes, optionally non-zero.
pub fn raw(value: &[u8], size: usize, name: &str, nonzero: bool) -> Result<Vec<u8>, String> {
    require(value.len() == size, &format!("invalid {name} length"))?;
    if nonzero {
        require(value.iter().any(|byte| *byte != 0), &format!("invalid {name}"))?;
    }
    Ok(value.to_vec())
}

/// Whether every byte is zero. The reference's accounts compare several commitments against the
/// all-zero value, which is how "absent" is spelled on chain.
pub fn zeroed(value: &[u8]) -> bool {
    value.iter().all(|byte| *byte == 0)
}

/// `_integer`.
pub fn integer(value: i64, low: i64, high: i64) -> Result<i64, String> {
    require(low <= value && value <= high, "integer outside allowed range")?;
    Ok(value)
}

/// `base58_encode`.
pub fn base58_encode(value: &[u8]) -> String {
    let leading = value.iter().take_while(|byte| **byte == 0).count();
    let mut digits: Vec<u8> = Vec::new();
    for byte in value.iter().skip(leading) {
        let mut carry = *byte as u32;
        for digit in digits.iter_mut() {
            carry += (*digit as u32) << 8;
            *digit = (carry % 58) as u8;
            carry /= 58;
        }
        while carry > 0 {
            digits.push((carry % 58) as u8);
            carry /= 58;
        }
    }
    let mut text = String::from_utf8(vec![b'1'; leading]).unwrap_or_default();
    for digit in digits.iter().rev() {
        text.push(ALPHABET[*digit as usize] as char);
    }
    text
}

/// `base58_decode`: a fixed length, with leading `1`s becoming leading zero bytes.
pub fn base58_decode(value: &str, length: usize) -> Result<Vec<u8>, String> {
    integer(length as i64, 1, MAX_TRANSACTION_BYTES as i64)?;
    require(!value.is_empty() && value.len() <= length * 2, "invalid base58 length")?;
    let leading = value.bytes().take_while(|byte| *byte == b'1').count();
    let mut bytes: Vec<u8> = Vec::new();
    for character in value.bytes().skip(leading) {
        let digit = ALPHABET
            .iter()
            .position(|candidate| *candidate == character)
            .ok_or_else(|| "invalid base58 character".to_string())? as u32;
        // `number = number * 58 + digit`, most significant byte last while building.
        let mut carry = digit;
        for byte in bytes.iter_mut() {
            carry += (*byte as u32) * 58;
            *byte = (carry & 0xff) as u8;
            carry >>= 8;
        }
        while carry > 0 {
            bytes.push((carry & 0xff) as u8);
            carry >>= 8;
        }
    }
    let mut decoded = vec![0u8; leading];
    decoded.extend(bytes.iter().rev());
    raw(&decoded, length, "decoded base58", false)
}

/// `is_edwards_point`: decompression, not verification.
///
/// A small-order point and a noncanonical encoding both decompress, and Solana's PDA search takes
/// the first digest that does not — so a stricter check here would find a different address.
pub fn is_edwards_point(encoded: &[u8]) -> Result<bool, String> {
    raw(encoded, 32, "compressed point", false)?;
    let mut bytes = [0u8; 32];
    bytes.copy_from_slice(encoded);
    Ok(CompressedEdwardsY(bytes).decompress().is_some())
}

/// `find_program_address`: the highest bump whose digest is not on the curve.
pub fn find_program_address(program_id: &[u8], seeds: &[Vec<u8>]) -> Result<([u8; 32], u8), String> {
    raw(program_id, 32, "program ID", false)?;
    require(seeds.len() <= 15, "PDA permits at most 15 seeds plus bump")?;
    for seed in seeds {
        require(seed.len() <= 32, "PDA seed exceeds 32 bytes")?;
    }
    for bump in (0..=255u8).rev() {
        let mut hasher = Sha256::new();
        for seed in seeds {
            hasher.update(seed);
        }
        hasher.update([bump]);
        hasher.update(program_id);
        hasher.update(b"ProgramDerivedAddress");
        let digest: [u8; 32] = hasher.finalize().into();
        if !is_edwards_point(&digest)? {
            return Ok((digest, bump));
        }
    }
    Err("no viable PDA bump".to_string())
}

pub fn config_address(program_id: &[u8]) -> Result<([u8; 32], u8), String> {
    find_program_address(program_id, &[b"config".to_vec()])
}

pub fn forecast_address(program_id: &[u8], forecast_id_hash: &[u8]) -> Result<([u8; 32], u8), String> {
    find_program_address(
        program_id,
        &[
            b"forecast".to_vec(),
            raw(forecast_id_hash, 32, "forecast identity", true)?,
        ],
    )
}

/// `shortvec`: canonical Solana compact-u16.
pub fn shortvec(value: i64) -> Result<Vec<u8>, String> {
    let mut value = integer(value, 0, 65_535)?;
    let mut encoded = Vec::new();
    loop {
        let digit = (value & 127) as u8;
        value >>= 7;
        encoded.push(digit | if value != 0 { 128 } else { 0 });
        if value == 0 {
            return Ok(encoded);
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountMeta {
    pub pubkey: [u8; 32],
    pub is_signer: bool,
    pub is_writable: bool,
}

impl AccountMeta {
    pub fn new(pubkey: &[u8], is_signer: bool, is_writable: bool) -> Result<Self, String> {
        let mut key = [0u8; 32];
        key.copy_from_slice(&raw(pubkey, 32, "account", false)?);
        Ok(Self {
            pubkey: key,
            is_signer,
            is_writable,
        })
    }
}

#[derive(Debug, Clone)]
pub struct Instruction {
    pub program_id: [u8; 32],
    pub accounts: Vec<AccountMeta>,
    pub data: Vec<u8>,
}

impl Instruction {
    pub fn new(program_id: &[u8], accounts: Vec<AccountMeta>, data: Vec<u8>) -> Result<Self, String> {
        let mut key = [0u8; 32];
        key.copy_from_slice(&raw(program_id, 32, "program ID", false)?);
        Ok(Self {
            program_id: key,
            accounts,
            data,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledMessage {
    pub data: Vec<u8>,
    pub signer_keys: Vec<[u8; 32]>,
    pub account_keys: Vec<[u8; 32]>,
}

/// `compile_message`: one legacy message, in the order the runtime requires.
pub fn compile_message(
    payer: &[u8],
    blockhash: &[u8],
    instructions: &[Instruction],
) -> Result<CompiledMessage, String> {
    let mut payer_key = [0u8; 32];
    payer_key.copy_from_slice(&raw(payer, 32, "fee payer", true)?);
    let mut blockhash_key = [0u8; 32];
    blockhash_key.copy_from_slice(&raw(blockhash, 32, "blockhash", true)?);
    require(
        !instructions.is_empty() && instructions.len() <= 256,
        "invalid instructions",
    )?;

    let mut privileges: std::collections::BTreeMap<[u8; 32], (bool, bool)> = std::collections::BTreeMap::new();
    privileges.insert(payer_key, (true, true));
    for instruction in instructions {
        for account in &instruction.accounts {
            let entry = privileges.entry(account.pubkey).or_insert((false, false));
            entry.0 |= account.is_signer;
            entry.1 |= account.is_writable;
        }
        privileges.entry(instruction.program_id).or_insert((false, false));
    }
    require(
        privileges.len() <= 256,
        "legacy transaction supports at most 256 accounts",
    )?;

    // BTreeMap order is raw public-key lexical order, which is the SDK's order within a class;
    // the writable fee payer is placed first rather than sorted.
    let mut ordered: Vec<[u8; 32]> = privileges.keys().filter(|key| **key != payer_key).cloned().collect();
    ordered.sort_by_key(|key| {
        let (signer, writable) = privileges[key];
        (!signer, !writable, *key)
    });
    let mut keys = vec![payer_key];
    keys.extend(ordered);
    let signers: Vec<[u8; 32]> = keys.iter().filter(|key| privileges[*key].0).cloned().collect();
    let readonly_signed = privileges.values().filter(|(s, w)| *s && !*w).count();
    let readonly_unsigned = privileges.values().filter(|(s, w)| !*s && !*w).count();
    require(
        signers.len() < 128 && readonly_unsigned <= 255,
        "legacy header exceeds bounds",
    )?;

    let mut data = vec![signers.len() as u8, readonly_signed as u8, readonly_unsigned as u8];
    data.extend(shortvec(keys.len() as i64)?);
    for key in &keys {
        data.extend(key);
    }
    data.extend(blockhash_key);
    data.extend(shortvec(instructions.len() as i64)?);
    let indexes: std::collections::BTreeMap<[u8; 32], usize> =
        keys.iter().enumerate().map(|(index, key)| (*key, index)).collect();
    for instruction in instructions {
        data.push(indexes[&instruction.program_id] as u8);
        data.extend(shortvec(instruction.accounts.len() as i64)?);
        for account in &instruction.accounts {
            data.push(indexes[&account.pubkey] as u8);
        }
        data.extend(shortvec(instruction.data.len() as i64)?);
        data.extend(&instruction.data);
    }
    require(
        shortvec(signers.len() as i64)?.len() + 64 * signers.len() + data.len() <= MAX_TRANSACTION_BYTES,
        "transaction exceeds Solana packet size",
    )?;
    Ok(CompiledMessage {
        data,
        signer_keys: signers,
        account_keys: keys,
    })
}

/// `assemble_transaction`: the wire transaction, signatures in the message's own signer order.
///
/// Every check here is a check on the *message* the port just compiled, which is why they are
/// refusals rather than debug assertions: a message whose signer list is not its account list's
/// prefix is a message the runtime would reject, and a signature map that is missing one signer or
/// carrying an extra is a transaction that cannot be assembled at all. Signing with a zero key is
/// refused too — a slot filled with zeroes is a slot nobody signed.
pub fn assemble_transaction(message: &CompiledMessage, signatures: &[([u8; 32], [u8; 64])]) -> Result<Vec<u8>, String> {
    require(
        message.data.len() >= 3
            && !message.signer_keys.is_empty()
            && message.signer_keys.len() < 128
            && message.data[0] as usize == message.signer_keys.len()
            && message.account_keys.len() >= message.signer_keys.len()
            && message.account_keys[..message.signer_keys.len()] == message.signer_keys[..],
        "invalid signer ordering",
    )?;
    let mut distinct: Vec<[u8; 32]> = message.signer_keys.clone();
    distinct.sort();
    distinct.dedup();
    require(distinct.len() == message.signer_keys.len(), "invalid signer ordering")?;
    let mut encoded_keys = shortvec(message.account_keys.len() as i64)?;
    for key in &message.account_keys {
        encoded_keys.extend(raw(key, 32, "message account", false)?);
    }
    require(
        message.data[3..3 + encoded_keys.len()] == encoded_keys[..],
        "compiled message account metadata mismatch",
    )?;
    // The reference compares the signature map's *keys* with the signer list as a set, so a
    // signature for the wrong account is a refusal rather than something silently dropped, and the
    // wire is emitted in the message's own order rather than the caller's.
    require(
        signatures.len() == message.signer_keys.len(),
        "missing or extraneous signature",
    )?;
    let mut wire = shortvec(message.signer_keys.len() as i64)?;
    for key in &message.signer_keys {
        let Some((_, signature)) = signatures.iter().find(|(signed, _)| signed == key) else {
            return Err("missing or extraneous signature".to_string());
        };
        wire.extend(raw(signature, 64, "signature", true)?);
    }
    wire.extend_from_slice(&message.data);
    require(
        wire.len() <= MAX_TRANSACTION_BYTES,
        "transaction exceeds Solana packet size",
    )?;
    Ok(wire)
}

// ---------------------------------------------------------------- the publication side

pub const CONFIG_RESERVED: &[u8; 8] = b"FNCONF01";
pub const FORECAST_RESERVED: &[u8; 8] = b"FNFORE01";
pub const CONFIG_SIZE: usize = 104;
pub const FORECAST_SIZE: usize = 360;

/// `encode_initialize`.
pub fn encode_initialize(relayer: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = vec![0];
    out.extend(raw(relayer, 32, "relayer", true)?);
    Ok(out)
}

/// `encode_set_relayer`.
pub fn encode_set_relayer(relayer: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = vec![3];
    out.extend(raw(relayer, 32, "relayer", true)?);
    Ok(out)
}

/// `encode_propose_admin`.
pub fn encode_propose_admin(administrator: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = vec![4];
    out.extend(raw(administrator, 32, "administrator", true)?);
    Ok(out)
}

/// `encode_accept_admin`.
pub fn encode_accept_admin() -> Vec<u8> {
    vec![5]
}

pub struct RegisterParts<'a> {
    pub forecast_id_hash: &'a [u8],
    pub creator_hash: &'a [u8],
    pub specification_hash: &'a [u8],
    pub open_at_ms: i64,
    pub close_at_ms: i64,
    pub revision: i64,
    pub occurred_at_ms: i64,
    pub event_hash: &'a [u8],
    pub snapshot_hash: &'a [u8],
}

/// `encode_register`.
pub fn encode_register(parts: RegisterParts<'_>) -> Result<Vec<u8>, String> {
    let hashes = [
        raw(parts.forecast_id_hash, 32, "publication commitment", true)?,
        raw(parts.creator_hash, 32, "publication commitment", true)?,
        raw(parts.specification_hash, 32, "publication commitment", true)?,
    ];
    let trailing = [
        raw(parts.event_hash, 32, "publication commitment", true)?,
        raw(parts.snapshot_hash, 32, "publication commitment", true)?,
    ];
    for timestamp in [parts.open_at_ms, parts.close_at_ms, parts.occurred_at_ms] {
        integer(timestamp, 0, MAX_INTEGER)?;
    }
    integer(parts.revision, 1, MAX_INTEGER)?;
    if parts.open_at_ms >= parts.close_at_ms || parts.occurred_at_ms >= parts.close_at_ms {
        return Err("publication must precede close".to_string());
    }
    let mut out = vec![1];
    for hash in &hashes {
        out.extend(hash);
    }
    out.extend(parts.open_at_ms.to_le_bytes());
    out.extend(parts.close_at_ms.to_le_bytes());
    out.extend((parts.revision as u64).to_le_bytes());
    out.extend(parts.occurred_at_ms.to_le_bytes());
    for hash in &trailing {
        out.extend(hash);
    }
    Ok(out)
}

/// `ConfigAccount`: who administers the program, and who is proposed to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigAccount {
    pub administrator: [u8; 32],
    pub relayer: [u8; 32],
    pub pending_administrator: [u8; 32],
}

/// `decode_config`.
pub fn decode_config(data: &[u8]) -> Result<ConfigAccount, String> {
    raw(data, CONFIG_SIZE, "config account", false)?;
    if &data[0..8] != CONFIG_RESERVED {
        return Err("invalid config discriminator".to_string());
    }
    let mut administrator = [0u8; 32];
    administrator.copy_from_slice(&data[8..40]);
    let mut relayer = [0u8; 32];
    relayer.copy_from_slice(&data[40..72]);
    let mut pending_administrator = [0u8; 32];
    pending_administrator.copy_from_slice(&data[72..104]);
    let separated = !zeroed(&administrator) && !zeroed(&relayer) && administrator != relayer;
    if !separated {
        return Err("invalid authority separation".to_string());
    }
    // A pending administrator that is already one of the two current authorities is not a
    // rotation; it is a no-op that would silently leave the separation unchanged.
    if !zeroed(&pending_administrator) && (pending_administrator == administrator || pending_administrator == relayer) {
        return Err("invalid pending administrator".to_string());
    }
    Ok(ConfigAccount {
        administrator,
        relayer,
        pending_administrator,
    })
}

/// `ForecastAccount`: the on-chain publication, which the intake path reads to prove inclusion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForecastAccount {
    pub forecast_id_hash: [u8; 32],
    pub creator_hash: [u8; 32],
    pub specification_hash: [u8; 32],
    pub open_at_ms: i64,
    pub close_at_ms: i64,
    pub revision: i64,
    pub occurred_at_ms: i64,
    pub state: i64,
    pub outcome: i64,
    pub paused_from: i64,
    pub event_hash: [u8; 32],
    pub snapshot_hash: [u8; 32],
    pub resolution_hash: [u8; 32],
    pub dispute_hash: [u8; 32],
    pub reputation_hash: [u8; 32],
    pub trigger_hash: [u8; 32],
    pub challenge_until_ms: i64,
    pub chain_finalize_not_before_ms: i64,
    pub paused_at_ms: i64,
    pub pending_disputes: i64,
    pub material_disputes: i64,
}

fn at(raw: &[u8], from: usize) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(&raw[from..from + 32]);
    out
}

/// `decode_forecast`: the account, but only if every cross-field rule holds.
///
/// The rules are not decoration. A state byte and a resolution hash that disagree, a challenge
/// window on an unchallenged forecast, a reputation commitment before the outcome is known — each
/// would let an inclusion proof succeed against a record the program would never have written.
#[allow(clippy::too_many_lines)]
pub fn decode_forecast(data: &[u8]) -> Result<ForecastAccount, String> {
    raw(data, FORECAST_SIZE, "forecast account", false)?;
    if &data[0..8] != FORECAST_RESERVED || data[139] != 0 {
        return Err("invalid forecast discriminator/reserved".to_string());
    }
    let value = ForecastAccount {
        forecast_id_hash: at(data, 8),
        creator_hash: at(data, 40),
        specification_hash: at(data, 72),
        open_at_ms: i64::from_le_bytes(data[104..112].try_into().unwrap_or([0; 8])),
        close_at_ms: i64::from_le_bytes(data[112..120].try_into().unwrap_or([0; 8])),
        revision: u64::from_le_bytes(data[120..128].try_into().unwrap_or([0; 8])) as i64,
        occurred_at_ms: i64::from_le_bytes(data[128..136].try_into().unwrap_or([0; 8])),
        state: data[136] as i64,
        outcome: data[137] as i64,
        paused_from: data[138] as i64,
        event_hash: at(data, 140),
        snapshot_hash: at(data, 172),
        resolution_hash: at(data, 204),
        dispute_hash: at(data, 236),
        reputation_hash: at(data, 268),
        trigger_hash: at(data, 300),
        challenge_until_ms: i64::from_le_bytes(data[332..340].try_into().unwrap_or([0; 8])),
        chain_finalize_not_before_ms: i64::from_le_bytes(data[340..348].try_into().unwrap_or([0; 8])),
        paused_at_ms: i64::from_le_bytes(data[348..356].try_into().unwrap_or([0; 8])),
        pending_disputes: u16::from_le_bytes(data[356..358].try_into().unwrap_or([0; 2])) as i64,
        material_disputes: u16::from_le_bytes(data[358..360].try_into().unwrap_or([0; 2])) as i64,
    };
    for commitment in [
        value.forecast_id_hash,
        value.creator_hash,
        value.specification_hash,
        value.event_hash,
        value.snapshot_hash,
    ] {
        if zeroed(&commitment) {
            return Err("invalid required account commitment".to_string());
        }
    }
    for timestamp in [
        value.open_at_ms,
        value.close_at_ms,
        value.occurred_at_ms,
        value.challenge_until_ms,
        value.chain_finalize_not_before_ms,
        value.paused_at_ms,
    ] {
        integer(timestamp, 0, MAX_INTEGER)?;
    }
    integer(value.revision, 1, MAX_INTEGER)?;
    integer(value.state, 2, 11)?;
    if value.open_at_ms >= value.close_at_ms {
        return Err("invalid publication window".to_string());
    }
    let pause_ok = (value.state == 9 && (4..=8).contains(&value.paused_from) && value.paused_at_ms > 0)
        || (value.state != 9 && value.paused_from == 0 && value.paused_at_ms == 0);
    if !pause_ok {
        return Err("invalid pause state".to_string());
    }
    // A paused forecast is judged by the state it paused from, everywhere below.
    let effective = if value.state == 9 {
        value.paused_from
    } else {
        value.state
    };
    let resolved = effective >= 5;
    let resolution_ok = if resolved {
        !zeroed(&value.resolution_hash) && (1..=3).contains(&value.outcome) && value.chain_finalize_not_before_ms > 0
    } else {
        zeroed(&value.resolution_hash) && value.outcome == 0 && value.chain_finalize_not_before_ms == 0
    };
    if !resolution_ok {
        return Err("invalid resolution state".to_string());
    }
    let challenge_ok = if effective >= 6 {
        value.challenge_until_ms > 0
    } else {
        value.challenge_until_ms == 0 && value.pending_disputes == 0 && value.material_disputes == 0
    };
    if !challenge_ok {
        return Err("invalid challenge state".to_string());
    }
    if value.pending_disputes + value.material_disputes > 256 {
        return Err("too many disputes".to_string());
    }
    let finalization_ok = value.chain_finalize_not_before_ms >= value.challenge_until_ms
        && (!matches!(effective, 10 | 11) || value.occurred_at_ms >= value.challenge_until_ms);
    if !finalization_ok {
        return Err("invalid finalization time".to_string());
    }
    if matches!(effective, 6 | 10 | 11) && (value.pending_disputes != 0 || value.material_disputes != 0) {
        return Err("unresolved disputes".to_string());
    }
    if effective == 8 && (value.pending_disputes != 0 || value.material_disputes == 0) {
        return Err("invalid escalation".to_string());
    }
    if (value.pending_disputes != 0 || value.material_disputes != 0) && zeroed(&value.dispute_hash) {
        return Err("missing dispute commitment".to_string());
    }
    if effective < 10 && !zeroed(&value.reputation_hash) {
        return Err("premature reputation commitment".to_string());
    }
    if value.state == 2 && (!zeroed(&value.trigger_hash) || value.occurred_at_ms >= value.close_at_ms) {
        return Err("invalid open state".to_string());
    }
    if value.state != 2 && value.occurred_at_ms < value.close_at_ms && zeroed(&value.trigger_hash) {
        return Err("missing early trigger".to_string());
    }
    if value.occurred_at_ms < value.close_at_ms && resolved && value.outcome != 1 {
        return Err("early outcome must be YES".to_string());
    }
    Ok(value)
}

/// The fields of a forecast advance, as `encode_advance` writes them.
pub struct AdvanceFields<'a> {
    pub revision: i64,
    pub occurred_at_ms: i64,
    pub previous_event_hash: &'a [u8],
    pub event_hash: &'a [u8],
    pub snapshot_hash: &'a [u8],
    pub state: i64,
    pub outcome: i64,
    pub resolution_hash: &'a [u8],
    pub dispute_hash: &'a [u8],
    pub reputation_hash: &'a [u8],
    pub trigger_hash: &'a [u8],
    pub challenge_until_ms: i64,
    pub pending_disputes: i64,
    pub material_disputes: i64,
}

/// `encode_advance`: the 255-byte pre-image every advance commitment is taken over.
///
/// The validations are the point of having this at all. A decoder that re-encodes the fields it
/// read and compares is checking that the bytes are *reachable* from a valid encoding — a state
/// the program would never write cannot be smuggled in as a payload that happens to parse.
pub fn encode_advance(fields: &AdvanceFields<'_>) -> Result<Vec<u8>, String> {
    integer(fields.revision, 1, MAX_INTEGER)?;
    integer(fields.occurred_at_ms, 0, MAX_INTEGER)?;
    integer(fields.challenge_until_ms, 0, MAX_INTEGER)?;
    integer(fields.state, 2, 11)?;
    integer(fields.outcome, 0, 3)?;
    integer(fields.pending_disputes, 0, 256)?;
    integer(fields.material_disputes, 0, 256)?;
    require(
        fields.pending_disputes + fields.material_disputes <= 256,
        "too many disputes",
    )?;
    let previous = raw(fields.previous_event_hash, 32, "event commitment", true)?;
    let event = raw(fields.event_hash, 32, "event commitment", true)?;
    let snapshot = raw(fields.snapshot_hash, 32, "event commitment", true)?;
    require(previous != event, "new event must differ from predecessor")?;
    let resolution = raw(fields.resolution_hash, 32, "optional commitment", false)?;
    let dispute = raw(fields.dispute_hash, 32, "optional commitment", false)?;
    let reputation = raw(fields.reputation_hash, 32, "optional commitment", false)?;
    let trigger = raw(fields.trigger_hash, 32, "optional commitment", false)?;
    // A count of disputes with no commitment to what they are is a count nobody can check.
    require(
        (fields.pending_disputes == 0 && fields.material_disputes == 0) || !zeroed(&dispute),
        "dispute commitment required",
    )?;
    let mut out = vec![2u8];
    out.extend((fields.revision as u64).to_le_bytes());
    out.extend(fields.occurred_at_ms.to_le_bytes());
    out.extend(previous);
    out.extend(event);
    out.extend(snapshot);
    out.push(fields.state as u8);
    out.push(fields.outcome as u8);
    out.extend(resolution);
    out.extend(dispute);
    out.extend(reputation);
    out.extend(trigger);
    out.extend(fields.challenge_until_ms.to_le_bytes());
    out.extend((fields.pending_disputes as u16).to_le_bytes());
    out.extend((fields.material_disputes as u16).to_le_bytes());
    Ok(out)
}
