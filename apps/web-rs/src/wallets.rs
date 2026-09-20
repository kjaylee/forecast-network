//! Wallet addresses and signatures: the two codecs that decide whether a sign-in is genuine.
//!
//! Both are places where being *slightly* more permissive than the reference is a security bug
//! rather than a compatibility one.
//!
//! `valid_ed25519_public_key` is RFC 8032 §§5.1.3–5.1.4 decoding **plus** rejection of the full
//! 8-torsion. The second part is the whole reason it exists: several WebCrypto implementations
//! accept vacuous signatures for small-order keys, so verifying the signature alone does not
//! establish ownership of anything. A port that decompressed and stopped would accept an identity
//! anybody can forge.
//!
//! `decode_address` requires the input to be *canonical* — 32 bytes, re-encoding to exactly what
//! was given, and a point that is not small-order. A noncanonical encoding has more than one byte
//! string for the same key, and two accounts for one key is a confusion worth refusing.
//!
//! Note that this is deliberately *stricter* than `solana::is_edwards_point`, which matches
//! `curve25519-dalek` decompression alone because a PDA search has to find the same address the
//! chain would. Two different questions, two different checks.
//!
//! Not yet ported from `wallets.py`: the `WalletService` (challenge, link, unlink).

use crate::solana;
use num_bigint::BigUint;
use num_traits::{One, Zero};

pub const CHAIN: &str = "solana:devnet";
pub const PURPOSE: &str = "link_forecast_profile";
pub const CHALLENGE_LIFETIME_MS: i64 = 5 * 60 * 1000;
pub const BASE58_ALPHABET: &[u8; 58] = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WalletError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl WalletError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

fn address_invalid() -> WalletError {
    WalletError::new(400, "wallet_address_invalid", "Enter a valid Solana wallet address.")
}

fn not_canonical() -> WalletError {
    WalletError::new(
        400,
        "wallet_address_invalid",
        "Enter a canonical 32-byte Solana public key.",
    )
}

fn small_order() -> WalletError {
    WalletError::new(
        400,
        "wallet_address_invalid",
        "Use a valid, non-small-order Ed25519 wallet public key.",
    )
}

fn signature_invalid() -> WalletError {
    WalletError::new(
        400,
        "wallet_signature_invalid",
        "The wallet returned an invalid signature.",
    )
}

/// `p`, the field prime.
fn field_prime() -> BigUint {
    (BigUint::one() << 255u32) - BigUint::from(19u32)
}

/// `d = -121665/121666`.
fn curve_d() -> BigUint {
    let p = field_prime();
    let numerator = &p - BigUint::from(121_665u32);
    let inverse = BigUint::from(121_666u32).modpow(&(&p - BigUint::from(2u32)), &p);
    (numerator * inverse) % &p
}

/// `sqrt(-1)`, which is `2^((p-1)/4)`.
fn sqrt_minus_one() -> BigUint {
    let p = field_prime();
    BigUint::from(2u32).modpow(&((&p - BigUint::one()) >> 2u32), &p)
}

/// `_valid_ed25519_public_key`: RFC 8032 decoding, plus the torsion rejection.
///
/// Public points only — no secret-dependent operation and no signature algorithm lives here.
pub fn valid_ed25519_public_key(raw: &[u8]) -> bool {
    if raw.len() != 32 {
        return false;
    }
    let p = field_prime();
    let encoded = BigUint::from_bytes_le(raw);
    let sign = (&encoded >> 255u32) & BigUint::one();
    let y = &encoded & ((BigUint::one() << 255u32) - BigUint::one());
    if y >= p {
        return false;
    }
    let y_squared = (&y * &y) % &p;
    let denominator = (curve_d() * &y_squared + BigUint::one()) % &p;
    if denominator.is_zero() {
        return false;
    }
    let x_squared = ((&y_squared + &p - BigUint::one()) * denominator.modpow(&(&p - BigUint::from(2u32)), &p)) % &p;
    let mut x = x_squared.modpow(&((&p + BigUint::from(3u32)) >> 3u32), &p);
    if (&x * &x) % &p != x_squared {
        x = (x * sqrt_minus_one()) % &p;
    }
    // `x == 0 and sign == 1` is a noncanonical spelling of zero: the reference refuses it, and so
    // does every verifier that follows RFC 8032 literally.
    if (&x * &x) % &p != x_squared || (x.is_zero() && sign.is_one()) {
        return false;
    }
    if (&x % BigUint::from(2u32)) != sign {
        x = &p - &x;
    }
    // RFC projective doubling, three times: [8]P must not be (0:Z:Z), which rejects every point
    // of order 1, 2, 4 and 8 at once rather than enumerating them.
    let mut y = y;
    let mut z = BigUint::one();
    for _ in 0..3 {
        let square_x = (&x * &x) % &p;
        let square_y = (&y * &y) % &p;
        let twice_square_z = (BigUint::from(2u32) * &z * &z) % &p;
        let total = (&square_x + &square_y) % &p;
        let difference = (&square_x + &p - &square_y) % &p;
        let cross = (&total + &p - ((&x + &y) * (&x + &y)) % &p) % &p;
        let factor = (&twice_square_z + &difference) % &p;
        x = (&cross * &factor) % &p;
        y = (&difference * &total) % &p;
        z = (&factor * &difference) % &p;
    }
    !z.is_zero() && !(x.is_zero() && ((&y + &p - &z) % &p).is_zero())
}

/// `encode_address`: canonical base58, exposed for portable verification.
pub fn encode_address(raw: &[u8]) -> String {
    solana::base58_encode(raw)
}

/// `decode_address`: 32 bytes, canonical, and a valid non-small-order point.
pub fn decode_address(value: &str) -> Result<[u8; 32], WalletError> {
    if value.len() < 32 || value.len() > 44 {
        return Err(address_invalid());
    }
    let Ok(decoded) = solana::base58_decode(value, 32) else {
        return Err(address_invalid());
    };
    let mut raw = [0u8; 32];
    raw.copy_from_slice(&decoded);
    // The re-encode is what makes this canonical rather than merely decodable: a different
    // spelling of the same key would otherwise give one key two addresses.
    if encode_address(&raw) != value {
        return Err(not_canonical());
    }
    if !valid_ed25519_public_key(&raw) {
        return Err(small_order());
    }
    Ok(raw)
}

/// `decode_signature`: base64, exactly 64 bytes, and canonical padding.
pub fn decode_signature(value: &str) -> Result<[u8; 64], WalletError> {
    use base64::Engine;
    if value.len() != 88 {
        return Err(signature_invalid());
    }
    let Ok(raw) = base64::engine::general_purpose::STANDARD.decode(value) else {
        return Err(signature_invalid());
    };
    if raw.len() != 64 || base64::engine::general_purpose::STANDARD.encode(&raw) != value {
        return Err(signature_invalid());
    }
    let mut out = [0u8; 64];
    out.copy_from_slice(&raw);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/wallet-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("wallet golden")).expect("json")
    }

    fn from_hex(text: &str) -> Vec<u8> {
        (0..text.len() / 2)
            .map(|index| u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).expect("hex"))
            .collect()
    }

    #[test]
    fn the_torsion_check_is_the_one_that_makes_this_worth_having() {
        // A small-order key is not a point anybody can prove ownership of: several WebCrypto
        // implementations accept vacuous signatures for them, which is why signature
        // verification alone is not enough and why this check exists.
        let document = golden();
        for entry in document["smallOrder"].as_array().expect("small order") {
            let raw = from_hex(entry["hex"].as_str().unwrap());
            assert_eq!(
                valid_ed25519_public_key(&raw),
                entry["valid"].as_bool().unwrap(),
                "small-order {}",
                entry["hex"]
            );
            assert!(
                !valid_ed25519_public_key(&raw),
                "a small-order point is never a valid key"
            );
        }
        for entry in document["ordinary"].as_array().expect("ordinary") {
            let raw = from_hex(entry["hex"].as_str().unwrap());
            assert_eq!(
                valid_ed25519_public_key(&raw),
                entry["valid"].as_bool().unwrap(),
                "ordinary {}",
                entry["hex"]
            );
        }
        for entry in document["malformed"].as_array().expect("malformed") {
            let raw = from_hex(entry["hex"].as_str().unwrap());
            assert_eq!(
                valid_ed25519_public_key(&raw),
                entry["valid"].as_bool().unwrap(),
                "malformed {}",
                entry["name"]
            );
        }
    }

    #[test]
    fn every_address_rounds_trips_and_every_refusal_matches() {
        let document = golden();
        for entry in document["rounds"].as_array().expect("rounds") {
            let raw = from_hex(entry["raw"].as_str().unwrap());
            assert_eq!(encode_address(&raw), entry["address"].as_str().unwrap(), "encode");
        }
        for case in document["decodes"].as_array().expect("decodes") {
            let name = case["name"].as_str().unwrap();
            let value = case["value"].as_str().unwrap_or_default();
            let produced = decode_address(value).map(|raw| raw.to_vec());
            match (produced, case["error"].is_null()) {
                (Ok(raw), true) => assert_eq!(json_hex(&raw), case["result"].as_str().unwrap(), "{name}"),
                (Ok(_), false) => panic!("{name}: accepted where the reference refused"),
                (Err(error), true) => panic!("{name}: refused with {:?} where the reference accepted", error.message),
                (Err(error), false) => {
                    assert_eq!(error.code, case["error"]["code"].as_str().unwrap(), "{name}");
                    assert_eq!(error.message, case["error"]["message"].as_str().unwrap(), "{name}");
                }
            }
        }
    }

    fn json_hex(raw: &[u8]) -> String {
        raw.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    #[test]
    fn a_signature_is_exactly_sixty_four_canonical_base64_bytes() {
        let document = golden();
        for case in document["signatures"].as_array().expect("signatures") {
            let name = case["name"].as_str().unwrap();
            let value = case["value"].as_str().unwrap_or_default();
            let produced = decode_signature(value);
            match (produced, case["error"].is_null()) {
                (Ok(raw), true) => assert_eq!(json_hex(&raw), case["result"].as_str().unwrap(), "{name}"),
                (Ok(_), false) => panic!("{name}: accepted where the reference refused"),
                (Err(error), true) => panic!("{name}: refused with {:?} where the reference accepted", error.message),
                (Err(error), false) => assert_eq!(error.code, case["error"]["code"].as_str().unwrap(), "{name}"),
            }
        }
    }
}
