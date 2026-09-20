//! The contract the compiler model is shown, and the candidates it may refer to by name.
//!
//! The schema is not the domain's schema. Three substitutions are made because the model cannot
//! be trusted with the arithmetic the domain performs, and because asking it to repeat a
//! 64-character hash is asking it to invent one:
//!
//!   - `close_at_ms` is replaced by `close_at_utc`, stated as a human-readable instant. The
//!     reference performs all date arithmetic itself and keeps the existing record schema;
//!     a model that has to convert to epoch milliseconds gets it wrong.
//!   - the verified `forecast_id`/`specification_hash` pair on a duplicate candidate is replaced
//!     by a bounded `candidate_ref` such as `c0`. The adapter substitutes the real identity back
//!     after the fact, so a model cannot assert a similarity to a forecast it was not shown.
//!   - `pattern` and `uniqueItems` are dropped and `$ref`s flattened, because the providers'
//!     structured-output modes reject or silently ignore them, and a constraint that is dropped
//!     in transit is worse than one that was never stated.
//!
//! The schema is built from the committed `schemas/v1` rendering rather than from a copy, so a
//! change to the domain contract reaches the model or fails the build.

use serde_json::{json, Map, Value};

use forecast_domain::lifecycle::Forecast;

use forecast_domain::Record;

use super::coordinator::CoordinatorError;

/// `compiler-utc-candidate-ref-v3`.
pub const COMPILER_WIRE_VERSION: &str = "compiler-utc-candidate-ref-v3";
pub const MAX_CANDIDATES: usize = 40;
pub const MAX_CANDIDATE_CONTEXT_BYTES: usize = 128 * 1024;

const SPEC_SCHEMA: &str = include_str!("../../../../schemas/v1/ForecastSpecification.schema.json");

fn refused(code: &str, message: &str) -> CoordinatorError {
    CoordinatorError::Rejected {
        code: code.to_string(),
        message: message.to_string(),
        artifacts: Vec::new(),
    }
}

/// Resolve a `$ref` against `$defs`, dropping the keywords the providers' structured output
/// cannot honour and turning a `const` into the single-valued `enum` they can.
fn inline(node: &Value, definitions: &Value) -> Value {
    match node {
        Value::Object(fields) => {
            if let Some(reference) = fields.get("$ref").and_then(Value::as_str) {
                let name = reference.rsplit('/').next().unwrap_or_default();
                return inline(&definitions[name], definitions);
            }
            let mut result = Map::new();
            for (key, value) in fields {
                if key == "pattern" || key == "uniqueItems" {
                    continue;
                }
                result.insert(key.clone(), inline(value, definitions));
            }
            if let Some(constant) = result.remove("const") {
                // `type(constant) is int` in the reference, so a boolean constant is spelled as
                // a string. Booleans are not integers here.
                let kind = if constant.is_i64() || constant.is_u64() {
                    "integer"
                } else {
                    "string"
                };
                result.insert("type".to_string(), json!(kind));
                result.insert("enum".to_string(), json!([constant]));
            }
            Value::Object(result)
        }
        Value::Array(items) => Value::Array(items.iter().map(|item| inline(item, definitions)).collect()),
        other => other.clone(),
    }
}

fn order(definitions: &Value, name: &str) -> Vec<String> {
    definitions[name]["required"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

/// `_spec_schema`: what the compiler model is asked to produce.
///
/// `candidate_count` bounds both `maxItems` and the `candidate_ref` enum. Python removes
/// `maxItems` again for Gemini because that provider cannot expand bounded array state; that is
/// `gemini_compiler_schema`'s job, and only what the provider is shown changes.
pub fn spec_schema(candidate_count: usize) -> Value {
    let document: Value = serde_json::from_str(SPEC_SCHEMA).expect("the committed schema parses");
    let definitions = &document["$defs"];
    let mut result = inline(&definitions["ForecastSpecification"], definitions);

    let spec_order = order(definitions, "ForecastSpecification");
    let duplicate_order: Vec<String> = order(definitions, "DuplicateCandidate")
        .into_iter()
        .filter(|name| name != "forecast_id" && name != "specification_hash")
        .collect();

    let properties = result["properties"]
        .as_object_mut()
        .expect("the specification has properties");
    properties.remove("close_at_ms");
    properties.insert(
        "close_at_utc".to_string(),
        json!({"type": "string", "minLength": 20, "maxLength": 20,
               "description": "Exact deadline YYYY-MM-DDTHH:MM:SSZ (UTC), repeated verbatim in canonical question and YES/NO rules."}),
    );
    properties.insert(
        "compiler_wire_version".to_string(),
        json!({"type": "string", "enum": [COMPILER_WIRE_VERSION]}),
    );
    if let Some(title) = properties.get_mut("share_title").and_then(Value::as_object_mut) {
        title.insert("maxLength".to_string(), json!(60));
        title.insert(
            "description".to_string(),
            json!(
                "Concise timeless English question: no deadline, date, year-end, before/by date, \
                   this/next year or other timeframe phrase. Preserve product names and model numbers."
            ),
        );
    }

    let duplicates = properties
        .get_mut("duplicate_candidates")
        .and_then(Value::as_object_mut)
        .expect("the specification has duplicate candidates");
    duplicates.insert("maxItems".to_string(), json!(MAX_CANDIDATES.min(candidate_count)));
    let fields = duplicates["items"]["properties"]
        .as_object_mut()
        .expect("duplicate fields");
    fields.remove("forecast_id");
    fields.remove("specification_hash");
    // A bounded short reference rather than the identity pair: the adapter restores the real
    // one after the fact, so the model never sees a hash it could copy or invent.
    let mut reference = json!({"type": "string", "minLength": 2, "maxLength": 3});
    if candidate_count > 0 {
        reference["enum"] = json!((0..candidate_count)
            .map(|index| format!("c{index}"))
            .collect::<Vec<String>>());
    }
    fields.insert("candidate_ref".to_string(), reference);
    let mut item_order = duplicate_order;
    item_order.push("candidate_ref".to_string());
    duplicates["items"]["required"] = json!(item_order);

    // The reference sets `required` from the property order it built, which is the dataclass
    // field order with the two substituted fields appended. `Map` here is sorted, so the order
    // is restored explicitly from the committed file's own `required`.
    let mut required: Vec<String> = spec_order.into_iter().filter(|name| name != "close_at_ms").collect();
    required.push("close_at_utc".to_string());
    required.push("compiler_wire_version".to_string());
    result["required"] = json!(required);
    result
}

/// `_candidate_context`: the candidates a compiler run may refer to, by reference.
///
/// The verified identity and hash travel alongside the specification so that they can be
/// substituted back after the model has answered, and so that a reference cannot be resolved to
/// a forecast whose specification does not hash to what it claims.
pub fn candidate_context(candidates: &[Forecast]) -> Result<Vec<Value>, CoordinatorError> {
    if candidates.len() > MAX_CANDIDATES {
        return Err(refused(
            "ai_rejected",
            "Duplicate candidate set exceeds the validated search boundary",
        ));
    }
    let mut seen = Vec::new();
    for candidate in candidates {
        if seen.contains(&candidate.forecast_id) {
            return Err(refused(
                "compiler_candidate_context",
                "Duplicate candidate identities are ambiguous",
            ));
        }
        seen.push(candidate.forecast_id.clone());
    }
    let mut result = Vec::new();
    for (index, candidate) in candidates.iter().enumerate() {
        // A candidate whose specification does not hash to the hash it publishes is not a
        // candidate; it is a record someone has edited.
        candidate.specification.validate().map_err(|_| {
            refused(
                "compiler_candidate_context",
                "Duplicate candidate context must contain canonical forecasts",
            )
        })?;
        let value = serde_json::to_value(&candidate.specification).unwrap_or(Value::Null);
        let digest = candidate.specification.specification_hash().map_err(|_| {
            refused(
                "compiler_candidate_context",
                "Candidate specification commitment mismatch",
            )
        })?;
        if digest != candidate.specification_hash {
            return Err(refused(
                "compiler_candidate_context",
                "Candidate specification commitment mismatch",
            ));
        }
        result.push(json!({
            "candidate_ref": format!("c{index}"),
            "forecast_id": candidate.forecast_id,
            "specification_hash": candidate.specification_hash,
            "specification": value,
        }));
    }
    Ok(result)
}

/// `_assert_candidate_context`: the candidates did not change while the model was thinking.
///
/// Recomputing the context after a call and comparing hashes is what makes the model's answer
/// refer to the forecasts that were actually shown, rather than to forecasts that have since
/// been edited underneath it.
pub fn assert_candidate_context(candidates: &[Forecast], expected: &str) -> Result<(), CoordinatorError> {
    let context = candidate_context(candidates)?;
    let digest = forecast_domain::content_hash(&Value::Array(context))
        .map_err(|_| refused("ai_rejected", "Duplicate candidate context could not be hashed"))?;
    if digest != expected {
        return Err(refused(
            "compiler_candidate_context_changed",
            "Duplicate candidate input changed during compilation",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod candidate_tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    /// The selection, replayed against the corpus the vector ranked rather than one rebuilt here:
    /// the ordering is the property, and a fixture that published its own questions would be
    /// ranking a different corpus.
    #[test]
    fn the_reference_candidate_selection_is_reproduced_query_for_query() {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/candidates-golden.json");
        let document: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).expect("candidates golden")).expect("json");
        let db = Sqlite::from_migrations();
        for table in ["users", "forecasts"] {
            for row in document["rows"][table].as_array().cloned().unwrap_or_default() {
                let fields = row.as_object().expect("a fixture row");
                let columns: Vec<&str> = fields.keys().map(String::as_str).collect();
                let placeholders = vec!["?"; columns.len()].join(",");
                let params: Vec<Value> = columns.iter().map(|name| fields[*name].clone()).collect();
                db.run(
                    &format!("INSERT INTO {table}({}) VALUES({placeholders})", columns.join(",")),
                    &params,
                )
                .unwrap_or_else(|error| panic!("{table}: {error}"));
            }
        }
        for entry in document["calls"].as_array().expect("calls") {
            let question = entry["input"]["question"].as_str().unwrap();
            let ranked = block(candidate_forecasts(&db, question)).expect("the selection");
            let identifiers: Vec<Value> = ranked.iter().map(|forecast| json!(forecast.forecast_id)).collect();
            assert_eq!(json!(identifiers), entry["result"], "{question:?}: a different corpus");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-compiler-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("compiler golden")).expect("json")
    }

    fn forecast(value: &Value) -> Forecast {
        forecast_domain::lifecycle::Snapshot::from_json(&value.to_string())
            .expect("golden forecast")
            .base()
            .clone()
    }

    #[test]
    fn the_contract_the_model_sees_is_the_reference_contract() {
        let document = golden();
        for count in ["0", "1", "3"] {
            assert_eq!(
                spec_schema(count.parse().unwrap()),
                document["spec_schema"][count],
                "the schema for {count} candidates differs"
            );
        }
    }

    #[test]
    fn a_candidate_is_offered_by_reference_and_its_identity_kept_aside() {
        let document = golden();
        let one = forecast(&document["candidate_forecasts"]["one"]);
        assert_eq!(
            candidate_context(&[one]).unwrap(),
            document["candidate_context"]["one"].as_array().unwrap().clone()
        );

        let first = forecast(&document["candidate_forecasts"]["two"][0]);
        let second = forecast(&document["candidate_forecasts"]["two"][1]);
        assert_eq!(
            candidate_context(&[first.clone(), second.clone()]).unwrap(),
            document["candidate_context"]["two"].as_array().unwrap().clone()
        );
        assert_eq!(
            candidate_context(&[]).unwrap(),
            document["candidate_context"]["empty"].as_array().unwrap().clone()
        );

        // The commitment: the same candidates hash the same, and a set that no longer matches the
        // hash taken before the call is refused rather than silently compiled against.
        let both = vec![first.clone(), second.clone()];
        let unchanged = document["commitment"][0].clone();
        assert!(unchanged["accepted"].as_bool().unwrap());
        assert!(assert_candidate_context(&both, unchanged["expected"].as_str().unwrap()).is_ok());
        let changed = document["commitment"][1].clone();
        assert!(!changed["accepted"].as_bool().unwrap());
        let error = assert_candidate_context(&both, changed["expected"].as_str().unwrap()).unwrap_err();
        assert_eq!(error.code(), Some(changed["code"].as_str().unwrap()));
    }
}

// ---------------------------------------------------------------- candidate selection

/// The words a question's terms are drawn from, and the ones that carry no signal.
const STOPWORDS: [&str; 8] = ["will", "the", "before", "after", "this", "that", "with", "and"];

/// `_candidate_forecasts`: rank across the corpus, then fetch only a bounded UTF-8 context.
///
/// Three orderings decide what the model is even allowed to see. The lexical shortlist ranks exact
/// normalized matches first, then term matches, then recency, then id — so a port that reordered
/// the `ORDER BY` would hand the compiler a different corpus. The byte budget is applied *twice*:
/// once against an estimate (`spec_bytes + 320`, which reserves the wrapper keys and commas) and
/// again against the encoded form, because the first pass avoids fetching hundreds of snapshots the
/// second pass would discard anyway. And the reserve of 2 accounts for the enclosing `[]`.
pub async fn candidate_forecasts(db: &dyn crate::db::Database, question: &str) -> Result<Vec<Forecast>, worker::Error> {
    use crate::db::{int, text};
    let normalized = crate::automation::casefold(question)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    // The reference's own alternation: ASCII words of three or more, or Hangul of two or more. A
    // `regex`-crate pattern cannot express the two scripts in one character class, so the scan is
    // explicit.
    let mut words: Vec<String> = Vec::new();
    let mut ascii = String::new();
    let mut hangul = String::new();
    let flush = |ascii: &mut String, hangul: &mut String, words: &mut Vec<String>| {
        if ascii.chars().count() >= 3 {
            words.push(ascii.clone());
        }
        if hangul.chars().count() >= 2 {
            words.push(hangul.clone());
        }
        ascii.clear();
        hangul.clear();
    };
    for character in normalized.chars() {
        if character.is_ascii_lowercase() || character.is_ascii_digit() {
            hangul.clear();
            ascii.push(character);
        } else if ('\u{ac00}'..='\u{d7a3}').contains(&character) {
            ascii.clear();
            hangul.push(character);
        } else {
            flush(&mut ascii, &mut hangul, &mut words);
        }
    }
    flush(&mut ascii, &mut hangul, &mut words);
    let mut terms: Vec<String> = words
        .into_iter()
        .filter(|word| !STOPWORDS.contains(&word.as_str()))
        .collect();
    terms.sort();
    terms.dedup();
    // `key=lambda word: (-len(word), word)`: longest first, then alphabetical.
    terms.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()).then(a.cmp(b)));
    terms.truncate(10);

    let mut ranking = "CASE WHEN normalized_question=? THEN 1000 ELSE 0 END".to_string();
    let mut params: Vec<Value> = vec![json!(normalized)];
    for term in &terms {
        ranking.push_str("+CASE WHEN normalized_question LIKE ? THEN 1 ELSE 0 END");
        params.push(json!(format!("%{term}%")));
    }
    let rows = db
        .all(
            &format!(
                "SELECT id,length(CAST(json_extract(snapshot,'$.specification') AS BLOB)) AS spec_bytes \
                 FROM forecasts ORDER BY ({ranking}) DESC,created_at DESC,id LIMIT 400"
            ),
            &params,
        )
        .await?;
    let mut chosen: Vec<String> = Vec::new();
    let mut reserved: usize = 2;
    for row in &rows {
        // Reserve wrapper keys and IDs and commas in addition to the full criteria.
        let size = int(row, "spec_bytes").unwrap_or(0).max(0) as usize + 320;
        if reserved + size > MAX_CANDIDATE_CONTEXT_BYTES {
            continue;
        }
        chosen.push(text(row, "id").unwrap_or("").to_string());
        reserved += size;
        if chosen.len() == MAX_CANDIDATES {
            break;
        }
    }
    if chosen.is_empty() {
        return Ok(Vec::new());
    }
    let placeholders = vec!["?"; chosen.len()].join(",");
    let records = db
        .all(
            &format!("SELECT id,snapshot FROM forecasts WHERE id IN ({placeholders})"),
            &chosen.iter().map(|id| json!(id)).collect::<Vec<_>>(),
        )
        .await?;
    let by_id: std::collections::BTreeMap<String, Forecast> = records
        .iter()
        .filter_map(|row| {
            let id = text(row, "id")?.to_string();
            Forecast::from_json(text(row, "snapshot")?)
                .ok()
                .map(|forecast| (id, forecast))
        })
        .collect();
    let mut result: Vec<Forecast> = Vec::new();
    let mut size: usize = 2;
    for identifier in &chosen {
        let Some(forecast) = by_id.get(identifier) else {
            continue;
        };
        let encoded = crate::source_watch::compact(&json!({
            "forecast_id": forecast.forecast_id,
            "specification_hash": forecast.specification_hash,
            "specification": serde_json::to_value(&forecast.specification).unwrap_or(Value::Null),
        }));
        // The reference's `size + len(encoded) + 1 <= MAX`, with the `+ 1` folded in: the
        // separator between two entries is part of the budget.
        if size + encoded.len() < MAX_CANDIDATE_CONTEXT_BYTES {
            result.push(forecast.clone());
            size += encoded.len() + 1;
        }
    }
    Ok(result)
}
