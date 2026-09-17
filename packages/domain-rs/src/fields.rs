//! Field-level contracts shared by every record family (`forecast_domain.records` in Python):
//! nonblank text with code-point limits, nonnegative safe integers, anchored patterns, enums,
//! constants and `schema_version == 1`. Records call these from `validate()`; the Python
//! reference runs the same checks generically from dataclass metadata.

use serde::{de::DeserializeOwned, Deserialize, Deserializer, Serialize};

use crate::canonical::{canonical_bytes, parse_strict};
use crate::errors::{require, Result, ValidationError};

pub const MAX_TEXT_LENGTH: usize = 1_000_000;

pub fn is_hash(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

pub fn is_id(value: &str) -> bool {
    let mut chars = value.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    value.len() <= 128 && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
}

pub fn is_asset(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some('A'..='Z'))
        && value.len() <= 16
        && chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
}

pub fn is_base58_32_44(value: &str) -> bool {
    (32..=44).contains(&value.len())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() && !matches!(c, '0' | 'O' | 'I' | 'l'))
}

pub fn is_signature(value: &str) -> bool {
    value.len() == 128 && value.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

pub fn check_text(path: &str, value: &str, max: Option<usize>) -> Result<()> {
    require(!value.trim().is_empty(), &format!("{path}: text must not be blank"))?;
    require(
        value.chars().count() <= MAX_TEXT_LENGTH,
        &format!("{path}: text exceeds {MAX_TEXT_LENGTH} characters"),
    )?;
    if let Some(limit) = max {
        require(
            value.chars().count() <= limit,
            &format!("{path}: allows at most {limit} entries"),
        )?;
    }
    Ok(())
}

pub fn check_int(path: &str, value: i64) -> Result<()> {
    require(
        (0..=crate::MAX_SAFE_INTEGER).contains(&value),
        &format!("{path}: expected nonnegative safe integer"),
    )
}

pub fn check_range(path: &str, value: i64, min: i64, max: i64) -> Result<()> {
    check_int(path, value)?;
    require(value >= min, &format!("{path}: below minimum {min}"))?;
    require(value <= max, &format!("{path}: above maximum {max}"))
}

pub fn check_hash(path: &str, value: &str) -> Result<()> {
    require(is_hash(value), &format!("{path}: malformed value"))
}

pub fn check_id(path: &str, value: &str) -> Result<()> {
    check_text(path, value, None)?;
    require(is_id(value), &format!("{path}: malformed value"))
}

pub fn check_enum(path: &str, value: &str, allowed: &[&str]) -> Result<()> {
    require(
        allowed.contains(&value),
        &format!("{path}: expected one of {allowed:?}"),
    )
}

pub fn check_const(path: &str, value: &str, expected: &str) -> Result<()> {
    require(value == expected, &format!("{path}: expected constant {expected:?}"))
}

pub fn check_schema_version(path: &str, value: i64) -> Result<()> {
    require(value == 1, &format!("{path}.schema_version: expected constant 1"))
}

/// Python `from_dict` requires every field key, including ones that default to null.
pub fn required_option<'de, D, T>(deserializer: D) -> std::result::Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

/// Shared decoding entry: strict JSON, exact field set, then the record's own validation.
pub trait Record: Serialize + DeserializeOwned + Sized {
    fn validate(&self) -> Result<()>;

    fn from_json(text: &str) -> Result<Self> {
        let value = parse_strict(text)?;
        Self::from_value(value)
    }

    fn from_value(value: serde_json::Value) -> Result<Self> {
        let record: Self = serde_json::from_value(value).map_err(|error| ValidationError::new(error.to_string()))?;
        record.validate()?;
        Ok(record)
    }

    fn canonical(&self) -> Result<Vec<u8>> {
        self.validate()?;
        canonical_bytes(self)
    }
}
