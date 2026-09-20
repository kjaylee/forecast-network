//! `_strict_json`: the boundary every model answer crosses before anything reads it.
//!
//! Three refusals that a permissive parse would not make, and each one has a reason:
//!
//! - A duplicate key. `json.loads` keeps the last and says nothing, so a model could send one
//!   answer twice and be read as having given the other.
//! - A non-integer number. Every numeric field in these contracts is a count or a basis point,
//!   and a float is a value that has been computed rather than chosen.
//! - Anything that is not an object at the top level.
//!
//! One difference from the reference, in the safe direction: a literal too large for a `u64` is
//! an arbitrary-precision integer to Python and a float to `serde_json`, so this refuses what the
//! reference accepts. Refusing an answer is the direction that cannot credit a reward.
//!
//! Measured rather than asserted: across 6,000 generated documents the two agree everywhere
//! except on documents containing such a literal, where this refuses and the reference does not.
//! The golden carries the flag that marks them, so the allowance cannot widen without a reader
//! seeing it.

use serde::de::{Deserializer, Error as DeError, MapAccess, SeqAccess, Visitor};
use serde::Deserialize;
use serde_json::{Map, Number, Value};
use std::fmt;

pub const MAX_MODEL_OUTPUT_BYTES: usize = 64_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JsonError {
    pub message: &'static str,
    pub code: &'static str,
}

/// Parse a model answer, or say which of the three boundaries it crossed.
pub fn strict_json(raw: &str) -> Result<Map<String, Value>, JsonError> {
    let oversize = JsonError {
        message: "AI output exceeds its structured response boundary",
        code: "ai_output_size",
    };
    if raw.len() > MAX_MODEL_OUTPUT_BYTES {
        return Err(oversize);
    }
    let malformed = JsonError {
        message: "AI returned malformed structured output",
        code: "ai_output_json",
    };
    let mut deserializer = serde_json::Deserializer::from_str(raw);
    let value = deserializer.deserialize_any(Strict).map_err(|_| malformed)?;
    // `json.loads` reads everything it is given, so trailing content is malformed rather than
    // ignored.
    deserializer.end().map_err(|_| malformed)?;
    match value {
        Value::Object(fields) => Ok(fields),
        _ => Err(malformed),
    }
}

struct Strict;

/// A nested value has to come back through the same visitor. `Value`'s own `Deserialize` would
/// resolve a duplicate key and accept a decimal, and the strictness would stop at the top level —
/// which is where the fields a reward is computed from do not live.
struct StrictValue(Value);

impl<'de> Deserialize<'de> for StrictValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(Strict).map(StrictValue)
    }
}

impl<'de> Visitor<'de> for Strict {
    type Value = Value;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a JSON value with no duplicate keys and no decimal numbers")
    }

    fn visit_bool<E: DeError>(self, value: bool) -> Result<Value, E> {
        Ok(Value::Bool(value))
    }

    fn visit_i64<E: DeError>(self, value: i64) -> Result<Value, E> {
        Ok(Value::Number(Number::from(value)))
    }

    fn visit_u64<E: DeError>(self, value: u64) -> Result<Value, E> {
        Ok(Value::Number(Number::from(value)))
    }

    /// Every numeric field in these contracts is a count or a basis point.
    fn visit_f64<E: DeError>(self, _value: f64) -> Result<Value, E> {
        Err(E::custom("non-integer numeric field"))
    }

    fn visit_str<E: DeError>(self, value: &str) -> Result<Value, E> {
        Ok(Value::String(value.to_string()))
    }

    fn visit_string<E: DeError>(self, value: String) -> Result<Value, E> {
        Ok(Value::String(value))
    }

    fn visit_none<E: DeError>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_unit<E: DeError>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut access: A) -> Result<Value, A::Error> {
        let mut items = Vec::new();
        while let Some(StrictValue(item)) = access.next_element::<StrictValue>()? {
            items.push(item);
        }
        Ok(Value::Array(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Value, A::Error> {
        let mut fields = Map::new();
        while let Some(key) = access.next_key::<String>()? {
            let StrictValue(value) = access.next_value::<StrictValue>()?;
            if fields.contains_key(&key) {
                return Err(DeError::custom("duplicate key"));
            }
            fields.insert(key, value);
        }
        Ok(Value::Object(fields))
    }
}

/// Parse the nested value a container holds. The `Value` type keeps the last duplicate key, so
/// the strictness has to come from the same visitor rather than from a second pass.
fn strict_value(raw: &str) -> Result<Value, serde_json::Error> {
    let mut deserializer = serde_json::Deserializer::from_str(raw);
    deserializer.deserialize_any(Strict)
}

/// A helper for tests and for callers that hold a value rather than its text.
pub fn strict_value_from(raw: &str) -> Result<Value, JsonError> {
    strict_value(raw).map_err(|_| JsonError {
        message: "AI returned malformed structured output",
        code: "ai_output_json",
    })
}

#[cfg(test)]
mod golden {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn the_boundary_decides_the_same_as_python() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-strict-golden.json");
        let corpus: Value = serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
        )
        .expect("golden JSON");
        let cases = corpus["cases"].as_array().expect("cases");
        assert!(cases.len() > 500, "the corpus must actually be there");
        let mut wrong = Vec::new();
        for case in cases {
            let raw = case[0].as_str().expect("raw");
            let accepted = case[1].as_bool().expect("accepted");
            let expected = case[2].as_str();
            let wide = case[3].as_bool().expect("wide");
            let got = strict_json(raw);
            let matches = match (&got, accepted) {
                (Ok(_), true) => true,
                (Err(error), false) => Some(error.code) == expected,
                // The one documented difference, and only where it is documented.
                (Err(error), true) => wide && error.code == "ai_output_json",
                _ => false,
            };
            if !matches && wrong.len() < 8 {
                let raw = if raw.len() > 60 { &raw[..60] } else { raw };
                wrong.push(format!(
                    "{raw:?} -> {got:?}, Python said accepted={accepted} code={expected:?}"
                ));
            }
        }
        assert!(
            wrong.is_empty(),
            "the boundary disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_object_of_counts_and_prose_is_accepted() {
        let value = strict_json(r#"{"a": 1, "b": "x", "c": [true, null], "d": {"e": 0}}"#).unwrap();
        assert_eq!(value["a"], 1);
        assert_eq!(value["c"], serde_json::json!([true, null]));
    }

    #[test]
    fn a_duplicate_key_is_refused_rather_than_resolved() {
        // `json.loads` keeps the last one silently, which would read one answer as another.
        assert_eq!(strict_json(r#"{"a": 1, "a": 2}"#).unwrap_err().code, "ai_output_json");
        assert_eq!(
            strict_json(r#"{"o": {"a": 1, "a": 2}}"#).unwrap_err().code,
            "ai_output_json"
        );
    }

    #[test]
    fn a_decimal_number_is_refused_anywhere_it_appears() {
        for raw in [r#"{"a": 1.5}"#, r#"{"a": [1, 2.0]}"#, r#"{"o": {"a": 1e5}}"#] {
            assert_eq!(strict_json(raw).unwrap_err().code, "ai_output_json", "{raw}");
        }
        // An integer is not a decimal, however it is written.
        assert!(strict_json(r#"{"a": -1, "b": 0}"#).is_ok());
    }

    #[test]
    fn a_non_object_or_oversized_answer_is_refused() {
        assert_eq!(strict_json("[1, 2]").unwrap_err().code, "ai_output_json");
        assert_eq!(strict_json("").unwrap_err().code, "ai_output_json");
        assert_eq!(strict_json(r#"{"a": 1} trailing"#).unwrap_err().code, "ai_output_json");
        let big = format!(r#"{{"a": "{}"}}"#, "x".repeat(MAX_MODEL_OUTPUT_BYTES));
        assert_eq!(strict_json(&big).unwrap_err().code, "ai_output_size");
    }
}
