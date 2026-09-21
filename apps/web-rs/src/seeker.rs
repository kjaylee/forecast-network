//! Seeker ownership, verified against mainnet.
//!
//! Every Seeker mints one Seeker Genesis Token: a non-transferable Token-2022 mint that is a
//! member of the SGT group. Finding a member of that group in the signed-in wallet is what proves
//! the account belongs to a Seeker owner. The SKR balance is read alongside as plain information
//! and grants nothing.
//!
//! Everything here reads a provider's `jsonParsed` reply, which means the shape is a convention
//! rather than a contract: an account can arrive partial, an amount as a number instead of a
//! string, an extension list that is not a list. Each of those is *refused* rather than read past.
//! A parser that is merely lenient would report a verified Seeker where the reference reported
//! nothing, which is the one failure mode this module must not have.

use serde_json::{json, Value};

use crate::db::{int, text, Database, Row};

pub const SGT_GROUP: &str = "GT22s89nU4iWFkNXj1Bw6uYhJJWDRPpShHt4Bk8f99Te";
pub const SKR_MINT: &str = "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3";
pub const SKR_DECIMALS: u32 = 6;
pub const TOKEN_PROGRAM: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
pub const TOKEN_2022_PROGRAM: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";

/// The address a verification must be against: the active converted wallet identity, or the
/// linked wallet while none has converted.
pub const CURRENT_ADDRESS: &str =
    "COALESCE((SELECT address FROM wallet_identities WHERE user_id=? AND status='active' \
     AND converted_at IS NOT NULL),(SELECT address FROM wallet_links WHERE user_id=?))";

/// A future that borrows for as long as its caller does, for the seams that reach the request's
/// own database handle.
pub type BorrowedFuture<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + 'a>>;
pub type BoxFuture<T> = std::pin::Pin<Box<dyn std::future::Future<Output = T>>>;
/// The mainnet transport. It is injected so the parsing stays testable without a network.
pub type Rpc = Box<dyn Fn(String, Vec<Value>) -> BoxFuture<Result<Value, ()>>>;
/// `rate_limit(scope, limit, window_ms)`, injected like every other counter in this port.
/// `self.rate_limit(scope, limit, window_ms)`, injected because the limiter is storage.
///
/// A trait rather than a boxed closure, for the reason `PointsSummary` is: `Fn`'s `Output` is an
/// associated type and a trait object over `Fn` is invariant in it, so a closure that reads the
/// request's own database cannot be passed where a `'static` box is wanted — and a rate limit is
/// exactly that. A method can name its own lifetime, and the future it returns borrows `&self`.
pub trait RateLimit {
    fn check<'a>(&'a self, scope: String, limit: i64, window_ms: i64) -> BorrowedFuture<'a, Result<(), ()>>;
}

/// A limiter that never refuses, for the paths where the reference does not count.
pub struct NoLimit;

impl RateLimit for NoLimit {
    fn check<'a>(&'a self, _scope: String, _limit: i64, _window_ms: i64) -> BorrowedFuture<'a, Result<(), ()>> {
        Box::pin(async { Ok(()) })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SeekerError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl SeekerError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

fn changed() -> SeekerError {
    SeekerError::new(
        409,
        "seeker_verification_changed",
        "Your wallet or verification changed. Try verifying again.",
    )
}

fn rpc_unavailable() -> SeekerError {
    SeekerError::new(
        503,
        "seeker_rpc_unavailable",
        "Solana could not be reached. Try again shortly.",
    )
}

/// `_parsed_info`: the `info` object of a parsed account, or nothing.
///
/// A partial reply is not an error here — it is simply an account with no information, which the
/// callers then refuse on their own terms.
pub fn parsed_info(account: &Value) -> Value {
    let info = &account["data"]["parsed"]["info"];
    if info.is_object() {
        info.clone()
    } else {
        Value::Object(serde_json::Map::new())
    }
}

/// `candidate_mints`: Token-2022 accounts holding exactly one indivisible unit.
///
/// Only those can be a genesis token. The check is on the *amount and decimals*, not on the mint
/// address, because the mint is what is being discovered.
pub fn candidate_mints(token_accounts: &[Value]) -> Vec<String> {
    let mut mints: Vec<String> = Vec::new();
    for entry in token_accounts {
        let info = parsed_info(&entry["account"]);
        let amount = &info["tokenAmount"];
        if amount["decimals"].as_i64() == Some(0) && amount["amount"].as_str() == Some("1") && info["mint"].is_string()
        {
            let mint = info["mint"].as_str().unwrap_or_default().to_string();
            if !mints.contains(&mint) {
                mints.push(mint);
            }
        }
    }
    mints
}

/// `_rpc_values`: the `value` array every one of these replies has to carry.
fn rpc_values(reply: &Value) -> Result<&Vec<Value>, String> {
    reply
        .get("value")
        .and_then(Value::as_array)
        .ok_or_else(|| "Incomplete RPC response".to_string())
}

/// `_token_accounts`: the reply, but only if every account in it is complete.
fn token_accounts(reply: &Value) -> Result<&Vec<Value>, String> {
    let accounts = rpc_values(reply)?;
    for entry in accounts {
        let info = parsed_info(&entry["account"]);
        let amount = &info["tokenAmount"];
        // `isascii()` and `isdecimal()` together: the amount is ASCII decimal digits, which
        // excludes both a number and the Unicode digits Python's `isdecimal` would accept.
        let complete = info["mint"].is_string()
            && amount.is_object()
            && amount["decimals"].as_i64().is_some()
            && amount["amount"].as_str().is_some_and(is_ascii_decimal);
        if !complete {
            return Err("Incomplete token account".to_string());
        }
    }
    Ok(accounts)
}

fn is_ascii_decimal(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit())
}

/// `_mint_accounts`: the reply, but only if it is exactly the count asked for and every account
/// and extension in it is complete.
fn mint_accounts(reply: &Value, count: usize) -> Result<&Vec<Value>, String> {
    let accounts = rpc_values(reply)?;
    if accounts.len() != count {
        return Err("Incomplete mint accounts".to_string());
    }
    for account in accounts {
        let info = parsed_info(account);
        let supply_ok = info["supply"].as_str().is_some_and(is_ascii_decimal);
        if info["decimals"].as_i64().is_none() || !supply_ok || !info["extensions"].is_array() {
            return Err("Incomplete mint account".to_string());
        }
        for extension in info["extensions"].as_array().cloned().unwrap_or_default() {
            if !extension.is_object() || !extension["extension"].is_string() {
                return Err("Incomplete mint extension".to_string());
            }
            if extension["extension"].as_str() == Some("tokenGroupMember") {
                let state = &extension["state"];
                let complete = state.is_object() && state["group"].is_string() && state["mint"].is_string();
                if !complete {
                    return Err("Incomplete token group member".to_string());
                }
            }
        }
    }
    Ok(accounts)
}

/// `genesis_member`: the first mint whose group-member extension points at the SGT group.
///
/// The member number is carried when it is an integer and dropped when it is not: a Seeker whose
/// reply omitted it is still a Seeker.
pub fn genesis_member(mint_accounts: &[Value], mints: &[String]) -> Option<(String, Option<i64>)> {
    for (mint, account) in mints.iter().zip(mint_accounts.iter()) {
        let info = parsed_info(account);
        for extension in info["extensions"].as_array().cloned().unwrap_or_default() {
            if extension["extension"].as_str() != Some("tokenGroupMember") {
                continue;
            }
            let state = &extension["state"];
            if state["group"].as_str() == Some(SGT_GROUP) && state["mint"].as_str() == Some(mint.as_str()) {
                return Some((mint.clone(), state["memberNumber"].as_i64()));
            }
        }
    }
    None
}

/// `skr_atomic`: the SKR balance in atomic units, ignoring accounts that are not SKR.
pub fn skr_atomic(token_accounts: &[Value]) -> i64 {
    let mut total = 0i64;
    for entry in token_accounts {
        let info = parsed_info(&entry["account"]);
        if info["mint"].as_str() != Some(SKR_MINT) {
            continue;
        }
        if let Some(amount) = info["tokenAmount"]["amount"].as_str() {
            if let Ok(value) = amount.parse::<i64>() {
                total += value;
            }
        }
    }
    total
}

/// `skr_display`: the balance as a person reads it.
pub fn skr_display(atomic: i64) -> String {
    let scale = 10i64.pow(SKR_DECIMALS);
    let whole = atomic.div_euclid(scale);
    let fraction = atomic.rem_euclid(scale);
    let text = format!("{whole}.{fraction:0width$}", width = SKR_DECIMALS as usize)
        .trim_end_matches('0')
        .trim_end_matches('.')
        .to_string();
    if text.is_empty() {
        "0".to_string()
    } else {
        text
    }
}

/// `projection`: a verification as the public record shows it.
pub fn projection(row: Option<&Row>) -> Value {
    // The reference tests truthiness, so an empty row is no row: a projection of nothing would
    // otherwise report a verified badge with no member and no time.
    let Some(row) = row.filter(|row| !row.is_empty()) else {
        return Value::Null;
    };
    json!({
        "verified": true,
        "memberNumber": int(row, "member_number"),
        "skr": skr_display(int(row, "skr_atomic").unwrap_or(0)),
        "verifiedAt": int(row, "verified_at"),
        "refreshedAt": int(row, "refreshed_at"),
    })
}

/// `SeekerVerification`.
pub struct SeekerVerification<'a> {
    pub db: &'a dyn Database,
    pub rpc: Option<&'a Rpc>,
    pub now_ms: &'a dyn Fn() -> i64,
    pub rate_limit: &'a dyn RateLimit,
}

impl SeekerVerification<'_> {
    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    pub fn available(&self) -> bool {
        self.rpc.is_some()
    }

    async fn call(&self, method: &str, params: Vec<Value>) -> Result<Value, SeekerError> {
        let Some(rpc) = self.rpc else {
            return Err(SeekerError::new(
                503,
                "seeker_unavailable",
                "Seeker verification is not configured.",
            ));
        };
        (rpc)(method.to_string(), params).await.map_err(|_| rpc_unavailable())
    }

    /// `status`: evidence has no passive TTL, and a historical proof must not badge a wallet that
    /// is no longer the one signed in.
    pub async fn status(&self, user_id: &str) -> Result<Value, SeekerError> {
        let sql = format!(
            "SELECT * FROM seeker_verifications WHERE user_id=? AND invalidated_at IS NULL AND address={CURRENT_ADDRESS}"
        );
        let row = self
            .db
            .first(&sql, &[json!(user_id), json!(user_id), json!(user_id)])
            .await
            .map_err(|_| rpc_unavailable())?;
        Ok(projection(row.as_ref()))
    }

    /// `public_badge`: the only field a public record carries.
    pub async fn public_badge(&self, user_id: &str) -> Result<Value, SeekerError> {
        let status = self.status(user_id).await?;
        if status.is_null() {
            Ok(Value::Null)
        } else {
            Ok(json!({"memberNumber": status["memberNumber"]}))
        }
    }

    /// `_address`: the wallet this user signed in with.
    async fn address(&self, user_id: &str) -> Result<String, SeekerError> {
        let identity = self
            .db
            .first(
                "SELECT address FROM wallet_identities WHERE user_id=? AND status='active' AND converted_at IS NOT NULL",
                &[json!(user_id)],
            )
            .await
            .map_err(|_| rpc_unavailable())?;
        if let Some(found) = identity.as_ref().and_then(|row| text(row, "address")) {
            return Ok(found.to_string());
        }
        let link = self
            .db
            .first("SELECT address FROM wallet_links WHERE user_id=?", &[json!(user_id)])
            .await
            .map_err(|_| rpc_unavailable())?;
        match link.as_ref().and_then(|row| text(row, "address")) {
            Some(found) => Ok(found.to_string()),
            None => Err(SeekerError::new(
                409,
                "wallet_required",
                "Sign in with your Seeker wallet before verifying.",
            )),
        }
    }
}

impl SeekerVerification<'_> {
    /// `verify`: find the genesis token, or record that there is not one.
    ///
    /// The whole method is one signed-in wallet, one look, and one write. A wallet with no SGT
    /// *invalidates* a previous verification rather than leaving it standing — the evidence that
    /// was true of a different wallet is not evidence about this one.
    pub async fn verify(&self, user_id: &str) -> Result<Value, SeekerError> {
        if self.rpc.is_none() {
            return Err(SeekerError::new(
                503,
                "seeker_unavailable",
                "Seeker verification is not configured.",
            ));
        }
        if self
            .rate_limit
            .check(format!("seeker-verify:{user_id}"), 6, 3_600_000)
            .await
            .is_err()
        {
            return Err(SeekerError::new(
                429,
                "rate_limited",
                "Too many verification attempts. Try again later.",
            ));
        }
        let address = self.address(user_id).await?;
        let previous = self
            .db
            .first(
                "SELECT revision,address FROM seeker_verifications WHERE user_id=?",
                &[json!(user_id)],
            )
            .await
            .map_err(|_| rpc_unavailable())?;
        let revision = previous.as_ref().and_then(|row| int(row, "revision")).unwrap_or(-1);

        let reply = self
            .call(
                "getTokenAccountsByOwner",
                vec![
                    json!(address),
                    json!({"programId": TOKEN_2022_PROGRAM}),
                    json!({"encoding": "jsonParsed"}),
                ],
            )
            .await?;
        let mints = candidate_mints(token_accounts(&reply).map_err(|_| rpc_unavailable())?);

        let mut member = None;
        for batch in mints.chunks(100) {
            // One binding promise at a time: the runtime cannot fan out RPC.
            let accounts = self
                .call(
                    "getMultipleAccounts",
                    vec![json!(batch), json!({"encoding": "jsonParsed"})],
                )
                .await?;
            let accounts = mint_accounts(&accounts, batch.len()).map_err(|_| rpc_unavailable())?;
            member = genesis_member(accounts, batch);
            if member.is_some() {
                break;
            }
        }
        let skr = self
            .call(
                "getTokenAccountsByOwner",
                vec![
                    json!(address),
                    json!({"mint": SKR_MINT}),
                    json!({"encoding": "jsonParsed"}),
                ],
            )
            .await?;
        let atomic = skr_atomic(token_accounts(&skr).map_err(|_| rpc_unavailable())?);
        let slot = skr["context"]["slot"].as_i64().unwrap_or(0);

        let Some((mint, number)) = member else {
            // The same lookup that found nothing is what invalidates a previous proof, and only
            // for the wallet it was actually made from.
            if previous
                .as_ref()
                .and_then(|row| text(row, "address"))
                .is_some_and(|value| value == address)
            {
                let sql = format!(
                    "UPDATE seeker_verifications SET invalidated_at=?,revision=revision+1 \
                     WHERE user_id=? AND address=? AND revision=? AND address={CURRENT_ADDRESS}"
                );
                self.db
                    .execute(
                        &sql,
                        &[
                            json!(self.now()),
                            json!(user_id),
                            json!(address),
                            json!(revision),
                            json!(user_id),
                            json!(user_id),
                        ],
                    )
                    .await
                    .map_err(|_| rpc_unavailable())?;
                // The invalidation is confirmed by reading the revision back: this `Database`
                // reports no affected-row count.
                let after = self
                    .db
                    .first(
                        "SELECT revision FROM seeker_verifications WHERE user_id=?",
                        &[json!(user_id)],
                    )
                    .await
                    .map_err(|_| rpc_unavailable())?;
                if after.as_ref().and_then(|row| int(row, "revision")) != Some(revision + 1) {
                    return Err(changed());
                }
            }
            return Err(SeekerError::new(
                409,
                "seeker_not_found",
                "No Seeker Genesis Token was found in this wallet.",
            ));
        };

        let now = self.now();
        // One genesis token verifies one account: a Seeker already claimed elsewhere is a
        // refusal, not a second badge.
        let taken = self
            .db
            .first(
                "SELECT user_id FROM seeker_verifications WHERE genesis_mint=? AND user_id!=?",
                &[json!(mint), json!(user_id)],
            )
            .await
            .map_err(|_| rpc_unavailable())?;
        if taken.is_some() {
            return Err(SeekerError::new(
                409,
                "seeker_already_claimed",
                "This Seeker is already verified on another account.",
            ));
        }
        let sql = format!(
            "INSERT INTO seeker_verifications(user_id,address,genesis_mint,member_number,skr_atomic,slot,verified_at,refreshed_at) \
             SELECT ?,?,?,?,?,?,?,? WHERE ?={CURRENT_ADDRESS} ON CONFLICT(user_id) DO UPDATE SET address=excluded.address,\
             genesis_mint=excluded.genesis_mint,member_number=excluded.member_number,skr_atomic=excluded.skr_atomic,\
             slot=excluded.slot,refreshed_at=excluded.refreshed_at,invalidated_at=NULL,revision=seeker_verifications.revision+1 \
             WHERE seeker_verifications.revision=?"
        );
        self.db
            .execute(
                &sql,
                &[
                    json!(user_id),
                    json!(address),
                    json!(mint),
                    json!(number),
                    json!(atomic.to_string()),
                    json!(slot),
                    json!(now),
                    json!(now),
                    json!(address),
                    json!(user_id),
                    json!(user_id),
                    json!(revision),
                ],
            )
            .await
            .map_err(|_| rpc_unavailable())?;
        let written = self
            .db
            .first(
                "SELECT address,genesis_mint,refreshed_at FROM seeker_verifications WHERE user_id=?",
                &[json!(user_id)],
            )
            .await
            .map_err(|_| rpc_unavailable())?;
        let stored = written.as_ref().is_some_and(|row| {
            text(row, "address") == Some(address.as_str())
                && text(row, "genesis_mint") == Some(mint.as_str())
                && int(row, "refreshed_at") == Some(now)
        });
        if !stored {
            return Err(changed());
        }
        let status = self.status(user_id).await?;
        if status.is_null() {
            return Err(changed());
        }
        Ok(status)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/seeker-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("seeker golden")).expect("json")
    }

    /// The reference's fixture builders, transcribed so the corpus is the same objects.
    fn token(mint: &str, amount: &str, decimals: i64) -> Value {
        json!({"account": {"data": {"parsed": {"info": {
            "mint": mint, "tokenAmount": {"amount": amount, "decimals": decimals, "uiAmountString": amount},
        }}}}})
    }

    fn mint_account(mint: &str, group: Option<&str>, number: Value) -> Value {
        let extensions = match group {
            None => Vec::new(),
            Some(group) => vec![json!({"extension": "tokenGroupMember",
                                       "state": {"group": group, "mint": mint, "memberNumber": number}})],
        };
        json!({"data": {"parsed": {"info": {"decimals": 0, "supply": "1", "extensions": extensions}}}})
    }

    fn mint_reply(info: Value) -> Value {
        json!({"value": [{"data": {"parsed": {"info": info}}}]})
    }

    fn token_accounts_fixture() -> Vec<Value> {
        vec![
            token("SGTmint", "1", 0),
            token("SGTother", "1", 0),
            token("SKR", "5000000", 6),
            token("zero", "0", 0),
            token("two", "2", 0),
        ]
    }

    fn mint_accounts_fixture() -> Vec<Value> {
        vec![
            mint_account("SGTmint", Some(SGT_GROUP), json!(3)),
            mint_account("SGTother", None, json!(3)),
            mint_account("third", Some(SGT_GROUP), json!(3)),
        ]
    }

    #[test]
    fn the_parsers_decide_what_the_reference_decides() {
        let document = golden();

        for entry in document["parsed"].as_array().expect("parsed") {
            assert_eq!(
                parsed_info(&entry["value"]),
                entry["parsed"],
                "parsed_info({})",
                entry["name"]
            );
        }

        for case in document["candidates"].as_array().expect("candidates") {
            let name = case["name"].as_str().unwrap();
            let produced = match name {
                "candidate:many" => candidate_mints(&token_accounts_fixture()),
                "candidate:empty" => candidate_mints(&[]),
                "candidate:no-unit" => candidate_mints(&[token("a", "0", 0), token("b", "7", 0)]),
                "candidate:decimals" => candidate_mints(&[token("a", "1", 6)]),
                "candidate:duplicate" => candidate_mints(&[token("SGTmint", "1", 0), token("SGTmint", "1", 0)]),
                "candidate:malformed" => candidate_mints(&[json!({"account": {}}), json!({"no": "account"})]),
                other => panic!("unhandled candidate case {other}"),
            };
            assert_eq!(json!(produced), case["result"], "{name}");
        }

        // The refusals are the point: a lenient parser would report a Seeker where the reference
        // reported nothing.
        for case in document["tokenAccounts"].as_array().expect("token accounts") {
            let name = case["name"].as_str().unwrap();
            let reply = match name {
                "token:ok" => json!({"value": token_accounts_fixture()}),
                "token:not-a-list" => json!({"value": "x"}),
                "token:no-value" => json!([token("SGTmint", "1", 0)]),
                "token:amount-not-a-string" => {
                    json!({"value": [{"account": {"data": {"parsed": {"info": {"mint": "m",
                        "tokenAmount": {"amount": 1, "decimals": 0}}}}}}]})
                }
                "token:decimals-not-an-int" => {
                    json!({"value": [{"account": {"data": {"parsed": {"info": {"mint": "m",
                        "tokenAmount": {"amount": "1", "decimals": true}}}}}}]})
                }
                "token:non-ascii-amount" => {
                    json!({"value": [{"account": {"data": {"parsed": {"info": {"mint": "m",
                        "tokenAmount": {"amount": "١", "decimals": 0}}}}}}]})
                }
                _ => json!({"value": [{"account": {"data": {"parsed": {"info": {"mint": "m"}}}}}]}),
            };
            match (token_accounts(&reply), case["error"].as_str()) {
                (Ok(accounts), None) => assert_eq!(json!(accounts.len()), case["result"], "{name}"),
                (Ok(_), Some(error)) => panic!("{name}: accepted where the reference refused with {error:?}"),
                (Err(produced), Some(error)) => assert_eq!(produced, error, "{name}"),
                (Err(produced), None) => panic!("{name}: refused with {produced:?} where the reference accepted"),
            }
        }

        for case in document["mintAccounts"].as_array().expect("mint accounts") {
            let name = case["name"].as_str().unwrap();
            if name == "mint:member-number-not-an-int" {
                // This case asks for the *member search*, not the parser: a member number that is
                // not an integer drops the number rather than the Seeker.
                let reply = mint_reply(json!({"decimals": 0, "supply": "1",
                    "extensions": [{"extension": "tokenGroupMember",
                                    "state": {"group": SGT_GROUP, "mint": "m", "memberNumber": "3"}}]}));
                let accounts = mint_accounts(&reply, 1).expect("the mint account parses");
                let produced = genesis_member(accounts, &["m".into()]);
                let (mint, number) = produced.expect("the group member is found");
                assert_eq!(json!([mint, number]), case["result"], "{name}");
                continue;
            }
            let (reply, count) = match name {
                "mint:ok" => (json!({"value": mint_accounts_fixture()}), 3),
                "mint:count" => (json!({"value": mint_accounts_fixture()}), 2),
                "mint:supply-not-decimal" => (mint_reply(json!({"decimals": 0, "supply": "x", "extensions": []})), 1),
                "mint:supply-not-a-string" => (mint_reply(json!({"decimals": 0, "supply": 1, "extensions": []})), 1),
                "mint:decimals-not-an-int" => (
                    mint_reply(json!({"decimals": true, "supply": "1", "extensions": []})),
                    1,
                ),
                "mint:extensions-not-a-list" => {
                    (mint_reply(json!({"decimals": 0, "supply": "1", "extensions": "x"})), 1)
                }
                "mint:extension-not-a-dict" => (
                    mint_reply(json!({"decimals": 0, "supply": "1", "extensions": ["x"]})),
                    1,
                ),
                "mint:extension-unnamed" => (mint_reply(json!({"decimals": 0, "supply": "1", "extensions": [{}]})), 1),
                "mint:member-state-incomplete" => (
                    mint_reply(json!({"decimals": 0, "supply": "1",
                    "extensions": [{"extension": "tokenGroupMember", "state": {"group": "g"}}]})),
                    1,
                ),
                other => panic!("unhandled mint case {other}"),
            };
            match (mint_accounts(&reply, count), case["error"].as_str()) {
                (Ok(accounts), None) => assert_eq!(json!(accounts.len()), case["result"], "{name}"),
                (Ok(_), Some(error)) => panic!("{name}: accepted where the reference refused with {error:?}"),
                (Err(produced), Some(error)) => assert_eq!(produced, error, "{name}"),
                (Err(produced), None) => panic!("{name}: refused with {produced:?} where the reference accepted"),
            }
        }

        for case in document["members"].as_array().expect("members") {
            let name = case["name"].as_str().unwrap();
            let produced = match name {
                "member:found" => genesis_member(
                    &[mint_account("SGTmint", Some(SGT_GROUP), json!(3))],
                    &["SGTmint".into()],
                ),
                "member:first" => genesis_member(
                    &mint_accounts_fixture(),
                    &["SGTother".into(), "SGTmint".into(), "third".into()],
                ),
                "member:wrong-group" => genesis_member(&[mint_account("m", Some("other"), json!(3))], &["m".into()]),
                "member:mint-mismatch" => {
                    genesis_member(&[mint_account("m", Some(SGT_GROUP), json!(3))], &["different".into()])
                }
                "member:none" => genesis_member(&[], &[]),
                "member:no-number" => genesis_member(&[mint_account("m", Some(SGT_GROUP), Value::Null)], &["m".into()]),
                _ => genesis_member(&[mint_account("m", None, json!(3))], &["m".into()]),
            };
            let expected = if case["result"].is_null() {
                Value::Null
            } else {
                json!([case["result"][0], case["result"][1]])
            };
            match (produced, case["result"].is_null()) {
                (None, true) => {}
                (Some((mint, number)), false) => {
                    assert_eq!(json!([mint, number]), expected, "{name}")
                }
                (produced, _) => panic!("{name}: produced {produced:?} against {expected}"),
            }
        }

        for case in document["atomic"].as_array().expect("atomic") {
            let name = case["name"].as_str().unwrap();
            let produced = match name {
                "skr:sum" => skr_atomic(&[token(SKR_MINT, "1500000", 6), token(SKR_MINT, "500000", 6)]),
                "skr:other-mint" => skr_atomic(&[token("other", "999", 6)]),
                "skr:malformed" => skr_atomic(&[json!({"account": {}}), token(SKR_MINT, "7", 6)]),
                _ => skr_atomic(&[]),
            };
            assert_eq!(json!(produced), case["result"], "{name}");
        }

        for entry in document["displays"].as_array().expect("displays") {
            assert_eq!(
                skr_display(entry["atomic"].as_i64().unwrap()),
                entry["display"].as_str().unwrap(),
                "skr_display({})",
                entry["atomic"]
            );
        }

        for entry in document["projections"].as_array().expect("projections") {
            let row: Option<Row> = entry["row"].as_object().cloned();
            assert_eq!(projection(row.as_ref()), entry["projection"], "{}", entry["name"]);
        }
    }

    #[test]
    fn a_wallet_is_only_ever_the_one_signed_in() {
        // The address fragment is the part that must not drift: a badge attached to a wallet the
        // user has since left is evidence about somebody else.
        assert!(CURRENT_ADDRESS.contains("wallet_identities"));
        assert!(CURRENT_ADDRESS.contains("converted_at IS NOT NULL"));
        assert!(CURRENT_ADDRESS.contains("wallet_links"));
        assert_eq!(SKR_DECIMALS, 6);
    }
}
