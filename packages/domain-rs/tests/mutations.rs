//! Differential check against Python: 1,686 single-field mutations of the golden records must
//! produce the same accept/reject verdict, and every accepted record the same content hash.
//! Error strings are compared only for the semantic `validate()` rules (shared wording).

use std::path::PathBuf;

use serde_json::Value;

use forecast_domain::content_hash;
use forecast_domain::risk_feed::{
    CanonicalRiskDefinitionV2, Record, RiskFeedSeriesV2, RiskMappingProfileV2, SignedRiskFeed, SignedRiskFeedV2,
};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn load(name: &str) -> Value {
    let text = std::fs::read_to_string(repo_root().join("tests/golden").join(name)).expect("golden file");
    serde_json::from_str(&text).expect("golden JSON")
}

fn sample<'a>(golden: &'a Value, record: &str) -> &'a Value {
    match record {
        "SignedRiskFeed" => &golden["v1"]["envelope"],
        "SignedRiskFeedV2" => &golden["v2"]["envelope"],
        "CanonicalRiskDefinitionV2" => &golden["v2"]["definition"],
        "RiskMappingProfileV2" => &golden["v2"]["profile"],
        "RiskFeedSeriesV2" => &golden["v2"]["series"],
        other => panic!("unknown record {other}"),
    }
}

fn apply(mut doc: Value, case: &Value) -> Value {
    let path: Vec<&str> = case["path"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p.as_str().unwrap())
        .collect();
    let mut node = &mut doc;
    for key in &path[..path.len() - 1] {
        node = match node {
            Value::Array(items) => &mut items[key.parse::<usize>().unwrap()],
            Value::Object(map) => map.get_mut(*key).unwrap(),
            _ => unreachable!(),
        };
    }
    let last = path[path.len() - 1];
    match (node, case["op"].as_str().unwrap()) {
        (Value::Object(map), "delete") => {
            map.remove(last);
        }
        (Value::Object(map), "set") => {
            map.insert(last.to_string(), case["value"].clone());
        }
        (Value::Array(items), "delete") => {
            items.remove(last.parse::<usize>().unwrap());
        }
        (Value::Array(items), "set") => items[last.parse::<usize>().unwrap()] = case["value"].clone(),
        _ => unreachable!(),
    }
    doc
}

fn decode(record: &str, doc: Value) -> Result<String, String> {
    fn run<T: Record>(doc: Value) -> Result<String, String> {
        let decoded = T::from_value(doc).map_err(|e| e.to_string())?;
        content_hash(&decoded).map_err(|e| e.to_string())
    }
    match record {
        "SignedRiskFeed" => run::<SignedRiskFeed>(doc),
        "SignedRiskFeedV2" => run::<SignedRiskFeedV2>(doc),
        "CanonicalRiskDefinitionV2" => run::<CanonicalRiskDefinitionV2>(doc),
        "RiskMappingProfileV2" => run::<RiskMappingProfileV2>(doc),
        "RiskFeedSeriesV2" => run::<RiskFeedSeriesV2>(doc),
        other => panic!("unknown record {other}"),
    }
}

#[test]
fn every_python_verdict_is_reproduced() {
    let golden = load("risk-feed-golden.json");
    let vectors = load("risk-feed-mutations.json");
    let cases = vectors["cases"].as_array().unwrap();
    assert!(cases.len() > 1000);
    let mut mismatches = Vec::new();
    let mut semantic_wording = 0usize;
    for case in cases {
        let record = case["record"].as_str().unwrap();
        let doc = apply(sample(&golden, record).clone(), case);
        let outcome = decode(record, doc);
        let expected_ok = case["ok"].as_bool().unwrap();
        match (&outcome, expected_ok) {
            (Ok(hash), true) => {
                if hash != case["hash"].as_str().unwrap() {
                    mismatches.push(format!("{record} {:?}: hash differs", case["path"]));
                }
            }
            (Err(error), false) => {
                let expected = case["error"].as_str().unwrap();
                // Semantic rules share exact wording; structural/field errors share the path prefix.
                if !expected.contains(':') && error != expected {
                    mismatches.push(format!(
                        "{record} {:?}: wording {error:?} vs {expected:?}",
                        case["path"]
                    ));
                } else if !expected.contains(':') {
                    semantic_wording += 1;
                }
            }
            (Ok(_), false) => mismatches.push(format!(
                "{record} {:?} {}: Rust accepted, Python rejected ({})",
                case["path"], case["op"], case["error"]
            )),
            (Err(error), true) => mismatches.push(format!(
                "{record} {:?} {}: Rust rejected ({error}), Python accepted",
                case["path"], case["op"]
            )),
        }
    }
    assert!(
        mismatches.is_empty(),
        "{} mismatches:\n{}",
        mismatches.len(),
        mismatches.join("\n")
    );
    assert!(semantic_wording > 20, "semantic rules exercised");
}
