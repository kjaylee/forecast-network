//! Certified, buy-only binary LMSR (`forecast_domain.pricing`). Interval arithmetic at 80 decimal
//! digits with outward rounding: charging only when the upper cost bound fits the exact integer
//! spend can under-allocate an atom, never undercharge a fill.

use num_bigint::BigInt;
use num_traits::ToPrimitive;
use serde::{Deserialize, Serialize};

use crate::decimal::{add, div, exp, ln, mul, sub, Dec, Rounding};
use crate::errors::{require, Result, ValidationError};
use crate::fields::{
    check_enum, check_hash, check_id, check_int, check_range, check_schema_version, required_option, Record,
};
use crate::{content_hash, MAX_SAFE_INTEGER};

pub const ATOMIC_UNITS_PER_POINT: i64 = 1_000_000;
pub const DEFAULT_POLICY_ID: &str = "binary-lmsr-shadow-v1";

fn integer(value: i64, name: &str, positive: bool) -> Result<()> {
    require(
        i64::from(positive) <= value && value <= MAX_SAFE_INTEGER,
        &format!(
            "{name} must be a {}safe integer",
            if positive { "positive " } else { "" }
        ),
    )
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Interval {
    pub lo: Dec,
    pub hi: Dec,
}

impl Interval {
    pub fn exact(value: i64) -> Interval {
        Interval {
            lo: Dec::from_int(value),
            hi: Dec::from_int(value),
        }
    }

    pub fn add(&self, other: &Interval) -> Interval {
        Interval {
            lo: add(&self.lo, &other.lo, Rounding::Floor),
            hi: add(&self.hi, &other.hi, Rounding::Ceiling),
        }
    }

    pub fn sub(&self, other: &Interval) -> Interval {
        Interval {
            lo: sub(&self.lo, &other.hi, Rounding::Floor),
            hi: sub(&self.hi, &other.lo, Rounding::Ceiling),
        }
    }

    pub fn mul_positive(&self, value: i64) -> Result<Interval> {
        require(value > 0, "interval multiplier must be positive")?;
        let factor = Dec::from_int(value);
        Ok(Interval {
            lo: mul(&self.lo, &factor, Rounding::Floor),
            hi: mul(&self.hi, &factor, Rounding::Ceiling),
        })
    }

    pub fn div_positive(&self, value: i64) -> Result<Interval> {
        require(value > 0, "interval divisor must be positive")?;
        let divisor = Dec::from_int(value);
        Ok(Interval {
            lo: div(&self.lo, &divisor, Rounding::Floor)?,
            hi: div(&self.hi, &divisor, Rounding::Ceiling)?,
        })
    }

    pub fn exp_negative(&self) -> Result<Interval> {
        require(
            self.hi.is_zero() || self.hi.is_negative(),
            "stable exponential requires a nonpositive interval",
        )?;
        fn bounds(value: &Dec) -> (Dec, Dec) {
            if value.is_zero() {
                return (Dec::one(), Dec::one());
            }
            if value.compare(&Dec::from_int(-256)) != std::cmp::Ordering::Greater {
                return (Dec::zero(), Dec::parse("1e-77").expect("literal"));
            }
            let result = exp(value);
            (result.next_minus(), result.next_plus())
        }
        Ok(Interval {
            lo: bounds(&self.lo).0,
            hi: bounds(&self.hi).1,
        })
    }

    pub fn ln(&self) -> Result<Interval> {
        require(
            !self.lo.is_zero() && !self.lo.is_negative(),
            "logarithm requires a positive interval",
        )?;
        fn bounds(value: &Dec) -> Result<(Dec, Dec)> {
            if *value == Dec::one() {
                return Ok((Dec::zero(), Dec::zero()));
            }
            let result = ln(value)?;
            Ok((result.next_minus(), result.next_plus()))
        }
        Ok(Interval {
            lo: bounds(&self.lo)?.0,
            hi: bounds(&self.hi)?.1,
        })
    }
}

/// Monotone log(1 + exp(x)), bounded at the two interval endpoints.
pub fn softplus(value: &Interval) -> Result<Interval> {
    fn endpoint(x: &Dec) -> Result<Interval> {
        let negative = x.abs().neg();
        let tail = Interval {
            lo: negative.clone(),
            hi: negative,
        }
        .exp_negative()?;
        let base = x.clone().max(Dec::zero());
        let base = Interval {
            lo: base.clone(),
            hi: base,
        };
        Ok(base.add(&Interval::exact(1).add(&tail).ln()?))
    }
    Ok(Interval {
        lo: endpoint(&value.lo)?.lo,
        hi: endpoint(&value.hi)?.hi,
    })
}

/// Public diagnostic bounds for C(q); no floating point ledger conversion.
pub fn cost_interval(yes_atomic: i64, no_atomic: i64, liquidity_atomic: i64) -> Result<Interval> {
    integer(yes_atomic, "YES inventory", false)?;
    integer(no_atomic, "NO inventory", false)?;
    integer(liquidity_atomic, "liquidity", true)?;
    let tail = Interval::exact(-(yes_atomic - no_atomic).abs()).div_positive(liquidity_atomic)?;
    Ok(Interval::exact(yes_atomic.max(no_atomic)).add(
        &Interval::exact(1)
            .add(&tail.exp_negative()?)
            .ln()?
            .mul_positive(liquidity_atomic)?,
    ))
}

pub fn minimum_subsidy_atomic(liquidity_atomic: i64) -> Result<i64> {
    integer(liquidity_atomic, "liquidity", true)?;
    let cost = cost_interval(0, 0, liquidity_atomic)?;
    cost.hi
        .to_integral(Rounding::Ceiling)
        .to_i64()
        .ok_or_else(|| ValidationError::new("subsidy out of range"))
}

/// Presentation only: interval midpoint rounded to the nearest basis point.
pub fn price_bp(own: i64, other: i64, liquidity: i64) -> Result<i64> {
    if own == other {
        return Ok(5000);
    }
    let exp = Interval::exact(-(own - other).abs())
        .div_positive(liquidity)?
        .exp_negative()?;
    let denominator = Interval::exact(1).add(&exp);
    let mut low = div(&Dec::one(), &denominator.hi, Rounding::Floor)?;
    let mut high = div(&Dec::one(), &denominator.lo, Rounding::Ceiling)?;
    if own < other {
        let new_low = sub(&Dec::one(), &high, Rounding::Floor);
        let new_high = sub(&Dec::one(), &low, Rounding::Ceiling);
        low = new_low;
        high = new_high;
    }
    let midpoint = div(
        &add(&low, &high, Rounding::HalfEven),
        &Dec::from_int(2),
        Rounding::HalfEven,
    )?;
    mul(&midpoint, &Dec::from_int(10000), Rounding::HalfEven)
        .to_integral(Rounding::HalfEven)
        .to_i64()
        .ok_or_else(|| ValidationError::new("price out of range"))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PricingPolicy {
    pub schema_version: i64,
    pub policy_id: String,
    pub atomic_units_per_point: i64,
    pub liquidity_atomic: i64,
    pub subsidy_atomic: i64,
    pub minimum_fill_atomic: i64,
    pub maximum_fill_atomic: i64,
    pub maximum_owner_gross_atomic: i64,
    pub maximum_owner_unsettled_atomic: i64,
    pub quote_lifetime_ms: i64,
}

impl Default for PricingPolicy {
    fn default() -> Self {
        PricingPolicy {
            schema_version: 1,
            policy_id: DEFAULT_POLICY_ID.to_string(),
            atomic_units_per_point: ATOMIC_UNITS_PER_POINT,
            liquidity_atomic: 1_000 * ATOMIC_UNITS_PER_POINT,
            subsidy_atomic: 700 * ATOMIC_UNITS_PER_POINT,
            minimum_fill_atomic: ATOMIC_UNITS_PER_POINT,
            maximum_fill_atomic: 100 * ATOMIC_UNITS_PER_POINT,
            maximum_owner_gross_atomic: 300 * ATOMIC_UNITS_PER_POINT,
            maximum_owner_unsettled_atomic: 1_000 * ATOMIC_UNITS_PER_POINT,
            quote_lifetime_ms: 30_000,
        }
    }
}

impl Record for PricingPolicy {
    fn validate(&self) -> Result<()> {
        let p = "PricingPolicy";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.policy_id"), &self.policy_id)?;
        require(
            self.atomic_units_per_point == ATOMIC_UNITS_PER_POINT,
            &format!("{p}.atomic_units_per_point: expected constant {ATOMIC_UNITS_PER_POINT}"),
        )?;
        for (name, value) in [
            ("liquidity_atomic", self.liquidity_atomic),
            ("subsidy_atomic", self.subsidy_atomic),
            ("minimum_fill_atomic", self.minimum_fill_atomic),
            ("maximum_fill_atomic", self.maximum_fill_atomic),
            ("maximum_owner_gross_atomic", self.maximum_owner_gross_atomic),
            ("maximum_owner_unsettled_atomic", self.maximum_owner_unsettled_atomic),
        ] {
            check_range(&format!("{p}.{name}"), value, 1, MAX_SAFE_INTEGER)?;
        }
        check_range(&format!("{p}.quote_lifetime_ms"), self.quote_lifetime_ms, 1, 300_000)?;
        require(
            self.subsidy_atomic >= minimum_subsidy_atomic(self.liquidity_atomic)?,
            "uniform-prior subsidy does not cover b ln(2)",
        )?;
        require(
            self.minimum_fill_atomic <= self.maximum_fill_atomic
                && self.maximum_fill_atomic <= self.maximum_owner_gross_atomic
                && self.maximum_owner_gross_atomic <= self.maximum_owner_unsettled_atomic,
            "fill and gross limits must be ordered",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PricingState {
    pub schema_version: i64,
    pub market_id: String,
    pub specification_hash: String,
    pub policy: PricingPolicy,
    pub yes_claims_atomic: i64,
    pub no_claims_atomic: i64,
    pub deposits_atomic: i64,
    pub reserve_atomic: i64,
    pub revision: i64,
    #[serde(deserialize_with = "required_option")]
    pub closed_outcome: Option<String>,
}

impl Record for PricingState {
    fn validate(&self) -> Result<()> {
        let p = "PricingState";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.market_id"), &self.market_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        self.policy.validate()?;
        for (name, value) in [
            ("yes_claims_atomic", self.yes_claims_atomic),
            ("no_claims_atomic", self.no_claims_atomic),
            ("deposits_atomic", self.deposits_atomic),
            ("reserve_atomic", self.reserve_atomic),
            ("revision", self.revision),
        ] {
            check_int(&format!("{p}.{name}"), value)?;
        }
        if let Some(outcome) = &self.closed_outcome {
            check_enum(&format!("{p}.closed_outcome"), outcome, &["YES", "NO", "INVALID"])?;
        }
        require(
            self.reserve_atomic == self.policy.subsidy_atomic + self.deposits_atomic,
            "reserve must equal the subsidy plus exact acquisition costs",
        )?;
        require(
            self.reserve_atomic
                >= self
                    .yes_claims_atomic
                    .max(self.no_claims_atomic)
                    .max(self.deposits_atomic),
            "terminal liability exceeds reserve",
        )?;
        if self.revision == 0 {
            require(
                self.yes_claims_atomic == 0 && self.no_claims_atomic == 0 && self.deposits_atomic == 0,
                "initial inventory and deposits must be zero",
            )
        } else {
            require(
                self.deposits_atomic > 0 && self.yes_claims_atomic.max(self.no_claims_atomic) > 0,
                "accepted fills require deposits and claims",
            )?;
            let cost = cost_interval(
                self.yes_claims_atomic,
                self.no_claims_atomic,
                self.policy.liquidity_atomic,
            )?;
            let initial = cost_interval(0, 0, self.policy.liquidity_atomic)?;
            require(
                Dec::from_int(self.deposits_atomic).compare(&cost.sub(&initial).lo) != std::cmp::Ordering::Less,
                "deposits cannot be below integrated acquisition cost",
            )
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PricingQuote {
    pub schema_version: i64,
    pub market_id: String,
    pub owner_id: String,
    pub specification_hash: String,
    pub policy_hash: String,
    pub state_hash: String,
    pub state_revision: i64,
    pub side: String,
    pub spend_atomic: i64,
    pub claims_atomic: i64,
    pub winning_total_atomic: i64,
    pub net_gain_atomic: i64,
    pub before_probability_bp: i64,
    pub after_probability_bp: i64,
    pub price_impact_bp: i64,
    pub quoted_at_ms: i64,
    pub expires_at_ms: i64,
}

impl Record for PricingQuote {
    fn validate(&self) -> Result<()> {
        let p = "PricingQuote";
        check_schema_version(p, self.schema_version)?;
        check_id(&format!("{p}.market_id"), &self.market_id)?;
        check_id(&format!("{p}.owner_id"), &self.owner_id)?;
        check_hash(&format!("{p}.specification_hash"), &self.specification_hash)?;
        check_hash(&format!("{p}.policy_hash"), &self.policy_hash)?;
        check_hash(&format!("{p}.state_hash"), &self.state_hash)?;
        check_int(&format!("{p}.state_revision"), self.state_revision)?;
        check_enum(&format!("{p}.side"), &self.side, &["YES", "NO"])?;
        check_range(&format!("{p}.spend_atomic"), self.spend_atomic, 1, MAX_SAFE_INTEGER)?;
        check_range(&format!("{p}.claims_atomic"), self.claims_atomic, 1, MAX_SAFE_INTEGER)?;
        check_range(
            &format!("{p}.winning_total_atomic"),
            self.winning_total_atomic,
            1,
            MAX_SAFE_INTEGER,
        )?;
        check_int(&format!("{p}.net_gain_atomic"), self.net_gain_atomic)?;
        check_range(
            &format!("{p}.before_probability_bp"),
            self.before_probability_bp,
            0,
            10000,
        )?;
        check_range(
            &format!("{p}.after_probability_bp"),
            self.after_probability_bp,
            0,
            10000,
        )?;
        check_range(&format!("{p}.price_impact_bp"), self.price_impact_bp, 0, 10000)?;
        check_int(&format!("{p}.quoted_at_ms"), self.quoted_at_ms)?;
        check_int(&format!("{p}.expires_at_ms"), self.expires_at_ms)?;
        require(
            self.winning_total_atomic == self.claims_atomic
                && self.net_gain_atomic + self.spend_atomic == self.claims_atomic,
            "payout display must match the immutable claims and exact spend",
        )?;
        require(
            self.after_probability_bp >= self.before_probability_bp
                && self.price_impact_bp == self.after_probability_bp - self.before_probability_bp,
            "buy price impact must be nonnegative and consistent",
        )?;
        require(
            self.expires_at_ms > self.quoted_at_ms,
            "quote must have a positive lifetime",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PricingFill {
    pub schema_version: i64,
    pub quote: PricingQuote,
    pub accepted_at_ms: i64,
    pub state_after_hash: String,
}

impl Record for PricingFill {
    fn validate(&self) -> Result<()> {
        check_schema_version("PricingFill", self.schema_version)?;
        self.quote.validate()?;
        check_int("PricingFill.accepted_at_ms", self.accepted_at_ms)?;
        check_hash("PricingFill.state_after_hash", &self.state_after_hash)?;
        require(
            self.quote.quoted_at_ms <= self.accepted_at_ms && self.accepted_at_ms < self.quote.expires_at_ms,
            "fill acceptance must occur within the quote lifetime",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PricingReceipt {
    pub schema_version: i64,
    pub state: PricingState,
    pub fill: PricingFill,
}

impl Record for PricingReceipt {
    fn validate(&self) -> Result<()> {
        check_schema_version("PricingReceipt", self.schema_version)?;
        self.state.validate()?;
        self.fill.validate()?;
        let quote = &self.fill.quote;
        require(
            content_hash(&self.state)? == self.fill.state_after_hash,
            "receipt state hash must match accepted fill",
        )?;
        require(
            self.state.market_id == quote.market_id
                && self.state.specification_hash == quote.specification_hash
                && content_hash(&self.state.policy)? == quote.policy_hash
                && self.state.revision == quote.state_revision + 1
                && self.state.closed_outcome.is_none(),
            "receipt market context mismatch",
        )?;
        let yes = if quote.side == "YES" { quote.claims_atomic } else { 0 };
        let no = if quote.side == "NO" { quote.claims_atomic } else { 0 };
        let before = PricingState {
            yes_claims_atomic: self.state.yes_claims_atomic - yes,
            no_claims_atomic: self.state.no_claims_atomic - no,
            deposits_atomic: self.state.deposits_atomic - quote.spend_atomic,
            reserve_atomic: self.state.reserve_atomic - quote.spend_atomic,
            revision: self.state.revision - 1,
            ..self.state.clone()
        };
        before.validate()?;
        require(
            content_hash(&before)? == quote.state_hash,
            "receipt does not conserve claims or assets",
        )?;
        let expected = quote_buy(
            &before,
            &quote.owner_id,
            &quote.side,
            quote.spend_atomic,
            quote.quoted_at_ms,
        )?;
        require(&expected == quote, "receipt includes a forged quote")
    }
}

/// Marginal YES price for presentation; never a finite-fill payout.
pub fn market_probability_bp(state: &PricingState) -> Result<i64> {
    state.validate()?;
    price_bp(
        state.yes_claims_atomic,
        state.no_claims_atomic,
        state.policy.liquidity_atomic,
    )
}

pub fn initialize_market(policy: PricingPolicy, market_id: &str, specification_hash: &str) -> Result<PricingState> {
    let state = PricingState {
        schema_version: 1,
        market_id: market_id.to_string(),
        specification_hash: specification_hash.to_string(),
        reserve_atomic: policy.subsidy_atomic,
        policy,
        yes_claims_atomic: 0,
        no_claims_atomic: 0,
        deposits_atomic: 0,
        revision: 0,
        closed_outcome: None,
    };
    state.validate()?;
    Ok(state)
}

fn sides(state: &PricingState, side: &str) -> (i64, i64) {
    if side == "YES" {
        (state.yes_claims_atomic, state.no_claims_atomic)
    } else {
        (state.no_claims_atomic, state.yes_claims_atomic)
    }
}

fn floor_i64(value: &Dec) -> Result<i64> {
    let integral: BigInt = value.to_integral(Rounding::Floor);
    integral
        .to_i64()
        .ok_or_else(|| ValidationError::new("claim capacity exhausted"))
}

fn claims_for_spend(state: &PricingState, side: &str, spend: i64) -> Result<i64> {
    let (own, other) = sides(state, side);
    let liquidity = state.policy.liquidity_atomic;
    // Exact inverse: d = s + b softplus((other-own)/b + ln(1-exp(-s/b))).
    let spent = Interval::exact(spend);
    let tail = Interval::exact(-spend).div_positive(liquidity)?.exp_negative()?;
    let fraction = Interval::exact(1).sub(&tail);
    let shifted = Interval::exact(other - own)
        .div_positive(liquidity)?
        .add(&fraction.ln()?);
    let quantity = spent.add(&softplus(&shifted)?.mul_positive(liquidity)?);
    let mut candidate = floor_i64(&quantity.hi)?;
    require(candidate <= MAX_SAFE_INTEGER - own, "claim capacity exhausted")?;
    let before = cost_interval(own, other, liquidity)?;
    // Upper inverse bound leaves at most a few atoms to inspect; fail closed on wide intervals.
    let low = floor_i64(&quantity.lo)?;
    require(candidate - low <= 2, "pricing precision insufficient")?;
    let spend_dec = Dec::from_int(spend);
    while candidate > 0 {
        let upper_cost = cost_interval(own + candidate, other, liquidity)?.sub(&before).hi;
        if upper_cost.compare(&spend_dec) != std::cmp::Ordering::Greater {
            return Ok(candidate);
        }
        candidate -= 1;
        require(candidate >= low - 2, "pricing precision insufficient")?;
    }
    Err(ValidationError::new(
        "spend cannot purchase a positive conservatively priced claim",
    ))
}

pub fn quote_buy(
    state: &PricingState,
    owner_id: &str,
    side: &str,
    spend_atomic: i64,
    now_ms: i64,
) -> Result<PricingQuote> {
    state.validate()?;
    integer(spend_atomic, "spend", true)?;
    integer(now_ms, "quote time", false)?;
    require(side == "YES" || side == "NO", "side must be YES or NO")?;
    require(state.closed_outcome.is_none(), "market is closed")?;
    let policy = &state.policy;
    require(
        policy.minimum_fill_atomic <= spend_atomic && spend_atomic <= policy.maximum_fill_atomic,
        "spend outside published fill limits",
    )?;
    require(
        state.reserve_atomic + spend_atomic <= MAX_SAFE_INTEGER,
        "reserve capacity exhausted",
    )?;
    let claims = claims_for_spend(state, side, spend_atomic)?;
    let (own, other) = sides(state, side);
    require(
        state.reserve_atomic + spend_atomic >= (own + claims).max(other),
        "fill would exceed fully reserved terminal claims",
    )?;
    require(claims >= spend_atomic, "price is too saturated for this atomic spend")?;
    let before_bp = price_bp(own, other, policy.liquidity_atomic)?;
    let after_bp = price_bp(own + claims, other, policy.liquidity_atomic)?;
    let quote = PricingQuote {
        schema_version: 1,
        market_id: state.market_id.clone(),
        owner_id: owner_id.to_string(),
        specification_hash: state.specification_hash.clone(),
        policy_hash: content_hash(policy)?,
        state_hash: content_hash(state)?,
        state_revision: state.revision,
        side: side.to_string(),
        spend_atomic,
        claims_atomic: claims,
        winning_total_atomic: claims,
        net_gain_atomic: claims - spend_atomic,
        before_probability_bp: before_bp,
        after_probability_bp: after_bp,
        price_impact_bp: after_bp - before_bp,
        quoted_at_ms: now_ms,
        expires_at_ms: now_ms + policy.quote_lifetime_ms,
    };
    quote.validate()?;
    Ok(quote)
}

pub struct AcceptLimits {
    pub minimum_claims_atomic: i64,
    pub owner_gross_atomic: i64,
    pub owner_unsettled_atomic: i64,
}

/// Pure acceptance; adapters must CAS state and authoritative owner totals.
pub fn accept_quote(
    state: &PricingState,
    quote: &PricingQuote,
    owner_id: &str,
    now_ms: i64,
    limits: &AcceptLimits,
) -> Result<PricingReceipt> {
    quote.validate()?;
    integer(now_ms, "acceptance time", false)?;
    integer(limits.minimum_claims_atomic, "minimum claims", false)?;
    integer(limits.owner_gross_atomic, "gross cost", false)?;
    integer(limits.owner_unsettled_atomic, "unsettled cost", false)?;
    require(owner_id == quote.owner_id, "quote belongs to another owner")?;
    require(
        quote.quoted_at_ms <= now_ms && now_ms < quote.expires_at_ms,
        "quote expired or not yet valid",
    )?;
    let expected = quote_buy(state, owner_id, &quote.side, quote.spend_atomic, quote.quoted_at_ms)?;
    require(&expected == quote, "quote is forged or market revision changed")?;
    require(
        quote.claims_atomic >= limits.minimum_claims_atomic,
        "minimum claims not satisfied",
    )?;
    require(
        limits.owner_gross_atomic + quote.spend_atomic <= state.policy.maximum_owner_gross_atomic,
        "owner gross market cost limit exceeded",
    )?;
    require(
        limits.owner_unsettled_atomic + quote.spend_atomic <= state.policy.maximum_owner_unsettled_atomic,
        "owner unsettled gross cost limit exceeded",
    )?;
    let updated = PricingState {
        yes_claims_atomic: state.yes_claims_atomic + if quote.side == "YES" { quote.claims_atomic } else { 0 },
        no_claims_atomic: state.no_claims_atomic + if quote.side == "NO" { quote.claims_atomic } else { 0 },
        deposits_atomic: state.deposits_atomic + quote.spend_atomic,
        reserve_atomic: state.reserve_atomic + quote.spend_atomic,
        revision: state.revision + 1,
        ..state.clone()
    };
    let receipt = PricingReceipt {
        schema_version: 1,
        fill: PricingFill {
            schema_version: 1,
            quote: quote.clone(),
            accepted_at_ms: now_ms,
            state_after_hash: content_hash(&updated)?,
        },
        state: updated,
    };
    receipt.validate()?;
    Ok(receipt)
}

/// Only an authenticated finalization adapter may supply a final outcome.
pub fn close_market(state: &PricingState, outcome: &str) -> Result<PricingState> {
    state.validate()?;
    require(["YES", "NO", "INVALID"].contains(&outcome), "invalid final outcome")?;
    require(
        state.closed_outcome.as_deref().is_none_or(|current| current == outcome),
        "final outcome cannot change",
    )?;
    Ok(PricingState {
        closed_outcome: Some(outcome.to_string()),
        ..state.clone()
    })
}

pub fn payout_atomic(fill: &PricingFill, outcome: &str) -> Result<i64> {
    fill.validate()?;
    require(["YES", "NO", "INVALID"].contains(&outcome), "invalid final outcome")?;
    if outcome == "INVALID" {
        return Ok(fill.quote.spend_atomic);
    }
    Ok(if fill.quote.side == outcome {
        fill.quote.claims_atomic
    } else {
        0
    })
}

pub fn terminal_liability_atomic(state: &PricingState, outcome: &str) -> Result<i64> {
    state.validate()?;
    require(["YES", "NO", "INVALID"].contains(&outcome), "invalid final outcome")?;
    Ok(match outcome {
        "INVALID" => state.deposits_atomic,
        "YES" => state.yes_claims_atomic,
        _ => state.no_claims_atomic,
    })
}
