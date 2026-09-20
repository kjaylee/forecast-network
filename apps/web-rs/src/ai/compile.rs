//! `compile_question`: from a sentence someone typed to a question the lifecycle can commit to.
//!
//! This is the longest task in the pipeline and the one with the most ways to be wrong, so the
//! order of its steps is the design:
//!
//!   1. The model is asked for a specification against a schema with no arithmetic in it.
//!   2. Its answer is normalized — every date checked against the user's own words, in either
//!      language, and reduced to the one integer the lifecycle commits to.
//!   3. The commitment to the candidate set is re-checked. The model chose duplicates by
//!      reference while it was thinking, and the forecasts it referred to must be the same ones
//!      that were shown.
//!   4. The normalized specification is validated by the domain, then its sources are actually
//!      fetched: a question whose criteria name a page nobody can reach is not publishable.
//!   5. Two independent judges re-read that collected evidence and decide whether the criteria
//!      are objective, English, faithful and non-duplicative.
//!
//! Only then is a reward at stake. The probability estimate is deliberately last and deliberately
//! allowed to fail: it is a statistical signal, and losing it must not lose a specification that
//! four earlier steps spent the AI budget establishing.

use serde_json::{json, Map, Value};

use forecast_domain::content_hash;
use forecast_domain::lifecycle::Forecast;
use forecast_domain::models::{
    ambiguity_output_hash, duplicate_output_hash, AIProvenance, ForecastSpecification, ValidationAssessment,
};
use forecast_domain::Record;

use super::compile_policy::{AMBIGUITY_POLICY, COMPILER_POLICY, DUPLICATE_POLICY, FORECAST_POLICY, WINDOW_POLICY};
use super::compiler_time::normalize_compiler_output;
use super::compiler_wire::{
    assert_candidate_context, candidate_context, spec_schema, COMPILER_WIRE_VERSION, MAX_CANDIDATE_CONTEXT_BYTES,
};
use super::coordinator::{artifact, Artifact, Coordinator, CoordinatorError, Decision, ProviderConfig, POLICY_VERSION};
use super::resolution::{collect_sources, EvidenceFetcher};
use super::schema::schemas;
use super::text::{require_english_public_text, require_timeless_share_title};
use super::window::measurement_window;
use crate::sources::{validate_public_url, SOURCE_POLICY_VERSION};

pub const AMBIGUITY_LIMIT_BP: i64 = 1000;
pub const DUPLICATE_SIMILARITY_THRESHOLD_BP: i64 = 8500;
pub const MIN_DEADLINE_MS: i64 = 300_000;
pub const MAX_DEADLINE_MS: i64 = 5 * 366 * 86_400_000;

#[derive(Debug, Clone)]
pub struct CompileResult {
    pub specification: ForecastSpecification,
    pub assessment: ValidationAssessment,
    pub artifacts: Vec<Artifact>,
    pub ai_forecast: Option<Value>,
}

#[derive(Debug, Clone)]
pub struct PredictionResult {
    pub artifacts: Vec<Artifact>,
    pub ai_forecast: Value,
}

fn refused(code: &str, message: &str) -> CoordinatorError {
    CoordinatorError::Rejected {
        code: code.to_string(),
        message: message.to_string(),
        artifacts: Vec::new(),
    }
}

fn provenance(decision: &Decision, task: &str, input_hash: &str, output_hash: &str, now_ms: i64) -> AIProvenance {
    AIProvenance {
        schema_version: 1,
        task: task.to_string(),
        provider: decision.config.provider.clone(),
        model: decision.config.model.clone(),
        model_version: decision.version.clone(),
        policy_version: POLICY_VERSION.to_string(),
        input_hash: input_hash.to_string(),
        output_hash: output_hash.to_string(),
        created_at_ms: now_ms,
    }
}

/// A refusal that keeps everything gathered so far. The reference carries the artifacts through
/// every `except`, because a rejected question is exactly when an operator needs them.
fn carrying(mut error: CoordinatorError, earlier: &[Artifact]) -> CoordinatorError {
    let mut gathered = earlier.to_vec();
    match &error {
        CoordinatorError::Rejected { artifacts, .. } | CoordinatorError::Unavailable { artifacts, .. } => {
            gathered.extend(artifacts.iter().cloned());
        }
    }
    match &mut error {
        CoordinatorError::Rejected { artifacts, .. } | CoordinatorError::Unavailable { artifacts, .. } => {
            *artifacts = gathered;
        }
    }
    error
}

/// `datetime.fromtimestamp(ms / 1000, utc).isoformat(timespec="milliseconds")`, with `Z`.
///
/// The estimate payload states the instant twice: as epoch milliseconds, and as a readable UTC
/// timestamp beside it. Asking a model to convert one into the other is how a training cutoff
/// becomes "now".
fn as_of_utc(now_ms: i64) -> String {
    let (year, month, day) = super::compiler_time::civil_parts(now_ms);
    let day_seconds = now_ms.div_euclid(1000).rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        day_seconds / 3600,
        (day_seconds % 3600) / 60,
        day_seconds % 60,
        now_ms.rem_euclid(1000),
    )
}

/// What the compiler stage established, before anything was fetched or judged.
pub struct Compiled {
    pub specification: ForecastSpecification,
    pub normalization: Artifact,
}

/// Steps 1 and 2, plus every domain check that needs nothing but the answer itself.
fn compile_specification(
    compiler: &Decision,
    question: &str,
    context: &[Value],
    window: Option<&Value>,
    distinct_windows: bool,
    compiler_input_hash: &str,
    context_hash: &str,
) -> Result<Compiled, CoordinatorError> {
    let output = compiler.output.clone();
    let normalized_value = normalize_compiler_output(&output, question, context, distinct_windows)?;
    let mut normalized = normalized_value.as_object().cloned().unwrap_or_default();
    let distinct: Vec<String> = normalized
        .remove("_distinct_measurement_windows")
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default()
        .iter()
        .filter_map(|item| item.as_str().map(str::to_string))
        .collect();

    let specification: ForecastSpecification =
        serde_json::from_value(Value::Object(normalized.clone())).map_err(|_| {
            refused(
                "compiler_domain_validation",
                "AI specification failed deterministic validation",
            )
        })?;
    specification.validate().map_err(|_| {
        refused(
            "compiler_domain_validation",
            "AI specification failed deterministic validation",
        )
    })?;

    let mut public: Vec<String> = vec![
        specification.canonical_question.clone(),
        specification.share_title.clone(),
    ];
    public.extend(specification.rules.iter().map(|rule| rule.condition.clone()));
    public.extend(specification.invalidation_rules.iter().cloned());
    public.extend(specification.source_policy.sources().map(|source| source.name.clone()));
    public.extend(
        specification
            .duplicate_candidates
            .iter()
            .map(|candidate| candidate.explanation.clone()),
    );
    require_english_public_text(&public).map_err(|error| refused(error.code, error.message))?;

    // The model answered about `c0`; what it is bound to is the verified identity that was shown,
    // and the lookup records which identity each reference resolved to.
    let lookup: Vec<Value> = context
        .iter()
        .map(|item| {
            let mut entry = item.as_object().cloned().unwrap_or_default();
            entry.remove("specification");
            Value::Object(entry)
        })
        .collect();
    let mut record = Map::new();
    record.insert("schema_version".to_string(), json!(1));
    record.insert("kind".to_string(), json!("compiler_normalization"));
    record.insert("compiler_wire_version".to_string(), json!(COMPILER_WIRE_VERSION));
    record.insert(
        "normalization_version".to_string(),
        json!(if window.is_some() {
            "exact-utc-window-and-candidate-reference-v4"
        } else {
            "exact-utc-and-candidate-reference-v3"
        }),
    );
    if let Some(window) = window {
        record.insert("measurement_window".to_string(), window.clone());
    }
    if !distinct.is_empty() {
        record.insert("distinct_measurement_windows".to_string(), json!(distinct));
    }
    record.insert("raw_decision_artifact_hash".to_string(), json!(compiler.artifact.hash));
    record.insert("compiler_input_hash".to_string(), json!(compiler_input_hash));
    record.insert("candidate_context_hash".to_string(), json!(context_hash));
    record.insert(
        "candidate_lookup_hash".to_string(),
        json!(content_hash(&Value::Array(lookup.clone())).unwrap_or_default()),
    );
    record.insert("candidate_lookup".to_string(), Value::Array(lookup));
    record.insert(
        "close_at_utc".to_string(),
        output.get("close_at_utc").cloned().unwrap_or(Value::Null),
    );
    record.insert("close_at_ms".to_string(), json!(specification.close_at_ms));
    record.insert("normalized_specification".to_string(), Value::Object(normalized));
    record.insert(
        "specification_hash".to_string(),
        json!(specification.specification_hash().unwrap_or_default()),
    );
    let normalization = artifact("compiler-normalization", &Value::Object(record))
        .map_err(|_| refused("ai_rejected", "Compiler normalization could not be retained"))?;

    require_timeless_share_title(&specification.share_title).map_err(|error| refused(error.code, error.message))?;
    Ok(Compiled {
        specification,
        normalization,
    })
}

/// `compile_question`.
pub async fn compile_question(
    coordinator: &Coordinator,
    fetch: &EvidenceFetcher,
    question: &str,
    candidates: &[Forecast],
    now_ms: i64,
    distinct_measurement_windows: bool,
) -> Result<CompileResult, CoordinatorError> {
    let length = question.trim().chars().count();
    if !(12..=1000).contains(&length) {
        return Err(refused(
            "ai_rejected",
            "Use 12–1,000 characters and include a specific subject and deadline.",
        ));
    }
    let window = measurement_window(question).map_err(|error| refused(error.code, error.message))?;
    let context = candidate_context(candidates)?;
    let context_value = Value::Array(context.clone());
    let context_hash = content_hash(&context_value)
        .map_err(|_| refused("ai_rejected", "Duplicate candidate context could not be hashed"))?;
    let bytes = forecast_domain::canonical_bytes(&context_value)
        .map_err(|_| refused("ai_rejected", "Duplicate candidate context could not be canonicalized"))?;
    if bytes.len() > MAX_CANDIDATE_CONTEXT_BYTES {
        return Err(refused(
            "ai_rejected",
            "Duplicate candidate context exceeds the validated byte limit",
        ));
    }

    // The window instruction is appended rather than written into the base policy, so the two
    // cases are one sentence apart and the difference is visible in the artifact.
    let policy = match window.as_ref() {
        None => COMPILER_POLICY.to_string(),
        Some(_) => format!("{COMPILER_POLICY}{WINDOW_POLICY}"),
    };
    let mut payload = json!({
        "schema_version": 1, "question": question, "now_ms": now_ms, "output_language": "en",
        "compiler_wire_version": COMPILER_WIRE_VERSION,
        "approved_official_hosts": host_map(crate::sources::OFFICIAL_HOSTS),
        "approved_fallback_hosts": host_map(crate::sources::FALLBACK_HOSTS),
        "candidates": context,
        "policy": policy,
    });
    if let Some(window) = window.as_ref() {
        payload["measurement_window"] = window.clone();
    }
    let compiler_input_hash =
        content_hash(&payload).map_err(|_| refused("ai_rejected", "Compiler input could not be hashed"))?;

    let compiler = coordinator
        .call("MARKET_COMPILER", &payload, &spec_schema(candidates.len()), None, None)
        .await?;
    let mut artifacts = vec![compiler.artifact.clone()];

    // Steps 2 to 4 are the reference's single `try`: every refusal inside keeps what the compiler
    // already produced, which is the only record of what it actually answered.
    let prepared: Result<Compiled, CoordinatorError> =
        (|| {
            assert_candidate_context(candidates, &context_hash)?;
            let retained: Value = serde_json::from_str(&compiler.artifact.body).map_err(|_| {
                refused(
                    "compiler_candidate_context_changed",
                    "Compiler decision provenance changed",
                )
            })?;
            let same_input = content_hash(&payload).ok().as_deref() == Some(compiler_input_hash.as_str());
            let same_output = retained["output"] == Value::Object(compiler.output.clone());
            let same_artifact = content_hash(&retained).ok().as_deref() == Some(compiler.artifact.hash.as_str())
                && content_hash(&retained["input"]).ok().as_deref() == Some(compiler_input_hash.as_str());
            if !(same_input && same_output && same_artifact) {
                return Err(refused(
                    "compiler_candidate_context_changed",
                    "Compiler decision provenance changed",
                ));
            }
            let compiled = compile_specification(
                &compiler,
                question,
                &context,
                window.as_ref(),
                distinct_measurement_windows,
                &compiler_input_hash,
                &context_hash,
            )?;
            let specification = &compiled.specification;
            if specification.open_at_ms != now_ms
                || !(now_ms + MIN_DEADLINE_MS..=now_ms + MAX_DEADLINE_MS).contains(&specification.close_at_ms)
            {
                return Err(refused(
                    "compiler_deadline_range",
                    "The deadline must be at least five minutes and at most five years away.",
                ));
            }
            let known: Vec<(&str, &str)> = candidates
                .iter()
                .map(|shown| (shown.forecast_id.as_str(), shown.specification_hash.as_str()))
                .collect();
            if specification.duplicate_candidates.iter().any(|candidate| {
                !known.contains(&(candidate.forecast_id.as_str(), candidate.specification_hash.as_str()))
            }) {
                return Err(refused(
                    "ai_rejected",
                    "AI duplicate results do not match existing forecast commitments",
                ));
            }
            for source in specification.source_policy.sources() {
                validate_public_url(&source.url, source.is_official).map_err(|_| {
                    refused(
                        "compiler_domain_validation",
                        "AI specification failed deterministic validation",
                    )
                })?;
            }
            Ok(compiled)
        })();
    let compiled = match prepared {
        Ok(compiled) => compiled,
        Err(error) => return Err(carrying(error, &artifacts)),
    };
    artifacts.push(compiled.normalization.clone());
    let specification = compiled.specification;

    let specification_value = serde_json::to_value(&specification).unwrap_or(Value::Null);
    let collection = match collect_sources(fetch, &specification_value, now_ms).await {
        Ok(collection) => collection,
        Err(error) => return Err(carrying(error, &artifacts)),
    };
    let source_documents: Vec<Value> = collection
        .sources
        .iter()
        .map(|item| json!({"url": item.url, "text": item.excerpt}))
        .collect();
    artifacts.extend(collection.artifacts.iter().cloned());

    let review = json!({
        "schema_version": 1, "specification": specification_value, "original_question": question,
        "output_language": "en", "source_collection": collection.context,
        "source_documents": source_documents, "policy": AMBIGUITY_POLICY,
    });
    let ambiguity = match coordinator
        .call("AMBIGUITY_JUDGE", &review, &schemas::ambiguity(), None, None)
        .await
    {
        Ok(decision) => decision,
        Err(error) => return Err(carrying(error, &artifacts)),
    };
    artifacts.push(ambiguity.artifact.clone());

    let mut duplicate_policy = DUPLICATE_POLICY.to_string();
    if distinct_measurement_windows {
        duplicate_policy.push_str(
            " Candidates whose published question declares a different explicit [start, end) \
             measurement interval are distinct measurement contracts by policy; their \
             materially_different_rules=true classification is accurate.",
        );
    }
    let duplicate = match coordinator
        .call(
            "DUPLICATE_DETECTOR",
            &json!({"schema_version": 1, "specification": specification_value,
                    "candidates": context, "policy": duplicate_policy}),
            &schemas::duplicate(),
            None,
            None,
        )
        .await
    {
        Ok(decision) => decision,
        Err(error) => return Err(carrying(error, &artifacts)),
    };
    artifacts.push(duplicate.artifact.clone());
    artifacts.push(
        artifact("specification", &specification_value)
            .map_err(|_| refused("ai_rejected", "Specification could not be retained"))?,
    );

    let explanation = format!(
        "{}\nDuplicate check: {}",
        ambiguity.output["explanation"].as_str().unwrap_or_default(),
        duplicate.output["explanation"].as_str().unwrap_or_default(),
    );
    let objective = truthy(&ambiguity.output, "objectively_resolvable")
        && truthy(&ambiguity.output, "sources_appropriate")
        && truthy(&ambiguity.output, "intent_preserved")
        && truthy(&ambiguity.output, "title_consistent");
    let ambiguity_passed =
        truthy(&ambiguity.output, "ambiguity_passed") && truthy(&ambiguity.output, "english_language_passed");
    let completed = truthy(&duplicate.output, "check_completed") && truthy(&duplicate.output, "candidates_accurate");
    let digest = specification.specification_hash().unwrap_or_default();

    let assessment = ValidationAssessment {
        schema_version: 1,
        specification_hash: digest.clone(),
        deterministic_check_version: SOURCE_POLICY_VERSION.to_string(),
        deterministic_passed: true,
        objectively_resolvable: objective,
        ambiguity_passed,
        duplicate_check_completed: completed,
        ambiguity_limit_bp: AMBIGUITY_LIMIT_BP,
        duplicate_similarity_threshold_bp: DUPLICATE_SIMILARITY_THRESHOLD_BP,
        compiler: provenance(&compiler, "MARKET_COMPILER", &compiler_input_hash, &digest, now_ms),
        ambiguity_judge: provenance(
            &ambiguity,
            "AMBIGUITY_JUDGE",
            &digest,
            &ambiguity_output_hash(&digest, objective, ambiguity_passed, AMBIGUITY_LIMIT_BP, &explanation)
                .unwrap_or_default(),
            now_ms,
        ),
        duplicate_detector: provenance(
            &duplicate,
            "DUPLICATE_DETECTOR",
            &digest,
            &duplicate_output_hash(&digest, completed, DUPLICATE_SIMILARITY_THRESHOLD_BP, &explanation)
                .unwrap_or_default(),
            now_ms,
        ),
        validated_at_ms: now_ms,
        explanation: explanation.clone(),
    };
    artifacts.push(
        artifact("validation", &serde_json::to_value(&assessment).unwrap_or(Value::Null))
            .map_err(|_| refused("ai_rejected", "Validation assessment could not be retained"))?,
    );
    if assessment.require_publishable(&specification).is_err() {
        // The explanation is the message: it is the only thing that tells a user which criterion
        // was refused, and the code is what tells the operator it was a refusal at all.
        return Err(CoordinatorError::Rejected {
            code: "compiler_not_publishable".to_string(),
            message: explanation,
            artifacts,
        });
    }

    // A statistical signal, never a publication proof. Its failure cannot fabricate a percentage
    // or hide the specification the four earlier steps established.
    let mut ai_forecast = None;
    match estimate_probability(
        coordinator,
        &specification,
        &source_documents,
        now_ms,
        Some(&compiler.config),
        None,
    )
    .await
    {
        Ok(estimate) => {
            artifacts.extend(estimate.artifacts);
            ai_forecast = Some(estimate.ai_forecast);
        }
        Err(error) => artifacts.extend(error.artifacts().to_vec()),
    }
    assert_candidate_context(candidates, &context_hash)?;
    Ok(CompileResult {
        specification,
        assessment,
        artifacts,
        ai_forecast,
    })
}

/// The approved hosts as the model receives them: host to publisher, not a bare list.
///
/// The publisher is what tells the model which company a page belongs to, which is the difference
/// between "this host is approved" and "this is Apple's own newsroom".
fn host_map(table: &'static [(&'static str, &'static str)]) -> Value {
    Value::Object(
        table
            .iter()
            .map(|(host, owner)| ((*host).to_string(), json!(owner)))
            .collect(),
    )
}

fn truthy(output: &Map<String, Value>, key: &str) -> bool {
    output.get(key).and_then(Value::as_bool).unwrap_or(false)
}

/// `_estimate_probability`.
pub async fn estimate_probability(
    coordinator: &Coordinator,
    specification: &ForecastSpecification,
    source_documents: &[Value],
    now_ms: i64,
    provider: Option<&ProviderConfig>,
    source_provenance_hash: Option<&str>,
) -> Result<PredictionResult, CoordinatorError> {
    let digest = specification.specification_hash().unwrap_or_default();
    let mut payload = json!({
        "schema_version": 1, "specification": serde_json::to_value(specification).unwrap_or(Value::Null),
        "as_of_ms": now_ms, "as_of_utc": as_of_utc(now_ms), "source_documents": source_documents,
        "policy": FORECAST_POLICY,
    });
    if let Some(hash) = source_provenance_hash {
        payload["source_provenance_hash"] = json!(hash);
    }
    let prediction = coordinator
        .call("AI_FORECAST", &payload, &schemas::ai_forecast(), provider, None)
        .await?;
    let probability = prediction.output["yesProbabilityBp"].as_i64().unwrap_or(0);
    let rationale = prediction.output["rationale"].as_str().unwrap_or_default().to_string();
    let mut estimate = json!({
        "schema_version": 1, "kind": "ai_forecast", "specification_hash": digest, "as_of_ms": now_ms,
        "provider": prediction.config.provider, "model": prediction.config.model,
        "model_version": prediction.version, "policy_version": POLICY_VERSION,
        "yes_probability_bp": probability, "rationale": rationale,
        "decision_artifact_hash": prediction.artifact.hash,
    });
    if let Some(hash) = source_provenance_hash {
        estimate["source_provenance_hash"] = json!(hash);
    }
    let retained =
        artifact("ai-forecast", &estimate).map_err(|_| refused("ai_rejected", "AI forecast could not be retained"))?;
    Ok(PredictionResult {
        artifacts: vec![prediction.artifact, retained.clone()],
        ai_forecast: json!({
            "probability": probability as f64 / 100.0, "provider": prediction.config.provider,
            "model": prediction.config.model, "modelVersion": prediction.version, "asOf": now_ms,
            "specificationHash": digest, "artifactHash": retained.hash, "rationale": rationale,
        }),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai::coordinator::JsonFetcher;
    use crate::ai::resolution::EvidenceFetcher;
    use crate::sources::TextResponse;
    use std::sync::{Arc, Mutex};

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-compile-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("compile golden")).expect("json")
    }

    fn payload_of(request: &Value) -> Value {
        let raw = if let Some(text) = request["contents"][0]["parts"][0]["text"].as_str() {
            text
        } else {
            request["messages"]
                .as_array()
                .and_then(|items| items.last())
                .and_then(|last| last["content"].as_str())
                .unwrap_or("")
        };
        serde_json::from_str(raw).expect("the request body carries the payload as JSON")
    }

    #[test]
    fn the_reference_compilation_is_reproduced_call_for_call() {
        // Four model calls, one collected source, a normalized specification, a validation
        // assessment and an estimate — compared on the payloads, the records and every artifact
        // hash, because the front door is where a port is most likely to be nearly right.
        let document = golden();
        for case in document["cases"].as_array().expect("cases") {
            let name = case["name"].as_str().unwrap();
            let sources = case["sources"].as_array().expect("sources").clone();
            let fetch: EvidenceFetcher = Box::new(move |url, _headers| {
                let served = sources
                    .iter()
                    .find(|item| item["url"].as_str() == Some(url.as_str()))
                    .cloned();
                Box::pin(async move {
                    let served = served.ok_or(())?;
                    Ok(TextResponse {
                        status: served["status"].as_u64().unwrap_or(200) as u16,
                        headers: vec![(
                            "content-type".to_string(),
                            served["contentType"].as_str().unwrap_or("text/html").to_string(),
                        )],
                        body: served["body"].as_str().unwrap_or("").to_string(),
                    })
                })
            });

            let answers = Arc::new(Mutex::new(case["responses"].as_array().unwrap().clone()));
            let taken = answers.clone();
            let asked = Arc::new(Mutex::new(Vec::new()));
            let requests = asked.clone();
            let json: JsonFetcher = Box::new(move |url, _headers, body| {
                requests.lock().unwrap().push(body.clone());
                let answer = taken.lock().unwrap().remove(0);
                let text = answer.to_string();
                Box::pin(async move {
                    if url.contains("generativelanguage") {
                        Ok(json!({
                            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                            "modelVersion": "gemini-tested-revision",
                        }))
                    } else {
                        Ok(json!({"response": answer}))
                    }
                })
            });
            let providers = document["providers"]
                .as_array()
                .expect("providers")
                .iter()
                .map(|config| {
                    ProviderConfig::new(
                        config["provider"].as_str().unwrap(),
                        config["model"].as_str().unwrap(),
                        config["apiKey"].as_str().unwrap(),
                        None,
                    )
                    .expect("provider")
                })
                .collect();
            let coordinator = Coordinator { providers, fetch: json };
            let candidates: Vec<Forecast> = case["candidates"]
                .as_array()
                .unwrap()
                .iter()
                .map(|value| {
                    forecast_domain::lifecycle::Snapshot::from_json(&value.to_string())
                        .expect("golden candidate")
                        .base()
                        .clone()
                })
                .collect();

            let outcome = block(compile_question(
                &coordinator,
                &fetch,
                case["question"].as_str().unwrap(),
                &candidates,
                case["now_ms"].as_i64().unwrap(),
                case["distinct_measurement_windows"].as_bool().unwrap(),
            ));

            let asked = asked.lock().unwrap();
            let wanted = case["payloads"].as_array().unwrap();
            for (index, payload) in wanted.iter().enumerate() {
                assert_eq!(
                    &payload_of(&asked[index]),
                    payload,
                    "{name}: call {index} did not ask what the reference asked"
                );
            }

            let result = match outcome {
                Ok(result) => result,
                Err(error) => {
                    let wanted = &case["error"]["code"];
                    assert!(
                        !wanted.is_null(),
                        "{name}: refused with {:?} but the reference accepted",
                        error.code()
                    );
                    assert_eq!(error.code(), wanted.as_str(), "{name}: a different refusal");
                    continue;
                }
            };
            let expect = &case["expect"];
            assert_eq!(
                serde_json::to_value(&result.specification).unwrap(),
                expect["specification"],
                "{name}: the specification differs"
            );
            assert_eq!(
                serde_json::to_value(&result.assessment).unwrap(),
                expect["assessment"],
                "{name}: the assessment differs"
            );
            assert_eq!(
                result.ai_forecast.clone().unwrap_or(Value::Null),
                expect["aiForecast"],
                "{name}: the estimate differs"
            );
            let artifacts = expect["artifacts"].as_array().unwrap();
            assert_eq!(
                result.artifacts.len(),
                artifacts.len(),
                "{name}: a different number of artifacts"
            );
            for (index, item) in artifacts.iter().enumerate() {
                assert_eq!(
                    result.artifacts[index].kind,
                    item["kind"].as_str().unwrap(),
                    "{name} artifact {index} kind"
                );
                assert_eq!(
                    result.artifacts[index].hash,
                    item["hash"].as_str().unwrap(),
                    "{name} artifact {index} hash"
                );
                assert_eq!(
                    result.artifacts[index].body,
                    item["body"].as_str().unwrap(),
                    "{name} artifact {index} bytes"
                );
            }
        }
    }
}
