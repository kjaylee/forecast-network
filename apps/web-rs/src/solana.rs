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
