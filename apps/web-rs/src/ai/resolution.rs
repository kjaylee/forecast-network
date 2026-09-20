//! Collecting evidence and asking the two judges that decide what it establishes.
//!
//! The shape of the argument is the point. A judge is given the immutable specification, the
//! retained bytes, and what the source verifier said about each of them — and nothing else. It
//! is then asked again by an independent counter-judge whose job is to disagree. A resolution is
//! only built when both agree, and even then the domain has the last word: `require_proposable`
//! can refuse what the judges accepted.

use std::pin::Pin;

use forecast_domain::lifecycle::Forecast;
use forecast_domain::models::{
    counter_judge_input_hash, counter_judge_output_hash, resolution_input_hash, resolution_output_hash,
    source_verification_output_hash, AIProvenance, EvidenceSnapshot, Resolution, ResolutionOutput, SourceVerification,
};
use serde_json::{json, Value};

use super::coordinator::{artifact, Artifact, Coordinator, CoordinatorError, Decision, POLICY_VERSION};
use super::schema::schemas;
use crate::sources::{CollectedSource, SourceError, TextResponse};

pub type BoxFuture<T> = Pin<Box<dyn std::future::Future<Output = T>>>;
/// The evidence fetch, injected for the same reason the JSON one is.
pub type EvidenceFetcher = Box<dyn Fn(String, Vec<(String, String)>) -> BoxFuture<Result<TextResponse, ()>>>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceCollection {
    pub sources: Vec<CollectedSource>,
    pub snapshots: Vec<EvidenceSnapshot>,
    pub artifacts: Vec<Artifact>,
    pub context: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolutionResult {
    pub resolution: Resolution,
    pub artifacts: Vec<Artifact>,
}

fn rejected(code: &str, message: &str) -> CoordinatorError {
    CoordinatorError::Rejected {
        code: code.to_string(),
        message: message.to_string(),
        artifacts: Vec::new(),
    }
}

/// `_provenance`: what was asked, what answered, and the two hashes that bind them.
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

/// `_collect`: exhaust the primary sources, then the fallback ones only if nothing was retained.
///
/// A fallback is not a mandatory extra dependency, which is why it is consulted second and not
/// alongside: a question whose primary sources worked should not be failed by a fallback that did
/// not, and one whose primaries did not work should say which of the two kinds of failure it was.
pub async fn collect_sources(
    fetch: &EvidenceFetcher,
    specification: &Value,
    now_ms: i64,
) -> Result<SourceCollection, CoordinatorError> {
    let primary = specification["source_policy"]["primary_sources"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let fallback = specification["source_policy"]["fallback_sources"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    if !(1..=4).contains(&(primary.len() + fallback.len())) || primary.is_empty() {
        return Err(rejected(
            "ai_rejected",
            "A forecast must use one to four bounded evidence sources",
        ));
    }
    let mut collected: Vec<CollectedSource> = Vec::new();
    let mut snapshots: Vec<EvidenceSnapshot> = Vec::new();
    let mut failures: Vec<Value> = Vec::new();
    let mut failure_artifacts: Vec<Artifact> = Vec::new();
    let mut unavailable = false;
    let mut used_fallback = false;
    for (is_primary, group) in [(true, &primary), (false, &fallback)] {
        if !is_primary && !collected.is_empty() {
            break;
        }
        if !is_primary && !group.is_empty() {
            used_fallback = true;
        }
        for source in group {
            let source_id = source["source_id"].as_str().unwrap_or("").to_string();
            let url = source["url"].as_str().unwrap_or("").to_string();
            let official = source["is_official"].as_bool().unwrap_or(false);
            let outcome = crate::sources::collect(
                |target: String| fetch(target, Vec::new()),
                &source_id,
                &url,
                official,
                None,
                now_ms,
            )
            .await;
            match outcome {
                Ok(item) => {
                    snapshots.push(EvidenceSnapshot {
                        schema_version: 1,
                        evidence_id: item.evidence_id.clone(),
                        source_id: item.source_id.clone(),
                        url: item.url.clone(),
                        content_sha256: item.content_sha256.clone(),
                        snapshot_uri: item.snapshot_uri.clone(),
                        collected_at_ms: item.collected_at_ms,
                        // The collecting model is not retained here; the verifier's provenance is.
                        collector: None,
                    });
                    collected.push(item);
                }
                Err(error) => {
                    let unavailable_source = matches!(error, SourceError::Unavailable);
                    unavailable = unavailable || unavailable_source;
                    let failure = json!({
                        "schema_version": 1, "kind": "source_failure", "source_id": source_id, "url": url,
                        "reason": error.message(), "collected_at_ms": now_ms,
                    });
                    failures.push(failure.clone());
                    if let Ok(retained) = artifact("source-failure", &failure) {
                        failure_artifacts.push(retained);
                    }
                }
            }
        }
    }
    if collected.is_empty() {
        // An unreachable source is retried later and a refused one is recorded, so the two are
        // told apart here. Either way the message names the sources, so the operator can see
        // which refused.
        return if unavailable {
            Err(CoordinatorError::Unavailable {
                providers: Vec::new(),
                artifacts: failure_artifacts,
            })
        } else {
            Err(CoordinatorError::Rejected {
                code: "source_rejected".into(),
                message: failure_message(&failures),
                artifacts: failure_artifacts,
            })
        };
    }
    let context = json!({
        "schema_version": 1, "kind": "source_collection", "policy": "primary-first-v1",
        "used_fallback": used_fallback, "unavailable_sources": failures,
        "retained_sources": snapshots.iter().map(|item| json!({
            "source_id": item.source_id, "evidence_hash": item.evidence_hash().unwrap_or_default(),
        })).collect::<Vec<Value>>(),
        "unfetched_fallback_source_ids": if used_fallback { Vec::<String>::new() } else {
            fallback.iter().filter_map(|source| source["source_id"].as_str().map(str::to_string)).collect()
        },
    });
    let mut artifacts: Vec<Artifact> = collected
        .iter()
        .map(|item| Artifact {
            hash: item.content_sha256.clone(),
            kind: "source",
            body: item.artifact_body.clone(),
        })
        .collect();
    artifacts.extend(failure_artifacts);
    if let Ok(retained) = artifact("source-collection", &context) {
        artifacts.push(retained);
    }
    Ok(SourceCollection {
        sources: collected,
        snapshots,
        artifacts,
        context,
    })
}

fn failure_message(failures: &[Value]) -> String {
    let reasons: Vec<&str> = failures
        .iter()
        .filter_map(|failure| failure["reason"].as_str())
        .collect();
    format!("No usable published evidence source: {}", reasons.join("; "))
}

/// `propose_resolution`: verify each source, ask the judge, ask the counter-judge, build.
pub async fn propose_resolution(
    coordinator: &Coordinator,
    fetch: &EvidenceFetcher,
    forecast: &Forecast,
    now_ms: i64,
    publication_time_unknown: bool,
    determined_outcome: Option<&str>,
) -> Result<ResolutionResult, CoordinatorError> {
    let specification = serde_json::to_value(&forecast.specification).unwrap_or(Value::Null);
    if now_ms < forecast.specification.close_at_ms {
        return Err(rejected(
            "ai_rejected",
            "Evidence collection cannot resolve a forecast before its deadline",
        ));
    }
    let collection = collect_sources(fetch, &specification, now_ms).await?;
    let mut artifacts = collection.artifacts.clone();
    let mut verifications: Vec<SourceVerification> = Vec::new();
    for (index, item) in collection.sources.iter().enumerate() {
        let snapshot = &collection.snapshots[index];
        let evidence_hash = snapshot
            .evidence_hash()
            .map_err(|_| rejected("ai_rejected", "Evidence could not be hashed"))?;
        let payload = json!({
            "schema_version": 1, "source_policy": specification["source_policy"],
            "source_collection": collection.context,
            "snapshot": snapshot, "retained_text": item.excerpt,
            "policy": "Verify exact source identity, usable substantive document rather than \
                access-denied/captcha/error page, publication context, relevance to immutable \
                specification and no evidence of fabricated or unavailable content. Never interpret \
                a missing statement on a generic landing page as proof of a negative outcome.",
            "specification": specification,
        });
        let decision = coordinator
            .call("SOURCE_VERIFIER", &payload, &schemas::source(), None, None)
            .await?;
        artifacts.push(decision.artifact.clone());
        let verified = decision.output["verified"].as_bool().unwrap_or(false);
        let explanation = decision.output["explanation"].as_str().unwrap_or("").to_string();
        let evidence_hash_clone = evidence_hash.clone();
        let output_hash = source_verification_output_hash(&evidence_hash, &snapshot.source_id, verified, &explanation)
            .map_err(|_| rejected("ai_rejected", "Source verification could not be hashed"))?;
        verifications.push(SourceVerification {
            schema_version: 1,
            evidence_hash,
            source_id: snapshot.source_id.clone(),
            verified,
            explanation,
            verifier: provenance(&decision, "SOURCE_VERIFIER", &evidence_hash_clone, &output_hash, now_ms),
        });
        if !verified {
            return Err(CoordinatorError::Rejected {
                code: "ai_rejected".into(),
                message: "Resolution source verification failed".into(),
                artifacts,
            });
        }
    }
    let digest = resolution_input_hash(
        &forecast.forecast_id,
        &forecast.specification_hash,
        &collection.snapshots,
        &verifications,
    )
    .map_err(|_| rejected("ai_rejected", "Resolution input could not be hashed"))?;
    let mut payload = json!({
        "schema_version": 1, "specification": specification,
        "source_collection": collection.context,
        "source_verifications": verifications,
        "evidence": collection.sources.iter().zip(&collection.snapshots)
            .map(|(item, snapshot)| json!({"snapshot": snapshot, "retained_text": item.excerpt}))
            .collect::<Vec<Value>>(),
        "policy": super::coordinator::resolution_policy(),
    });
    if publication_time_unknown {
        payload["publication_time_relative_to_participation"] = json!("unknown");
        payload["publication_time_note"] = json!(
            "The retained evidence is authentic but its publication time cannot be placed before or \
             after participation, so this evidence cannot establish an outcome. Weigh that in the outcome you choose."
        );
    }
    if let Some(determined) = determined_outcome {
        payload["determined_outcome"] = json!(determined);
        payload["determined_outcome_note"] = json!(
            "A publication-time review of this forecast has closed and determined this outcome. Propose \
             it only if the retained evidence supports it. Cite in rule_matches exactly the clause whose \
             outcome is that one: a resolution matching no clause, or a clause of another outcome, is \
             refused even when the outcome itself is right. If the evidence does not support it, record \
             that in conflict_status and the reason summary instead of proposing a different outcome, \
             because no other outcome can be finalized."
        );
    }
    let judge = coordinator
        .call("RESOLUTION_JUDGE", &payload, &schemas::resolution(), None, None)
        .await?;
    artifacts.push(judge.artifact.clone());
    let output = &judge.output;
    let outcome = output["proposed_outcome"].as_str().unwrap_or("").to_string();
    let status = output["conflict_status"].as_str().unwrap_or("").to_string();
    let confidence = output["confidence_bp"].as_i64().unwrap_or(0);
    let rule_matches: Vec<String> = strings(output, "rule_matches");
    let rule_conflicts: Vec<String> = strings(output, "rule_conflicts");
    let reason_summary = output["reason_summary"].as_str().unwrap_or("").to_string();
    let conflict_explanation = output["conflict_explanation"].as_str().map(str::to_string);
    if status != "CLEAR" || !rule_conflicts.is_empty() || confidence < 8000 {
        return Err(CoordinatorError::Rejected {
            code: "ai_rejected".into(),
            message: "Evidence does not support a clear, sufficiently confident resolution".into(),
            artifacts,
        });
    }
    let judge_hash = resolution_output_hash(&ResolutionOutput {
        decision_input_hash: &digest,
        proposed_outcome: &outcome,
        confidence_bp: confidence,
        rule_matches: &rule_matches,
        rule_conflicts: &rule_conflicts,
        reason_summary: &reason_summary,
        conflict_status: &status,
        conflict_explanation: conflict_explanation.as_deref(),
    })
    .map_err(|_| rejected("ai_rejected", "Resolution output could not be hashed"))?;
    let judge_provenance = provenance(&judge, "RESOLUTION_JUDGE", &digest, &judge_hash, now_ms);

    let mut counter_payload = payload.clone();
    counter_payload["judge_decision"] = Value::Object(output.clone());
    counter_payload["policy"] = json!(super::coordinator::counter_policy());
    let counter = coordinator
        .call(
            "COUNTER_JUDGE",
            &counter_payload,
            &schemas::counter(),
            Some(&judge.config),
            None,
        )
        .await?;
    artifacts.push(counter.artifact.clone());
    if counter.output["agrees"].as_bool() != Some(true) {
        return Err(CoordinatorError::Rejected {
            code: "ai_rejected".into(),
            message: "The resolution judges disagree; further review is required.".into(),
            artifacts,
        });
    }
    let counter_input = counter_judge_input_hash(&digest, &judge_provenance)
        .map_err(|_| rejected("ai_rejected", "Counter-judge input could not be hashed"))?;
    let counter_output = counter_judge_output_hash(&judge_hash, true)
        .map_err(|_| rejected("ai_rejected", "Counter-judge output could not be hashed"))?;
    let resolution = Resolution {
        schema_version: 1,
        forecast_id: forecast.forecast_id.clone(),
        specification_hash: forecast.specification_hash.clone(),
        proposed_outcome: outcome,
        confidence_bp: confidence,
        evidence: collection.snapshots.clone(),
        source_verifications: verifications,
        rule_matches,
        rule_conflicts,
        reason_summary,
        judge: judge_provenance,
        counter_judge: provenance(&counter, "COUNTER_JUDGE", &counter_input, &counter_output, now_ms),
        counter_judge_agrees: true,
        conflict_status: status,
        proposed_at_ms: now_ms,
        conflict_explanation,
    };
    if resolution.require_proposable(&forecast.specification).is_err() {
        // Carries its own code: the generic one told the operator the evidence was insufficient,
        // which sent a diagnosis after the wrong cause. The usual reason is a judge that chose the
        // right outcome and cited no clause.
        return Err(CoordinatorError::Rejected {
            code: "resolution_domain_rejected".into(),
            message: "AI resolution failed immutable domain checks".into(),
            artifacts,
        });
    }
    if let Ok(retained) = artifact("resolution", &serde_json::to_value(&resolution).unwrap_or(Value::Null)) {
        artifacts.push(retained);
    }
    Ok(ResolutionResult { resolution, artifacts })
}

fn strings(output: &serde_json::Map<String, Value>, key: &str) -> Vec<String> {
    output
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    const BODY: &str = "<html><body><main>Product X was announced today by the company.</main></body></html>";

    /// A transport that answers one document for every URL, and records which ones were asked
    /// for. Nothing here is a network.
    fn fetch_ok() -> (EvidenceFetcher, Arc<Mutex<Vec<String>>>) {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorder = seen.clone();
        let fetcher: EvidenceFetcher = Box::new(move |target, _headers| {
            recorder.lock().unwrap().push(target);
            Box::pin(async {
                Ok(TextResponse {
                    status: 200,
                    headers: vec![("content-type".to_string(), "text/html".to_string())],
                    body: BODY.to_string(),
                })
            })
        });
        (fetcher, seen)
    }

    fn specification(primary: &[&str], fallback: &[&str]) -> Value {
        let source = |id: &str, url: &str, official: bool| json!({"schema_version": 1, "source_id": id, "name": id, "url": url, "is_official": official});
        json!({"source_policy": {
            "schema_version": 1,
            "primary_sources": primary.iter().enumerate()
                .map(|(index, url)| source(&format!("p{index}"), url, true)).collect::<Vec<Value>>(),
            "fallback_sources": fallback.iter().enumerate()
                .map(|(index, url)| source(&format!("f{index}"), url, false)).collect::<Vec<Value>>(),
        }})
    }

    #[test]
    fn a_fallback_is_not_consulted_when_a_primary_source_was_retained() {
        // A question whose primary worked should not be failed by a fallback that did not, and
        // should not spend a request finding that out.
        let (fetch, seen) = fetch_ok();
        let collection = block(collect_sources(
            &fetch,
            &specification(&["https://www.apple.com/newsroom/"], &["https://www.reuters.com/feed/"]),
            100,
        ))
        .unwrap();
        assert_eq!(collection.sources.len(), 1);
        assert_eq!(collection.context["used_fallback"], false);
        assert_eq!(collection.context["unfetched_fallback_source_ids"], json!(["f0"]));
        assert_eq!(seen.lock().unwrap().len(), 1, "the fallback was never fetched");
    }

    #[test]
    fn a_fallback_is_consulted_when_no_primary_could_be_retained() {
        let (fetch, seen) = fetch_ok();
        // A primary host that is not registered is refused before any request is made.
        let collection = block(collect_sources(
            &fetch,
            &specification(&["https://evil.test/feed/"], &["https://www.reuters.com/feed/"]),
            100,
        ))
        .unwrap();
        assert_eq!(collection.sources.len(), 1);
        assert_eq!(collection.context["used_fallback"], true);
        assert_eq!(seen.lock().unwrap().len(), 1, "only the fallback was fetched");
        assert_eq!(collection.context["unavailable_sources"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn a_question_with_no_usable_source_says_which_kind_of_failure_it_was() {
        let (fetch, _) = fetch_ok();
        // Every source refused on policy, so the caller is told the sources were rejected rather
        // than unavailable — which is the difference between retrying and not.
        let refused = block(collect_sources(
            &fetch,
            &specification(&["https://evil.test/a"], &["https://evil.test/b"]),
            100,
        ))
        .unwrap_err();
        assert_eq!(refused.code(), Some("source_rejected"));

        // A transport that answers nothing is unavailable, which is retryable.
        let dead: EvidenceFetcher = Box::new(|_target, _headers| Box::pin(async { Err(()) }));
        let unavailable = block(collect_sources(
            &dead,
            &specification(&["https://www.apple.com/newsroom/"], &[]),
            100,
        ))
        .unwrap_err();
        assert_eq!(unavailable.code(), None, "unavailable carries no refusal code");
    }

    #[test]
    fn the_source_bounds_are_checked_before_anything_is_fetched() {
        let (fetch, seen) = fetch_ok();
        let too_many = specification(
            &["https://www.apple.com/newsroom/"],
            &[
                "https://www.reuters.com/a",
                "https://www.reuters.com/b",
                "https://www.reuters.com/c",
                "https://www.reuters.com/d",
            ],
        );
        assert!(block(collect_sources(&fetch, &too_many, 100)).is_err());
        assert_eq!(seen.lock().unwrap().len(), 0, "nothing was requested");
    }

    /// The reference conversation: one setting, one source, three answers, one resolution.
    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-resolution-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("resolution golden")).expect("json")
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
    fn the_reference_resolution_is_reproduced_call_for_call() {
        // This is the pipeline that decides outcomes, so the comparison is not "the same verdict"
        // but the same conversation and the same bytes: the payloads each judge was given, the
        // resolution they produced, and every artifact hash the lifecycle will commit.
        let document = golden();
        let before = forecast_domain::lifecycle::Snapshot::from_json(&document["before"].to_string())
            .expect("golden forecast")
            .base()
            .clone();
        let sources = document["sources"].as_array().expect("sources").clone();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorder = seen.clone();
        let fetch: EvidenceFetcher = Box::new(move |url, _headers| {
            let served = sources
                .iter()
                .find(|item| item["url"].as_str() == Some(url.as_str()))
                .cloned();
            recorder.lock().unwrap().push(url);
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

        let answers = Arc::new(Mutex::new(document["responses"].as_array().expect("responses").clone()));
        let taken = answers.clone();
        let asked = Arc::new(Mutex::new(Vec::new()));
        let requests = asked.clone();
        let json: super::super::coordinator::JsonFetcher = Box::new(move |url, _headers, body| {
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
                super::super::coordinator::ProviderConfig::new(
                    config["provider"].as_str().unwrap(),
                    config["model"].as_str().unwrap(),
                    config["apiKey"].as_str().unwrap(),
                    None,
                )
                .expect("provider")
            })
            .collect();
        let coordinator = Coordinator { providers, fetch: json };

        let result = block(propose_resolution(
            &coordinator,
            &fetch,
            &before,
            document["now_ms"].as_i64().unwrap(),
            document["publication_time_unknown"].as_bool().unwrap(),
            document["determined_outcome"].as_str(),
        ))
        .unwrap();

        let asked = asked.lock().unwrap();
        let expected = document["expect"]["payloads"].as_array().unwrap();
        assert_eq!(asked.len(), expected.len(), "a different number of calls was made");
        for (index, payload) in expected.iter().enumerate() {
            assert_eq!(
                &payload_of(&asked[index]),
                payload,
                "call {index} did not ask what the reference asked"
            );
        }
        assert_eq!(
            serde_json::to_value(&result.resolution).unwrap(),
            document["expect"]["resolution"],
            "the resolution differs from the reference"
        );
        for (index, item) in document["expect"]["artifacts"].as_array().unwrap().iter().enumerate() {
            assert_eq!(
                result.artifacts[index].kind,
                item["kind"].as_str().unwrap(),
                "artifact {index} kind"
            );
            assert_eq!(
                result.artifacts[index].hash,
                item["hash"].as_str().unwrap(),
                "artifact {index} hash"
            );
            assert_eq!(
                result.artifacts[index].body,
                item["body"].as_str().unwrap(),
                "artifact {index} bytes"
            );
        }
        assert_eq!(
            result.artifacts.len(),
            document["expect"]["artifacts"].as_array().unwrap().len()
        );
        assert_eq!(seen.lock().unwrap().len(), 1, "the published source was read once");
    }
}
