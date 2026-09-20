//! Session cookie → user id, the same statement `Auth.authenticate` runs.

use crate::db::{self, Database, Row};
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use worker::*;

pub const SESSION_COOKIE: &str = "__Host-forecast_session";
pub const AUTH_CONTEXT_COOKIE: &str = "__Host-forecast_auth";

pub fn cookie(req: &Request, name: &str) -> Option<String> {
    let raw = req.headers().get("cookie").ok().flatten()?;
    if raw.len() > 8192 {
        return None;
    }
    raw.split(';').map(str::trim).find_map(|pair| {
        let (key, value) = pair.split_once('=')?;
        (key == name).then(|| value.trim_matches('"').to_string())
    })
}

pub fn token_hash(secret: &str, token: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac key");
    mac.update(token.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

pub async fn user_id(env: &Env, session: &D1DatabaseSession, req: &Request, now_ms: i64) -> Result<Option<String>> {
    let Some(token) = cookie(req, SESSION_COOKIE) else {
        return Ok(None);
    };
    let context = cookie(req, AUTH_CONTEXT_COOKIE);
    if token.len() > 256 || context.as_ref().is_some_and(|c| c.len() > 256) {
        return Ok(None);
    }
    let secret = env.secret("SESSION_SECRET")?.to_string();
    if secret.len() < 32 {
        return Err("configuration_unavailable".into());
    }
    let context_hash = context.map(|c| token_hash(&secret, &format!("wallet-context:{c}")));
    let statement = session
        .prepare(
            "SELECT u.id FROM sessions s JOIN users u ON u.id=s.user_id \
             LEFT JOIN wallet_login_contexts c ON c.token_hash=s.context_hash \
             WHERE s.token_hash=? AND s.expires_at>? AND ((s.context_hash IS NULL AND NOT EXISTS \
             (SELECT 1 FROM wallet_identities i WHERE i.user_id=u.id AND i.converted_at IS NOT NULL)) \
             OR (s.context_hash=? AND c.epoch=s.context_epoch AND c.revoked_at IS NULL AND c.expires_at>? \
             AND c.active_session_hash=s.token_hash))",
        )
        .bind(&[
            token_hash(&secret, &format!("session:{token}")).into(),
            wasm_bindgen::JsValue::from_f64(now_ms as f64),
            context_hash.map_or(wasm_bindgen::JsValue::NULL, |h| h.into()),
            wasm_bindgen::JsValue::from_f64(now_ms as f64),
        ])?;
    let row: Option<Value> = statement.first(None).await?;
    Ok(row
        .and_then(|r| r["id"].as_str().map(str::to_string))
        .filter(|id| json!(id) != Value::Null))
}

/// HMAC(SESSION_SECRET, client IP) as the anonymous rate-limit identity.
pub fn fingerprint(env: &Env, req: &Request) -> Result<String> {
    let secret = env.secret("SESSION_SECRET")?.to_string();
    let ip = req
        .headers()
        .get("CF-Connecting-IP")?
        .unwrap_or_else(|| "local".to_string());
    Ok(token_hash(&secret, &ip))
}

// ---------------------------------------------------------------- the service
//
// `Authentication`. The recovery code is a high-entropy random secret, never a password, so the
// only things that have to be got right about the storage are that a failure and a wrong code are
// told apart — one is the user's problem and one is ours — and that a wallet-converted profile
// cannot be reached with a code issued before the conversion.

/// `30 * 24 * 60 * 60 * 1000`.
pub const SESSION_LIFETIME_MS: i64 = 30 * 24 * 60 * 60 * 1000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl AuthError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

/// `invalid()`: 400, "Please check your input."
fn invalid() -> AuthError {
    AuthError::new(400, "invalid_input", "Please check your input.")
}

fn invalid_recovery_code() -> AuthError {
    AuthError::new(
        401,
        "invalid_recovery_code",
        "Enter an old Forecast recovery code, never a wallet recovery phrase.",
    )
}

fn wrong_recovery_code() -> AuthError {
    AuthError::new(401, "invalid_recovery_code", "Check your recovery code and try again.")
}

fn context_required() -> AuthError {
    AuthError::new(
        409,
        "wallet_context_required",
        "Prepare sign-in again before restoring your profile.",
    )
}

fn sign_in_changed() -> AuthError {
    AuthError::new(
        409,
        "wallet_login_changed",
        "This sign-in request changed. Start sign-in again.",
    )
}

/// `authentication_unavailable`. The reference catches the raw driver exception in `login` and
/// re-raises its own 503 from there; `register` and `logout` let it escape to the app's generic
/// handler. One code covers all three here, because the `Database` trait has already flattened the
/// driver's error to `worker::Error` and the distinction is no longer recoverable.
fn unavailable() -> AuthError {
    AuthError::new(
        503,
        "authentication_unavailable",
        "Sign-in is temporarily unavailable.",
    )
}

/// `re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value)`.
///
/// 192 bits of entropy is the floor; the alphabet is url-safe because the value is handed to a
/// person as a recovery code and to a browser as a cookie.
fn shaped(value: &str) -> bool {
    (32..=256).contains(&value.len())
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
}

/// `str.isspace()`, which is **not** `char::is_whitespace`.
///
/// Python's set is the Unicode `White_Space` property plus U+001C..U+001F — the file, group,
/// record and unit separators, which Unicode does not call whitespace at all. The difference is
/// load-bearing here because the trim runs *before* the control-character check: `"\u{1c}Ada"` is
/// a valid name in the reference and a rejected one under a plain Rust `trim`.
fn python_space(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

/// `text`: a trimmed string of a bounded length, with no control characters and no surrogates.
///
/// Named for what it validates rather than for the reference's `text`, which would shadow the row
/// accessor of the same name.
pub fn checked_text(value: Option<&Value>, limit: usize, minimum: usize) -> Result<String, AuthError> {
    // `type(value) is not str`: a number is not a string that happens to look like one.
    let Some(value) = value.and_then(Value::as_str) else {
        return Err(invalid());
    };
    let trimmed = value.trim_matches(python_space);
    let length = trimmed.chars().count();
    if length < minimum || length > limit {
        return Err(invalid());
    }
    // Rust strings are UTF-8, so a lone surrogate cannot be present; the control-character half of
    // the check still has to run, and it is the half that stops an injected newline.
    if trimmed.chars().any(|character| (character as u32) < 32) {
        return Err(invalid());
    }
    Ok(trimmed.to_string())
}

/// `public_user`: the only fields a user's own record exposes. Anything that would let a caller
/// reach the recovery hash — or the wallet — stays behind.
///
/// The reference indexes the row directly rather than reading the columns it wants, so a missing
/// column surfaces as whatever the driver holds there rather than as an omission. Copying the
/// value through keeps that reading.
pub fn public_user(row: &Row) -> Value {
    json!({
        "id": db::get(row, "id"),
        "displayName": db::get(row, "display_name"),
        "handle": db::get(row, "handle"),
        "createdAt": db::get(row, "created_at"),
    })
}

pub struct Authentication<'a> {
    pub db: &'a dyn Database,
    pub now_ms: &'a dyn Fn() -> i64,
    /// `token_hash(value)` with the secret already bound by the caller.
    pub token_hash: &'a dyn Fn(&str) -> String,
    pub random_token: &'a dyn Fn() -> String,
}

impl Authentication<'_> {
    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    /// `token`. A generator that cannot supply 192 bits is a configuration fault rather than a
    /// request fault, so it is an outage and not a rejected input.
    fn token(&self) -> Result<String, AuthError> {
        let value = (self.random_token)();
        if !shaped(&value) {
            return Err(AuthError::new(
                500,
                "token_provider_invalid",
                "Random token provider must supply at least 192 random bits",
            ));
        }
        Ok(value)
    }

    /// `register`. The recovery code is returned once and never stored in the clear.
    pub async fn register(&self, display_name: Option<&Value>) -> Result<Value, AuthError> {
        let display_name = checked_text(display_name, 40, 1)?;
        let (code, session, identifier) = (self.token()?, self.token()?, self.token()?);
        let now = self.now();
        // A truncated identifier is still 24 url-safe characters, so collisions stay improbable
        // without a uniqueness check that would cost a round trip.
        let uid = format!("u_{}", &identifier[..24]);
        let handle = format!("f_{}", identifier[..12].to_lowercase());
        self.db
            .batch(&[
                (
                    "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES(?,?,?,?,?)".to_string(),
                    vec![
                        json!(uid),
                        json!(display_name),
                        json!(handle),
                        json!((self.token_hash)(&format!("recovery:{code}"))),
                        json!(now),
                    ],
                ),
                (
                    "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)".to_string(),
                    vec![
                        json!((self.token_hash)(&format!("session:{session}"))),
                        json!(uid),
                        json!(now),
                        json!(now + SESSION_LIFETIME_MS),
                    ],
                ),
            ])
            .await
            .map_err(|_| unavailable())?;
        Ok(json!({
            "user": {"id": uid, "displayName": display_name, "handle": handle, "createdAt": now},
            "recoveryCode": code,
            "sessionToken": session,
        }))
    }

    /// `login`. Every write is guarded by a compare-and-set row, so a code that stopped being
    /// valid between the read and the batch cannot be spent anyway.
    pub async fn login(&self, recovery_code: Option<&Value>, context_token: Option<&Value>) -> Result<Value, AuthError> {
        let Some(recovery_code) = recovery_code.and_then(Value::as_str) else {
            return Err(invalid_recovery_code());
        };
        if !shaped(recovery_code) {
            return Err(invalid_recovery_code());
        }
        let mut context: Option<Row> = None;
        if let Some(value) = context_token {
            // `context_token is not None`: absence is not a failure, a malformed one is.
            let Some(token) = value.as_str() else {
                return Err(context_required());
            };
            if !shaped(token) {
                return Err(context_required());
            }
            let row = self
                .db
                .first(
                    "SELECT * FROM wallet_login_contexts WHERE token_hash=?",
                    &[json!((self.token_hash)(&format!("wallet-context:{token}")))],
                )
                .await
                .map_err(|_| unavailable())?;
            // A row that has gone missing, been revoked or expired has to be told apart from the
            // one the caller prepared; all three mean "start again".
            let usable = row.as_ref().is_some_and(|row| {
                db::get(row, "revoked_at") == &Value::Null
                    && db::int(row, "expires_at").is_some_and(|at| at > self.now())
            });
            if !usable {
                return Err(context_required());
            }
            context = row;
        }
        let user = self
            .db
            .first(
                concat!(
                    "SELECT * FROM users WHERE recovery_hash=? AND NOT EXISTS ",
                    "(SELECT 1 FROM wallet_identities i WHERE i.user_id=users.id AND i.converted_at IS NOT NULL)",
                ),
                &[json!((self.token_hash)(&format!("recovery:{recovery_code}")))],
            )
            .await
            .map_err(|_| unavailable())?;
        let Some(user) = user else {
            return Err(wrong_recovery_code());
        };
        let (session, now, guard) = (self.token()?, self.now(), self.token()?);
        let session_hash = (self.token_hash)(&format!("session:{session}"));
        let recovery_hash = (self.token_hash)(&format!("recovery:{recovery_code}"));
        let user_id = db::get(&user, "id").clone();
        let mut statements: Vec<(String, Vec<Value>)> = vec![(
            concat!(
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM users ",
                "WHERE id=? AND recovery_hash=?) AND NOT EXISTS(SELECT 1 FROM wallet_identities ",
                "WHERE user_id=? AND converted_at IS NOT NULL) THEN 1 ELSE 0 END",
            )
            .to_string(),
            vec![json!(guard), user_id.clone(), json!(recovery_hash), user_id.clone()],
        )];
        match &context {
            Some(context) => {
                let context_hash = db::get(context, "token_hash").clone();
                statements.extend([
                    // The check covers the active session as well as the latest pending proof: a
                    // wallet verify can commit without changing its latest challenge id.
                    (
                        concat!(
                            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM wallet_login_contexts ",
                            "WHERE token_hash=? AND epoch=? AND latest_challenge_id IS ? AND active_session_hash IS ? ",
                            "AND revoked_at IS NULL AND expires_at>?) THEN 1 ELSE 0 END",
                        )
                        .to_string(),
                        vec![
                            json!(format!("{guard}:context")),
                            context_hash.clone(),
                            db::get(context, "epoch").clone(),
                            db::get(context, "latest_challenge_id").clone(),
                            db::get(context, "active_session_hash").clone(),
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
                        "DELETE FROM sessions WHERE context_hash=?".to_string(),
                        vec![context_hash.clone()],
                    ),
                    (
                        concat!(
                            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,context_hash,context_epoch) ",
                            "VALUES(?,?,?,?,?,?)",
                        )
                        .to_string(),
                        vec![
                            json!(session_hash),
                            user_id.clone(),
                            json!(now),
                            json!(now + SESSION_LIFETIME_MS),
                            context_hash.clone(),
                            db::get(context, "epoch").clone(),
                        ],
                    ),
                    (
                        concat!(
                            "UPDATE wallet_login_contexts SET latest_challenge_id=NULL,active_session_hash=? ",
                            "WHERE token_hash=?",
                        )
                        .to_string(),
                        vec![json!(session_hash), context_hash],
                    ),
                ]);
            }
            None => statements.push((
                "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)".to_string(),
                vec![json!(session_hash), user_id.clone(), json!(now), json!(now + SESSION_LIFETIME_MS)],
            )),
        }
        statements.push((
            "DELETE FROM mutation_guards WHERE token IN (?,?)".to_string(),
            vec![json!(guard), json!(format!("{guard}:context"))],
        ));
        if self.db.batch(&statements).await.is_err() {
            // The guard refused, or the store did. Both arrive as an error, and which one it was
            // has to be read back out of the store rather than guessed from the message.
            let converted = self
                .db
                .first(
                    "SELECT address FROM wallet_identities WHERE user_id=? AND converted_at IS NOT NULL",
                    &[user_id],
                )
                .await
                .map_err(|_| unavailable())?;
            if converted.is_some() {
                return Err(AuthError::new(401, "invalid_recovery_code", "Sign in with your wallet to continue."));
            }
            if let Some(context) = &context {
                let latest = self
                    .db
                    .first(
                        "SELECT * FROM wallet_login_contexts WHERE token_hash=?",
                        &[db::get(context, "token_hash").clone()],
                    )
                    .await
                    .map_err(|_| unavailable())?;
                let changed = latest.as_ref().is_none_or(|latest| {
                    db::get(latest, "revoked_at") != &Value::Null
                        || db::int(latest, "expires_at").is_none_or(|at| at <= self.now())
                        || db::get(latest, "epoch") != db::get(context, "epoch")
                        || db::get(latest, "latest_challenge_id") != db::get(context, "latest_challenge_id")
                        || db::get(latest, "active_session_hash") != db::get(context, "active_session_hash")
                });
                if changed {
                    return Err(sign_in_changed());
                }
            }
            return Err(unavailable());
        }
        Ok(json!({"user": public_user(&user), "sessionToken": session}))
    }

    /// `authenticate`. `None` covers both "no session" and "a session that is no longer valid";
    /// the caller is not told which, because the difference is only useful to an attacker.
    pub async fn authenticate(
        &self,
        session_token: Option<&str>,
        context_token: Option<&str>,
    ) -> Result<Option<Value>, AuthError> {
        let Some(session_token) = session_token.filter(|token| !token.is_empty() && token.len() <= 256) else {
            return Ok(None);
        };
        if context_token.is_some_and(|token| token.len() > 256) {
            return Ok(None);
        }
        // An empty context cookie is bound as `NULL` rather than hashed, which is what the
        // reference's truthiness test does; the statement then requires `context_hash IS NULL`.
        let context_hash = context_token
            .filter(|token| !token.is_empty())
            .map(|token| json!((self.token_hash)(&format!("wallet-context:{token}"))))
            .unwrap_or(Value::Null);
        let now = self.now();
        let row = self
            .db
            .first(
                concat!(
                    "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id ",
                    "LEFT JOIN wallet_login_contexts c ON c.token_hash=s.context_hash ",
                    "WHERE s.token_hash=? AND s.expires_at>? AND ((s.context_hash IS NULL AND NOT EXISTS ",
                    "(SELECT 1 FROM wallet_identities i WHERE i.user_id=u.id AND i.converted_at IS NOT NULL)) ",
                    "OR (s.context_hash=? AND c.epoch=s.context_epoch AND c.revoked_at IS NULL AND c.expires_at>? ",
                    "AND c.active_session_hash=s.token_hash))",
                ),
                &[
                    json!((self.token_hash)(&format!("session:{session_token}"))),
                    json!(now),
                    context_hash,
                    json!(now),
                ],
            )
            .await
            .map_err(|_| unavailable())?;
        Ok(row.as_ref().map(public_user))
    }

    /// `logout`. Both the cookie's context and the session's own bound context are revoked, so a
    /// late cookie that overwrote the browser's cannot leave a context live.
    pub async fn logout(&self, session_token: Option<&str>, context_token: Option<&str>) -> Result<Value, AuthError> {
        let hash = |prefix: &str, token: Option<&str>| {
            token
                .filter(|token| !token.is_empty())
                .map(|token| (self.token_hash)(&format!("{prefix}{token}")))
        };
        let session_hash = hash("session:", session_token);
        let context_hash = hash("wallet-context:", context_token);
        let bound = self
            .db
            .first(
                "SELECT context_hash FROM sessions WHERE token_hash=?",
                &[session_hash.clone().map_or(Value::Null, Value::from)],
            )
            .await
            .map_err(|_| unavailable())?;
        let bound_hash = bound.as_ref().map_or(Value::Null, |row| db::get(row, "context_hash").clone());
        let now = self.now();
        let both = vec![
            context_hash.map_or(Value::Null, Value::from),
            bound_hash,
        ];
        let with_now = |both: Vec<Value>| {
            let mut params = vec![json!(now)];
            params.extend(both);
            params
        };
        self.db
            .batch(&[
                (
                    concat!(
                        "UPDATE wallet_login_contexts SET revoked_at=COALESCE(revoked_at,?),epoch=epoch+1,",
                        "active_session_hash=NULL WHERE token_hash IN (?,?)",
                    )
                    .to_string(),
                    with_now(both.clone()),
                ),
                (
                    concat!(
                        "UPDATE wallet_login_challenges SET revoked_at=? WHERE used_at IS NULL ",
                        "AND revoked_at IS NULL AND context_hash IN (?,?)",
                    )
                    .to_string(),
                    with_now(both.clone()),
                ),
                (
                    "DELETE FROM sessions WHERE token_hash=? OR context_hash IN (?,?)".to_string(),
                    {
                        let mut params = vec![session_hash.map_or(Value::Null, Value::from)];
                        params.extend(both);
                        params
                    },
                ),
            ])
            .await
            .map_err(|_| unavailable())?;
        Ok(json!({"ok": true}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use std::cell::Cell;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/auth-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("auth golden")).expect("json")
    }

    /// The generator's own hash: HMAC-SHA256 over the prefixed value under a fixed key. The key is
    /// arbitrary; the *prefix* is not, because it is what keeps a recovery code from being usable
    /// as a session token.
    fn digest(value: &str) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(b"golden-secret").expect("hmac key");
        mac.update(value.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    /// Assert one call against the golden's record, whether it succeeded or was refused.
    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, AuthError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
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
        *index += 1;
    }

    /// `authenticate` answers with an absent row rather than an error, which the vector records as
    /// a null result.
    fn present(produced: Result<Option<Value>, AuthError>) -> Result<Value, AuthError> {
        produced.map(|value| value.unwrap_or(Value::Null))
    }

    #[test]
    fn the_reference_authentication_is_reproduced_call_for_call() {
        let document = golden();
        let db = Sqlite::from_migrations();
        let now = document["now"].as_i64().unwrap();
        assert_eq!(document["sessionLifetimeMs"].as_i64(), Some(SESSION_LIFETIME_MS));
        let clock = || now;
        let counter = Cell::new(0);
        let token = || {
            counter.set(counter.get() + 1);
            format!("{:0<32}", format!("token{}", counter.get()))
        };
        let hash = |value: &str| digest(value);
        let auth = Authentication {
            db: &db,
            now_ms: &clock,
            token_hash: &hash,
            random_token: &token,
        };
        let calls = document["calls"].as_array().expect("calls");
        let mut index = 0usize;
        let input = |index: usize, field: &str| calls[index]["input"].get(field).cloned();
        let recorded = |calls: &[Value], name: &str| {
            calls
                .iter()
                .find(|entry| entry["call"] == json!(name))
                .unwrap_or_else(|| panic!("{name}: not in the golden"))
                .clone()
        };
        let string_at = |position: usize, field: &str| {
            calls[position]["input"].get(field).and_then(Value::as_str).map(str::to_string)
        };
        let refused = |name: &str| {
            let entry = calls
                .iter()
                .find(|entry| entry["call"] == json!(name))
                .unwrap_or_else(|| panic!("{name}: not in the golden"));
            (
                entry["error"]["status"].as_i64().unwrap(),
                entry["error"]["message"].as_str().unwrap().to_string(),
            )
        };

        // register: what a display name has to be before an account exists at all. The inputs come
        // back out of the vector rather than being restated, so a case cannot pass by being run
        // with a different argument than the one it was recorded with.
        for position in 0..8 {
            let name = input(position, "displayName");
            check(calls, &mut index, block(auth.register(name.as_ref())));
        }
        let registered = calls[0]["result"].clone();
        let session = registered["sessionToken"].as_str().unwrap().to_string();
        assert_eq!(
            calls[8]["input"]["recoveryCode"],
            registered["recoveryCode"],
            "every later login uses the code that registration handed back"
        );
        let user_id = registered["user"]["id"].as_str().unwrap().to_string();
        assert_eq!(registered["user"]["handle"], json!("f_token3000000"), "the handle is the token's first twelve");
        assert_eq!(registered["user"]["displayName"], json!("Ada Lovelace"), "the name is trimmed");

        // login without a context: off-shape, unknown, and absent are three different messages.
        check(calls, &mut index, block(auth.login(input(8, "recoveryCode").as_ref(), None)));
        check(calls, &mut index, block(auth.login(input(9, "recoveryCode").as_ref(), None)));
        check(calls, &mut index, block(auth.login(input(10, "recoveryCode").as_ref(), None)));
        check(calls, &mut index, block(auth.login(input(11, "recoveryCode").as_ref(), None)));
        // A malformed code, an unknown one and an absent one are all 401, and they are told
        // apart only by their message — which is the whole reason `text` and `shaped` are not
        // the same check.
        for name in ["login:short", "login:unknown", "login:not-a-string"] {
            assert_eq!(refused(name).0, 401, "{name}: a bad code is a 401");
        }
        assert_ne!(
            refused("login:short").1,
            refused("login:unknown").1,
            "an off-shape code and an unknown one are told apart by their message"
        );
        check(calls, &mut index, block(auth.login(input(12, "recoveryCode").as_ref(), None)));
        check(calls, &mut index, block(auth.login(input(13, "recoveryCode").as_ref(), None)));

        // login with a context: every reason a prepared sign-in stops being usable.
        let good = format!("{:0<32}", "contextgood");
        let revoked = format!("{:0<32}", "contextrevoked");
        let expired = format!("{:0<32}", "contextexpired");
        let prepare = |token: &str, expires_in: i64, revoked: bool| {
            db.run(
                concat!(
                    "INSERT INTO wallet_login_contexts(token_hash,epoch,latest_challenge_id,active_session_hash,",
                    "created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?)",
                ),
                &[
                    json!(digest(&format!("wallet-context:{token}"))),
                    json!(1),
                    Value::Null,
                    Value::Null,
                    json!(now - 1000),
                    json!(now + expires_in),
                    if revoked { json!(now) } else { Value::Null },
                ],
            )
            .expect("prepare context");
        };
        prepare(&revoked, 600_000, true);
        prepare(&expired, -1, false);
        db.run(
            "UPDATE wallet_login_contexts SET latest_challenge_id=? WHERE token_hash=?",
            &[json!("challenge-1"), json!(digest(&format!("wallet-context:{good}")))],
        )
        .expect("challenge");
        prepare(&good, 600_000, false);
        db.run(
            "UPDATE wallet_login_contexts SET latest_challenge_id=? WHERE token_hash=?",
            &[json!("challenge-1"), json!(digest(&format!("wallet-context:{good}")))],
        )
        .expect("challenge");

        check(calls, &mut index, block(auth.login(input(14, "recoveryCode").as_ref(), input(14, "contextToken").as_ref())));
        check(calls, &mut index, block(auth.login(input(15, "recoveryCode").as_ref(), input(15, "contextToken").as_ref())));
        check(calls, &mut index, block(auth.login(input(16, "recoveryCode").as_ref(), input(16, "contextToken").as_ref())));
        check(calls, &mut index, block(auth.login(input(17, "recoveryCode").as_ref(), input(17, "contextToken").as_ref())));
        check(calls, &mut index, block(auth.login(input(18, "recoveryCode").as_ref(), input(18, "contextToken").as_ref())));
        check(calls, &mut index, block(auth.login(input(19, "recoveryCode").as_ref(), input(19, "contextToken").as_ref())));
        let bound_session = calls[19]["result"]["sessionToken"].as_str().unwrap().to_string();

        // authenticate: the two branches, and the ways each fails.
        check(calls, &mut index, present(block(auth.authenticate(Some(&session), None))));
        check(calls, &mut index, present(block(auth.authenticate(Some(&bound_session), Some(&good)))));
        check(calls, &mut index, present(block(auth.authenticate(Some(&bound_session), None))));
        check(calls, &mut index, present(block(auth.authenticate(Some(&session), Some(&good)))));
        check(calls, &mut index, present(block(auth.authenticate(None, None))));
        check(calls, &mut index, present(block(auth.authenticate(Some(""), None))));
        check(calls, &mut index, present(block(auth.authenticate(string_at(26, "sessionToken").as_deref(), None))));
        check(calls, &mut index, present(block(auth.authenticate(Some(&session), string_at(27, "contextToken").as_deref()))));
        check(calls, &mut index, present(block(auth.authenticate(string_at(28, "sessionToken").as_deref(), None))));
        // A context bumped to a new epoch invalidates the session that recorded the old one.
        db.run(
            "UPDATE wallet_login_contexts SET epoch=epoch+1 WHERE token_hash=?",
            &[json!(digest(&format!("wallet-context:{good}")))],
        )
        .expect("bump");
        check(calls, &mut index, present(block(auth.authenticate(Some(&bound_session), Some(&good)))));
        assert!(
            recorded(calls, "authenticate:stale-epoch")["result"].is_null(),
            "a context on a new epoch invalidates the session that recorded the old one"
        );
        assert!(
            !recorded(calls, "authenticate:plain")["result"].is_null(),
            "while the plain branch is untouched by a context it never had"
        );

        // The wallet conversion closes the recovery-code door behind it.
        // The address comes out of the vector too: a fixture that invents its own would pass
        // while the two stores disagreed about what was linked.
        let address = document["rows"]["wallet_identities"][0]["address"].clone();
        db.run(
            "INSERT INTO wallet_identities(address,user_id,status,created_at,converted_at) VALUES(?,?,?,?,?)",
            &[address, json!(user_id), json!("active"), json!(now - 500), json!(now)],
        )
        .expect("conversion");

        check(calls, &mut index, present(block(auth.authenticate(Some(&session), None))));
        prepare(&format!("{:0<32}", "contextconverted"), 600_000, false);
        check(calls, &mut index, block(auth.login(input(31, "recoveryCode").as_ref(), input(31, "contextToken").as_ref())));
        check(calls, &mut index, block(auth.login(input(32, "recoveryCode").as_ref(), None)));
        check(calls, &mut index, block(auth.register(input(33, "displayName").as_ref())));
        check(calls, &mut index, present(block(auth.authenticate(Some(&bound_session), Some(&good)))));
        // Restoring the epoch reaches the bound branch: a conversion does not close a session the
        // wallet itself established, only the recovery-code path behind it.
        db.run(
            "UPDATE wallet_login_contexts SET epoch=epoch-1 WHERE token_hash=?",
            &[json!(digest(&format!("wallet-context:{good}")))],
        )
        .expect("restore");
        check(calls, &mut index, present(block(auth.authenticate(Some(&bound_session), Some(&good)))));
        assert!(
            !recorded(calls, "authenticate:converted-bound-restored")["result"].is_null(),
            "a wallet-established session outlives the conversion that closed the recovery path"
        );
        assert!(
            recorded(calls, "authenticate:converted")["result"].is_null(),
            "and the same conversion closes the recovery-code session beside it"
        );

        // logout revokes the session's own bound context as well as the cookie's.
        for position in 35..40 {
            let session_token = input(position, "sessionToken").and_then(|value| value.as_str().map(str::to_string));
            let context_token = input(position, "contextToken").and_then(|value| value.as_str().map(str::to_string));
            check(
                calls,
                &mut index,
                block(auth.logout(session_token.as_deref(), context_token.as_deref())),
            );
        }

        // The names and the projection, which are the two pure helpers in the module.
        for entry in document["names"].as_array().expect("names") {
            let value = &entry["input"];
            match checked_text(Some(value), 40, 1) {
                Ok(text) => {
                    assert!(entry["error"].is_null(), "{value}: accepted where the reference refused");
                    assert_eq!(json!(text), entry["result"], "{value}: a different trim");
                }
                Err(error) => {
                    assert!(!entry["error"].is_null(), "{value}: refused where the reference accepted");
                    assert_eq!(Some(error.code), entry["error"]["code"].as_str(), "{value}: code");
                }
            }
        }
        for entry in document["projections"].as_array().expect("projections") {
            let row: Row = entry["row"].as_object().expect("row").clone();
            assert_eq!(public_user(&row), entry["public"], "{row:?}: a different public user");
        }

        // And the store itself, which is what a divergent statement would have moved.
        for (table, expected) in document["rows"].as_object().expect("rows") {
            let (rows, _) = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .unwrap_or_else(|error| panic!("{table}: {error}"));
            assert_eq!(json!(rows), *expected, "{table}: different rows");
        }
        assert_eq!(index, calls.len(), "every recorded call is replayed");
    }
}
