//! Lifecycle parity: 39 recorded steps (every command, retries, conflicts, the v2 early-resolution
//! path) must produce the same snapshots, events, receipts, hashes and error classes as Python.
//! The 14 live D1 snapshots must decode and validate as well.

use std::path::PathBuf;

use serde_json::Value;

use forecast_domain::lifecycle::{apply_command, Command, CommandReceipt, LifecycleError, Snapshot};
use forecast_domain::{canonical_bytes, content_hash, Record};

fn golden() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/lifecycle-golden.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

#[test]
fn every_recorded_step_is_reproduced() {
    let vectors = golden();
    let steps = vectors["steps"].as_array().unwrap();
    assert!(steps.len() >= 39);
    for step in steps {
        let name = step["name"].as_str().unwrap();
        let before =
            Snapshot::from_value(step["before"].clone()).unwrap_or_else(|e| panic!("{name}: before decodes: {e}"));
        assert_eq!(
            serde_json::to_value(&before).unwrap(),
            step["before"],
            "{name}: before round trip"
        );
        let command =
            Command::from_value(step["command"].clone()).unwrap_or_else(|e| panic!("{name}: command decodes: {e}"));
        assert_eq!(
            serde_json::to_value(&command).unwrap(),
            step["command"],
            "{name}: command round trip"
        );
        let prior = if step["prior_receipt"].is_null() {
            None
        } else {
            Some(CommandReceipt::from_value(step["prior_receipt"].clone()).unwrap())
        };
        let now = step["now_ms"].as_i64().unwrap();
        match apply_command(&before, &command, now, prior.as_ref()) {
            Ok(result) => {
                assert!(
                    step.get("error").is_none(),
                    "{name}: Python rejected ({})",
                    step["error"]
                );
                assert_eq!(
                    serde_json::to_value(&result.forecast).unwrap(),
                    step["after"],
                    "{name}: after snapshot"
                );
                assert_eq!(
                    content_hash(&result.forecast).unwrap(),
                    step["after_hash"].as_str().unwrap(),
                    "{name}: after hash"
                );
                assert_eq!(
                    String::from_utf8(canonical_bytes(&result.forecast).unwrap()).unwrap(),
                    step["after_json"].as_str().unwrap(),
                    "{name}: canonical bytes"
                );
                assert_eq!(
                    serde_json::to_value(&result.receipt).unwrap(),
                    step["receipt"],
                    "{name}: receipt"
                );
                assert_eq!(
                    serde_json::to_value(&result.events).unwrap(),
                    step["events"],
                    "{name}: events"
                );
                // Every produced snapshot re-decodes strictly.
                let again = Snapshot::from_value(step["after"].clone()).unwrap();
                assert_eq!(again, result.forecast, "{name}: after re-decode");
            }
            Err(error) => {
                let expected_type = step["error_type"]
                    .as_str()
                    .unwrap_or_else(|| panic!("{name}: Python accepted but Rust failed: {error}"));
                let actual_type = match &error {
                    LifecycleError::Validation(_) => "ValidationError",
                    LifecycleError::Transition(_) => "TransitionError",
                    LifecycleError::Concurrency(_) => "ConcurrencyError",
                    LifecycleError::Idempotency(_) => "IdempotencyConflict",
                };
                assert_eq!(actual_type, expected_type, "{name}: error class ({error})");
                assert_eq!(
                    error.to_string(),
                    step["error"].as_str().unwrap(),
                    "{name}: error message"
                );
            }
        }
    }
}

#[test]
fn live_snapshots_decode_and_validate() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/live-forecasts.json");
    let Ok(text) = std::fs::read_to_string(path) else {
        return;
    };
    let rows: Value = serde_json::from_str(&text).unwrap();
    let mut count = 0;
    for row in rows["forecasts"].as_array().unwrap() {
        let snapshot =
            Snapshot::from_json(row["snapshot"].as_str().unwrap()).unwrap_or_else(|e| panic!("{}: {e}", row["id"]));
        let base = snapshot.base();
        assert_eq!(base.revision, row["revision"].as_i64().unwrap());
        assert_eq!(base.state, row["state"]);
        assert_eq!(base.specification_hash, row["specification_hash"]);
        assert_eq!(
            String::from_utf8(canonical_bytes(&snapshot).unwrap()).unwrap(),
            row["snapshot"].as_str().unwrap(),
            "{}: stored bytes are canonical",
            row["id"]
        );
        count += 1;
    }
    assert!(count > 0);
    for event in rows["events"].as_array().unwrap() {
        let record = forecast_domain::lifecycle::DomainEvent::from_json(event["event"].as_str().unwrap()).unwrap();
        assert_eq!(content_hash(&record).unwrap(), event["hash"].as_str().unwrap());
    }
}
