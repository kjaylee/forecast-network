//! Wallet sign-in: a browser-bound challenge, an injected signature check, and one guarded batch
//! that converts the identity.
//!
//! Nothing here asks for a transaction, a private key, a recovery credential or an RPC. The only
//! effect with a network behind it is the signature check, and *every* authorization decision is
//! repeated after it — because that await is where the request can be cancelled, the wallet
//! unlinked, or the challenge revoked on another worker.

use crate::auth::{self, Authentication};
use crate::db::{self, Database, Row};
use crate::wallets::{
    self, decode_address, decode_signature, BoxFuture, SignatureVerifier, WalletError, WalletService,
    CHAIN, CHALLENGE_LIFETIME_MS,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

/// The purpose recorded on a sign-in challenge. It is deliberately not `wallets::PURPOSE`: this
/// proof signs *both* signing in and linking, and a challenge that promised only one of them would
/// not authorize the batch below.
pub const PURPOSE: &str = "sign_in_and_link_forecast_profile";

/// `_failure`. Every mutation that loses a race reports the same thing, because the caller's move
/// is the same in all of them: start again.
fn failure(code: &'static str) -> WalletError {
    WalletError::new(
        409,
        code,
        "This sign-in request changed or was canceled. Start wallet sign-in again.",
    )
}

fn challenge_invalid(message: &'static str) -> WalletError {
    WalletError::new(400, "wallet_challenge_invalid", message)
}

/// `PointsService(self.db).summary(uid)`, injected: `points.rs` still speaks the D1 session
/// directly, so the summary arrives through a seam rather than from a second storage client here.
pub type PointsSummary = wallets::PointsSummary;

/// `verify_signature` with the reference's own ten-second deadline already applied by the seam that
/// makes the call, which is the only place a timer can exist at all.
pub type Verifier = SignatureVerifier;

/// `WalletLogin`.
pub struct WalletLogin<'a> {
    pub db: &'a dyn Database,
    pub now_ms: &'a dyn Fn() -> i64,
    pub token_hash: &'a dyn Fn(&str) -> String,
    pub random_token: &'a dyn Fn() -> String,
    pub verify_signature: &'a Verifier,
    pub points: &'a PointsSummary,
    pub origin: String,
    /// `on_create`, awaited only after ownership has been proven.
    pub on_create: Option<&'a dyn Fn() -> BoxFuture<Result<(), ()>>>,
}

impl<'a> WalletLogin<'a> {
    /// The reference constructs a `WalletService` and throws it away: the point is its origin and
    /// public-key boundary, which the two endpoints must agree on exactly. A sign-in message quotes
    /// the origin, so a second, looser parser here would let a signature made for one site be
    /// replayed at another.
    pub fn new(
        db: &'a dyn Database,
        now_ms: &'a dyn Fn() -> i64,
        token_hash: &'a dyn Fn(&str) -> String,
        random_token: &'a dyn Fn() -> String,
        verify_signature: &'a Verifier,
        points: &'a PointsSummary,
        origin: &str,
    ) -> Result<Self, String> {
        WalletService::new(db, now_ms, random_token, verify_signature, points, origin)?;
        Ok(Self {
            db,
            now_ms,
            token_hash,
            random_token,
            verify_signature,
            points,
            origin: origin.to_string(),
            on_create: None,
        })
    }

    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    fn auth(&self) -> Authentication<'_> {
        Authentication {
            db: self.db,
            now_ms: self.now_ms,
            token_hash: self.token_hash,
            random_token: self.random_token,
        }
    }

    /// `urlsplit(self.origin).netloc`. The origin was validated at construction to carry no path,
    /// query, fragment or credentials, so what follows the scheme *is* the netloc.
    fn netloc(&self) -> &str {
        self.origin.split_once("://").map_or("", |(_, rest)| rest)
    }

    fn _context_hash(&self, token: Option<&Value>) -> Result<String, WalletError> {
        let Some(token) = token.and_then(Value::as_str) else {
            return Err(failure("wallet_context_required"));
        };
        if !auth::shaped(token) {
            return Err(failure("wallet_context_required"));
        }
        Ok((self.token_hash)(&format!("wallet-context:{token}")))
    }

    /// `context`. An existing context is reused while it is live, so reloading the page does not
    /// invalidate a challenge the wallet is already being asked to sign.
    pub async fn context(&self, context_token: Option<&Value>) -> Result<Value, WalletError> {
        let now = self.now();
        if let Some(token) = context_token.filter(|value| truthy(value)) {
            let key = self._context_hash(Some(token))?;
            let row = self
                .db
                .first("SELECT * FROM wallet_login_contexts WHERE token_hash=?", &[json!(key)])
                .await
                .map_err(|_| storage_unavailable())?;
            let live = row.as_ref().is_some_and(|row| {
                db::get(row, "revoked_at") == &Value::Null && db::int(row, "expires_at").is_some_and(|at| at > now)
            });
            if live {
                return Ok(json!({
                    "contextToken": token.as_str(),
                    "expiresAt": db::int(row.as_ref().unwrap(), "expires_at"),
                }));
            }
        }
        let token = self.auth().token()?;
        let hash = self._context_hash(Some(&json!(token)))?;
        self.db
            .execute(
                "INSERT INTO wallet_login_contexts(token_hash,created_at,expires_at) VALUES(?,?,?)",
                &[json!(hash), json!(now), json!(now + auth::SESSION_LIFETIME_MS)],
            )
            .await
            .map_err(|_| storage_unavailable())?;
        Ok(json!({
            "contextToken": token,
            "expiresAt": now + auth::SESSION_LIFETIME_MS,
        }))
    }

    async fn _context(&self, token: Option<&Value>) -> Result<Row, WalletError> {
        let hash = self._context_hash(token)?;
        let row = self
            .db
            .first("SELECT * FROM wallet_login_contexts WHERE token_hash=?", &[json!(hash)])
            .await
            .map_err(|_| storage_unavailable())?;
        let live = row.as_ref().is_some_and(|row| {
            db::get(row, "revoked_at") == &Value::Null
                && db::int(row, "expires_at").is_some_and(|at| at > self.now())
        });
        if !live {
            return Err(failure("wallet_context_required"));
        }
        Ok(row.unwrap())
    }

    /// `_owner`. A retired identity is refused rather than ignored: the address is known to have
    /// belonged to someone, and silently starting a second profile for it would strand the first.
    async fn _owner(&self, address: &str) -> Result<Option<String>, WalletError> {
        let identity = self
            .db
            .first("SELECT * FROM wallet_identities WHERE address=?", &[json!(address)])
            .await
            .map_err(|_| storage_unavailable())?;
        if let Some(identity) = identity {
            if db::text(&identity, "status") != Some("active") {
                return Err(failure("wallet_identity_retired"));
            }
            return Ok(db::get(&identity, "user_id").as_str().map(str::to_string));
        }
        let linked = self
            .db
            .first("SELECT user_id FROM wallet_links WHERE address=?", &[json!(address)])
            .await
            .map_err(|_| storage_unavailable())?;
        if let Some(linked) = linked {
            return Ok(db::get(&linked, "user_id").as_str().map(str::to_string));
        }
        let historical = self
            .db
            .first(
                concat!(
                    "SELECT user_id FROM wallet_audit WHERE address=? UNION ALL ",
                    "SELECT user_id FROM point_awards WHERE wallet_address=? LIMIT 1",
                ),
                &[json!(address), json!(address)],
            )
            .await
            .map_err(|_| storage_unavailable())?;
        if historical.is_some() {
            return Err(failure("wallet_identity_retired"));
        }
        Ok(None)
    }
    /// `challenge`. The mode decides which of two entirely different things happens later, and
    /// the target profile is *committed to* in the message the wallet signs — so a concurrent
    /// signup cannot substitute a different UID for the one the user approved.
    pub async fn challenge(
        &self,
        context_token: Option<&Value>,
        body: &Value,
        session_token: Option<&str>,
    ) -> Result<Value, WalletError> {
        let Some(fields) = body.as_object() else {
            return Err(challenge_invalid("Choose your wallet and sign-in action."));
        };
        let keys: BTreeSet<&str> = fields.keys().map(String::as_str).collect();
        let allowed: BTreeSet<&str> = ["address", "mode", "expectedUserId", "displayName"].into_iter().collect();
        let required: BTreeSet<&str> = ["address", "mode", "expectedUserId"].into_iter().collect();
        if !required.is_subset(&keys) || !keys.is_subset(&allowed) {
            return Err(challenge_invalid("Choose your wallet and sign-in action."));
        }
        let address = fields["address"].as_str().unwrap_or_default();
        // An address that is not a canonical, non-small-order point is refused before anything is
        // read: the challenge is a statement about a key, and a key with two encodings has two.
        decode_address(address)?;
        let mode = fields["mode"].as_str().unwrap_or_default();
        if !matches!(mode, "login" | "migrate") {
            return Err(challenge_invalid("Choose a valid sign-in action."));
        }
        let context = self._context(context_token).await?;
        let current = self
            .auth()
            .authenticate(session_token, context_token.and_then(Value::as_str))
            .await?;
        let owner = self._owner(address).await?;
        let mut source_hash = Value::Null;
        let target: String;
        if mode == "migrate" {
            let expected = fields["expectedUserId"].as_str();
            let agrees = current
                .as_ref()
                .and_then(|user| user["id"].as_str())
                .zip(expected)
                .is_some_and(|(id, expected)| id == expected);
            if !agrees {
                return Err(failure("account_changed"));
            }
            target = current.as_ref().unwrap()["id"].as_str().unwrap_or_default().to_string();
            if owner.as_deref().is_some_and(|owner| owner != target) {
                return Err(failure("wallet_already_linked"));
            }
            let other = self
                .db
                .first(
                    "SELECT address FROM wallet_links WHERE user_id=? AND address!=?",
                    &[json!(target), json!(address)],
                )
                .await
                .map_err(|_| storage_unavailable())?;
            let active = self
                .db
                .first(
                    "SELECT address FROM wallet_identities WHERE user_id=? AND status='active' AND address!=?",
                    &[json!(target), json!(address)],
                )
                .await
                .map_err(|_| storage_unavailable())?;
            if other.is_some() || active.is_some() {
                return Err(failure("wallet_login_rotation_required"));
            }
            // `str(session_token)`: the reference stringifies whatever reached it, and a `None`
            // session is already ruled out by `agrees` above, so this is the session it read.
            source_hash = json!((self.token_hash)(&format!("session:{}", session_token.unwrap_or_default())));
        } else {
            if fields["expectedUserId"] != Value::Null {
                return Err(challenge_invalid("Use migration to keep an existing guest profile."));
            }
            target = match owner {
                Some(owner) => owner,
                None => {
                    let identifier = self.auth().token()?;
                    format!("u_{}", &identifier[..24])
                }
            };
            let current_id = current.as_ref().and_then(|user| user["id"].as_str());
            if current_id.is_some_and(|id| id != target) {
                return Err(failure("wallet_migration_required"));
            }
        }
        let fallback = format!("Forecaster {}", &address[..address.len().min(8)]);
        let display_name = auth::checked_text(Some(fields.get("displayName").unwrap_or(&json!(fallback))), 40, 1)?;
        let identifier = format!("wl_{}", self.auth().token()?);
        let now = self.now();
        let expiry = now + CHALLENGE_LIFETIME_MS;
        // A keyed commitment instead of the raw creator ID: without it the message would let
        // anyone dictionary-test public profile IDs against an unauthenticated address. The nonce
        // scopes each commitment to one challenge.
        let commitment = (self.token_hash)(&format!("wallet-login-target:{identifier}:{target}"));
        let message = format!(
            concat!(
                "Forecast Network wallet sign-in\n",
                "Sign in to the profile associated with this wallet, creating and linking one if needed.\n",
                "This signature verifies wallet ownership only. It does not authorize a transaction or transfer.\n",
                "Domain: {}\nURI: {}/auth/wallet\nOrigin: {}\n",
                "Address: {}\nChain: {}\nPurpose ID: {}\n",
                "Proof roles: sign_in_forecast_profile, link_forecast_profile\n",
                "Mode: {}\nProfile commitment: {}\n",
                "{}Challenge: {}\nIssued at: {}\nExpires at: {}\n",
            ),
            self.netloc(),
            self.origin,
            self.origin,
            address,
            CHAIN,
            PURPOSE,
            mode,
            commitment,
            // The migration line is present or absent, never blank: a sign-in message that named
            // no existing profile must not be replayable as one that did.
            if mode == "migrate" { format!("Existing profile: {target}\n") } else { String::new() },
            identifier,
            now,
            expiry,
        );
        let guard = self.auth().token()?;
        let context_hash = db::get(&context, "token_hash").clone();
        let statements = vec![
            (
                concat!(
                    "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_login_contexts ",
                    "WHERE token_hash=? AND epoch=? AND active_session_hash IS ? AND latest_challenge_id IS ? ",
                    "AND revoked_at IS NULL AND expires_at>?) THEN 1 ELSE 0 END",
                )
                .to_string(),
                vec![
                    json!(guard),
                    context_hash.clone(),
                    db::get(&context, "epoch").clone(),
                    db::get(&context, "active_session_hash").clone(),
                    db::get(&context, "latest_challenge_id").clone(),
                    json!(now),
                ],
            ),
            (
                concat!(
                    "UPDATE wallet_login_challenges SET revoked_at=? WHERE context_hash=? ",
                    "AND used_at IS NULL AND revoked_at IS NULL",
                )
                .to_string(),
                vec![json!(now), context_hash.clone()],
            ),
            (
                concat!(
                    "INSERT INTO wallet_login_challenges(id,context_hash,context_epoch,address,target_user_id,mode,",
                    "source_session_hash,display_name,origin,purpose,chain,message,created_at,expires_at) ",
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                )
                .to_string(),
                vec![
                    json!(identifier),
                    context_hash.clone(),
                    db::get(&context, "epoch").clone(),
                    json!(address),
                    json!(target),
                    json!(mode),
                    source_hash,
                    json!(display_name),
                    json!(self.origin),
                    json!(PURPOSE),
                    json!(CHAIN),
                    json!(message),
                    json!(now),
                    json!(expiry),
                ],
            ),
            (
                "UPDATE wallet_login_contexts SET latest_challenge_id=? WHERE token_hash=?".to_string(),
                vec![json!(identifier), context_hash],
            ),
            (
                "DELETE FROM mutation_guards WHERE token=?".to_string(),
                vec![json!(guard)],
            ),
        ];
        // The guard is what turns a lost race into an ordinary refusal instead of a half-applied
        // challenge; any failure here means the sign-in moved, so the code is the default one.
        self.db.batch(&statements).await.map_err(|_| failure("wallet_login_changed"))?;
        Ok(json!({
            "challengeId": identifier,
            "address": address,
            "message": message,
            "expiresAt": expiry,
            "chain": CHAIN,
            "mode": mode,
        }))
    }

    /// `_challenge`. Every field the later batch will rely on is re-checked here, because between
    /// the challenge and the batch there is an await.
    async fn _challenge(&self, identifier: &str, context_token: Option<&Value>, address: &str) -> Result<Row, WalletError> {
        let context = self._context(context_token).await?;
        let row = self
            .db
            .first("SELECT * FROM wallet_login_challenges WHERE id=?", &[json!(identifier)])
            .await
            .map_err(|_| storage_unavailable())?;
        let matches = row.as_ref().is_some_and(|row| {
            db::get(row, "context_hash") == db::get(&context, "token_hash")
                && db::get(row, "context_epoch") == db::get(&context, "epoch")
                && *db::get(&context, "latest_challenge_id") == json!(identifier)
                && *db::get(row, "address") == json!(address)
                && db::text(row, "origin") == Some(self.origin.as_str())
                && db::text(row, "purpose") == Some(PURPOSE)
                && db::text(row, "chain") == Some(CHAIN)
                && db::get(row, "used_at") == &Value::Null
                && db::get(row, "revoked_at") == &Value::Null
        });
        if !matches {
            return Err(failure("wallet_login_changed"));
        }
        let row = row.unwrap();
        if db::int(&row, "expires_at").is_some_and(|expires| expires <= self.now()) {
            return Err(WalletError::new(
                410,
                "wallet_challenge_expired",
                "The signature request expired. Start wallet sign-in again.",
            ));
        }
        Ok(row)
    }

    /// `verify`: check the signature, re-check everything it authorized, then convert the identity
    /// in one guarded batch.
    ///
    /// The challenge is read twice on purpose. The signature check is an await, and during it the
    /// request can be cancelled, the wallet unlinked, or the challenge used from another worker;
    /// the batch's own guard then re-checks all of it a third time, because a guard that is only
    /// computed in Rust would not be a compare-and-set.
    pub async fn verify(
        &self,
        context_token: Option<&Value>,
        body: &Value,
        session_token: Option<&str>,
    ) -> Result<Value, WalletError> {
        let Some(fields) = body.as_object() else {
            return Err(challenge_invalid("A challenge, wallet address, and signature are required."));
        };
        let keys: BTreeSet<&str> = fields.keys().map(String::as_str).collect();
        if keys != ["challengeId", "address", "signature"].into_iter().collect() {
            return Err(challenge_invalid("A challenge, wallet address, and signature are required."));
        }
        let identifier = fields["challengeId"].as_str().unwrap_or_default();
        if !identifier.starts_with("wl_") || !auth::shaped(&identifier[3..]) {
            return Err(failure("wallet_login_changed"));
        }
        let address = fields["address"].as_str().unwrap_or_default();
        let key = decode_address(address)?;
        let signature = decode_signature(fields["signature"].as_str().unwrap_or_default())?;
        let row = self._challenge(identifier, context_token, address).await?;
        if db::text(&row, "mode") == Some("migrate") {
            let source = db::text(&row, "source_session_hash").unwrap_or_default();
            let matches = session_token.is_some_and(|token| (self.token_hash)(&format!("session:{token}")) == source);
            if !matches {
                return Err(failure("account_changed"));
            }
        }
        // A verifier that never answers is an outage, not a bad signature. The reference's ten
        // second deadline lives in the seam that makes the call, which is where a timer can exist.
        let valid = (self.verify_signature)(
            key.to_vec(),
            db::text(&row, "message").unwrap_or_default().as_bytes().to_vec(),
            signature.to_vec(),
        )
        .await
        .map_err(|_| {
            WalletError::new(
                503,
                "wallet_verification_unavailable",
                "Wallet verification is temporarily unavailable.",
            )
        })?;
        // The reference's `is not True` is an identity test, so a verifier answering with a
        // truthy non-boolean is a failure. The seam is typed `bool`, so a negation is that test.
        if !valid {
            return Err(WalletError::new(
                401,
                "wallet_signature_invalid",
                "The signature does not match this wallet and sign-in request.",
            ));
        }
        let mut row = self._challenge(identifier, context_token, address).await?;
        let owner = self._owner(address).await?;
        let uid = db::text(&row, "target_user_id").unwrap_or_default().to_string();
        let mut now = self.now();
        if owner.as_deref().is_some_and(|owner| owner != uid) {
            return Err(failure("wallet_login_changed"));
        }
        let user = self
            .db
            .first("SELECT * FROM users WHERE id=?", &[json!(uid)])
            .await
            .map_err(|_| storage_unavailable())?;
        let mode = db::text(&row, "mode").unwrap_or_default().to_string();
        if mode == "login" && user.is_some() && owner.as_deref() != Some(uid.as_str()) {
            return Err(failure("wallet_login_changed"));
        }
        if mode == "login" && user.is_none() && owner.is_none() {
            if let Some(on_create) = self.on_create {
                // Account-creation quota is charged only after ownership is proven, so failed
                // signatures and ordinary returning-wallet logins cannot exhaust it.
                on_create().await.map_err(|_| storage_unavailable())?;
                // The callback may await a remote limiter, and cancellation and expiry still take
                // precedence — so everything is read again before the guarded batch.
                row = self._challenge(identifier, context_token, address).await?;
                now = self.now();
            }
        }
        // `row["mode"]` is read again rather than carried over: the reference re-reads the row
        // at each use, and the callback above may have replaced it.
        let mode = db::text(&row, "mode").unwrap_or_default().to_string();
        let session = self.auth().token()?;
        let guard = self.auth().token()?;
        let session_hash = (self.token_hash)(&format!("session:{session}"));
        let message = db::text(&row, "message").unwrap_or_default().to_string();
        let context_hash = db::get(&row, "context_hash").clone();
        let context_epoch = db::get(&row, "context_epoch").clone();
        let display_name = db::get(&row, "display_name").clone();
        let created_at = db::get(&row, "created_at").clone();
        let expires_at = db::get(&row, "expires_at").clone();
        let source_hash = db::get(&row, "source_session_hash").clone();
        let audit = audit_body(&SignIn {
            identifier,
            uid: &uid,
            address,
            origin: &self.origin,
            mode: &mode,
            message: &message,
            signature: &signature,
            now,
        });
        let mut statements: Vec<(String, Vec<Value>)> = vec![(
            concat!(
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_login_challenges n ",
                "JOIN wallet_login_contexts c ON c.token_hash=n.context_hash WHERE n.id=? AND n.context_hash=? AND n.address=? ",
                "AND n.target_user_id=? AND n.origin=? AND n.chain=? AND n.purpose=? AND n.used_at IS NULL AND n.revoked_at IS NULL ",
                "AND n.expires_at>? AND c.revoked_at IS NULL AND c.expires_at>? AND c.epoch=n.context_epoch ",
                "AND c.latest_challenge_id=n.id) AND NOT EXISTS(SELECT 1 FROM wallet_identities WHERE address=? ",
                "AND (user_id!=? OR status!='active')) AND NOT EXISTS(SELECT 1 FROM wallet_identities WHERE user_id=? ",
                "AND status='active' AND address!=?) AND NOT EXISTS(SELECT 1 FROM wallet_links WHERE ",
                "(address=? AND user_id!=?) OR (user_id=? AND address!=?)) AND (EXISTS(SELECT 1 FROM wallet_identities ",
                "WHERE address=? AND user_id=? AND status='active') OR EXISTS(SELECT 1 FROM wallet_links WHERE address=? AND user_id=?) ",
                "OR (NOT EXISTS(SELECT 1 FROM wallet_audit WHERE address=?) AND NOT EXISTS(SELECT 1 FROM point_awards ",
                "WHERE wallet_address=?))) THEN 1 ELSE 0 END",
            )
            .to_string(),
            vec![
                json!(guard),
                json!(identifier),
                context_hash.clone(),
                json!(address),
                json!(uid),
                json!(self.origin),
                json!(CHAIN),
                json!(PURPOSE),
                json!(now),
                json!(now),
                json!(address),
                json!(uid),
                json!(uid),
                json!(address),
                json!(address),
                json!(uid),
                json!(uid),
                json!(address),
                json!(address),
                json!(uid),
                json!(address),
                json!(uid),
                json!(address),
                json!(address),
            ],
        )];
        statements.push(match mode.as_str() {
            "migrate" => (
                concat!(
                    "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM sessions s ",
                    "LEFT JOIN wallet_login_contexts c ON c.token_hash=s.context_hash WHERE s.token_hash=? AND s.user_id=? ",
                    "AND s.expires_at>? AND (s.context_hash IS NULL OR (s.context_hash=? AND s.context_epoch=c.epoch ",
                    "AND c.active_session_hash=s.token_hash AND c.revoked_at IS NULL AND c.expires_at>?))) THEN 1 ELSE 0 END",
                )
                .to_string(),
                vec![json!(format!("{guard}:migration")), source_hash, json!(uid), json!(now), context_hash.clone(), json!(now)],
            ),
            _ if user.is_none() => (
                concat!(
                    "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS(SELECT 1 FROM users WHERE id=?) ",
                    "AND NOT EXISTS(SELECT 1 FROM wallet_identities WHERE address=?) ",
                    "AND NOT EXISTS(SELECT 1 FROM wallet_audit WHERE address=?) ",
                    "AND NOT EXISTS(SELECT 1 FROM point_awards WHERE wallet_address=?) THEN 1 ELSE 0 END",
                )
                .to_string(),
                vec![json!(format!("{guard}:signup")), json!(uid), json!(address), json!(address), json!(address)],
            ),
            _ => (
                concat!(
                    "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_links ",
                    "WHERE address=? AND user_id=?) OR EXISTS(SELECT 1 FROM wallet_identities ",
                    "WHERE address=? AND user_id=? AND status='active') THEN 1 ELSE 0 END",
                )
                .to_string(),
                vec![json!(format!("{guard}:owner")), json!(address), json!(uid), json!(address), json!(uid)],
            ),
        });
        if user.is_none() && mode != "migrate" {
            // Losing a concurrent signup cannot be allowed to replace the UID the user's signature
            // committed to, so the profile is created only if that UID is still free.
            statements.push((
                "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)".to_string(),
                vec![
                    json!(uid),
                    display_name,
                    // A handle derived from the UID, and a recovery hash that cannot be produced by
                    // any recovery code: the wallet is now the only way in.
                    json!(format!("f_{}", uid[2..14].to_lowercase())),
                    json!(format!("disabled-wallet:{uid}")),
                    json!(now),
                ],
            ));
        }
        statements.extend([
            (
                "UPDATE wallet_login_challenges SET used_at=? WHERE id=?".to_string(),
                vec![json!(now), json!(identifier)],
            ),
            // The retained proof signs signing in *and* linking, and is recorded under the
            // link-only purpose because that is the role the wallet API understands. It is not
            // presented as a legacy link-only message in either audit.
            (
                concat!(
                    "INSERT INTO wallet_challenges(id,user_id,address,origin,purpose,chain,message,created_at,",
                    "expires_at,used_at) VALUES(?,?,?,?,'link_forecast_profile',?,?,?,?,?)",
                )
                .to_string(),
                vec![
                    json!(identifier),
                    json!(uid),
                    json!(address),
                    json!(self.origin),
                    json!(CHAIN),
                    json!(message),
                    created_at,
                    expires_at,
                    json!(now),
                ],
            ),
            (
                concat!(
                    "INSERT INTO wallet_identities(address,user_id,status,created_at) ",
                    "VALUES(?,?,'active',?) ON CONFLICT(address) DO NOTHING",
                )
                .to_string(),
                vec![json!(address), json!(uid), json!(now)],
            ),
            (
                concat!(
                    "INSERT INTO wallet_links(user_id,address,chain,linked_at,generation,revision) VALUES(?,?,?,?,?,1) ",
                    "ON CONFLICT(user_id) DO UPDATE SET linked_at=excluded.linked_at,",
                    "generation=excluded.generation,revision=wallet_links.revision+1",
                )
                .to_string(),
                vec![json!(uid), json!(address), json!(CHAIN), json!(now), json!(identifier)],
            ),
            // Only the sessions that predate this wallet's conversion are dropped: the guard above
            // established that this address has never been converted, so the cut is exact.
            (
                concat!(
                    "DELETE FROM sessions WHERE user_id=? AND EXISTS(SELECT 1 FROM wallet_identities ",
                    "WHERE address=? AND converted_at IS NULL)",
                )
                .to_string(),
                vec![json!(uid), json!(address)],
            ),
            (
                "UPDATE users SET recovery_hash=? WHERE id=?".to_string(),
                vec![json!(format!("disabled-wallet:{uid}")), json!(uid)],
            ),
            (
                "UPDATE wallet_identities SET converted_at=? WHERE address=? AND converted_at IS NULL".to_string(),
                vec![json!(now), json!(address)],
            ),
            ("DELETE FROM sessions WHERE context_hash=?".to_string(), vec![context_hash.clone()]),
            (
                concat!(
                    "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,context_hash,context_epoch) ",
                    "VALUES(?,?,?,?,?,?)",
                )
                .to_string(),
                vec![
                    json!(session_hash),
                    json!(uid),
                    json!(now),
                    json!(now + auth::SESSION_LIFETIME_MS),
                    context_hash,
                    context_epoch,
                ],
            ),
            (
                "UPDATE wallet_login_contexts SET active_session_hash=? WHERE token_hash=?".to_string(),
                vec![json!(session_hash), db::get(&row, "context_hash").clone()],
            ),
            (
                concat!(
                    "INSERT INTO wallet_login_audit(id,challenge_id,user_id,address,body,created_at) ",
                    "VALUES(?,?,?,?,?,?)",
                )
                .to_string(),
                vec![json!(self.auth().token()?), json!(identifier), json!(uid), json!(address), json!(audit), json!(now)],
            ),
            (
                "DELETE FROM mutation_guards WHERE token IN (?,?,?,?)".to_string(),
                vec![
                    json!(guard),
                    json!(format!("{guard}:migration")),
                    json!(format!("{guard}:signup")),
                    json!(format!("{guard}:owner")),
                ],
            ),
        ]);
        self.db.batch(&statements).await.map_err(|_| failure("wallet_login_changed"))?;
        let saved = self
            .db
            .first("SELECT * FROM users WHERE id=?", &[json!(uid)])
            .await
            .map_err(|_| storage_unavailable())?;
        let Some(saved) = saved else {
            // The batch committed and the profile is still not there, which no request can cause.
            return Err(WalletError::new(
                500,
                "wallet_login_not_persisted",
                "Wallet sign-in transaction did not persist its profile",
            ));
        };
        Ok(json!({
            "user": auth::public_user(&saved),
            "sessionToken": session,
            "wallet": {"address": address, "chain": CHAIN, "linkedAt": now},
            "points": (self.points)(&uid).await.map_err(|_| storage_unavailable())?,
        }))
    }

    /// `cancel`. The context hash is validated before anything is revoked, so a malformed cookie
    /// cannot be used to revoke a context that is not the caller's.
    pub async fn cancel(&self, context_token: Option<&Value>) -> Result<Value, WalletError> {
        self._context_hash(context_token)?;
        Ok(self.auth().logout(None, context_token.and_then(Value::as_str)).await?)
    }
}

/// `json.dumps(..., sort_keys=True, separators=(",", ":"))` over the sign-in audit body.
///
/// Sorted, compact, and **ASCII**: the reference takes `json.dumps`'s default `ensure_ascii`,
/// so an audit for an origin or a name with non-ASCII characters stores `\uXXXX` where the raw
/// bytes would otherwise go. `canonical_bytes` is the other rule, and using it here would write
/// a different row for the same sign-in.
/// One sign-in, as the audit body records it.
struct SignIn<'a> {
    identifier: &'a str,
    uid: &'a str,
    address: &'a str,
    origin: &'a str,
    mode: &'a str,
    message: &'a str,
    signature: &'a [u8],
    now: i64,
}

fn audit_body(sign_in: &SignIn) -> String {
    let hex = |raw: &[u8]| hex::encode(Sha256::digest(raw));
    let body = forecast_domain::python_json_bytes(&json!({
        "schemaVersion": 1,
        "kind": "wallet_signed_in",
        "challengeId": sign_in.identifier,
        "userId": sign_in.uid,
        "address": sign_in.address,
        "origin": sign_in.origin,
        "chain": CHAIN,
        "purpose": PURPOSE,
        "proofRoles": ["sign_in_forecast_profile", "link_forecast_profile"],
        "mode": sign_in.mode,
        "messageSha256": hex(sign_in.message.as_bytes()),
        "signatureSha256": hex(sign_in.signature),
        "signatureVerified": true,
        "verification": "ed25519_sign_message",
        "verifiedAt": sign_in.now,
    }))
    .unwrap_or_default();
    String::from_utf8(body).unwrap_or_default()
}

/// Python truthiness for the values that reach these checks: an empty string and a null both read
/// as absent, and a number or an object as present.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
        Value::Number(_) => true,
    }
}

/// The two services share an error shape, and this module is where they meet. The conversion
/// is identity on every field: `_failure` reads the same to a caller whichever service raised it.
impl From<auth::AuthError> for WalletError {
    fn from(error: auth::AuthError) -> Self {
        Self {
            status: error.status,
            code: error.code,
            message: error.message,
        }
    }
}

fn storage_unavailable() -> WalletError {
    WalletError::new(
        503,
        "wallet_storage_unavailable",
        "The wallet link could not be saved. Please try again later.",
    )
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use hmac::{Hmac, Mac};
    use std::cell::{Cell, RefCell};
    use std::collections::VecDeque;
    use std::rc::Rc;

    const SECRET: &[u8] = b"golden-secret";
    const ORIGIN: &str = "https://forecast.eastsea.xyz";
    const ESCAPED: &str = "https://éxample.test";

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/wallet-login-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("wallet login golden")).expect("json")
    }

    fn digest(value: &str) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(SECRET).expect("hmac key");
        mac.update(value.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    /// Everything one worker injects, behind an `Rc`.
    ///
    /// Held this way rather than borrowed from a struct so the closures that carry it are
    /// `'static`: an injected effect that borrows the same value the store lives in makes every
    /// effect share a lifetime with the database for no benefit at all.
    struct State {
        now: Cell<i64>,
        counter: Cell<i64>,
        verified: RefCell<Vec<Value>>,
        creations: Cell<i64>,
        points: RefCell<VecDeque<Value>>,
    }

    impl State {
        fn new(now: i64, recorded: &[Value]) -> Rc<Self> {
            Rc::new(Self {
                now: Cell::new(now),
                counter: Cell::new(0),
                verified: RefCell::new(Vec::new()),
                creations: Cell::new(0),
                // `PointsService(self.db).summary(uid)` is unported, so each summary arrives from
                // the vector, in the order the sign-ins produced them. A sign-in that returned an
                // earlier one's points would be caught here rather than papered over.
                points: RefCell::new(
                    recorded
                        .iter()
                        .filter(|entry| !entry["result"]["points"].is_null())
                        .map(|entry| entry["result"]["points"].clone())
                        .collect(),
                ),
            })
        }

        fn token(&self) -> String {
            self.counter.set(self.counter.get() + 1);
            format!("{:0<32}", format!("wl{}", self.counter.get()))
        }

        fn rows(db: &Sqlite, table: &str) -> Value {
            let (rows, _) = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .unwrap_or_else(|error| panic!("{table}: {error}"));
            json!(rows)
        }
    }

    /// The six injected effects, each owning a handle on the shared state.
    struct Effects {
        clock: Box<dyn Fn() -> i64>,
        hash: Box<dyn Fn(&str) -> String>,
        token: Box<dyn Fn() -> String>,
        verifier: Box<SignatureVerifier>,
        points: Box<PointsSummary>,
        on_create: Box<dyn Fn() -> BoxFuture<Result<(), ()>>>,
    }

    impl Effects {
        fn new(state: &Rc<State>) -> Self {
            let clock = Rc::clone(state);
            let token = Rc::clone(state);
            let verifier = Rc::clone(state);
            let points = Rc::clone(state);
            let on_create = Rc::clone(state);
            Self {
                clock: Box::new(move || clock.now.get()),
                hash: Box::new(digest),
                token: Box::new(move || token.token()),
                // The verifier answers one exact signature and records every message it was handed
                // — the only place the recomposed sign-in message can be compared byte for byte.
                verifier: Box::new(move |key, message, signature| {
                    verifier.verified.borrow_mut().push(json!({
                        "key": hex::encode(&key),
                        "message": String::from_utf8_lossy(&message),
                        "signature": hex::encode(&signature),
                        "valid": signature == vec![1u8; 64],
                    }));
                    let valid = signature == vec![1u8; 64];
                    Box::pin(async move { Ok(valid) })
                }),
                points: Box::new(move |_uid| {
                    let value = points.points.borrow_mut().pop_front().unwrap_or(Value::Null);
                    Box::pin(async move { Ok(value) })
                }),
                on_create: Box::new(move || {
                    on_create.creations.set(on_create.creations.get() + 1);
                    Box::pin(async { Ok(()) })
                }),
            }
        }

        fn login<'a>(&'a self, db: &'a dyn Database, origin: &str) -> Result<WalletLogin<'a>, String> {
            WalletLogin::new(db, &*self.clock, &*self.hash, &*self.token, &*self.verifier, &*self.points, origin)
        }
    }

    /// A service borrowed from a vector, together with the position being replayed.
    struct Replay<'a, 'w> {
        calls: &'a [Value],
        index: usize,
        login: &'a WalletLogin<'w>,
    }

    impl Replay<'_, '_> {
        /// Check one call against the vector, including *which* call it is — so a replay that
        /// drifts out of step fails instead of quietly comparing against its neighbour.
        fn check(&mut self, position: usize, produced: Result<Value, WalletError>) {
            let entry = &self.calls[position];
            let name = entry["call"].as_str().unwrap().to_string();
            assert_eq!(entry["call"], self.calls[self.index]["call"], "call {position} is not the one being replayed");
            match produced {
                Ok(value) => {
                    assert!(entry["error"].is_null(), "{name}: succeeded where the reference refused");
                    assert_eq!(value, entry["result"], "{name}: a different result");
                }
                Err(error) => {
                    assert!(!entry["error"].is_null(), "{name}: refused with {error:?} where the reference succeeded");
                    assert_eq!(error.status as i64, entry["error"]["status"].as_i64().unwrap(), "{name}: status");
                    assert_eq!(Some(error.code), entry["error"]["code"].as_str(), "{name}: code");
                    assert_eq!(Some(error.message), entry["error"]["message"].as_str(), "{name}: message");
                }
            }
            self.index += 1;
        }

        /// The recorded inputs are the arguments: a case cannot pass by being run with a different
        /// argument than the one it was recorded with.
        fn recorded(&self, position: usize, field: &str) -> Value {
            self.calls[position]["input"].get(field).cloned().unwrap_or(Value::Null)
        }

        fn session(&self, position: usize) -> Option<String> {
            self.recorded(position, "sessionToken").as_str().map(str::to_string)
        }

        /// Dispatch on what the vector says the call *is*, not on what it is named — 
        /// `verify:other-context` is a context call, and a replay that guessed from the name would
        /// run the wrong method against the right entry.
        fn at(&mut self, position: usize) {
            let kind = self.calls[position]["kind"].as_str().unwrap().to_string();
            match kind.as_str() {
                "context" => self.context_at(position),
                "cancel" => self.cancel_at(position),
                "challenge" => self.challenge_at(position),
                "verify" => self.verify_at(position),
                other => panic!("call {position}: unknown kind {other}"),
            }
        }

        fn context_at(&mut self, position: usize) {
            let supplied = self.recorded(position, "contextToken");
            let produced = block(self.login.context(Some(&supplied)));
            self.check(position, produced);
        }

        fn cancel_at(&mut self, position: usize) {
            let supplied = self.recorded(position, "contextToken");
            let produced = block(self.login.cancel(Some(&supplied)));
            self.check(position, produced);
        }

        fn challenge_at(&mut self, position: usize) {
            let context = self.recorded(position, "contextToken");
            let body = self.recorded(position, "body");
            let session = self.session(position);
            let produced = block(self.login.challenge(Some(&context), &body, session.as_deref()));
            self.check(position, produced);
        }

        fn verify_at(&mut self, position: usize) {
            let context = self.recorded(position, "contextToken");
            let body = self.recorded(position, "body");
            let session = self.session(position);
            let produced = block(self.login.verify(Some(&context), &body, session.as_deref()));
            self.check(position, produced);
        }
    }

    #[test]
    fn the_reference_wallet_sign_in_is_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let now = document["now"].as_i64().unwrap();
        assert_eq!(document["origin"], json!(ORIGIN));

        let db = Sqlite::from_migrations();
        let state = State::new(now, &calls);
        let effects = Effects::new(&state);
        let mut login = effects.login(&db, ORIGIN).expect("the reference origin is exact");
        login.on_create = Some(&*effects.on_create);
        let auth = Authentication {
            db: &db,
            now_ms: &*effects.clock,
            token_hash: &*effects.hash,
            random_token: &*effects.token,
        };
        let mut replay = Replay { calls: &calls, index: 0, login: &login };

        // A context is reused while it is live, and every way of failing to supply one — including
        // an explicitly empty string, which Python reads as absent — produces a fresh one.
        for position in 0..6 {
            replay.at(position);
        }
        // Revoking a context in the store is what a second worker signing out looks like here.
        let first = calls[0]["result"]["contextToken"].as_str().unwrap().to_string();
        db.run(
            "UPDATE wallet_login_contexts SET revoked_at=? WHERE token_hash=?",
            &[json!(now), json!(digest(&format!("wallet-context:{first}")))],
        )
        .expect("revoke");
        for position in 6..11 {
            replay.at(position);
        }
        let ctx = calls[6]["result"]["contextToken"].clone();

        // The challenge: the body, the mode and the address, each refused before anything is read.
        for position in 11..19 {
            replay.at(position);
        }
        assert_eq!(calls[11]["input"]["contextToken"], ctx, "every challenge names the live context");
        // A guest profile has to exist before the migrate path has anything to convert, and it
        // consumes three tokens — so it is replayed here, in place, rather than assumed.
        let guest = block(auth.register(Some(&json!("Guest Migrator")))).expect("guest");
        let guest_session = guest["sessionToken"].clone();
        let guest_code = guest["recoveryCode"].as_str().unwrap().to_string();
        assert_eq!(calls[19]["input"]["body"]["expectedUserId"], guest["user"]["id"], "the guest the vector migrates");
        for position in 19..22 {
            replay.at(position);
        }
        assert_eq!(calls[20]["input"]["sessionToken"], guest_session, "the migrate challenges carry the guest's session");

        // A sign-in that creates a profile, and the verifier sees the exact message it signed.
        replay.at(22);
        replay.at(23);
        assert_eq!(state.creations.get(), 1, "ownership was proven before the quota was charged");

        // Every refusal that can be reached without disturbing what was just created.
        for position in 24..36 {
            replay.at(position);
        }

        // A returning wallet: the same address, now that it owns a profile.
        for position in 36..39 {
            replay.at(position);
        }
        assert_eq!(
            calls[36]["input"]["body"]["address"], calls[25]["result"]["address"],
            "the returning case signs in with the address the earlier challenge named"
        );
        assert_eq!(state.creations.get(), 2, "a second wallet is a second profile");

        // Migrate: the guest's own session converts the profile it belongs to, and the recovery
        // code it used to have stops working in the same batch.
        for position in 39..42 {
            replay.at(position);
        }
        // A recovery code is not a session, so it is the other service that answers here.
        let produced = block(auth.login(Some(&json!(guest_code)), None)).map_err(WalletError::from);
        replay.check(42, produced);
        assert_eq!(calls[42]["kind"], json!("login"));
        assert_ne!(
            calls[41]["result"]["user"]["id"], calls[23]["result"]["user"]["id"],
            "the wallet did not create a profile for an address that already had one"
        );
        // The challenge was issued with `displayName: "Named Migrator"` and the profile still
        // reads `Guest Migrator`: the reference stores the challenge's name but applies it only
        // when it *creates* a profile, and migrate never does. Reproduced rather than corrected —
        // the profile's name belongs to the account, and the account is not being renamed.
        assert_eq!(calls[40]["input"]["body"]["displayName"], json!("Named Migrator"));
        assert_eq!(
            calls[41]["result"]["user"]["displayName"], json!("Guest Migrator"),
            "the migrated profile keeps the name it registered with, not the challenge's"
        );

        // An expired challenge is a different refusal from a changed one, and the store refuses to
        // let a proof's expiry move — so the clock moves, which is what expiry actually is.
        for position in 43..45 {
            replay.at(position);
        }
        state.now.set(now + 6 * 60 * 1000);
        replay.at(45);
        state.now.set(now);

        // A retired wallet is refused rather than re-created, by both routes into `_owner`: an
        // identity row that is no longer active, and a history with no identity row at all.
        let user_id = calls[23]["result"]["user"]["id"].as_str().unwrap().to_string();
        let retired = calls[47]["input"]["body"]["address"].as_str().unwrap().to_string();
        db.run(
            "INSERT INTO wallet_identities(address,user_id,status,created_at) VALUES(?,?,'tombstone',?)",
            &[json!(retired), json!(user_id), json!(now)],
        )
        .expect("retire");
        db.run(
            concat!(
                "INSERT INTO wallet_audit(id,user_id,address,kind,challenge_id,body,created_at) ",
                "VALUES(?,?,?,?,?,?,?)",
            ),
            &[
                json!("wa_retired"),
                json!(user_id),
                calls[48]["input"]["body"]["address"].clone(),
                json!("wallet_unlinked"),
                Value::Null,
                json!("{}"),
                json!(now),
            ],
        )
        .expect("history");
        for position in 46..50 {
            replay.at(position);
        }

        assert_eq!(replay.index, calls.len(), "every recorded call is replayed");
        assert_eq!(json!(*state.verified.borrow()), document["verified"], "the verifier saw different messages");
        assert_eq!(state.creations.get(), document["creations"].as_i64().unwrap(), "creations");

        // And the store itself, which is what a divergent statement would have moved.
        for (table, expected) in document["rows"].as_object().expect("rows") {
            assert_eq!(State::rows(&db, table), *expected, "{table}: different rows");
        }
    }

    #[test]
    fn an_audit_body_escapes_non_ascii_the_way_python_does() {
        // The reference takes `json.dumps`'s *default* `ensure_ascii`, so an origin whose bytes are
        // not ASCII is stored as `\uXXXX`. `canonical_bytes` is the other rule, and swapping them
        // writes a different audit row for the same sign-in — which is why this fixture exists.
        let document = golden();
        let calls = document["escapedCalls"].as_array().expect("escapedCalls").clone();
        let now = document["now"].as_i64().unwrap();
        assert_eq!(document["escapedOrigin"], json!(ESCAPED));

        let db = Sqlite::from_migrations();
        let state = State::new(now, &calls);
        let effects = Effects::new(&state);
        let mut login = effects.login(&db, ESCAPED).expect("a non-ASCII origin is still an exact origin");
        login.on_create = Some(&*effects.on_create);

        let mut replay = Replay { calls: &calls, index: 0, login: &login };
        for position in 0..3 {
            replay.at(position);
        }
        assert_eq!(replay.index, calls.len());
        assert_eq!(json!(*state.verified.borrow()), document["escapedVerified"], "the verifier saw different messages");

        let body = document["escapedRows"]["wallet_login_audit"][0]["body"].as_str().unwrap().to_string();
        assert!(body.is_ascii(), "an audit body is ASCII, with every non-ASCII character escaped: {body}");
        assert!(
            body.contains(r"https://\u00e9xample.test"),
            "and the origin's e-acute is stored as its escape, not its bytes: {body}"
        );
        for (table, expected) in document["escapedRows"].as_object().expect("rows") {
            assert_eq!(State::rows(&db, table), *expected, "{table}: different rows");
        }
    }

    #[test]
    fn the_origin_boundary_is_the_one_the_wallet_service_already_enforces() {
        // The reference constructs a `WalletService` and discards it purely for this check, so the
        // two endpoints cannot disagree about which origins a signed message may name.
        let db = Sqlite::from_migrations();
        let state = State::new(0, &[]);
        let effects = Effects::new(&state);
        for origin in ["http://forecast.eastsea.xyz", "https://forecast.eastsea.xyz/", "forecast.eastsea.xyz"] {
            assert!(effects.login(&db, origin).is_err(), "{origin} is not an exact origin");
        }
        assert!(effects.login(&db, ORIGIN).is_ok());
        assert!(effects.login(&db, "http://localhost").is_ok(), "local development is the one exception");
    }
}
