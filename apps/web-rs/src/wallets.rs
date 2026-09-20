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

use crate::db::{int, text, Database, Row};
use crate::solana;
use num_bigint::BigUint;
use num_traits::{One, Zero};
use serde_json::{json, Value};

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
    pub(crate) const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
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

// ---------------------------------------------------------------- the service

pub type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T>>>;
/// `verify_signature(public_key, message, signature)`, injected so the protocol stays testable.
pub type SignatureVerifier = dyn Fn(Vec<u8>, Vec<u8>, Vec<u8>) -> BoxFuture<Result<bool, ()>>;
/// `PointsService.summary`, injected rather than reached for: `points.rs` still speaks the D1
/// session directly, and migrating it is a change to live read routes rather than to this module.
pub type PointsSummary = dyn Fn(&str) -> BoxFuture<Result<Value, ()>>;

/// `WalletService`.
pub struct WalletService<'a> {
    pub db: &'a dyn Database,
    pub now_ms: &'a dyn Fn() -> i64,
    pub random_token: &'a dyn Fn() -> String,
    pub verify_signature: &'a SignatureVerifier,
    pub points: &'a PointsSummary,
    pub origin: String,
}

impl<'a> WalletService<'a> {
    /// The origin has to be an *exact* origin: no path, no query, no credentials, no control
    /// characters, and plain http only for local development. The message a user signs quotes it,
    /// so a loose comparison would let a signature made for one site be replayed at another.
    pub fn new(
        db: &'a dyn Database,
        now_ms: &'a dyn Fn() -> i64,
        random_token: &'a dyn Fn() -> String,
        verify_signature: &'a SignatureVerifier,
        points: &'a PointsSummary,
        origin: &str,
    ) -> Result<Self, String> {
        let complaint = "Wallet origin must be an exact HTTPS origin or local development origin".to_string();
        if origin.len() > 255 || origin.chars().any(|character| (character as u32) < 33) {
            return Err(complaint);
        }
        let Some((scheme, rest)) = origin.split_once("://") else {
            return Err(complaint);
        };
        if scheme != "https" && scheme != "http" {
            return Err(complaint);
        }
        // No path, query, fragment or credentials: `netloc` and nothing else.
        if rest.is_empty() || rest.contains(['/', '?', '#', '@']) {
            return Err(complaint);
        }
        if scheme == "http" && !matches!(rest, "localhost" | "127.0.0.1" | "::1") {
            return Err(complaint);
        }
        Ok(Self {
            db,
            now_ms,
            random_token,
            verify_signature,
            points,
            origin: origin.to_string(),
        })
    }

    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    /// `_token`: a challenge identifier has to be unguessable, so a generator that returns
    /// something short or shaped wrong is a configuration fault rather than a bad request.
    fn token(&self) -> Result<String, WalletError> {
        let value = (self.random_token)();
        let shaped = (32..=128).contains(&value.len())
            && value
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'));
        if shaped {
            Ok(value)
        } else {
            Err(WalletError::new(
                500,
                "wallet_token_invalid",
                "Wallet challenges require a secure token.",
            ))
        }
    }

    async fn user(&self, user_id: &str) -> Result<(), WalletError> {
        let found = self
            .db
            .first("SELECT id FROM users WHERE id=?", &[json!(user_id)])
            .await
            .map_err(|_| storage_unavailable())?;
        if found.is_none() {
            return Err(WalletError::new(
                401,
                "authentication_required",
                "Please sign in to connect a wallet.",
            ));
        }
        Ok(())
    }

    /// `_login_identity_guard`: the wallet a session signs in with cannot be removed or replaced
    /// from inside that session. Otherwise a stolen session could re-point the account.
    async fn login_identity_guard(&self, user_id: &str, address: Option<&str>) -> Result<(), WalletError> {
        let identity = self
            .db
            .first(
                "SELECT address FROM wallet_identities WHERE user_id=? AND status='active'",
                &[json!(user_id)],
            )
            .await
            .map_err(|_| storage_unavailable())?;
        match identity.as_ref().and_then(|row| text(row, "address")) {
            Some(active) if Some(active) != address => Err(WalletError::new(
                409,
                "wallet_login_rotation_required",
                "Your sign-in wallet cannot be removed or replaced from a session. Keep using this wallet to sign in.",
            )),
            _ => Ok(()),
        }
    }

    /// `get_wallet`.
    pub async fn get_wallet(&self, user_id: &str) -> Result<Value, WalletError> {
        self.user(user_id).await?;
        let row = self
            .db
            .first(
                "SELECT address,chain,linked_at FROM wallet_links WHERE user_id=?",
                &[json!(user_id)],
            )
            .await
            .map_err(|_| storage_unavailable())?;
        let wallet = match row.as_ref() {
            Some(row) => json!({"address": text(row, "address"), "chain": text(row, "chain"),
                                "linkedAt": int(row, "linked_at")}),
            None => Value::Null,
        };
        Ok(json!({"wallet": wallet, "points": (self.points)(user_id).await.unwrap_or(Value::Null)}))
    }

    /// `challenge`: the exact text a wallet is asked to sign.
    ///
    /// Every field that decides what the signature means is written into the message, so a
    /// signature cannot be replayed for a different user, wallet, site, chain or purpose.
    pub async fn challenge(&self, user_id: &str, address: &str) -> Result<Value, WalletError> {
        self.user(user_id).await?;
        decode_address(address)?;
        self.login_identity_guard(user_id, Some(address)).await?;
        let identifier = format!("wc_{}", self.token()?);
        let now = self.now();
        let expiry = now + CHALLENGE_LIFETIME_MS;
        let message = format!(
            "Forecast Network wallet connection\n\
             Purpose: Link this wallet to your Forecast Network profile.\n\
             This signature verifies wallet ownership only. It does not authorize a transaction or transfer.\n\
             User: {user_id}\nAddress: {address}\nOrigin: {origin}\n\
             Chain: {CHAIN} (Solana Devnet)\nPurpose ID: {PURPOSE}\n\
             Challenge: {identifier}\nIssued at: {now}\nExpires at: {expiry}\n\
             Expires at (UTC): {expires_utc}\n",
            origin = self.origin,
            expires_utc = utc_isoformat(expiry),
        );
        self.db
            .execute(
                "INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,expires_at) \
                 VALUES(?,?,?,?,?,?,?,?,?)",
                &[
                    json!(identifier),
                    json!(user_id),
                    json!(address),
                    json!(self.origin),
                    json!(PURPOSE),
                    json!(CHAIN),
                    json!(message),
                    json!(now),
                    json!(expiry),
                ],
            )
            .await
            .map_err(|_| storage_unavailable())?;
        Ok(
            json!({"challengeId": identifier, "address": address, "message": message,
                  "expiresAt": expiry, "chain": CHAIN}),
        )
    }

    /// `_check_challenge`: the request has to be *this* user's, *this* wallet's, *this* site's.
    fn check_challenge(
        row: Option<&Row>,
        user_id: &str,
        address: &str,
        origin: &str,
        now: i64,
    ) -> Result<(), WalletError> {
        let matches = row.is_some_and(|row| {
            text(row, "user_id") == Some(user_id)
                && text(row, "address") == Some(address)
                && text(row, "origin") == Some(origin)
                && text(row, "purpose") == Some(PURPOSE)
                && text(row, "chain") == Some(CHAIN)
        });
        if !matches {
            return Err(WalletError::new(
                400,
                "wallet_challenge_invalid",
                "This wallet request does not match your account, wallet, or site.",
            ));
        }
        let row = row.expect("checked");
        if int(row, "used_at").is_some() {
            return Err(WalletError::new(
                409,
                "wallet_challenge_used",
                "This wallet signature has already been used. Request a new challenge.",
            ));
        }
        if int(row, "revoked_at").is_some() {
            return Err(WalletError::new(
                409,
                "wallet_challenge_revoked",
                "This wallet request was canceled. Connect again for a new challenge.",
            ));
        }
        if int(row, "expires_at").unwrap_or(0) <= now {
            return Err(WalletError::new(
                410,
                "wallet_challenge_expired",
                "The wallet request has expired. Connect again for a new challenge.",
            ));
        }
        Ok(())
    }
}

fn storage_unavailable() -> WalletError {
    WalletError::new(
        503,
        "wallet_storage_unavailable",
        "The wallet link could not be saved. Please try again later.",
    )
}

/// `datetime.fromtimestamp(ms/1000, utc).isoformat(timespec="milliseconds")`.
fn utc_isoformat(ms: i64) -> String {
    let seconds = ms.div_euclid(1000);
    let (year, month, day) = crate::ai::compiler_time::civil_parts(ms);
    let day_seconds = seconds.rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}+00:00",
        day_seconds / 3600,
        (day_seconds % 3600) / 60,
        day_seconds % 60,
        ms.rem_euclid(1000),
    )
}

impl WalletService<'_> {
    /// `link`: verify the signature and attach the wallet, in one guarded batch.
    ///
    /// The challenge is checked twice — before and after the signature is verified — because the
    /// verification is an await, and a request can be cancelled or a wallet linked during one.
    /// The reference's own `asyncio.timeout(10)` around the verifier belongs to the seam that
    /// makes the call, which is where a timer can exist at all.
    #[allow(clippy::too_many_lines)]
    pub async fn link(&self, user_id: &str, body: &Value) -> Result<Value, WalletError> {
        self.user(user_id).await?;
        let fields: std::collections::BTreeSet<&str> = body
            .as_object()
            .map(|fields| fields.keys().map(String::as_str).collect())
            .unwrap_or_default();
        if fields != ["challengeId", "address", "signature"].into_iter().collect() {
            return Err(WalletError::new(
                400,
                "wallet_challenge_invalid",
                "A challenge, wallet address, and signature are required.",
            ));
        }
        let identifier = body["challengeId"].as_str().unwrap_or_default();
        let shaped = identifier.strip_prefix("wc_").is_some_and(|rest| {
            (32..=128).contains(&rest.len())
                && rest
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
        });
        if !shaped {
            return Err(WalletError::new(
                400,
                "wallet_challenge_invalid",
                "Request a new wallet challenge.",
            ));
        }
        let address = body["address"].as_str().unwrap_or_default();
        let public_key = decode_address(address)?;
        let signature = decode_signature(body["signature"].as_str().unwrap_or_default())?;
        let row = self
            .db
            .first("SELECT * FROM wallet_challenges WHERE id=?", &[json!(identifier)])
            .await
            .map_err(|_| storage_unavailable())?;
        Self::check_challenge(row.as_ref(), user_id, address, &self.origin, self.now())?;
        let message = row
            .as_ref()
            .and_then(|row| text(row, "message"))
            .unwrap_or_default()
            .as_bytes()
            .to_vec();
        let verified = (self.verify_signature)(public_key.to_vec(), message.clone(), signature.to_vec())
            .await
            .map_err(|_| {
                WalletError::new(
                    503,
                    "wallet_verification_unavailable",
                    "Wallet verification is temporarily unavailable. Please try again.",
                )
            })?;
        if !verified {
            return Err(WalletError::new(
                401,
                "wallet_signature_invalid",
                "The signature does not match this wallet and connection request.",
            ));
        }
        self.login_identity_guard(user_id, Some(address)).await?;
        Self::check_challenge(row.as_ref(), user_id, address, &self.origin, self.now())?;
        let existing = self
            .db
            .first("SELECT user_id FROM wallet_links WHERE address=?", &[json!(address)])
            .await
            .map_err(|_| storage_unavailable())?;
        if existing
            .as_ref()
            .and_then(|row| text(row, "user_id"))
            .is_some_and(|owner| owner != user_id)
        {
            return Err(already_linked());
        }

        let now = self.now();
        let guard = self.token()?;
        let audit = crate::source_watch::compact(&json!({
            "schemaVersion": 1, "kind": "wallet_linked", "userId": user_id, "address": address,
            "origin": self.origin, "purpose": PURPOSE, "chain": CHAIN, "challengeId": identifier,
            "messageSha256": crate::source_watch::hash_hex(&String::from_utf8_lossy(&message)),
            "signatureSha256": crate::source_watch::hash_hex(&String::from_utf8_lossy(&signature)),
            "signatureVerified": true, "verification": "ed25519_sign_message", "verifiedAt": now,
        }));
        let written = self
            .db
            .batch(&[
                (
                    "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_challenges \
                     WHERE id=? AND user_id=? AND address=? AND origin=? AND purpose=? AND chain=? \
                     AND expires_at>? AND used_at IS NULL AND revoked_at IS NULL) AND NOT EXISTS(SELECT 1 FROM wallet_links \
                     WHERE address=? AND user_id!=?) THEN 1 ELSE 0 END"
                        .to_string(),
                    vec![
                        json!(guard),
                        json!(identifier),
                        json!(user_id),
                        json!(address),
                        json!(self.origin),
                        json!(PURPOSE),
                        json!(CHAIN),
                        json!(now),
                        json!(address),
                        json!(user_id),
                    ],
                ),
                (
                    "UPDATE wallet_challenges SET used_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL"
                        .to_string(),
                    vec![json!(now), json!(identifier)],
                ),
                (
                    "INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) VALUES(?,?,?,?,?,1) \
                     ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,chain=excluded.chain,\
                     linked_at=excluded.linked_at,generation=excluded.generation,revision=wallet_links.revision+1"
                        .to_string(),
                    vec![json!(user_id), json!(address), json!(CHAIN), json!(now), json!(identifier)],
                ),
                (
                    "INSERT INTO wallet_audit(id,user_id,address,kind,challenge_id,body,created_at) \
                     VALUES(?,?,?,'wallet_linked',?,?,?)"
                        .to_string(),
                    vec![json!(self.token()?), json!(user_id), json!(address), json!(identifier), json!(audit), json!(now)],
                ),
                (
                    "DELETE FROM mutation_guards WHERE token=?".to_string(),
                    vec![json!(guard)],
                ),
            ])
            .await;
        if written.is_err() {
            // Diagnosed rather than guessed at: a challenge that is no longer usable and a wallet
            // that is now owned by somebody else are different answers.
            let fresh = self
                .db
                .first("SELECT * FROM wallet_challenges WHERE id=?", &[json!(identifier)])
                .await
                .map_err(|_| storage_unavailable())?;
            Self::check_challenge(fresh.as_ref(), user_id, address, &self.origin, self.now())?;
            let owner = self
                .db
                .first("SELECT user_id FROM wallet_links WHERE address=?", &[json!(address)])
                .await
                .map_err(|_| storage_unavailable())?;
            if owner
                .as_ref()
                .and_then(|row| text(row, "user_id"))
                .is_some_and(|value| value != user_id)
            {
                return Err(already_linked());
            }
            return Err(storage_unavailable());
        }
        self.get_wallet(user_id).await
    }

    /// `unlink`: removing a wallet also cancels any signature request still in flight.
    pub async fn unlink(&self, user_id: &str) -> Result<Value, WalletError> {
        self.user(user_id).await?;
        self.login_identity_guard(user_id, None).await?;
        let row = self
            .db
            .first("SELECT * FROM wallet_links WHERE user_id=?", &[json!(user_id)])
            .await
            .map_err(|_| storage_unavailable())?;
        let now = self.now();
        let guard = self.token()?;
        let Some(row) = row else {
            // Nothing is linked, so this is a cancel: an in-flight challenge is revoked rather
            // than left to be signed for a wallet the user is walking away from.
            let cleared = self
                .db
                .batch(&[
                    (
                        "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS \
                         (SELECT 1 FROM wallet_links WHERE user_id=?) THEN 1 ELSE 0 END"
                            .to_string(),
                        vec![json!(guard), json!(user_id)],
                    ),
                    (
                        "UPDATE wallet_challenges SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL"
                            .to_string(),
                        vec![json!(now), json!(user_id)],
                    ),
                    (
                        "DELETE FROM mutation_guards WHERE token=?".to_string(),
                        vec![json!(guard)],
                    ),
                ])
                .await;
            if cleared.is_err() {
                let linked = self
                    .db
                    .first("SELECT user_id FROM wallet_links WHERE user_id=?", &[json!(user_id)])
                    .await
                    .map_err(|_| storage_unavailable())?;
                if linked.is_some() {
                    return Err(conflict());
                }
                return Err(storage_unavailable());
            }
            return Ok(json!({"wallet": Value::Null,
                             "points": (self.points)(user_id).await.unwrap_or(Value::Null)}));
        };

        let revision = int(&row, "revision").unwrap_or(0);
        let generation = text(&row, "generation").unwrap_or_default().to_string();
        let address = text(&row, "address").unwrap_or_default().to_string();
        let audit = crate::source_watch::compact(&json!({
            "schemaVersion": 1, "kind": "wallet_unlinked", "userId": user_id, "address": address,
            "chain": CHAIN, "origin": self.origin, "unlinkedAt": now,
            "previousRevision": revision, "previousGeneration": generation,
        }));
        let removed = self
            .db
            .batch(&[
                (
                    "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_links \
                     WHERE user_id=? AND address=? AND revision=? AND generation=?) THEN 1 ELSE 0 END"
                        .to_string(),
                    vec![json!(guard), json!(user_id), json!(address), json!(revision), json!(generation)],
                ),
                (
                    "DELETE FROM wallet_links WHERE user_id=? AND revision=? AND generation=?".to_string(),
                    vec![json!(user_id), json!(revision), json!(generation)],
                ),
                (
                    "UPDATE wallet_challenges SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL"
                        .to_string(),
                    vec![json!(now), json!(user_id)],
                ),
                (
                    "INSERT INTO wallet_audit(id,user_id,address,kind,body,created_at) VALUES(?,?,?,'wallet_unlinked',?,?)"
                        .to_string(),
                    vec![json!(self.token()?), json!(user_id), json!(address), json!(audit), json!(now)],
                ),
                (
                    "DELETE FROM mutation_guards WHERE token=?".to_string(),
                    vec![json!(guard)],
                ),
            ])
            .await;
        if removed.is_err() {
            let current = self
                .db
                .first(
                    "SELECT revision,generation FROM wallet_links WHERE user_id=?",
                    &[json!(user_id)],
                )
                .await
                .map_err(|_| storage_unavailable())?;
            let Some(current) = current else {
                return Ok(json!({"wallet": Value::Null,
                                 "points": (self.points)(user_id).await.unwrap_or(Value::Null)}));
            };
            if int(&current, "revision") == Some(revision) && text(&current, "generation") == Some(generation.as_str())
            {
                return Err(storage_unavailable());
            }
            return Err(conflict());
        }
        Ok(json!({"wallet": Value::Null,
                  "points": (self.points)(user_id).await.unwrap_or(Value::Null)}))
    }
}

fn already_linked() -> WalletError {
    WalletError::new(
        409,
        "wallet_already_linked",
        "This wallet is already linked to another profile.",
    )
}

fn conflict() -> WalletError {
    WalletError::new(
        409,
        "wallet_conflict",
        "The wallet link changed. Refresh your profile and try again.",
    )
}

#[cfg(test)]
mod service_tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn from_hex(text: &str) -> Vec<u8> {
        (0..text.len() / 2)
            .map(|index| u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).expect("hex"))
            .collect()
    }

    #[test]
    fn the_origin_has_to_be_exact() {
        // The origin is inside the text a user signs, so a loose comparison would let a signature
        // made for one site be replayed at another.
        let db = Sqlite::from_migrations();
        let now = || 0i64;
        let token = || "t".repeat(32);
        let verifier: Box<SignatureVerifier> = Box::new(|_, _, _| Box::pin(async { Ok(true) }));
        let points: Box<PointsSummary> = Box::new(|_| Box::pin(async { Ok(Value::Null) }));
        let make = |origin: &str| WalletService::new(&db, &now, &token, &verifier, &points, origin);
        for origin in ["https://forecast.eastsea.xyz", "http://localhost", "http://127.0.0.1"] {
            assert!(make(origin).is_ok(), "{origin}");
        }
        let long = format!("https://{}", "a".repeat(255));
        for origin in [
            "https://forecast.eastsea.xyz/",
            "https://forecast.eastsea.xyz/path",
            "https://user@forecast.eastsea.xyz",
            "https://forecast.eastsea.xyz#x",
            "http://forecast.eastsea.xyz",
            "ftp://forecast.eastsea.xyz",
            "https://",
            long.as_str(),
        ] {
            assert!(make(origin).is_err(), "{origin} was accepted");
        }
    }

    #[test]
    fn the_message_a_wallet_signs_names_everything_it_binds() {
        // A signature is only worth what the text it covers says. A message that omitted the
        // origin, the chain or the purpose could be replayed for another site, chain or use.
        let db = Sqlite::from_migrations();
        db.run(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)",
            &[json!("user-a"), json!("A"), json!("a"), json!("h"), json!(1)],
        )
        .expect("user");
        let now = || 1_000i64;
        let token = || "a".repeat(40);
        let verifier: Box<SignatureVerifier> = Box::new(|_, _, _| Box::pin(async { Ok(true) }));
        let points: Box<PointsSummary> = Box::new(|_| Box::pin(async { Ok(Value::Null) }));
        let registry = WalletService::new(&db, &now, &token, &verifier, &points, "https://forecast.eastsea.xyz")
            .expect("a service");
        // A real Ed25519 point, since the challenge decodes the address before writing anything.
        let address = encode_address(&from_hex(
            "5866666666666666666666666666666666666666666666666666666666666666",
        ));
        let challenged = block(registry.challenge("user-a", &address)).expect("a challenge");
        let message = challenged["message"].as_str().unwrap().to_string();
        for needle in [
            "User: user-a".to_string(),
            format!("Address: {address}"),
            "Origin: https://forecast.eastsea.xyz".to_string(),
            "Chain: solana:devnet (Solana Devnet)".to_string(),
            format!("Purpose ID: {PURPOSE}"),
            format!("Challenge: {}", challenged["challengeId"].as_str().unwrap()),
            "Issued at: 1000".to_string(),
            format!("Expires at: {}", 1_000 + CHALLENGE_LIFETIME_MS),
        ] {
            assert!(message.contains(&needle), "the message omits {needle:?}");
        }
        assert_eq!(challenged["expiresAt"], 1_000 + CHALLENGE_LIFETIME_MS);
        // A challenge naming a wallet that is not a valid point never gets written.
        assert_eq!(
            block(registry.challenge("user-a", "1")).unwrap_err().code,
            "wallet_address_invalid"
        );
    }
}
