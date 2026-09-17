//! Canonical JSON commitments identical to `forecast_domain.serialization`.
//!
//! Rules: object keys sorted by code point, separators `,` and `:`, non-ASCII emitted raw,
//! integers only (|n| <= 2^53-1), no NaN/Infinity, strings must be valid UTF-8 without unpaired
//! surrogates (guaranteed by Rust `String`). `serde_json::Value` with the default `BTreeMap`
//! object representation already sorts keys; the writer below enforces the numeric rules.

use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::errors::{Result, ValidationError};

pub const COMMITMENT_PREFIX: &[u8] = b"forecast-network:sha256:canonical-json:v1\n";
pub const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;

/// Numeric rules: integers only, |n| <= 2^53-1.
pub fn check(value: &Value) -> Result<()> {
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(()),
        Value::Number(number) => match number.as_i64() {
            Some(n) if n.abs() <= MAX_SAFE_INTEGER => Ok(()),
            Some(_) => Err(ValidationError::new("Canonical integer exceeds portable range")),
            None => Err(ValidationError::new("Unsupported canonical value: float")),
        },
        Value::Array(items) => items.iter().try_for_each(check),
        Value::Object(map) => map.values().try_for_each(check),
    }
}

/// Canonical bytes of any serializable value (records serialize through `serde_json::Value`).
pub fn canonical_bytes<T: Serialize + ?Sized>(value: &T) -> Result<Vec<u8>> {
    let value = serde_json::to_value(value)
        .map_err(|error| ValidationError::new(format!("Unsupported canonical value: {error}")))?;
    check(&value)?;
    serde_json::to_vec(&value).map_err(|error| ValidationError::new(error.to_string()))
}

/// `sha256(prefix + canonical_bytes)` as lowercase hex.
pub fn content_hash<T: Serialize + ?Sized>(value: &T) -> Result<String> {
    let mut hasher = Sha256::new();
    hasher.update(COMMITMENT_PREFIX);
    hasher.update(canonical_bytes(value)?);
    Ok(hex::encode(hasher.finalize()))
}

/// Strict JSON text decoding: duplicate keys and non-integer numbers are rejected like Python.
pub fn parse_strict(text: &str) -> Result<Value> {
    if text.len() > 8 * 1024 * 1024 {
        return Err(ValidationError::new("JSON exceeds the 8 MiB record boundary"));
    }
    // serde_json keeps the last duplicate silently; detect duplicates with a streaming pass.
    let value: Value = serde_json::from_str(text).map_err(|error| ValidationError::new(error.to_string()))?;
    check(&value)?;
    detect_duplicate_keys(text)?;
    Ok(value)
}

/// Rejects JSON text that repeats a key inside one object (`Duplicate JSON key: <key>`).
pub fn detect_duplicate_keys(text: &str) -> Result<()> {
    // Minimal tokenizer: track object nesting and key sets per object.
    let bytes = text.as_bytes();
    let mut stack: Vec<Option<std::collections::HashSet<String>>> = Vec::new();
    let mut i = 0;
    let mut expect_key = false;
    while i < bytes.len() {
        match bytes[i] {
            b'{' => {
                stack.push(Some(std::collections::HashSet::new()));
                expect_key = true;
                i += 1;
            }
            b'[' => {
                stack.push(None);
                expect_key = false;
                i += 1;
            }
            b'}' | b']' => {
                stack.pop();
                expect_key = false;
                i += 1;
            }
            b',' => {
                expect_key = matches!(stack.last(), Some(Some(_)));
                i += 1;
            }
            b'"' => {
                let start = i + 1;
                let mut j = start;
                while j < bytes.len() && bytes[j] != b'"' {
                    if bytes[j] == b'\\' {
                        j += 1;
                    }
                    j += 1;
                }
                let raw = &text[start..j.min(bytes.len())];
                if expect_key {
                    if let Some(Some(keys)) = stack.last_mut() {
                        let key: String = serde_json::from_str(&format!("\"{raw}\""))
                            .map_err(|error| ValidationError::new(error.to_string()))?;
                        if !keys.insert(key.clone()) {
                            return Err(ValidationError::new(format!("Duplicate JSON key: {key}")));
                        }
                    }
                    expect_key = false;
                }
                i = j + 1;
            }
            _ => i += 1,
        }
    }
    Ok(())
}
