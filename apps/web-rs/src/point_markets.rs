//! The transactional point market: treasury, quotes, fills, settlement.
//!
//! `markets.rs` beside this file is the read side the live Worker already serves. This is the
//! write side: the funded, buy-only LMSR persistence, where points are actually committed.
//!
//! The arithmetic is not here — `forecast-domain::pricing` already reproduces the 80-digit LMSR,
//! and this module never computes a price of its own. What is here is the transaction around it,
//! and the shape of that transaction is the whole design:
//!
//!   - **Quotes never authorize money.** A quote is a row; a fill is a batch that re-reads the
//!     state, re-derives the receipt from it, and moves the balances in the same transaction. A
//!     quote that was priced under a revision nobody can return to is refused, not repriced.
//!   - **Every gate is repeated inside the writing batch.** The host checks containment, funding
//!     and source currency before calling; the batch checks them again, because the gap between
//!     the two is exactly where a fill would land in a market that had just closed.
//!   - **A lost acknowledgement is not a second fill.** Every entry point that can be retried
//!     looks for the receipt its own request key would have produced before it does anything,
//!     and returns it.
//!
//! The guards are `market_write_guards(id, passed)` with `CHECK(passed=1)`: a batch whose guard
//! evaluates false fails to insert, which is how a compare-and-set becomes a transaction.

use serde_json::{json, Value};

use forecast_domain::pricing::{
    accept_quote, close_market, initialize_market, market_probability_bp, quote_buy, AcceptLimits, PricingPolicy,
    PricingQuote, PricingReceipt, PricingState, ATOMIC_UNITS_PER_POINT,
};
use forecast_domain::{canonical_bytes, content_hash, python_json_bytes, Record};

use crate::db::{int, text, Database, Row};

/// `ATOMIC_UNITS_PER_POINT`.
pub const SCALE: i64 = ATOMIC_UNITS_PER_POINT;
pub const TREASURY_ISSUANCE_CAP: i64 = 20_000 * SCALE;
pub const GLOBAL_GROSS_CAP: i64 = 10_000 * SCALE;
pub const MARKET_GROSS_CAP: i64 = 2_000 * SCALE;
pub const MAX_BATCH: i64 = 25;

/// One statement as the batch executes it.
pub type Statement = (String, Vec<Value>);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl MarketError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

pub fn invalid() -> MarketError {
    MarketError::new(400, "market_invalid_request", "Check the market request and try again.")
}

pub fn conflict() -> MarketError {
    MarketError::new(
        409,
        "market_conflict",
        "The market or your balance changed. Request a fresh quote.",
    )
}

fn unavailable(message: &'static str) -> MarketError {
    MarketError::new(404, "market_unavailable", message)
}

fn insufficient() -> MarketError {
    MarketError::new(
        409,
        "market_balance_insufficient",
        "There are not enough available points.",
    )
}

/// `_identifier`.
pub fn identifier(value: &str) -> Result<&str, MarketError> {
    if value.is_empty() || value.chars().count() > 128 || value.chars().any(|c| (c as u32) < 32) {
        return Err(invalid());
    }
    Ok(value)
}

/// `_hash`: a plain digest of the canonical form, with no domain prefix.
///
/// These hashes key rows — a funding identity, a fill identity — rather than committing to a
/// domain record, so they must not carry the commitment prefix a record hash does.
///
/// The reference hashes with `json.dumps`'s *default* `ensure_ascii`, so this is
/// `python_json_bytes` and not `canonical_bytes`. The keys are arbitrary user text, and the two
/// disagree on any non-ASCII character: a request key written `café` would name one row here and
/// a different one in the reference, which is one request spending twice.
pub fn hash_of(value: &Value) -> String {
    let bytes = python_json_bytes(value).unwrap_or_default();
    let text = String::from_utf8_lossy(&bytes).to_string();
    crate::source_watch::hash_hex(&text)
}

fn canonical(value: &Value) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

/// `_guard`: a statement that fails its table's `CHECK(passed=1)` when the condition is false.
pub fn guard(identifier: &str, condition: &str, params: Vec<Value>) -> Statement {
    let mut bound = vec![json!(identifier)];
    bound.extend(params);
    (
        format!("INSERT INTO market_write_guards(id,passed) SELECT ?, ( CASE WHEN {condition} THEN 1 ELSE 0 END )"),
        bound,
    )
}

/// `PointMarkets`. Authentication and admin authorization belong to the routing adapter.
pub struct PointMarkets<'a> {
    pub db: &'a dyn Database,
    pub clock: &'a dyn Fn() -> i64,
    pub token: &'a dyn Fn() -> String,
    pub live_enabled: bool,
}

impl PointMarkets<'_> {
    fn now(&self) -> i64 {
        (self.clock)()
    }

    fn token(&self) -> String {
        (self.token)()
    }

    /// `_mode`.
    pub fn mode(&self, mode: &str) -> Result<(), MarketError> {
        if mode != "shadow" && mode != "active" {
            return Err(invalid());
        }
        if mode == "active" && !self.live_enabled {
            return Err(MarketError::new(
                403,
                "market_unavailable",
                "Live point markets are not enabled.",
            ));
        }
        Ok(())
    }

    /// `_state`: the row's state, but only if the row still describes it.
    ///
    /// Every one of these comparisons is the point: a state whose hash, revision,
    /// specification, identity or policy no longer matches its row is not a stale copy of the
    /// market, it is a different market sharing a primary key.
    pub fn state_of(row: &Row) -> Result<PricingState, MarketError> {
        let body = text(row, "state").ok_or_else(conflict)?;
        let state: PricingState = serde_json::from_str(body).map_err(|_| conflict())?;
        state.validate().map_err(|_| conflict())?;
        let matches = content_hash(&state).ok().as_deref() == text(row, "state_hash")
            && state.revision == int(row, "revision").unwrap_or(-1)
            && Some(state.specification_hash.as_str()) == text(row, "specification_hash")
            && Some(state.market_id.as_str()) == text(row, "forecast_id")
            && content_hash(&state.policy).ok().as_deref() == text(row, "policy_hash")
            && Some(canonical(&serde_json::to_value(&state.policy).unwrap_or(Value::Null)).as_str())
                == text(row, "policy");
        if !matches {
            return Err(conflict());
        }
        Ok(state)
    }

    /// `_row`: the market for a forecast, or the 404 the caller is told about.
    pub async fn row(&self, forecast_id: &str) -> Result<Row, MarketError> {
        let forecast_id = identifier(forecast_id)?;
        self.db
            .first("SELECT * FROM point_markets WHERE forecast_id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| conflict())?
            .ok_or_else(|| unavailable("This forecast does not have a funded market."))
    }

    /// `budget`: what the treasury has issued, holds in reserve, and may still issue.
    pub async fn budget(&self, mode: &str) -> Result<Value, MarketError> {
        if mode != "active" && mode != "shadow" {
            return Err(invalid());
        }
        let row = self
            .db
            .first("SELECT * FROM market_treasuries WHERE mode=?", &[json!(mode)])
            .await
            .map_err(|_| conflict())?
            .ok_or_else(conflict)?;
        let reserved = self
            .db
            .first(
                "SELECT COALESCE(SUM(reserve_atomic),0) amount FROM point_markets WHERE mode=?",
                &[json!(mode)],
            )
            .await
            .map_err(|_| conflict())?;
        Ok(json!({
            "mode": mode,
            "issuedAtomic": int(&row, "issued_atomic").unwrap_or(0).to_string(),
            "availableAtomic": int(&row, "available_atomic").unwrap_or(0).to_string(),
            "reservedAtomic": reserved.as_ref().and_then(|row| int(row, "amount")).unwrap_or(0).to_string(),
            "issuanceCapAtomic": TREASURY_ISSUANCE_CAP.to_string(),
            "liveEnabled": self.live_enabled,
        }))
    }
}

impl PointMarkets<'_> {
    /// `fund_treasury`: an explicit administrative grant, never automatic and never purchasable.
    pub async fn fund_treasury(&self, amount: i64, request_key: &str, mode: &str) -> Result<Value, MarketError> {
        self.mode(mode)?;
        identifier(request_key)?;
        if !(1..=20_000).contains(&amount) {
            return Err(invalid());
        }
        // The funding identity is the request, not the amount: the same key twice is the same
        // grant, and a key that has already been used with a different amount is a conflict.
        let identity = format!("fund:{}", hash_of(&json!([mode, request_key])));
        if let Some(old) = self
            .db
            .first("SELECT * FROM market_funding WHERE id=?", &[json!(identity)])
            .await
            .map_err(|_| conflict())?
        {
            let matches = int(&old, "amount_atomic") == Some(amount * SCALE) && text(&old, "mode") == Some(mode);
            if !matches {
                return Err(conflict());
            }
            return self.budget(mode).await;
        }
        let guard_token = self.token();
        let issued = self
            .db
            .batch(&[
                guard(
                    &guard_token,
                    "EXISTS(SELECT 1 FROM market_treasuries WHERE mode=? AND issued_atomic+?<=?)",
                    vec![json!(mode), json!(amount * SCALE), json!(TREASURY_ISSUANCE_CAP)],
                ),
                (
                    "INSERT INTO market_funding(id,mode,amount_atomic,created_at) VALUES(?,?,?,?)".to_string(),
                    vec![json!(identity), json!(mode), json!(amount * SCALE), json!(self.now())],
                ),
                (
                    "UPDATE market_treasuries SET issued_atomic=issued_atomic+?,available_atomic=available_atomic+?,revision=revision+1 WHERE mode=?"
                        .to_string(),
                    vec![json!(amount * SCALE), json!(amount * SCALE), json!(mode)],
                ),
                (
                    "DELETE FROM market_write_guards WHERE id=?".to_string(),
                    vec![json!(guard_token)],
                ),
            ])
            .await;
        if issued.is_err() {
            // A lost acknowledgement is not a second grant.
            if let Some(old) = self
                .db
                .first("SELECT * FROM market_funding WHERE id=?", &[json!(identity)])
                .await
                .map_err(|_| conflict())?
            {
                if int(&old, "amount_atomic") == Some(amount * SCALE) && text(&old, "mode") == Some(mode) {
                    return self.budget(mode).await;
                }
            }
            return Err(conflict());
        }
        self.budget(mode).await
    }

    /// `create`: open a market on a forecast, reserving its subsidy from the treasury.
    pub async fn create(
        &self,
        forecast_id: &str,
        policy: Option<PricingPolicy>,
        mode: &str,
        expected_specification_hash: &str,
    ) -> Result<Value, MarketError> {
        self.mode(mode)?;
        identifier(forecast_id)?;
        let policy = policy.unwrap_or_default();
        let well_formed_hash = expected_specification_hash.chars().count() == 64
            && expected_specification_hash
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
        if !well_formed_hash {
            return Err(invalid());
        }
        policy.validate().map_err(|_| invalid())?;
        // The pilot is deliberately small, and a policy is a published contract: a market asking
        // for limits beyond it is a different product, not a bigger market.
        if policy.maximum_fill_atomic > 100 * SCALE
            || policy.maximum_owner_gross_atomic > 300 * SCALE
            || policy.maximum_owner_unsettled_atomic > 1_000 * SCALE
        {
            return Err(invalid());
        }
        let policy_value = serde_json::to_value(&policy).unwrap_or(Value::Null);
        let state =
            initialize_market(policy.clone(), forecast_id, expected_specification_hash).map_err(|_| invalid())?;
        if let Some(previous) = self
            .db
            .first("SELECT * FROM point_markets WHERE forecast_id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| conflict())?
        {
            let matches = text(&previous, "policy_hash") == content_hash(&policy).ok().as_deref()
                && text(&previous, "mode") == Some(mode)
                && text(&previous, "specification_hash") == Some(expected_specification_hash);
            if !matches {
                return Err(conflict());
            }
            return self.view(&previous).await;
        }
        let now = self.now();
        let guard_token = self.token();
        let day = now - now.rem_euclid(86_400_000);
        let created = self
            .db
            .batch(&[
                guard(
                    &guard_token,
                    "EXISTS(SELECT 1 FROM forecasts WHERE id=? AND specification_hash=? AND state='OPEN' AND open_at<=? AND close_at>?) \
                     AND (?='shadow' OR NOT EXISTS(SELECT 1 FROM point_positions WHERE forecast_id=? AND amount>0)) \
                     AND EXISTS(SELECT 1 FROM market_treasuries WHERE mode=? AND available_atomic>=?) \
                     AND (SELECT COUNT(*) FROM point_markets WHERE mode=? AND status!='settled')<20 \
                     AND (SELECT COUNT(*) FROM point_markets WHERE mode=? AND created_at>=?)<5",
                    vec![
                        json!(forecast_id),
                        json!(expected_specification_hash),
                        json!(now),
                        json!(now),
                        json!(mode),
                        json!(forecast_id),
                        json!(mode),
                        json!(policy.subsidy_atomic),
                        json!(mode),
                        json!(mode),
                        json!(day),
                    ],
                ),
                (
                    "INSERT INTO point_markets(forecast_id,mode,specification_hash,policy_hash,policy,state,state_hash,revision,reserve_atomic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)".to_string(),
                    vec![
                        json!(forecast_id),
                        json!(mode),
                        json!(expected_specification_hash),
                        json!(content_hash(&policy).unwrap_or_default()),
                        json!(canonical(&policy_value)),
                        json!(canonical(&serde_json::to_value(&state).unwrap_or(Value::Null))),
                        json!(content_hash(&state).unwrap_or_default()),
                        json!(0),
                        json!(policy.subsidy_atomic),
                        json!(now),
                    ],
                ),
                (
                    "UPDATE market_treasuries SET available_atomic=available_atomic-?,revision=revision+1 WHERE mode=?".to_string(),
                    vec![json!(policy.subsidy_atomic), json!(mode)],
                ),
                (
                    "DELETE FROM market_write_guards WHERE id=?".to_string(),
                    vec![json!(guard_token)],
                ),
            ])
            .await;
        if created.is_err() {
            if let Some(old) = self
                .db
                .first("SELECT * FROM point_markets WHERE forecast_id=?", &[json!(forecast_id)])
                .await
                .map_err(|_| conflict())?
            {
                let matches = text(&old, "policy_hash") == content_hash(&policy).ok().as_deref()
                    && text(&old, "mode") == Some(mode)
                    && text(&old, "specification_hash") == Some(expected_specification_hash);
                if matches {
                    return self.view(&old).await;
                }
            }
            return Err(conflict());
        }
        self.view(&self.row(forecast_id).await?).await
    }
}

impl PointMarkets<'_> {
    /// `_open_guard`: the containment gates repeated *inside* the writing transaction.
    ///
    /// The host checks all of this before it calls. Repeating it here is not redundancy: between
    /// the check and the batch a market can close, a hold can be taken, a source can go stale, and
    /// a fill that lands in that gap is a fill in a market that no longer exists.
    pub fn open_guard(guard_token: &str, row: &Row, now: i64) -> Statement {
        guard(
            guard_token,
            "EXISTS(SELECT 1 FROM point_markets m JOIN forecasts f ON f.id=m.forecast_id WHERE m.forecast_id=? \
             AND m.status='open' AND m.revision=? AND m.state_hash=? AND m.policy_hash=? AND f.specification_hash=m.specification_hash \
             AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?) \
             AND NOT EXISTS(SELECT 1 FROM active_participation_holds WHERE forecast_id=?) \
             AND NOT EXISTS(SELECT 1 FROM official_source_reviews WHERE forecast_id=? AND specification_hash=? AND state!='complete') \
             AND (?='shadow' OR (EXISTS(SELECT 1 FROM official_watch_bindings WHERE forecast_id=?) \
             AND NOT EXISTS(SELECT 1 FROM official_watch_bindings b JOIN official_watch_sources s ON (s.id=b.source_id OR (s.parent_id=b.source_id AND s.enabled=1)) \
             WHERE b.forecast_id=? AND (s.enabled!=1 OR s.failure_count>0 OR s.checked_at IS NULL \
             OR s.lease_until>? OR s.checked_at<?-s.interval_ms-60000))) \
             OR EXISTS(SELECT 1 FROM risk_feed_bindings_v2 b JOIN risk_prediction_clocks_v2 c ON c.forecast_id=b.forecast_id \
             WHERE b.forecast_id=? AND c.recorded_at>?-7200000 \
             AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id)))",
            vec![
                json!(text(row, "forecast_id")),
                json!(int(row, "revision")),
                json!(text(row, "state_hash")),
                json!(text(row, "policy_hash")),
                json!(now),
                json!(now),
                json!(text(row, "forecast_id")),
                json!(text(row, "forecast_id")),
                json!(text(row, "specification_hash")),
                json!(text(row, "mode")),
                json!(text(row, "forecast_id")),
                json!(text(row, "forecast_id")),
                json!(now),
                json!(now),
                json!(text(row, "forecast_id")),
                json!(now),
            ],
        )
    }

    /// `_view`: the market as a reader sees it, including whether its probability is current.
    pub async fn view(&self, row: &Row) -> Result<Value, MarketError> {
        let state = Self::state_of(row)?;
        let decision = self
            .db
            .first(
                "SELECT * FROM forecast_eligibility_decisions WHERE forecast_id=?",
                &[json!(text(row, "forecast_id"))],
            )
            .await
            .map_err(|_| conflict())?;
        let mut probability = market_probability_bp(&state).ok();
        let mut probability_revision = Some(state.revision);
        let mut probability_status = "current";
        // A market under eligibility review reports no probability at all: a frozen number that
        // still looks live is worse than no number.
        if let Some(decision) = decision.as_ref() {
            probability_status = "frozen_before_evidence";
            match self.eligible_prefix(row, decision).await? {
                None => {
                    probability = None;
                    probability_revision = None;
                    probability_status = "eligibility_review";
                }
                Some(prefix) => {
                    probability = market_probability_bp(&prefix).ok();
                    probability_revision = Some(prefix.revision);
                }
            }
        }
        let voids = self
            .db
            .first(
                "SELECT COUNT(*) n,COALESCE(SUM(spend),0) amount FROM market_fill_voids WHERE forecast_id=?",
                &[json!(text(row, "forecast_id"))],
            )
            .await
            .map_err(|_| conflict())?;
        Ok(json!({
            "forecastId": text(row, "forecast_id"), "mode": text(row, "mode"), "status": text(row, "status"),
            "yesProbabilityBps": probability, "probabilityStatus": probability_status,
            "probabilityRevision": probability_revision, "revision": int(row, "revision"),
            "voidedFillCount": voids.as_ref().and_then(|row| int(row, "n")).unwrap_or(0),
            "refundedPoints": voids.as_ref().and_then(|row| int(row, "amount")).unwrap_or(0),
            "atomicScale": SCALE, "maxSpendPoints": 100.min(state.policy.maximum_fill_atomic / SCALE),
            "policyHash": text(row, "policy_hash"), "specificationHash": text(row, "specification_hash"),
            "liveEnabled": self.live_enabled,
            "reserveAtomic": int(row, "reserve_atomic").unwrap_or(0).to_string(),
        }))
    }

    /// `_eligible_prefix`: the unchanged receipt at the cutoff, never a repricing of earlier fills.
    pub async fn eligible_prefix(&self, row: &Row, decision: &Row) -> Result<Option<PricingState>, MarketError> {
        if text(decision, "event_time_basis") != Some("published_instant")
            || text(decision, "specification_hash") != text(row, "specification_hash")
        {
            return Ok(None);
        }
        let forecast_id = text(row, "forecast_id").unwrap_or_default().to_string();
        let cutoff = int(decision, "cutoff_at").unwrap_or(0);
        // A fill recorded out of revision order around the cutoff means the receipt at the cutoff
        // cannot be reconstructed, so the review has to be done by a person.
        let inversion = self
            .db
            .first(
                "SELECT 1 FROM market_fills late JOIN market_fills early \
                 ON early.forecast_id=late.forecast_id AND early.revision>late.revision \
                 WHERE late.forecast_id=? AND late.created_at>=? AND early.created_at<?",
                &[json!(forecast_id), json!(cutoff), json!(cutoff)],
            )
            .await
            .map_err(|_| conflict())?;
        if inversion.is_some() {
            return Ok(None);
        }
        let before = self
            .db
            .first(
                "SELECT * FROM market_fills WHERE forecast_id=? AND created_at<? ORDER BY revision DESC LIMIT 1",
                &[json!(forecast_id), json!(cutoff)],
            )
            .await
            .map_err(|_| conflict())?;
        let Some(before) = before else {
            let state = Self::state_of(row)?;
            let start = initialize_market(
                state.policy,
                &forecast_id,
                text(row, "specification_hash").unwrap_or_default(),
            )
            .ok();
            return Ok(start);
        };
        let Ok(receipt) = serde_json::from_str::<PricingReceipt>(text(&before, "body").unwrap_or_default()) else {
            return Ok(None);
        };
        let state = receipt.state;
        let count = self
            .db
            .first(
                "SELECT COUNT(*) n FROM market_fills WHERE forecast_id=? AND revision<=?",
                &[json!(forecast_id), json!(int(&before, "revision"))],
            )
            .await
            .map_err(|_| conflict())?;
        let consistent = state.market_id == forecast_id
            && Some(state.specification_hash.as_str()) == text(row, "specification_hash")
            && content_hash(&state.policy).ok().as_deref() == text(row, "policy_hash")
            && state.revision == int(&before, "revision").unwrap_or(-1)
            && count.as_ref().and_then(|row| int(row, "n")) == Some(state.revision);
        Ok(if consistent { Some(state) } else { None })
    }

    /// `get`: the market for a forecast, or nothing if there is not one.
    pub async fn get(&self, forecast_id: &str) -> Result<Option<Value>, MarketError> {
        let forecast_id = identifier(forecast_id)?;
        let row = self
            .db
            .first("SELECT * FROM point_markets WHERE forecast_id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| conflict())?;
        match row {
            Some(row) => Ok(Some(self.view(&row).await?)),
            None => Ok(None),
        }
    }

    /// `_quote_view`.
    pub fn quote_view(quote: &PricingQuote, quote_id: Option<&str>, mode: &str) -> Value {
        json!({
            "quoteId": quote_id, "forecastId": quote.market_id, "userId": quote.owner_id, "mode": mode,
            "side": quote.side, "spendPoints": quote.spend_atomic / SCALE,
            "claimsAtomic": quote.claims_atomic.to_string(), "priceBeforeBps": quote.before_probability_bp,
            "priceAfterBps": quote.after_probability_bp, "expiresAt": quote.expires_at_ms,
            "revision": quote.state_revision, "policyHash": quote.policy_hash,
            "specificationHash": quote.specification_hash,
        })
    }

    /// `_buy_input`.
    pub fn buy_input(side: &str, spend: i64) -> Result<(), MarketError> {
        if side != "YES" && side != "NO" {
            return Err(invalid());
        }
        if !(1..=100).contains(&spend) {
            return Err(invalid());
        }
        Ok(())
    }

    /// `preview`: what a buy would cost against the market as it stands. It binds nothing.
    pub async fn preview(&self, forecast_id: &str, side: &str, spend: i64) -> Result<Value, MarketError> {
        Self::buy_input(side, spend)?;
        let forecast_id = identifier(forecast_id)?;
        let forecast = self
            .db
            .first(
                "SELECT specification_hash FROM forecasts WHERE id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?
            .ok_or_else(|| unavailable("Forecast not found."))?;
        let row = self
            .db
            .first("SELECT * FROM point_markets WHERE forecast_id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| conflict())?;
        let state = match row.as_ref() {
            Some(row) => Self::state_of(row)?,
            None => initialize_market(
                PricingPolicy::default(),
                forecast_id,
                text(&forecast, "specification_hash").unwrap_or_default(),
            )
            .map_err(|_| invalid())?,
        };
        let quote = quote_buy(&state, "preview", side, spend * SCALE, self.now()).map_err(|_| conflict())?;
        let mut view = Self::quote_view(&quote, None, "preview");
        view["nonbinding"] = json!(true);
        view["atomicScale"] = json!(SCALE);
        Ok(view)
    }

    /// `_account`.
    pub async fn account(&self, user_id: &str, mode: &str) -> Result<Row, MarketError> {
        identifier(user_id)?;
        if mode == "shadow" {
            self.db
                .execute(
                    "INSERT OR IGNORE INTO market_shadow_accounts(user_id,updated_at) SELECT id,? FROM users WHERE id=?",
                    &[json!(self.now()), json!(user_id)],
                )
                .await
                .map_err(|_| conflict())?;
        }
        let table = if mode == "shadow" {
            "market_shadow_accounts"
        } else {
            "point_accounts"
        };
        self.db
            .first(
                &format!(
                    "SELECT a.*,COALESCE(f.remainder_atomic,0) fraction FROM {table} a \
                     LEFT JOIN point_fractions f ON f.user_id=a.user_id AND f.mode=? WHERE a.user_id=?"
                ),
                &[json!(mode), json!(user_id)],
            )
            .await
            .map_err(|_| conflict())?
            .ok_or_else(|| MarketError::new(401, "market_unavailable", "Sign in to use a market."))
    }

    /// `_totals`: the owner's exposure, unsettled and gross, in atomic units.
    pub async fn totals(&self, user_id: &str, forecast_id: &str, mode: &str) -> Result<(i64, i64), MarketError> {
        let row = self
            .db
            .first(
                "SELECT COALESCE(SUM(p.gross),0) unsettled,COALESCE(SUM( CASE WHEN p.forecast_id=? THEN p.gross ELSE 0 END ),0) gross \
                 FROM market_positions p JOIN point_markets m ON m.forecast_id=p.forecast_id \
                 WHERE p.user_id=? AND m.mode=? AND p.settled=0",
                &[json!(forecast_id), json!(user_id), json!(mode)],
            )
            .await
            .map_err(|_| conflict())?;
        match row {
            Some(row) => Ok((
                int(&row, "gross").unwrap_or(0) * SCALE,
                int(&row, "unsettled").unwrap_or(0) * SCALE,
            )),
            None => Ok((0, 0)),
        }
    }

    /// `quote`: price a buy now and store it. The stored row is what a later fill re-derives from.
    pub async fn quote(&self, user_id: &str, forecast_id: &str, side: &str, spend: i64) -> Result<Value, MarketError> {
        Self::buy_input(side, spend)?;
        let row = self.row(forecast_id).await?;
        let mode = text(&row, "mode").unwrap_or_default().to_string();
        self.mode(&mode)?;
        let account = self.account(user_id, &mode).await?;
        if int(&account, "available").unwrap_or(0) < spend {
            return Err(insufficient());
        }
        let state = Self::state_of(&row)?;
        let now = self.now();
        let (gross, unsettled) = self.totals(user_id, forecast_id, &mode).await?;
        let quote = quote_buy(&state, user_id, side, spend * SCALE, now).map_err(|_| conflict())?;
        // A quote that would breach an owner limit is refused before it is stored, so it can
        // never be filled: storing it and refusing the fill would leave a price on the record.
        accept_quote(
            &state,
            &quote,
            user_id,
            now,
            &AcceptLimits {
                minimum_claims_atomic: quote.claims_atomic,
                owner_gross_atomic: gross,
                owner_unsettled_atomic: unsettled,
            },
        )
        .map_err(|_| conflict())?;
        let identity = self.token();
        let guard_token = self.token();
        let stored = self
            .db
            .batch(&[
                Self::open_guard(&guard_token, &row, now),
                (
                    "INSERT INTO market_quotes(id,user_id,forecast_id,body,quote_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?)"
                        .to_string(),
                    vec![
                        json!(identity),
                        json!(user_id),
                        json!(forecast_id),
                        json!(canonical(&serde_json::to_value(&quote).unwrap_or(Value::Null))),
                        json!(content_hash(&quote).unwrap_or_default()),
                        json!(now),
                        json!(quote.expires_at_ms),
                    ],
                ),
                (
                    "DELETE FROM market_write_guards WHERE id=?".to_string(),
                    vec![json!(guard_token)],
                ),
            ])
            .await;
        if stored.is_err() {
            return Err(conflict());
        }
        Ok(Self::quote_view(&quote, Some(&identity), &mode))
    }
}

impl PointMarkets<'_> {
    /// `_receipt_view`: a fill as the buyer sees it, including whether it was later voided.
    pub async fn receipt_view(&self, row: &Row, mode: &str) -> Result<Value, MarketError> {
        let receipt: PricingReceipt =
            serde_json::from_str(text(row, "body").unwrap_or_default()).map_err(|_| conflict())?;
        let void = self
            .db
            .first(
                "SELECT spend,created_at,decision_id FROM market_fill_voids WHERE fill_id=?",
                &[json!(text(row, "id"))],
            )
            .await
            .map_err(|_| conflict())?;
        let mut view = Self::quote_view(&receipt.fill.quote, text(row, "quote_id"), mode);
        view["id"] = json!(text(row, "id"));
        view["status"] = json!(if void.is_some() { "void" } else { "accepted" });
        view["acceptedAt"] = json!(receipt.fill.accepted_at_ms);
        view["refundedPoints"] = json!(void.as_ref().and_then(|row| int(row, "spend")).unwrap_or(0));
        view["voidedAt"] = json!(void.as_ref().and_then(|row| int(row, "created_at")));
        view["eligibilityDecisionId"] = json!(void.as_ref().and_then(|row| text(row, "decision_id")));
        Ok(view)
    }

    /// `receipt_status`: reconcile an uncertain response without submitting or retrying a buy.
    ///
    /// Absence is definitive only when the same read sees a persisted cutoff — the insert trigger
    /// on the decision table prevents an in-flight buy from landing later. An open market with no
    /// receipt visible yet can only be reported as pending, never as failed.
    pub async fn receipt_status(
        &self,
        user_id: &str,
        forecast_id: &str,
        quote_id: &str,
        idempotency_key: &str,
    ) -> Result<Value, MarketError> {
        for value in [user_id, forecast_id, quote_id, idempotency_key] {
            identifier(value)?;
        }
        let row = self
            .db
            .first(
                "SELECT f.*,q.id owned_quote_id,q.forecast_id owned_forecast_id,m.mode,qf.id quoted_fill_id,d.id cutoff_decision_id \
                 FROM market_quotes q JOIN point_markets m ON m.forecast_id=q.forecast_id \
                 LEFT JOIN market_fills f ON f.user_id=q.user_id AND f.idempotency_key=? \
                 LEFT JOIN market_fills qf ON qf.quote_id=q.id \
                 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=q.forecast_id \
                 WHERE q.id=? AND q.user_id=? AND q.forecast_id=?",
                &[json!(idempotency_key), json!(quote_id), json!(user_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?
            .ok_or_else(|| unavailable("Market receipt not found."))?;
        if text(&row, "id").is_some() {
            let agrees = text(&row, "quote_id") == Some(quote_id) && text(&row, "forecast_id") == Some(forecast_id);
            if !agrees {
                return Err(conflict());
            }
            let mode = text(&row, "mode").unwrap_or_default().to_string();
            let receipt = self.receipt_view(&row, &mode).await?;
            return Ok(json!({
                "forecastId": forecast_id, "userId": user_id, "quoteId": quote_id,
                "status": receipt["status"], "receipt": receipt,
            }));
        }
        // A fill exists for this quote under a different request key: the caller is asking about
        // a buy that was already made, and the answer is not "no".
        if text(&row, "quoted_fill_id").is_some() {
            return Err(conflict());
        }
        let settled = text(&row, "cutoff_decision_id").is_some();
        Ok(json!({
            "forecastId": forecast_id, "userId": user_id, "quoteId": quote_id,
            "status": if settled { "not_accepted" } else { "pending" }, "receipt": Value::Null,
        }))
    }

    /// `_ledger`: the account movement a market write made, as a row.
    #[allow(clippy::too_many_arguments)]
    fn ledger(
        identity: &str,
        uid: &str,
        row: &Row,
        kind: &str,
        available: i64,
        committed: i64,
        account: &Row,
        fraction: i64,
        now: i64,
    ) -> Statement {
        (
            "INSERT INTO market_account_ledger(id,user_id,mode,forecast_id,kind,available_delta,committed_delta,\
             available_after,committed_after,fraction_before,fraction_after,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                .to_string(),
            vec![
                json!(identity),
                json!(uid),
                json!(text(row, "mode")),
                json!(text(row, "forecast_id")),
                json!(kind),
                json!(available),
                json!(committed),
                json!(int(account, "available").unwrap_or(0) + available),
                json!(int(account, "committed").unwrap_or(0) + committed),
                json!(int(account, "fraction")),
                json!(fraction),
                json!(now),
            ],
        )
    }

    /// `accept`: turn a quote into a fill.
    ///
    /// This is the only place a buy is charged for. It re-reads the quote, re-derives the receipt
    /// from the market's own state, and moves the balances in one batch — the price the buyer was
    /// quoted is only honoured if the state it was quoted against is still the state.
    pub async fn accept(
        &self,
        user_id: &str,
        forecast_id: &str,
        quote_id: &str,
        min_claims_atomic: i64,
        idempotency_key: &str,
    ) -> Result<Value, MarketError> {
        for value in [user_id, forecast_id, quote_id, idempotency_key] {
            identifier(value)?;
        }
        if min_claims_atomic < 0 {
            return Err(invalid());
        }
        let request_hash = hash_of(&json!([user_id, forecast_id, quote_id, min_claims_atomic]));
        // Accepted receipts stay retrievable after expiry, closure or a kill switch, so this
        // lookup comes before anything that could refuse the market.
        if let Some(old) = self
            .db
            .first(
                "SELECT f.*,m.mode FROM market_fills f JOIN point_markets m ON m.forecast_id=f.forecast_id \
                 WHERE f.user_id=? AND f.idempotency_key=?",
                &[json!(user_id), json!(idempotency_key)],
            )
            .await
            .map_err(|_| conflict())?
        {
            if text(&old, "request_hash") != Some(request_hash.as_str()) {
                return Err(conflict());
            }
            let mode = text(&old, "mode").unwrap_or_default().to_string();
            return self.receipt_view(&old, &mode).await;
        }
        let row = self.row(forecast_id).await?;
        let mode = text(&row, "mode").unwrap_or_default().to_string();
        self.mode(&mode)?;
        let issued = self
            .db
            .first(
                "SELECT * FROM market_quotes WHERE id=? AND user_id=? AND forecast_id=?",
                &[json!(quote_id), json!(user_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?
            .ok_or_else(conflict)?;
        let quote: PricingQuote =
            serde_json::from_str(text(&issued, "body").unwrap_or_default()).map_err(|_| conflict())?;
        let intact = content_hash(&quote).ok().as_deref() == text(&issued, "quote_hash")
            && quote.owner_id == user_id
            && quote.market_id == forecast_id;
        if !intact {
            return Err(conflict());
        }
        let account = self.account(user_id, &mode).await?;
        let (gross, unsettled) = self.totals(user_id, forecast_id, &mode).await?;
        let state = Self::state_of(&row)?;
        let now = self.now();
        let receipt = accept_quote(
            &state,
            &quote,
            user_id,
            now,
            &AcceptLimits {
                minimum_claims_atomic: min_claims_atomic,
                owner_gross_atomic: gross,
                owner_unsettled_atomic: unsettled,
            },
        )
        .map_err(|_| conflict())?;
        let spend = quote.spend_atomic / SCALE;
        if quote.spend_atomic % SCALE != 0 || int(&account, "available").unwrap_or(0) < spend {
            return Err(insufficient());
        }
        let fill_id = format!("fill:{}", hash_of(&json!([user_id, idempotency_key])));
        let guard_token = self.token();
        let caps_token = self.token();
        let policy = &state.policy;
        let fraction = int(&account, "fraction").unwrap_or(0);
        let filled = self
            .db
            .batch(&[
                Self::open_guard(&guard_token, &row, now),
                guard(
                    &caps_token,
                    "COALESCE((SELECT gross FROM market_positions WHERE user_id=? AND forecast_id=?),0)*?+?<=? \
                     AND COALESCE((SELECT SUM(p.gross)*? FROM market_positions p JOIN point_markets m ON m.forecast_id=p.forecast_id \
                     WHERE p.user_id=? AND m.mode=? AND p.settled=0),0)+?<=? \
                     AND COALESCE((SELECT SUM(p.gross)*? FROM market_positions p JOIN point_markets m ON m.forecast_id=p.forecast_id \
                     WHERE m.mode=? AND p.settled=0),0)+?<=? AND (SELECT gross_atomic FROM point_markets WHERE forecast_id=?)+?<=?",
                    vec![
                        json!(user_id),
                        json!(forecast_id),
                        json!(SCALE),
                        json!(quote.spend_atomic),
                        json!(policy.maximum_owner_gross_atomic),
                        json!(SCALE),
                        json!(user_id),
                        json!(mode),
                        json!(quote.spend_atomic),
                        json!(policy.maximum_owner_unsettled_atomic),
                        json!(SCALE),
                        json!(mode),
                        json!(quote.spend_atomic),
                        json!(GLOBAL_GROSS_CAP),
                        json!(forecast_id),
                        json!(quote.spend_atomic),
                        json!(MARKET_GROSS_CAP),
                    ],
                ),
                (
                    "INSERT INTO market_fills(id,quote_id,user_id,forecast_id,idempotency_key,request_hash,body,revision,side,spend,claims_atomic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                        .to_string(),
                    vec![
                        json!(fill_id),
                        json!(quote_id),
                        json!(user_id),
                        json!(forecast_id),
                        json!(idempotency_key),
                        json!(request_hash),
                        json!(canonical(&serde_json::to_value(&receipt).unwrap_or(Value::Null))),
                        json!(receipt.state.revision),
                        json!(quote.side),
                        json!(spend),
                        json!(quote.claims_atomic),
                        json!(now),
                    ],
                ),
                Self::ledger(&fill_id, user_id, &row, "market_buy", -spend, spend, &account, fraction, now),
                (
                    "INSERT INTO market_positions(user_id,forecast_id,gross,yes_claims_atomic,no_claims_atomic) VALUES(?,?,?,?,?) \
                     ON CONFLICT(user_id,forecast_id) DO UPDATE SET gross=gross+excluded.gross,yes_claims_atomic=yes_claims_atomic+excluded.yes_claims_atomic,\
                     no_claims_atomic=no_claims_atomic+excluded.no_claims_atomic"
                        .to_string(),
                    vec![
                        json!(user_id),
                        json!(forecast_id),
                        json!(spend),
                        json!(if quote.side == "YES" { quote.claims_atomic } else { 0 }),
                        json!(if quote.side == "NO" { quote.claims_atomic } else { 0 }),
                    ],
                ),
                (
                    "UPDATE point_markets SET state=?,state_hash=?,revision=?,reserve_atomic=reserve_atomic+?,gross_atomic=gross_atomic+? WHERE forecast_id=?"
                        .to_string(),
                    vec![
                        json!(canonical(&serde_json::to_value(&receipt.state).unwrap_or(Value::Null))),
                        json!(content_hash(&receipt.state).unwrap_or_default()),
                        json!(receipt.state.revision),
                        json!(quote.spend_atomic),
                        json!(quote.spend_atomic),
                        json!(forecast_id),
                    ],
                ),
                (
                    "DELETE FROM market_write_guards WHERE id IN (?,?)".to_string(),
                    vec![json!(guard_token), json!(caps_token)],
                ),
            ])
            .await;
        if filled.is_err() {
            // A lost acknowledgement is not a second fill.
            if let Some(old) = self
                .db
                .first(
                    "SELECT * FROM market_fills WHERE user_id=? AND idempotency_key=?",
                    &[json!(user_id), json!(idempotency_key)],
                )
                .await
                .map_err(|_| conflict())?
            {
                if text(&old, "request_hash") == Some(request_hash.as_str()) {
                    return self.receipt_view(&old, &mode).await;
                }
            }
            return Err(conflict());
        }
        let stored = self
            .db
            .first("SELECT * FROM market_fills WHERE id=?", &[json!(fill_id)])
            .await
            .map_err(|_| conflict())?
            .ok_or_else(conflict)?;
        self.receipt_view(&stored, &mode).await
    }

    /// `positions`: what one owner holds in one market.
    pub async fn positions(&self, user_id: &str, forecast_id: &str) -> Result<Value, MarketError> {
        let row = self.row(forecast_id).await?;
        let mode = text(&row, "mode").unwrap_or_default().to_string();
        let account = self.account(user_id, &mode).await?;
        let position = self
            .db
            .first(
                "SELECT * FROM market_positions WHERE user_id=? AND forecast_id=?",
                &[json!(user_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?;
        let voids = self
            .db
            .first(
                "SELECT COUNT(*) n,COALESCE(SUM(spend),0) amount FROM market_fill_voids WHERE user_id=? AND forecast_id=?",
                &[json!(user_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?;
        Ok(json!({
            "forecastId": forecast_id, "mode": mode,
            "availablePoints": int(&account, "available").unwrap_or(0),
            "committedPoints": int(&account, "committed").unwrap_or(0),
            "fractionAtomic": int(&account, "fraction").unwrap_or(0).to_string(),
            "grossPoints": position.as_ref().and_then(|row| int(row, "gross")).unwrap_or(0),
            "yesClaimsAtomic": position.as_ref().and_then(|row| int(row, "yes_claims_atomic")).unwrap_or(0).to_string(),
            "noClaimsAtomic": position.as_ref().and_then(|row| int(row, "no_claims_atomic")).unwrap_or(0).to_string(),
            "voidedFillCount": voids.as_ref().and_then(|row| int(row, "n")).unwrap_or(0),
            "refundedPoints": voids.as_ref().and_then(|row| int(row, "amount")).unwrap_or(0),
            "settled": position.as_ref().and_then(|row| int(row, "settled")).unwrap_or(0) != 0,
        }))
    }
}

impl PointMarkets<'_> {
    /// `void_after_evidence`: refund a proven late suffix without touching any accepted receipt.
    ///
    /// Nothing is deleted and nothing is rewritten. A void is a new row that references the fill,
    /// so the receipt the buyer holds stays exactly what it was; what changes is that the market
    /// no longer counts it. That is the only way a correction can be made to a receipted action.
    pub async fn void_after_evidence(
        &self,
        forecast_id: &str,
        decision_id: &str,
        limit: i64,
    ) -> Result<Value, MarketError> {
        identifier(forecast_id)?;
        identifier(decision_id)?;
        if !(1..=MAX_BATCH).contains(&limit) {
            return Err(invalid());
        }
        let decision = self
            .db
            .first(
                "SELECT * FROM forecast_eligibility_decisions WHERE id=? AND forecast_id=?",
                &[json!(decision_id), json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?
            .ok_or_else(conflict)?;
        let row = self
            .db
            .first("SELECT * FROM point_markets WHERE forecast_id=?", &[json!(forecast_id)])
            .await
            .map_err(|_| conflict())?;
        let Some(row) = row else {
            return Ok(json!({"status": "completed", "processed": 0, "remaining": 0}));
        };
        if Self::state_of(&row).is_err() {
            return Ok(json!({"status": "review", "processed": 0, "reason": "market_reconciliation"}));
        }
        let anomaly = self
            .db
            .first(
                "SELECT 1 FROM market_eligibility_anomalies WHERE forecast_id=?",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?;
        if anomaly.is_some() {
            return Ok(json!({"status": "review", "processed": 0, "reason": "market_reconciliation"}));
        }
        let mode = text(&row, "mode").unwrap_or_default().to_string();
        let cutoff = int(&decision, "cutoff_at").unwrap_or(0);
        let fills = self
            .db
            .all(
                "SELECT * FROM market_effective_fills WHERE forecast_id=? AND created_at>=? ORDER BY revision DESC LIMIT ?",
                &[json!(forecast_id), json!(cutoff), json!(limit)],
            )
            .await
            .map_err(|_| conflict())?;
        let mut processed = 0;
        for fill in &fills {
            let uid = text(fill, "user_id").unwrap_or_default().to_string();
            let account = self.account(&uid, &mode).await?;
            let current = self.row(forecast_id).await?;
            let voided = self
                .db
                .execute(
                    "INSERT INTO market_fill_voids(fill_id,decision_id,user_id,forecast_id,mode,spend,claims_atomic,side,\
                     available_before,committed_before,reserve_before_atomic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    &[
                        json!(text(fill, "id")),
                        json!(decision_id),
                        json!(uid),
                        json!(forecast_id),
                        json!(mode),
                        json!(int(fill, "spend")),
                        json!(int(fill, "claims_atomic")),
                        json!(text(fill, "side")),
                        json!(int(&account, "available")),
                        json!(int(&account, "committed")),
                        json!(int(&current, "reserve_atomic")),
                        json!(self.now()),
                    ],
                )
                .await;
            if voided.is_ok() {
                processed += 1;
                continue;
            }
            // Lost acknowledgements and competing coordinators cannot refund twice. Anything
            // else is a race, and a race retries against fresh authoritative balances.
            if let Some(old) = self
                .db
                .first(
                    "SELECT decision_id FROM market_fill_voids WHERE fill_id=?",
                    &[json!(text(fill, "id"))],
                )
                .await
                .map_err(|_| conflict())?
            {
                if text(&old, "decision_id") == Some(decision_id) {
                    continue;
                }
            }
            let anomaly = self
                .db
                .first(
                    "SELECT 1 FROM market_eligibility_anomalies WHERE forecast_id=?",
                    &[json!(forecast_id)],
                )
                .await
                .map_err(|_| conflict())?;
            if anomaly.is_some() {
                return Ok(json!({"status": "review", "processed": processed, "reason": "market_reconciliation"}));
            }
            return Err(conflict());
        }
        let remaining = self
            .db
            .first(
                "SELECT COUNT(*) n FROM market_effective_fills WHERE forecast_id=? AND created_at>=?",
                &[json!(forecast_id), json!(cutoff)],
            )
            .await
            .map_err(|_| conflict())?;
        let remaining = remaining.as_ref().and_then(|row| int(row, "n")).unwrap_or(0);
        let ambiguous = if text(&decision, "event_time_basis") == Some("observed_upper_bound") {
            self.db
                .first(
                    "SELECT 1 FROM market_effective_fills WHERE forecast_id=? AND created_at<?",
                    &[json!(forecast_id), json!(cutoff)],
                )
                .await
                .map_err(|_| conflict())?
                .is_some()
        } else {
            false
        };
        Ok(json!({
            "status": if remaining > 0 { "pending" } else if ambiguous { "review" } else { "completed" },
            "processed": processed, "remaining": remaining,
            "reason": if ambiguous { json!("publication_time_uncertain") } else { Value::Null },
        }))
    }

    /// `settle`: pay out a finalized outcome, in bounded batches.
    ///
    /// The payout arithmetic is the domain's; what is here is that it happens exactly once per
    /// position and that an INVALID outcome returns the principal *and* the whole subsidy, which
    /// is the difference between a voided market and a lost one.
    pub async fn settle(&self, forecast_id: &str, limit: i64) -> Result<Value, MarketError> {
        if !(1..=MAX_BATCH).contains(&limit) {
            return Err(invalid());
        }
        let row = self.row(forecast_id).await?;
        let now = self.now();
        // A disabled buy switch must not strand liabilities that were already accepted.
        let final_row = self
            .db
            .first(
                "SELECT * FROM forecasts f WHERE f.id=? AND f.state IN ('FINALIZED','ARCHIVED') \
                 AND f.finalized_outcome IN ('YES','NO','INVALID') AND EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=f.id \
                 AND json_extract(e.event,'$.command_name')='finalize' AND e.created_at<=?)",
                &[json!(forecast_id), json!(now)],
            )
            .await
            .map_err(|_| conflict())?
            .ok_or_else(conflict)?;
        if text(&final_row, "specification_hash") != text(&row, "specification_hash") {
            return Err(conflict());
        }
        if text(&row, "status") == Some("settled") {
            let mut view = self.view(&row).await?;
            view["processed"] = json!(0);
            view["remaining"] = json!(0);
            return Ok(view);
        }
        let state = Self::state_of(&row)?;
        let outcome = text(&final_row, "finalized_outcome").unwrap_or_default().to_string();
        let closed = close_market(&state, &outcome).map_err(|_| conflict())?;
        let mode = text(&row, "mode").unwrap_or_default().to_string();
        let people = self
            .db
            .all(
                "SELECT * FROM market_positions WHERE forecast_id=? AND settled=0 ORDER BY user_id LIMIT ?",
                &[json!(forecast_id), json!(limit)],
            )
            .await
            .map_err(|_| conflict())?;
        let guard_token = self.token();
        let mut statements: Vec<Statement> = vec![
            guard(
                &guard_token,
                "EXISTS(SELECT 1 FROM point_markets m JOIN forecasts f ON f.id=m.forecast_id WHERE m.forecast_id=? \
                 AND m.revision=? AND m.state_hash=? AND m.status!='settled' AND f.specification_hash=m.specification_hash \
                 AND f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome=? \
                 AND EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=f.id AND json_extract(e.event,'$.command_name')='finalize' AND e.created_at<=?))",
                vec![
                    json!(forecast_id),
                    json!(int(&row, "revision")),
                    json!(text(&row, "state_hash")),
                    json!(outcome),
                    json!(now),
                ],
            ),
            (
                "UPDATE point_markets SET state=?,state_hash=?,status='settling',final_outcome=?,closed_at=COALESCE(closed_at,?) WHERE forecast_id=?"
                    .to_string(),
                vec![
                    json!(canonical(&serde_json::to_value(&closed).unwrap_or(Value::Null))),
                    json!(content_hash(&closed).unwrap_or_default()),
                    json!(outcome),
                    json!(now),
                    json!(forecast_id),
                ],
            ),
        ];
        let mut payout_total = 0i64;
        for position in &people {
            let uid = text(position, "user_id").unwrap_or_default().to_string();
            let gross = int(position, "gross").unwrap_or(0);
            let payout = match closed.closed_outcome.as_deref() {
                Some("INVALID") => gross * SCALE,
                Some("YES") => int(position, "yes_claims_atomic").unwrap_or(0),
                _ => int(position, "no_claims_atomic").unwrap_or(0),
            };
            payout_total += payout;
            let account = self.account(&uid, &mode).await?;
            let sum = payout + int(&account, "fraction").unwrap_or(0);
            let whole = sum.div_euclid(SCALE);
            let fraction = sum.rem_euclid(SCALE);
            let identity = format!("market-settlement:{}", hash_of(&json!([forecast_id, uid])));
            statements.push((
                "INSERT INTO market_settlements(id,user_id,forecast_id,outcome,gross,payout_atomic,created_at) VALUES(?,?,?,?,?,?,?)"
                    .to_string(),
                vec![
                    json!(identity),
                    json!(uid),
                    json!(forecast_id),
                    json!(outcome),
                    json!(gross),
                    json!(payout),
                    json!(now),
                ],
            ));
            statements.push(Self::ledger(
                &identity,
                &uid,
                &row,
                "market_settlement",
                whole,
                -gross,
                &account,
                fraction,
                now,
            ));
            statements.push((
                "UPDATE market_positions SET settled=1 WHERE user_id=? AND forecast_id=? AND settled=0".to_string(),
                vec![json!(uid), json!(forecast_id)],
            ));
        }
        statements.extend([
            (
                "UPDATE point_markets SET reserve_atomic=reserve_atomic-? WHERE forecast_id=?".to_string(),
                vec![json!(payout_total), json!(forecast_id)],
            ),
            (
                "INSERT INTO market_closures(forecast_id,returned_atomic,created_at) SELECT forecast_id,reserve_atomic,? FROM point_markets m \
                 WHERE forecast_id=? AND NOT EXISTS(SELECT 1 FROM market_positions WHERE forecast_id=m.forecast_id AND settled=0)"
                    .to_string(),
                vec![json!(now), json!(forecast_id)],
            ),
            (
                "UPDATE market_treasuries SET available_atomic=available_atomic+COALESCE((SELECT returned_atomic FROM market_closures WHERE forecast_id=?),0),\
                 revision=revision+1 WHERE mode=? AND EXISTS(SELECT 1 FROM market_closures WHERE forecast_id=?)"
                    .to_string(),
                vec![json!(forecast_id), json!(mode), json!(forecast_id)],
            ),
            (
                "UPDATE point_markets SET status='settled',reserve_atomic=0 WHERE forecast_id=? AND EXISTS(SELECT 1 FROM market_closures WHERE forecast_id=?)"
                    .to_string(),
                vec![json!(forecast_id), json!(forecast_id)],
            ),
            (
                "DELETE FROM market_write_guards WHERE id=?".to_string(),
                vec![json!(guard_token)],
            ),
        ]);
        if self.db.batch(&statements).await.is_err() {
            // Concurrent batches and lost acknowledgements are safe to replay, but only if the
            // work is actually done: otherwise this is a real conflict and the caller retries.
            let current = self.row(forecast_id).await?;
            let completed = self
                .db
                .first(
                    "SELECT COUNT(*) n FROM market_positions WHERE forecast_id=? AND settled=0",
                    &[json!(forecast_id)],
                )
                .await
                .map_err(|_| conflict())?;
            let mut all_accepted = !people.is_empty();
            for position in &people {
                let accepted = self
                    .db
                    .first(
                        "SELECT 1 FROM market_settlements WHERE forecast_id=? AND user_id=?",
                        &[json!(forecast_id), json!(text(position, "user_id"))],
                    )
                    .await
                    .map_err(|_| conflict())?;
                all_accepted &= accepted.is_some();
            }
            if text(&current, "status") == Some("settled") || all_accepted {
                let mut view = self.view(&current).await?;
                view["processed"] = json!(0);
                view["remaining"] = json!(completed.as_ref().and_then(|row| int(row, "n")).unwrap_or(0));
                return Ok(view);
            }
            return Err(conflict());
        }
        let remaining = self
            .db
            .first(
                "SELECT COUNT(*) n FROM market_positions WHERE forecast_id=? AND settled=0",
                &[json!(forecast_id)],
            )
            .await
            .map_err(|_| conflict())?;
        let fresh = self.row(forecast_id).await?;
        let mut view = self.view(&fresh).await?;
        view["processed"] = json!(people.len());
        view["remaining"] = json!(remaining.as_ref().and_then(|row| int(row, "n")).unwrap_or(0));
        Ok(view)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{Row, Sqlite};
    use std::cell::{Cell, RefCell};

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/market-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("market golden")).expect("json")
    }

    /// Seed a table from the golden's own rows, so the fixture cannot diverge silently.
    fn seed(db: &Sqlite, table: &str, rows: &[Value]) {
        for row in rows {
            let fields: Vec<String> = row.as_object().expect("a row").keys().cloned().collect();
            let placeholders = vec!["?"; fields.len()].join(",");
            let sql = format!("INSERT INTO {table}({}) VALUES({placeholders})", fields.join(","));
            let params: Vec<Value> = fields.iter().map(|field| row[field].clone()).collect();
            db.run(&sql, &params).unwrap_or_else(|error| panic!("{table}: {error}"));
        }
    }

    /// Assert one call against the golden's record of it, whether it succeeded or was refused.
    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, MarketError>) {
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
                    "{name}: refused with {:?} where the reference succeeded",
                    error
                );
                assert_eq!(
                    error.status as i64,
                    entry["error"]["status"].as_i64().unwrap(),
                    "{name}: a different status"
                );
                assert_eq!(
                    Some(error.code),
                    entry["error"]["code"].as_str(),
                    "{name}: a different code"
                );
            }
        }
        *index += 1;
    }

    #[test]
    fn the_reference_market_lifecycle_is_reproduced_call_for_call() {
        let document = golden();
        let db = Sqlite::from_migrations();
        // Only what the fixture itself put there. `market_treasuries` is seeded by the migration
        // and `point_accounts` by a trigger on the user insert, so copying the vector's rows for
        // them would duplicate state the schema already establishes.
        for table in ["users", "forecasts"] {
            let rows = &document["fixtureRows"][table];
            if let Some(rows) = rows.as_array() {
                seed(&db, table, rows);
            }
        }
        let counter = RefCell::new(0i64);
        let token = move || {
            *counter.borrow_mut() += 1;
            format!("test-market-token-{}", counter.borrow())
        };
        // The reference's fixture advances its clock through the finalize lifecycle: the market
        // settles at close + 2100, which is the instant the forecast became final rather than the
        // instant the agent got round to it.
        let clock = Cell::new(1_000_000i64);
        let now = || clock.get();
        let markets = PointMarkets {
            db: &db,
            clock: &now,
            token: &token,
            live_enabled: true,
        };
        let calls = document["calls"].as_array().expect("calls");
        let mut index = 0usize;

        check(calls, &mut index, block(markets.budget("shadow")));
        check(calls, &mut index, block(markets.fund_treasury(700, &token(), "shadow")));
        // A grant is keyed by a hash of the request key, and that key is arbitrary text. The
        // reference hashes it with `json.dumps`'s default `ensure_ascii`, so the same key written
        // with a non-ASCII character must name the same row — and a port that wrote the character's
        // bytes instead of its escape would treat one grant as two.
        check(
            calls,
            &mut index,
            block(markets.fund_treasury(50, "treasury-café-1", "shadow")),
        );
        check(
            calls,
            &mut index,
            block(markets.fund_treasury(50, "treasury-café-1", "shadow")),
        );
        check(calls, &mut index, block(markets.fund_treasury(700, &token(), "shadow")));

        let specification_hash = |fid: &str| -> String {
            let row = db
                .run("SELECT specification_hash FROM forecasts WHERE id=?", &[json!(fid)])
                .expect("forecast")
                .0;
            crate::db::text(&row[0], "specification_hash").unwrap().to_string()
        };
        let one = specification_hash("market-one");
        check(
            calls,
            &mut index,
            block(markets.create("market-one", None, "shadow", &one)),
        );
        check(
            calls,
            &mut index,
            block(markets.get("market-one")).map(|found| found.unwrap_or(Value::Null)),
        );
        check(calls, &mut index, block(markets.preview("market-one", "YES", 100)));

        let quote = block(markets.quote("user-a", "market-one", "YES", 100)).expect("a quote");
        check(calls, &mut index, Ok(quote.clone()));
        let quote_id = quote["quoteId"].as_str().unwrap().to_string();
        let claims = quote["claimsAtomic"].as_str().unwrap().to_string();
        let accept_key = token();

        check(
            calls,
            &mut index,
            block(markets.accept("user-a", "market-one", &quote_id, claims.parse().unwrap(), &accept_key)),
        );
        check(
            calls,
            &mut index,
            block(markets.accept("user-a", "market-one", &quote_id, claims.parse().unwrap(), &token())),
        );

        // A fill is keyed on arbitrary text, the same way a grant is. The same key has to name
        // the same fill, or one request spends twice.
        let unicode_quote = block(markets.quote("user-b", "market-one", "YES", 20)).expect("a third quote");
        let unicode_claims: i64 = unicode_quote["claimsAtomic"].as_str().unwrap().parse().unwrap();
        let unicode_id = unicode_quote["quoteId"].as_str().unwrap().to_string();
        check(calls, &mut index, Ok(unicode_quote));
        check(
            calls,
            &mut index,
            block(markets.accept("user-b", "market-one", &unicode_id, unicode_claims, "fill-café-1")),
        );
        check(
            calls,
            &mut index,
            block(markets.accept("user-b", "market-one", &unicode_id, unicode_claims, "fill-café-1")),
        );

        let other = block(markets.quote("user-b", "market-one", "NO", 60)).expect("a second quote");
        check(calls, &mut index, Ok(other.clone()));
        check(
            calls,
            &mut index,
            block(markets.accept(
                "user-b",
                "market-one",
                other["quoteId"].as_str().unwrap(),
                other["claimsAtomic"].as_str().unwrap().parse().unwrap(),
                &token(),
            )),
        );
        check(calls, &mut index, block(markets.positions("user-a", "market-one")));
        check(
            calls,
            &mut index,
            block(markets.receipt_status("user-a", "market-one", &quote_id, &accept_key)),
        );

        // The active path, which carries the extra source-currency gates.
        let three = specification_hash("market-three");
        check(calls, &mut index, block(markets.fund_treasury(700, &token(), "active")));
        check(
            calls,
            &mut index,
            block(markets.create("market-three", None, "active", &three)),
        );
        db.run(
            "INSERT OR IGNORE INTO official_watch_sources(id,url,kind,interval_ms,next_poll,checked_at) VALUES(?,?,'index',60000,?,?)",
            &[
                json!("source:market-three"),
                json!("https://example.com/market-three"),
                json!(1_060_000),
                json!(1_000_000),
            ],
        )
        .expect("source");
        db.run(
            "INSERT OR IGNORE INTO official_watch_bindings(forecast_id,source_id,families) VALUES(?,?,?)",
            &[json!("market-three"), json!("source:market-three"), json!("[]")],
        )
        .expect("binding");

        let settle_quote = block(markets.quote("user-c", "market-three", "YES", 90)).expect("a third quote");
        check(calls, &mut index, Ok(settle_quote.clone()));
        check(
            calls,
            &mut index,
            block(markets.accept(
                "user-c",
                "market-three",
                settle_quote["quoteId"].as_str().unwrap(),
                settle_quote["claimsAtomic"].as_str().unwrap().parse().unwrap(),
                &token(),
            )),
        );
        // Finalize the forecast the way the planner does, then settle against it.
        finalize(&db, "market-three");
        clock.set(1_003_100);
        check(calls, &mut index, block(markets.settle("market-three", MAX_BATCH)));
        check(calls, &mut index, block(markets.settle("market-three", MAX_BATCH)));

        assert_eq!(
            index,
            calls.len(),
            "the replay did not make every call the reference made"
        );

        // And the rows: a price that matches is not a proof if the fill that charged for it left
        // a different receipt behind.
        // Only the tables this module writes. `users` and `forecasts` are its input, and the one
        // forecast the replay finalizes is finalized by the planner in the reference — reproducing
        // that here would be testing `scheduler.rs`, not the market.
        for table in [
            "market_treasuries",
            "market_funding",
            "point_markets",
            "market_shadow_accounts",
            "point_fractions",
            "market_quotes",
            "market_fills",
            "market_positions",
            "market_settlements",
            "market_closures",
            "market_account_ledger",
            "point_accounts",
            "point_positions",
        ] {
            let expected = &document["rows"][table];
            let Some(expected) = expected.as_array() else {
                continue;
            };
            let found = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .expect("rows")
                .0;
            let produced: Vec<Value> = found.iter().map(row_to_value).collect();
            assert_eq!(&produced, expected, "{table} differs");
        }
    }

    fn row_to_value(row: &Row) -> Value {
        Value::Object(row.clone())
    }

    /// Move `market-three` to FINALIZED with a YES outcome, the way the lifecycle command does.
    ///
    /// The market only reads three things from it — the state, the specification hash and a
    /// `finalize` event — so the fixture writes exactly those rather than replaying the whole
    /// command, which is `scheduler.rs`'s subject and not this module's.
    fn finalize(db: &Sqlite, forecast_id: &str) {
        let row = db
            .run("SELECT snapshot FROM forecasts WHERE id=?", &[json!(forecast_id)])
            .expect("forecast")
            .0;
        let snapshot = crate::db::text(&row[0], "snapshot").unwrap().to_string();
        let mut value: Value = serde_json::from_str(&snapshot).expect("snapshot");
        value["state"] = json!("FINALIZED");
        value["finalized_outcome"] = json!("YES");
        value["finalized_at_ms"] = json!(1_000_000);
        db.run(
            "UPDATE forecasts SET state='FINALIZED',finalized_outcome='YES',snapshot=? WHERE id=?",
            &[json!(serde_json::to_string(&value).unwrap()), json!(forecast_id)],
        )
        .expect("finalized");
        db.run(
            "INSERT INTO events(forecast_id,revision,hash,event,created_at) VALUES(?,?,?,?,?)",
            &[
                json!(forecast_id),
                json!(value["revision"]),
                json!("e".repeat(64)),
                json!(serde_json::to_string(&json!({"command_name": "finalize"})).unwrap()),
                json!(1_000_000),
            ],
        )
        .expect("event");
    }
}
