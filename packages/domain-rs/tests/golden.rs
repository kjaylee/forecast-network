//! Parity with the Python reference: canonical bytes, content hashes, signing bytes and
//! Ed25519 signatures must match the vectors Python exported to `tests/golden/`, and the
//! same malformed inputs must be rejected. The published JSON schemas remain the authority
//! for field sets.

use std::collections::BTreeSet;
use std::path::PathBuf;

use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use forecast_domain::risk_feed::{
    freshness_as_of_ms, signing_bytes, signing_bytes_v2, CanonicalRiskDefinitionV2, Record, RiskFeedPayload,
    RiskFeedPayloadV2, RiskFeedSeriesV2, RiskMappingProfileV2, SignedRiskFeed, SignedRiskFeedV2,
};
use forecast_domain::{canonical_bytes, content_hash};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn golden() -> Value {
    let text = std::fs::read_to_string(repo_root().join("tests/golden/risk-feed-golden.json")).expect("golden vectors");
    serde_json::from_str(&text).expect("golden JSON")
}

fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn verify_signature(public_key_hex: &str, signature_hex: &str, message: &[u8]) {
    let key: [u8; 32] = hex::decode(public_key_hex).unwrap().try_into().unwrap();
    let signature: [u8; 64] = hex::decode(signature_hex).unwrap().try_into().unwrap();
    VerifyingKey::from_bytes(&key)
        .unwrap()
        .verify(message, &Signature::from_bytes(&signature))
        .expect("signature");
}

#[test]
fn v1_envelope_reproduces_python_hashes_and_signature() {
    let vectors = golden();
    let v1 = &vectors["v1"];
    let envelope = SignedRiskFeed::from_value(v1["envelope"].clone()).expect("v1 envelope decodes");
    let canonical = envelope.payload.canonical().unwrap();
    assert_eq!(
        String::from_utf8(canonical).unwrap(),
        v1["canonical_payload"].as_str().unwrap()
    );
    assert_eq!(
        content_hash(&envelope.payload).unwrap(),
        v1["payload_hash"].as_str().unwrap()
    );
    assert_eq!(content_hash(&envelope).unwrap(), v1["envelope_hash"].as_str().unwrap());
    let message = signing_bytes(&envelope.payload).unwrap();
    assert!(message.starts_with(b"forecast-risk-feed-v1:signature:"));
    assert_eq!(sha256_hex(&message), v1["signing_bytes_sha256"].as_str().unwrap());
    verify_signature(&envelope.public_key_hex, &envelope.signature_hex, &message);
    // Re-encoding through serde yields the exact input object (field set and values).
    assert_eq!(serde_json::to_value(&envelope).unwrap(), v1["envelope"]);
}

#[test]
fn v2_envelope_and_registry_records_reproduce_python_hashes() {
    let vectors = golden();
    let v2 = &vectors["v2"];
    let envelope = SignedRiskFeedV2::from_value(v2["envelope"].clone()).expect("v2 envelope decodes");
    assert_eq!(
        String::from_utf8(envelope.payload.canonical().unwrap()).unwrap(),
        v2["canonical_payload"].as_str().unwrap()
    );
    assert_eq!(
        content_hash(&envelope.payload).unwrap(),
        v2["payload_hash"].as_str().unwrap()
    );
    assert_eq!(content_hash(&envelope).unwrap(), v2["envelope_hash"].as_str().unwrap());
    let message = signing_bytes_v2(&envelope.payload).unwrap();
    assert!(message.starts_with(b"forecast-risk-feed-v2:signature:"));
    assert_eq!(sha256_hex(&message), v2["signing_bytes_sha256"].as_str().unwrap());
    verify_signature(&envelope.public_key_hex, &envelope.signature_hex, &message);
    assert_eq!(serde_json::to_value(&envelope).unwrap(), v2["envelope"]);

    let definition = CanonicalRiskDefinitionV2::from_value(v2["definition"].clone()).unwrap();
    assert_eq!(
        content_hash(&definition).unwrap(),
        v2["definition_hash"].as_str().unwrap()
    );
    let profile = RiskMappingProfileV2::from_value(v2["profile"].clone()).unwrap();
    assert_eq!(content_hash(&profile).unwrap(), v2["profile_hash"].as_str().unwrap());
    let series = RiskFeedSeriesV2::from_value(v2["series"].clone()).unwrap();
    assert_eq!(content_hash(&series).unwrap(), v2["series_hash"].as_str().unwrap());
    for binding in &envelope.payload.bindings {
        assert_eq!(binding.definition_hash, v2["definition_hash"].as_str().unwrap());
        assert_eq!(binding.mapping_profile_hash, v2["profile_hash"].as_str().unwrap());
        // Python verdict: the golden episode start is not cadence-aligned, so it does not conform.
        assert!(!series.conforms(binding));
        let shift = binding.target_start_ms % series.cadence_ms;
        let aligned = forecast_domain::risk_feed::RiskFeedBindingV2 {
            target_start_ms: binding.target_start_ms - shift,
            target_end_ms: binding.target_end_ms - shift,
            authorization_valid_from_ms: binding.authorization_valid_from_ms - shift,
            authorization_valid_until_ms: binding.authorization_valid_until_ms - shift,
            operational_valid_from_ms: binding.target_start_ms - shift,
            operational_valid_until_ms: binding.target_end_ms - shift - series.policy_horizon_ms,
            ..binding.clone()
        };
        aligned.validate().unwrap();
        assert!(series.conforms(&aligned), "cadence-aligned episode conforms");
    }
    for signal in &envelope.payload.signals {
        let expected = signal.oldest_member_as_of_ms.unwrap_or(signal.forecast_as_of_ms);
        assert_eq!(freshness_as_of_ms(signal), expected);
    }
}

#[test]
fn canonical_edge_cases_match_python_json_dumps() {
    let vectors = golden();
    let edge = &vectors["canonical_edge"];
    let bytes = canonical_bytes(&edge["value"]).unwrap();
    assert_eq!(String::from_utf8(bytes).unwrap(), edge["canonical"].as_str().unwrap());
    assert_eq!(content_hash(&edge["value"]).unwrap(), edge["hash"].as_str().unwrap());
    assert!(canonical_bytes(&json!({"x": 1.5})).is_err(), "floats are not canonical");
    assert!(
        canonical_bytes(&json!({"x": 9007199254740992i64})).is_err(),
        "integers beyond 2^53-1 are not portable"
    );
    assert!(canonical_bytes(&json!({"x": -9007199254740991i64})).is_ok());
}

#[test]
fn strict_decoding_rejects_the_same_inputs_as_python() {
    let vectors = golden();
    let envelope = &vectors["v2"]["envelope"];

    let text = serde_json::to_string(envelope).unwrap();
    assert!(SignedRiskFeedV2::from_json(&text).is_ok());
    let duplicated = text.replacen("\"schema_version\":1", "\"schema_version\":1,\"schema_version\":1", 1);
    assert!(SignedRiskFeedV2::from_json(&duplicated)
        .unwrap_err()
        .to_string()
        .contains("Duplicate"));
    let float = text.replacen("\"sequence\":", "\"sequence\":1.0,\"ignored\":", 1);
    assert!(SignedRiskFeedV2::from_json(&float).is_err());

    let mut unknown = envelope.clone();
    unknown["extra"] = json!(true);
    assert!(
        SignedRiskFeedV2::from_value(unknown).is_err(),
        "unknown fields are rejected"
    );

    let mut missing_optional = envelope.clone();
    missing_optional["payload"]["signals"][0]
        .as_object_mut()
        .unwrap()
        .remove("source_watermark_ms");
    assert!(
        SignedRiskFeedV2::from_value(missing_optional).is_err(),
        "null-able fields must still be present"
    );

    let mut wrong_version = envelope.clone();
    wrong_version["schema_version"] = json!(2);
    assert!(SignedRiskFeedV2::from_value(wrong_version).is_err());

    let mut boolean_int = envelope.clone();
    boolean_int["payload"]["sequence"] = json!(true);
    assert!(SignedRiskFeedV2::from_value(boolean_int).is_err());

    let mut negative = envelope.clone();
    negative["payload"]["issued_at_ms"] = json!(-1);
    assert!(SignedRiskFeedV2::from_value(negative)
        .unwrap_err()
        .to_string()
        .contains("nonnegative"));

    let mut long_lived = envelope.clone();
    let issued = long_lived["payload"]["issued_at_ms"].as_i64().unwrap();
    long_lived["payload"]["expires_at_ms"] = json!(issued + 120_001);
    assert_eq!(
        SignedRiskFeedV2::from_value(long_lived).unwrap_err().to_string(),
        "feed lifetime exceeds 120 seconds"
    );

    let mut bad_hash = envelope.clone();
    bad_hash["payload"]["weight_set_hash"] = json!("ABC");
    assert!(SignedRiskFeedV2::from_value(bad_hash)
        .unwrap_err()
        .to_string()
        .contains("malformed"));

    let mut blank = envelope.clone();
    blank["payload"]["bindings"][0]["episode_id"] = json!("  ");
    assert!(SignedRiskFeedV2::from_value(blank).is_err());

    let mut uncovered = envelope.clone();
    uncovered["payload"]["channel_coverage"][0]["status"] = json!("unavailable");
    assert!(
        SignedRiskFeedV2::from_value(uncovered).is_err(),
        "coverage must match bound channels"
    );

    let mut single_pool = envelope.clone();
    single_pool["payload"]["signals"][0]["source"] = json!("crowd");
    if single_pool["payload"]["signals"][0]["oldest_member_as_of_ms"].is_null() {
        assert_eq!(
            SignedRiskFeedV2::from_value(single_pool).unwrap_err().to_string(),
            "pooled sources declare constituent provenance"
        );
    }
}

#[test]
fn v1_payload_semantics_match_python() {
    let vectors = golden();
    let payload = &vectors["v1"]["envelope"]["payload"];
    assert!(RiskFeedPayload::from_value(payload.clone()).is_ok());

    let mut early = payload.clone();
    early["signals"][0]["observed_at_ms"] = json!(payload["issued_at_ms"].as_i64().unwrap() + 1);
    assert_eq!(
        RiskFeedPayload::from_value(early).unwrap_err().to_string(),
        "signal outside binding or after issue time"
    );

    let mut horizon = payload.clone();
    if horizon["bindings"][0]["channel"] == json!("depegRisk1d") {
        horizon["bindings"][0]["horizon_hours"] = json!(48);
        assert_eq!(
            RiskFeedPayload::from_value(horizon).unwrap_err().to_string(),
            "channel horizon mismatch"
        );
    }

    let mut no_signals = payload.clone();
    no_signals["signals"] = json!([]);
    assert!(RiskFeedPayload::from_value(no_signals)
        .unwrap_err()
        .to_string()
        .contains("signals"));
}

#[test]
fn v2_payload_semantics_match_python() {
    let vectors = golden();
    let payload = &vectors["v2"]["envelope"]["payload"];
    let decoded = RiskFeedPayloadV2::from_value(payload.clone()).unwrap();
    assert!(!decoded.bindings.is_empty());

    let mut empty_bindings = payload.clone();
    empty_bindings["bindings"] = json!([]);
    empty_bindings["signals"] = json!([]);
    assert!(
        RiskFeedPayloadV2::from_value(empty_bindings).is_err(),
        "coverage still names the bound channel"
    );

    let mut late_signal = payload.clone();
    late_signal["signals"][0]["evaluation_completed_at_ms"] = json!(payload["issued_at_ms"].as_i64().unwrap() + 1);
    assert_eq!(
        RiskFeedPayloadV2::from_value(late_signal).unwrap_err().to_string(),
        "signal outside binding authorization or after issue time"
    );

    let mut escaped = payload.clone();
    let binding = &payload["bindings"][0];
    if binding["mapping_kind"] == json!("containing_upper_estimate") {
        escaped["bindings"][0]["operational_valid_until_ms"] = json!(binding["target_end_ms"].as_i64().unwrap());
        assert_eq!(
            RiskFeedPayloadV2::from_value(escaped).unwrap_err().to_string(),
            "policy horizon escapes target end"
        );
    }
}

/// The Python schema generator stays the authority: every record's field set equals the published one.
#[test]
fn field_sets_match_published_schemas() {
    let vectors = golden();
    let v1 = &vectors["v1"]["envelope"];
    let v2 = &vectors["v2"]["envelope"];
    let samples: Vec<(&str, Value)> = vec![
        ("SignedRiskFeed", v1.clone()),
        ("RiskFeedPayload", v1["payload"].clone()),
        ("RiskFeedBinding", v1["payload"]["bindings"][0].clone()),
        ("RiskFeedSignal", v1["payload"]["signals"][0].clone()),
        ("SignedRiskFeedV2", v2.clone()),
        ("RiskFeedPayloadV2", v2["payload"].clone()),
        ("RiskFeedBindingV2", v2["payload"]["bindings"][0].clone()),
        ("RiskFeedSignalV2", v2["payload"]["signals"][0].clone()),
        ("CanonicalRiskDefinitionV2", vectors["v2"]["definition"].clone()),
        ("RiskMappingProfileV2", vectors["v2"]["profile"].clone()),
        ("RiskFeedSeriesV2", vectors["v2"]["series"].clone()),
    ];
    for (name, sample) in samples {
        let decoded: Value = match name {
            "SignedRiskFeed" => serde_json::to_value(SignedRiskFeed::from_value(sample).unwrap()).unwrap(),
            "RiskFeedPayload" => serde_json::to_value(RiskFeedPayload::from_value(sample).unwrap()).unwrap(),
            "RiskFeedBinding" => {
                serde_json::to_value(forecast_domain::risk_feed::RiskFeedBinding::from_value(sample).unwrap()).unwrap()
            }
            "RiskFeedSignal" => {
                serde_json::to_value(forecast_domain::risk_feed::RiskFeedSignal::from_value(sample).unwrap()).unwrap()
            }
            "SignedRiskFeedV2" => serde_json::to_value(SignedRiskFeedV2::from_value(sample).unwrap()).unwrap(),
            "RiskFeedPayloadV2" => serde_json::to_value(RiskFeedPayloadV2::from_value(sample).unwrap()).unwrap(),
            "RiskFeedBindingV2" => {
                serde_json::to_value(forecast_domain::risk_feed::RiskFeedBindingV2::from_value(sample).unwrap())
                    .unwrap()
            }
            "RiskFeedSignalV2" => {
                serde_json::to_value(forecast_domain::risk_feed::RiskFeedSignalV2::from_value(sample).unwrap()).unwrap()
            }
            "CanonicalRiskDefinitionV2" => {
                serde_json::to_value(CanonicalRiskDefinitionV2::from_value(sample).unwrap()).unwrap()
            }
            "RiskMappingProfileV2" => serde_json::to_value(RiskMappingProfileV2::from_value(sample).unwrap()).unwrap(),
            "RiskFeedSeriesV2" => serde_json::to_value(RiskFeedSeriesV2::from_value(sample).unwrap()).unwrap(),
            _ => unreachable!(),
        };
        let rust_fields: BTreeSet<String> = decoded.as_object().unwrap().keys().cloned().collect();
        let schema_text = std::fs::read_to_string(repo_root().join(format!("schemas/v1/{name}.schema.json"))).unwrap();
        let schema: Value = serde_json::from_str(&schema_text).unwrap();
        let definition = &schema["$defs"][name];
        let properties: BTreeSet<String> = definition["properties"].as_object().unwrap().keys().cloned().collect();
        let required: BTreeSet<String> = definition["required"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert_eq!(rust_fields, properties, "{name}: properties");
        assert_eq!(rust_fields, required, "{name}: every field is required on the wire");
        assert_eq!(
            definition["additionalProperties"],
            json!(false),
            "{name}: closed object"
        );
    }
}
