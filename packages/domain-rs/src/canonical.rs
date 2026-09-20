//! Canonical JSON commitments identical to `forecast_domain.serialization`.
//!
//! Rules: object keys sorted by code point, separators `,` and `:`, non-ASCII emitted raw by
//! default (`canonical_bytes`) and escaped by `python_json_bytes`,
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

/// `json.dumps(value, sort_keys=True, separators=(",", ":"))` with Python's **default**
/// `ensure_ascii=True` — the sibling of [`canonical_bytes`], not a replacement for it.
///
/// Python's default escapes every non-ASCII character as `\uXXXX`, using a surrogate pair above
/// the BMP; `serde_json` writes the character's UTF-8 bytes. The reference uses *both*, and the
/// difference is one keyword argument there and a load-bearing distinction here:
///
///   * A **commitment** (`forecast_domain.serialization.canonical_bytes`, and the `sort_keys`
///     calls that pass `ensure_ascii=False`) drops the escapes, so one text has one byte string
///     and one hash no matter how it is written.
///   * An **audit body**, and the hashes that key rows — `wallet_login`, `markets._hash`,
///     `points`, `resolution_timing`'s proof hashes, `participation_holds`' request hash — keep
///     the escapes, so the stored record is printable ASCII and a profile name with an accent
///     cannot change the row's bytes.
///
/// Picking the wrong one is invisible until the first value they disagree on, and then it is a
/// different digest for the same event rather than a visible failure.
pub fn python_json_bytes<T: Serialize + ?Sized>(value: &T) -> Result<Vec<u8>> {
    Ok(ensure_ascii(&canonical_bytes(value)?))
}

/// Python's `ensure_ascii` over already-valid JSON text.
///
/// The rule is *not* "escape the non-ASCII characters": it is "escape everything outside
/// `0x20..=0x7e`", so DEL is escaped as `\u007f` even though it is ASCII — and `serde_json` leaves
/// it raw. Above the BMP the escape is a surrogate pair, which is why this goes through UTF-16.
///
/// Walking the text rather than parsing it is sound because every structural character of JSON is
/// printable ASCII: a character outside that range can only be inside a string literal, and
/// `canonical_bytes` has already turned the control characters into escapes this pass leaves alone.
pub fn ensure_ascii(bytes: &[u8]) -> Vec<u8> {
    let text = String::from_utf8_lossy(bytes);
    let mut out = String::with_capacity(text.len());
    for character in text.chars() {
        if (0x20..=0x7e).contains(&(character as u32)) {
            out.push(character);
            continue;
        }
        let mut units = [0u16; 2];
        for unit in character.encode_utf16(&mut units) {
            out.push_str(&format!("\\u{unit:04x}"));
        }
    }
    out.into_bytes()
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/canonical-json-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("canonical json golden")).expect("json")
    }

    #[test]
    fn both_encoding_rules_match_python() {
        // The reference uses `json.dumps(..., sort_keys=True, separators=(",", ":"))` with
        // `ensure_ascii` both ways, and the two agree on every ASCII value — which is exactly why
        // a port can pick the wrong one and pass a whole suite before the first accent arrives.
        let document = golden();
        for case in document["cases"].as_array().expect("cases") {
            let name = case["name"].as_str().unwrap();
            let value = &case["value"];
            assert_eq!(
                String::from_utf8(canonical_bytes(value).expect("canonical")).unwrap(),
                case["raw"].as_str().unwrap(),
                "{name}: the commitment rule"
            );
            assert_eq!(
                String::from_utf8(python_json_bytes(value).expect("escaped")).unwrap(),
                case["escaped"].as_str().unwrap(),
                "{name}: the default ensure_ascii rule"
            );
            // `forecast_domain.dumps` is the commitment rule reached the other way; it has to
            // agree with `canonical_bytes` or there are two rules where there should be one.
            assert_eq!(
                String::from_utf8(canonical_bytes(&value).expect("canonical")).unwrap(),
                case["commitment"].as_str().unwrap(),
                "{name}: the commitment rule through the domain helper"
            );
        }
    }

    #[test]
    fn the_escaped_rule_covers_ascii_it_is_easy_to_miss() {
        // `ensure_ascii` is not "escape the non-ASCII characters": the rule is everything outside
        // `0x20..=0x7e`, so DEL is escaped too — and `serde_json` leaves it raw. An astral
        // character becomes two escapes, which is what going through UTF-16 buys.
        assert_eq!(
            String::from_utf8(python_json_bytes(&json!({"t": "\u{7f}"})).unwrap()).unwrap(),
            r#"{"t":"\u007f"}"#
        );
        assert_eq!(
            String::from_utf8(python_json_bytes(&json!({"t": "\u{1f600}"})).unwrap()).unwrap(),
            r#"{"t":"\ud83d\ude00"}"#
        );
        assert_eq!(
            String::from_utf8(python_json_bytes(&json!("plain")).unwrap()).unwrap(),
            r#""plain""#
        );
    }
}
