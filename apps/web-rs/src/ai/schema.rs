//! The structural contract every model answer has to satisfy before anything reads it.
//!
//! This is a validator rather than a deserializer on purpose. `serde` would tell a caller which
//! field it did not like; this has to tell the *reference's* caller, in the reference's words,
//! and it has to refuse the same inputs — a duplicate key, a non-integer number, a field set
//! that is close but not exact. A permissive parse would accept a contract change silently.

use serde_json::{json, Map, Value};

pub const MAX_MODEL_OUTPUT_BYTES: usize = 64_000;
const MAX_DEPTH: usize = 24;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SchemaError {
    pub message: &'static str,
    pub code: &'static str,
}

impl SchemaError {
    fn new(message: &'static str, code: &'static str) -> Self {
        Self { message, code }
    }
}

/// `_object`: every named property is required and nothing else is allowed.
pub fn object(properties: Vec<(&str, Value)>) -> Value {
    let mut fields = Map::new();
    for (name, schema) in &properties {
        fields.insert((*name).to_string(), schema.clone());
    }
    json!({
        "type": "object",
        "properties": Value::Object(fields),
        "required": properties.iter().map(|(name, _)| json!(name)).collect::<Vec<Value>>(),
        "additionalProperties": false,
    })
}

pub fn string_schema(min_length: usize, max_length: usize) -> Value {
    json!({"type": "string", "minLength": min_length, "maxLength": max_length})
}

pub fn integer_schema(minimum: i64, maximum: i64) -> Value {
    json!({"type": "integer", "minimum": minimum, "maximum": maximum})
}

/// The shapes the coordinator asks models for. They are functions because two of them are
/// parameterised by a value the model has to echo back exactly.
pub mod schemas {
    use super::*;

    pub fn ambiguity() -> Value {
        object(vec![
            ("objectively_resolvable", json!({"type": "boolean"})),
            ("ambiguity_passed", json!({"type": "boolean"})),
            ("sources_appropriate", json!({"type": "boolean"})),
            ("english_language_passed", json!({"type": "boolean"})),
            ("intent_preserved", json!({"type": "boolean"})),
            ("title_consistent", json!({"type": "boolean"})),
            ("explanation", string_schema(1, 4000)),
        ])
    }

    pub fn duplicate() -> Value {
        object(vec![
            ("check_completed", json!({"type": "boolean"})),
            ("candidates_accurate", json!({"type": "boolean"})),
            ("explanation", string_schema(1, 4000)),
        ])
    }

    pub fn source() -> Value {
        object(vec![
            ("verified", json!({"type": "boolean"})),
            ("explanation", string_schema(1, 4000)),
        ])
    }

    pub fn resolution() -> Value {
        object(vec![
            (
                "proposed_outcome",
                json!({"type": "string", "enum": ["YES", "NO", "INVALID"]}),
            ),
            ("confidence_bp", integer_schema(0, 10000)),
            ("rule_matches", strings(12)),
            ("rule_conflicts", strings(12)),
            ("reason_summary", string_schema(1, 4000)),
            (
                "conflict_status",
                json!({"type": "string", "enum": ["CLEAR", "UNRESOLVED"]}),
            ),
            (
                "conflict_explanation",
                json!({"anyOf": [string_schema(1, 4000), {"type": "null"}]}),
            ),
        ])
    }

    pub fn counter() -> Value {
        object(vec![
            ("agrees", json!({"type": "boolean"})),
            ("explanation", string_schema(1, 4000)),
        ])
    }

    pub fn dispute_evidence() -> Value {
        object(vec![
            ("evidence_validated", json!({"type": "boolean"})),
            ("explanation", string_schema(1, 4000)),
        ])
    }

    pub fn dispute_analysis() -> Value {
        object(vec![
            ("material_conflict", json!({"type": "boolean"})),
            ("reason_summary", string_schema(1, 4000)),
        ])
    }

    pub fn ai_forecast() -> Value {
        object(vec![
            ("yesProbabilityBp", integer_schema(0, 10000)),
            ("rationale", string_schema(1, 4000)),
        ])
    }

    pub const EARLY_COUNTER_EXPLANATION_MAX: usize = 1000;

    /// The early counter-review has to echo the time binding it was given, so the answer is
    /// pinned to the exact basis and instant rather than to a timestamp it chose.
    pub fn early_counter(basis: &str, event_not_after_ms: i64) -> Value {
        object(vec![
            ("agrees", json!({"type": "boolean"})),
            ("explanation", string_schema(1, EARLY_COUNTER_EXPLANATION_MAX)),
            ("event_time_basis", json!({"type": "string", "const": basis})),
            (
                "event_not_after_ms",
                json!({"type": "integer", "const": event_not_after_ms}),
            ),
        ])
    }

    /// The reference's `_STRINGS` sets no `minItems`: an empty list is allowed, and the schema
    /// the model is shown has to say so, because the schema is part of what is asked.
    fn strings(max_items: usize) -> Value {
        json!({"type": "array", "items": string_schema(1, 4000), "maxItems": max_items})
    }
}

/// Validate one value against one schema, in the reference's order of complaints.
pub fn validate_output(value: &Value, schema: &Value, depth: usize) -> Result<(), SchemaError> {
    if depth > MAX_DEPTH {
        return Err(SchemaError::new("AI output exceeded structural depth", "ai_rejected"));
    }
    if let Some(options) = schema.get("anyOf").and_then(Value::as_array) {
        for option in options {
            if validate_output(value, option, depth + 1).is_ok() {
                return Ok(());
            }
        }
        return Err(SchemaError::new(
            "AI output does not match its nullable contract",
            "ai_rejected",
        ));
    }
    let kind = schema.get("type").and_then(Value::as_str);
    let matches_kind = match kind {
        Some("object") => value.is_object(),
        Some("array") => value.is_array(),
        Some("string") => value.is_string(),
        // `type(value) is not int` in the reference: a boolean is not an integer here, and
        // neither is a float, even when it happens to be whole. Python integers are arbitrary
        // precision, so a literal too large for serde is still an integer — and still out of
        // range, which is a different complaint from being the wrong type.
        Some("integer") => value.as_i64().is_some() || value.as_u64().is_some() || integer_literal(value) == Some(true),
        Some("boolean") => value.is_boolean(),
        Some("null") => value.is_null(),
        _ => true,
    };
    if kind.is_some() && !matches_kind {
        return Err(SchemaError::new(
            "AI output contains a field of the wrong type",
            "ai_output_type",
        ));
    }
    if let Some(options) = schema.get("enum").and_then(Value::as_array) {
        if !options.contains(value) {
            return Err(SchemaError::new(
                "AI output contains an unknown choice",
                "ai_output_enum",
            ));
        }
    }
    if let Some(constant) = schema.get("const") {
        if std::mem::discriminant(constant) != std::mem::discriminant(value) || constant != value {
            return Err(SchemaError::new(
                "AI output changed the contract version",
                "ai_rejected",
            ));
        }
    }
    match kind {
        Some("object") => {
            let Some(properties) = schema["properties"].as_object() else {
                return Ok(());
            };
            let Some(fields) = value.as_object() else { return Ok(()) };
            let expected: std::collections::BTreeSet<&String> = properties.keys().collect();
            let actual: std::collections::BTreeSet<&String> = fields.keys().collect();
            if expected != actual {
                return Err(SchemaError::new(
                    "AI output has missing or unexpected fields",
                    "ai_output_fields",
                ));
            }
            for (name, child) in properties {
                validate_output(&fields[name], child, depth + 1)?;
            }
        }
        Some("array") => {
            let Some(items) = value.as_array() else { return Ok(()) };
            let minimum = schema.get("minItems").and_then(Value::as_u64).unwrap_or(0) as usize;
            let maximum = schema.get("maxItems").and_then(Value::as_u64).unwrap_or(1000) as usize;
            if !(minimum..=maximum).contains(&items.len()) {
                return Err(SchemaError::new(
                    "AI output has too many or too few list entries",
                    "ai_rejected",
                ));
            }
            for child in items {
                validate_output(child, &schema["items"], depth + 1)?;
            }
        }
        Some("string") => {
            // Length is counted in characters, as the reference counts a Python string.
            let Some(text) = value.as_str() else { return Ok(()) };
            let minimum = schema.get("minLength").and_then(Value::as_u64).unwrap_or(0) as usize;
            let maximum = schema.get("maxLength").and_then(Value::as_u64).unwrap_or(16000) as usize;
            if !(minimum..=maximum).contains(&text.chars().count()) {
                return Err(SchemaError::new(
                    "AI output text is outside its permitted length",
                    "ai_output_text_length",
                ));
            }
        }
        Some("integer") => {
            let Some(number) = value.as_i64().or_else(|| value.as_u64().map(|value| value as i64)) else {
                // An integer literal serde could not hold is out of range by definition.
                if integer_literal(value) == Some(true) {
                    return Err(SchemaError::new(
                        "AI output numeric value is outside its permitted range",
                        "ai_output_range",
                    ));
                }
                return Ok(());
            };
            let minimum = schema.get("minimum").and_then(Value::as_i64).unwrap_or(0);
            let maximum = schema
                .get("maximum")
                .and_then(Value::as_i64)
                .unwrap_or(9_007_199_254_740_991);
            if !(minimum..=maximum).contains(&number) {
                return Err(SchemaError::new(
                    "AI output numeric value is outside its permitted range",
                    "ai_output_range",
                ));
            }
        }
        _ => {}
    }
    Ok(())
}

/// Whether a number serde could not represent is written as an integer at all.
fn integer_literal(value: &Value) -> Option<bool> {
    let text = value.as_number()?.to_string();
    Some(!text.contains('.') && !text.contains('e') && !text.contains('E'))
}

#[cfg(test)]
mod golden {
    use super::*;
    use std::path::PathBuf;

    fn corpus() -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-schema-golden.json");
        serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{} is missing: {error}", path.display())),
        )
        .expect("golden JSON")
    }

    fn built(name: &str) -> Value {
        match name {
            "ambiguity" => schemas::ambiguity(),
            "duplicate" => schemas::duplicate(),
            "source" => schemas::source(),
            "resolution" => schemas::resolution(),
            "counter" => schemas::counter(),
            "dispute_evidence" => schemas::dispute_evidence(),
            "dispute_analysis" => schemas::dispute_analysis(),
            "ai_forecast" => schemas::ai_forecast(),
            "early_counter" => schemas::early_counter("published_instant", 1000),
            other => panic!("the corpus has a schema this does not build: {other}"),
        }
    }

    #[test]
    fn the_schemas_are_the_ones_the_reference_uses() {
        // Not only the validator: the shapes themselves have to be the reference's, or a model
        // is asked for something slightly different and the difference is invisible until it
        // answers.
        let corpus = corpus();
        for (name, expected) in corpus["schemas"].as_object().expect("schemas") {
            assert_eq!(&built(name), expected, "the {name} schema has drifted");
        }
    }

    #[test]
    fn the_validator_decides_the_same_as_python() {
        let corpus = corpus();
        let mut wrong = Vec::new();
        for case in corpus["cases"].as_array().expect("cases") {
            let name = case[0].as_str().expect("name");
            let value = &case[1];
            let accepted = case[2].as_bool().expect("accepted");
            let expected = case[3].as_str();
            let got = validate_output(value, &built(name), 0);
            let matches = match (&got, accepted) {
                (Ok(()), true) => true,
                (Err(error), false) => Some(error.code) == expected,
                _ => false,
            };
            if !matches && wrong.len() < 8 {
                wrong.push(format!(
                    "{name}: {value} -> {got:?}, Python said accepted={accepted} code={expected:?}"
                ));
            }
        }
        assert!(
            wrong.is_empty(),
            "the validator disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_field_set_has_to_be_exact() {
        let schema = schemas::source();
        assert!(validate_output(&json!({"verified": true, "explanation": "x"}), &schema, 0).is_ok());
        // Missing and extra are both refused; "close enough" is how a contract changes silently.
        assert_eq!(
            validate_output(&json!({"verified": true}), &schema, 0)
                .unwrap_err()
                .code,
            "ai_output_fields"
        );
        assert_eq!(
            validate_output(&json!({"verified": true, "explanation": "x", "extra": 1}), &schema, 0)
                .unwrap_err()
                .code,
            "ai_output_fields"
        );
    }

    #[test]
    fn a_boolean_is_not_an_integer() {
        // `type(value) is not int` — in Python `True` is an int, but the schema says boolean,
        // and in Rust `as_i64` on a boolean is None. Both refuse; this records which.
        let schema = integer_schema(0, 10000);
        assert!(validate_output(&json!(0), &schema, 0).is_ok());
        assert!(validate_output(&json!(10000), &schema, 0).is_ok());
        assert_eq!(
            validate_output(&json!(10001), &schema, 0).unwrap_err().code,
            "ai_output_range"
        );
        assert_eq!(
            validate_output(&json!(true), &schema, 0).unwrap_err().code,
            "ai_output_type"
        );
        assert_eq!(
            validate_output(&json!(1.5), &schema, 0).unwrap_err().code,
            "ai_output_type"
        );
        // An integer literal too large for serde is out of range rather than mistyped — but
        // only while serde can still see that it was an integer. Past u64 it becomes a float and
        // the distinction is lost. That case cannot reach here in production: `strict_json`
        // refuses every non-integer number before this runs.
        let large: serde_json::Value = serde_json::from_str("99999999999999999999").unwrap();
        assert_eq!(validate_output(&large, &schema, 0).unwrap_err().code, "ai_output_type");
    }

    #[test]
    fn an_enum_and_a_const_are_both_binding() {
        let schema = schemas::resolution();
        let base = json!({"proposed_outcome": "INVALID", "confidence_bp": 9000, "rule_matches": [],
                          "rule_conflicts": [], "reason_summary": "x", "conflict_status": "CLEAR",
                          "conflict_explanation": null});
        assert!(validate_output(&base, &schema, 0).is_ok());
        let mut unknown = base.clone();
        unknown["proposed_outcome"] = json!("MAYBE");
        assert_eq!(
            validate_output(&unknown, &schema, 0).unwrap_err().code,
            "ai_output_enum"
        );

        let pinned = schemas::early_counter("published_instant", 1000);
        assert!(validate_output(
            &json!({"agrees": true, "explanation": "x", "event_time_basis": "published_instant", "event_not_after_ms": 1000}),
            &pinned, 0
        )
        .is_ok());
        let moved = json!({"agrees": true, "explanation": "x", "event_time_basis": "published_instant", "event_not_after_ms": 1001});
        // The reference leaves this message without a code, so it takes the default.
        assert_eq!(validate_output(&moved, &pinned, 0).unwrap_err().code, "ai_rejected");
    }

    #[test]
    fn a_nullable_field_accepts_null_and_a_string_but_not_a_number() {
        let schema = schemas::resolution();
        let mut value = json!({"proposed_outcome": "YES", "confidence_bp": 9000, "rule_matches": ["a"],
                               "rule_conflicts": [], "reason_summary": "x", "conflict_status": "CLEAR",
                               "conflict_explanation": "why"});
        assert!(validate_output(&value, &schema, 0).is_ok());
        value["conflict_explanation"] = json!(null);
        assert!(validate_output(&value, &schema, 0).is_ok());
        value["conflict_explanation"] = json!(7);
        assert_eq!(validate_output(&value, &schema, 0).unwrap_err().code, "ai_rejected");
    }

    #[test]
    fn a_list_is_bounded_at_both_ends() {
        let schema = json!({"type": "array", "items": string_schema(1, 10), "minItems": 1, "maxItems": 2});
        assert!(validate_output(&json!(["a"]), &schema, 0).is_ok());
        assert_eq!(validate_output(&json!([]), &schema, 0).unwrap_err().code, "ai_rejected");
        assert_eq!(
            validate_output(&json!(["a", "b", "c"]), &schema, 0).unwrap_err().code,
            "ai_rejected"
        );
    }
}
