//! 80-significant-digit decimal arithmetic with directed rounding, matching Python's `decimal`
//! contexts used by `forecast_domain.pricing` (`prec=80`, FLOOR / CEILING / HALF_EVEN) including
//! correctly rounded `exp` and `ln` and the adjacent representable neighbours (`next_plus`,
//! `next_minus`). Every operation computes the exact rational result and rounds once, so values
//! are identical to the reference; only the textual form may differ.

use std::cmp::Ordering;
use std::fmt;

use num_bigint::BigInt;
use num_integer::Integer;
use num_rational::BigRational;
use num_traits::{One, Signed, ToPrimitive, Zero};

use crate::errors::{Result, ValidationError};

pub const PRECISION: u32 = 80;
/// `Context(prec=80)` with the default `Emin` (-999999): the smallest positive representable.
const MIN_EXPONENT: i64 = -999_999 - (PRECISION as i64 - 1);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rounding {
    Floor,
    Ceiling,
    HalfEven,
}

/// A finite decimal `coefficient × 10^exponent`, kept with no trailing zeros in the coefficient.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Dec {
    coefficient: BigInt,
    exponent: i64,
}

fn pow10(n: u32) -> BigInt {
    BigInt::from(10).pow(n)
}

fn digits(value: &BigInt) -> u32 {
    if value.is_zero() {
        1
    } else {
        value.abs().to_string().len() as u32
    }
}

impl Dec {
    pub fn zero() -> Dec {
        Dec {
            coefficient: BigInt::zero(),
            exponent: 0,
        }
    }

    pub fn one() -> Dec {
        Dec::from_int(1)
    }

    pub fn from_int(value: i64) -> Dec {
        Dec::normalize(BigInt::from(value), 0)
    }

    pub fn from_bigint(value: BigInt) -> Dec {
        Dec::normalize(value, 0)
    }

    fn normalize(mut coefficient: BigInt, mut exponent: i64) -> Dec {
        if coefficient.is_zero() {
            return Dec::zero();
        }
        let ten = BigInt::from(10);
        loop {
            let (quotient, remainder) = coefficient.div_rem(&ten);
            if !remainder.is_zero() {
                break;
            }
            coefficient = quotient;
            exponent += 1;
        }
        Dec { coefficient, exponent }
    }

    /// `Decimal(text)` for the forms Python prints: `-1.5E-77`, `0.000123`, `1000`, `1E+3`.
    pub fn parse(text: &str) -> Result<Dec> {
        let trimmed = text.trim();
        let (negative, body) = match trimmed.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, trimmed.strip_prefix('+').unwrap_or(trimmed)),
        };
        let (mantissa, exponent) = match body.find(['e', 'E']) {
            Some(index) => (
                &body[..index],
                body[index + 1..]
                    .parse::<i64>()
                    .map_err(|_| ValidationError::new("invalid decimal"))?,
            ),
            None => (body, 0),
        };
        let (whole, fraction) = match mantissa.find('.') {
            Some(index) => (&mantissa[..index], &mantissa[index + 1..]),
            None => (mantissa, ""),
        };
        let digits: String = format!("{whole}{fraction}");
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Err(ValidationError::new("invalid decimal"));
        }
        let mut coefficient: BigInt = digits.parse().expect("digits");
        if negative {
            coefficient = -coefficient;
        }
        Ok(Dec::normalize(coefficient, exponent - fraction.len() as i64))
    }

    pub fn to_rational(&self) -> BigRational {
        if self.exponent >= 0 {
            BigRational::from_integer(&self.coefficient * pow10(self.exponent as u32))
        } else {
            BigRational::new(self.coefficient.clone(), pow10((-self.exponent) as u32))
        }
    }

    pub fn is_zero(&self) -> bool {
        self.coefficient.is_zero()
    }

    pub fn is_negative(&self) -> bool {
        self.coefficient.is_negative()
    }

    pub fn neg(&self) -> Dec {
        Dec {
            coefficient: -self.coefficient.clone(),
            exponent: self.exponent,
        }
    }

    pub fn abs(&self) -> Dec {
        Dec {
            coefficient: self.coefficient.abs(),
            exponent: self.exponent,
        }
    }

    /// Exponent of the most significant digit (`adjusted()` in Python).
    pub fn adjusted(&self) -> i64 {
        self.exponent + i64::from(digits(&self.coefficient)) - 1
    }

    pub fn compare(&self, other: &Dec) -> Ordering {
        self.to_rational().cmp(&other.to_rational())
    }

    pub fn max(self, other: Dec) -> Dec {
        if self.compare(&other) == Ordering::Less {
            other
        } else {
            self
        }
    }

    /// `to_integral_value(rounding=…)`.
    pub fn to_integral(&self, rounding: Rounding) -> BigInt {
        round_rational_to_unit(&self.to_rational(), 0, rounding)
    }

    /// Smallest representable value strictly greater than `self`.
    pub fn next_plus(&self) -> Dec {
        if self.is_zero() {
            return Dec {
                coefficient: BigInt::one(),
                exponent: MIN_EXPONENT,
            };
        }
        if self.is_negative() {
            return self.neg().next_minus().neg();
        }
        let ulp = self.adjusted() - i64::from(PRECISION) + 1;
        Dec::normalize(rescale(&self.coefficient, self.exponent, ulp) + 1, ulp)
    }

    /// Largest representable value strictly less than `self`.
    pub fn next_minus(&self) -> Dec {
        if self.is_zero() {
            return self.next_plus().neg();
        }
        if self.is_negative() {
            return self.neg().next_plus().neg();
        }
        let power_of_ten = self.coefficient.is_one();
        let ulp = self.adjusted() - i64::from(PRECISION) + if power_of_ten { 0 } else { 1 };
        Dec::normalize(rescale(&self.coefficient, self.exponent, ulp) - 1, ulp)
    }
}

/// Express `coefficient × 10^exponent` at a coarser or finer exponent (exact; `target <= exponent`).
fn rescale(coefficient: &BigInt, exponent: i64, target: i64) -> BigInt {
    debug_assert!(target <= exponent);
    coefficient * pow10((exponent - target) as u32)
}

/// Round `value / 10^unit_exponent` to an integer with the given mode.
fn round_rational_to_unit(value: &BigRational, unit_exponent: i64, rounding: Rounding) -> BigInt {
    let scaled = if unit_exponent >= 0 {
        value / BigRational::from_integer(pow10(unit_exponent as u32))
    } else {
        value * BigRational::from_integer(pow10((-unit_exponent) as u32))
    };
    let floor = scaled.floor().to_integer();
    let remainder = &scaled - BigRational::from_integer(floor.clone());
    if remainder.is_zero() {
        return floor;
    }
    match rounding {
        Rounding::Floor => floor,
        Rounding::Ceiling => floor + 1,
        Rounding::HalfEven => {
            let half = BigRational::new(BigInt::one(), BigInt::from(2));
            match remainder.cmp(&half) {
                Ordering::Less => floor,
                Ordering::Greater => floor + 1,
                Ordering::Equal => {
                    if floor.is_even() {
                        floor
                    } else {
                        floor + 1
                    }
                }
            }
        }
    }
}

/// Adjusted exponent of a nonzero rational: floor(log10(|value|)).
fn rational_adjusted(value: &BigRational) -> i64 {
    let numerator = value.numer().abs();
    let denominator = value.denom().clone();
    let mut estimate = i64::from(digits(&numerator)) - i64::from(digits(&denominator));
    // |value| >= 10^estimate ?
    loop {
        let lower = if estimate >= 0 {
            numerator >= &denominator * pow10(estimate as u32)
        } else {
            &numerator * pow10((-estimate) as u32) >= denominator
        };
        if !lower {
            estimate -= 1;
            continue;
        }
        let upper = if estimate + 1 >= 0 {
            numerator < &denominator * pow10((estimate + 1) as u32)
        } else {
            &numerator * pow10((-(estimate + 1)) as u32) < denominator
        };
        if !upper {
            estimate += 1;
            continue;
        }
        return estimate;
    }
}

/// Round an exact rational to 80 significant digits.
pub fn round_rational(value: &BigRational, rounding: Rounding) -> Dec {
    if value.is_zero() {
        return Dec::zero();
    }
    let unit = rational_adjusted(value) - i64::from(PRECISION) + 1;
    let coefficient = round_rational_to_unit(value, unit, rounding);
    Dec::normalize(coefficient, unit)
}

/// `Context.add/subtract/multiply/divide` with directed rounding.
pub fn add(a: &Dec, b: &Dec, rounding: Rounding) -> Dec {
    round_rational(&(a.to_rational() + b.to_rational()), rounding)
}

pub fn sub(a: &Dec, b: &Dec, rounding: Rounding) -> Dec {
    round_rational(&(a.to_rational() - b.to_rational()), rounding)
}

pub fn mul(a: &Dec, b: &Dec, rounding: Rounding) -> Dec {
    round_rational(&(a.to_rational() * b.to_rational()), rounding)
}

pub fn div(a: &Dec, b: &Dec, rounding: Rounding) -> Result<Dec> {
    if b.is_zero() {
        return Err(ValidationError::new("division by zero"));
    }
    Ok(round_rational(&(a.to_rational() / b.to_rational()), rounding))
}

/// Ziv rounding: an approximation `mantissa × 10^exponent` with absolute error `± error` units of
/// the same scale rounds unambiguously when both ends agree.
fn ziv(mantissa: &BigInt, error: &BigInt, exponent: i64) -> Option<Dec> {
    let scale = BigRational::from_integer(pow10((-exponent) as u32));
    let low = BigRational::from_integer(mantissa - error) / &scale;
    let high = BigRational::from_integer(mantissa + error) / &scale;
    if low.is_zero() || high.is_zero() || low.is_negative() != high.is_negative() {
        return None;
    }
    let a = round_rational(&low, Rounding::HalfEven);
    let b = round_rational(&high, Rounding::HalfEven);
    (a == b).then_some(a)
}

/// Correctly rounded `exp(x)` (ROUND_HALF_EVEN at 80 digits) for any finite `x`.
pub fn exp(x: &Dec) -> Dec {
    if x.is_zero() {
        return Dec::one();
    }
    let mut work: u32 = 110;
    loop {
        if let Some(result) = exp_attempt(x, work) {
            return result;
        }
        work += 40;
    }
}

fn exp_attempt(x: &Dec, work: u32) -> Option<Dec> {
    let scale = pow10(work);
    let value = x.to_rational();
    // Halve the argument until |y| < 2^-8, then square back.
    let magnitude = value.abs().ceil().to_integer().bits() as u32;
    let halvings = magnitude + 8;
    let reduced = &value / BigRational::from_integer(BigInt::from(2).pow(halvings));
    let y = (reduced * BigRational::from_integer(scale.clone()))
        .floor()
        .to_integer();
    // Series in fixed point: each term truncates by at most one unit.
    let mut sum = scale.clone();
    let mut term = scale.clone();
    let mut terms: u64 = 1;
    for i in 1..10_000u64 {
        term = (&term * &y).div_floor(&(BigInt::from(i) * &scale));
        if term.is_zero() {
            break;
        }
        sum += &term;
        terms += 1;
    }
    // Floating mantissa: value = mantissa × 10^exponent, mantissa carries `work` digits.
    let mut mantissa = sum;
    let mut exponent: i64 = -(work as i64);
    let mut error = BigInt::from(terms + 2);
    for _ in 0..halvings {
        // (m ± e)^2 = m^2 ± 2me + e^2 ; truncation adds one unit.
        let product = &mantissa * &mantissa;
        let cross: BigInt = &error * BigInt::from(2) * &mantissa;
        let square: BigInt = &error * &error;
        error = cross.div_floor(&scale) + square.div_floor(&scale) + BigInt::from(3);
        mantissa = product.div_floor(&scale);
        exponent = 2 * exponent + i64::from(work);
        // Renormalise to keep `work` significant digits.
        let deficit = i64::from(work) - i64::from(digits(&mantissa));
        if deficit > 0 {
            let factor = pow10(deficit as u32);
            mantissa *= &factor;
            error = error * &factor + 1;
            exponent -= deficit;
        }
        if error.bits() as u32 > work * 3 {
            return None;
        }
    }
    ziv(&mantissa, &error, exponent)
}

fn isqrt(value: &BigInt) -> BigInt {
    value.sqrt()
}

/// Correctly rounded `ln(v)` (ROUND_HALF_EVEN at 80 digits) for `v > 0`.
pub fn ln(v: &Dec) -> Result<Dec> {
    if v.is_zero() || v.is_negative() {
        return Err(ValidationError::new("logarithm requires a positive value"));
    }
    if v.coefficient.is_one() && v.exponent == 0 {
        return Ok(Dec::zero());
    }
    // Values within 10^-k of one need k extra digits to keep relative precision.
    let closeness = {
        let delta = sub(v, &Dec::one(), Rounding::Floor);
        if delta.is_zero() {
            0
        } else {
            (-delta.adjusted()).max(0) as u32
        }
    };
    let mut work: u32 = 110 + closeness;
    loop {
        if let Some(result) = ln_attempt(v, work) {
            return Ok(result);
        }
        work += 40;
    }
}

/// `ln` of a fixed-point value in `[1, 10)` (`m` scaled by `10^work`): square roots pull the value
/// under 1.5 (absolute error shrinks for values above one), then `2·atanh((m-1)/(m+1))`.
/// Returns `(mantissa, error)` in units of `10^-work`.
fn ln_core(mut m: BigInt, work: u32) -> (BigInt, BigInt) {
    let scale = pow10(work);
    let mut error = BigInt::zero();
    let upper = &scale * 3 / 2;
    let mut roots: u32 = 0;
    while m > upper {
        let root = isqrt(&(&m * &scale));
        // sqrt(v ± e) = sqrt(v) ± e / (2 sqrt v); values stay >= 1 so the error does not grow.
        let (quotient, remainder) = (&error * &scale).div_rem(&(&root * 2));
        error = quotient + if remainder.is_zero() { 0 } else { 1 } + 1;
        m = root;
        roots += 1;
    }
    let numerator = &m - &scale;
    let denominator = &m + &scale;
    let z = (&numerator * &scale).div_floor(&denominator);
    let z_error = &error + 2;
    let z2 = (&z * &z).div_floor(&scale);
    let mut power = z.clone();
    let mut sum = z.clone();
    let mut terms: u64 = 1;
    for i in 1..10_000u64 {
        power = (&power * &z2).div_floor(&scale);
        let term = &power / BigInt::from(2 * i + 1);
        if term.is_zero() {
            break;
        }
        sum += &term;
        terms += 1;
    }
    // d(atanh z)/dz = 1/(1-z^2) <= 1.1 for |z| <= 0.25.
    let scaled_error: BigInt = &z_error * BigInt::from(11);
    let sum_error = scaled_error.div_floor(&BigInt::from(10)) + BigInt::from(terms + 2);
    let factor = BigInt::from(2).pow(roots + 1);
    (sum * &factor, sum_error * &factor)
}

fn ln_attempt(v: &Dec, work: u32) -> Option<Dec> {
    let scale = pow10(work);
    // v = c × 10^e with c in [1, 10): ln v = ln c + e ln 10 keeps full relative precision for tiny v.
    let e = v.adjusted();
    let c = Dec::normalize(v.coefficient.clone(), v.exponent - e);
    let c_fixed = (c.to_rational() * BigRational::from_integer(scale.clone()))
        .floor()
        .to_integer();
    let (mut mantissa, mut error) = ln_core(c_fixed, work);
    if e != 0 {
        let (ln10, ln10_error) = ln_core(&scale * 10, work);
        mantissa += &ln10 * BigInt::from(e);
        error += ln10_error * BigInt::from(e.abs());
    }
    if error.bits() as u32 > work * 3 {
        return None;
    }
    ziv(&mantissa, &error, -(work as i64))
}

impl fmt::Display for Dec {
    /// Plain positional notation (values only; Python's scientific form is not reproduced).
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let digits = self.coefficient.abs().to_string();
        let sign = if self.is_negative() { "-" } else { "" };
        if self.exponent >= 0 {
            return write!(f, "{sign}{digits}{}", "0".repeat(self.exponent as usize));
        }
        let point = digits.len() as i64 + self.exponent;
        if point > 0 {
            write!(f, "{sign}{}.{}", &digits[..point as usize], &digits[point as usize..])
        } else {
            write!(f, "{sign}0.{}{digits}", "0".repeat((-point) as usize))
        }
    }
}

impl From<i64> for Dec {
    fn from(value: i64) -> Self {
        Dec::from_int(value)
    }
}

pub fn to_i64(value: &BigInt) -> Option<i64> {
    value.to_i64()
}
