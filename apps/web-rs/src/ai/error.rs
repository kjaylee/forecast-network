//! The AI failure → HTTP failure table.
//!
//! Every refusal an AI task produces has to become a status and a message a person can act on, and
//! the reference's table is opinionated in two ways worth keeping. First, the *unknown* codes are
//! deliberately neutral: a rejection this layer does not recognise is a 502 with a message that
//! says nothing about the exception, because echoing model output to a caller turns a diagnostic
//! into an oracle. Second, the codes that mean "the user has to say more" are told apart from the
//! codes that mean "try again later", which is the difference between a 422 and a 502.

use super::coordinator::CoordinatorError;

/// An application error, as the route layer reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AiError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl AiError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

/// The codes that mean the deadline the question states is not usable, in the reference's order.
const DEADLINE_CODES: [&str; 4] = [
    "compiler_deadline_timezone",
    "compiler_deadline_invalid",
    "compiler_deadline_mismatch",
    "compiler_deadline_range",
];

/// The codes that mean the model's answer was malformed rather than wrong.
const OUTPUT_CODES: [&str; 9] = [
    "ai_output_size",
    "ai_output_json",
    "ai_output_type",
    "ai_output_enum",
    "ai_output_fields",
    "ai_output_text_length",
    "ai_output_range",
    "ai_output_incomplete",
    "compiler_domain_validation",
];

/// `Application._ai_error`.
///
/// `source_failure` is the reference's `isinstance(exc.__cause__, SourceUnavailable)`: the caller
/// knows what it was awaiting, and this layer only knows what came back. The other half of that
/// test — a retained `source-failure` artifact — is read here, because the artifacts travel with
/// the error.
pub fn ai_error(error: &CoordinatorError, source_failure: bool) -> AiError {
    match error {
        CoordinatorError::Unavailable { artifacts, .. } => {
            let from_artifacts = artifacts.iter().any(|artifact| artifact.kind == "source-failure");
            if source_failure || from_artifacts {
                return AiError::new(
                    503,
                    "source_temporarily_unavailable",
                    "The official evidence page could not be reached. Try again later or specify an official text page that opens without signing in.",
                );
            }
            AiError::new(
                503,
                "ai_unavailable",
                "The AI provider is unavailable. Please try again later.",
            )
        }
        CoordinatorError::Rejected { code, .. } => {
            if DEADLINE_CODES.contains(&code.as_str()) {
                return AiError::new(
                    422,
                    "deadline_clarification_required",
                    "Specify the closing date, time, and time zone. The question and resolution rules must use the same deadline.",
                );
            }
            match code.as_str() {
                "question_already_resolved" => AiError::new(
                    422,
                    "question_already_resolved",
                    "Official evidence already answers this question. Choose an unresolved future event.",
                ),
                "source_rejected" => AiError::new(
                    502,
                    "source_not_usable",
                    "The selected evidence page could not be read or is unsupported. Specify a shorter official text page that opens without signing in, then request another review.",
                ),
                "resolution_domain_rejected" => AiError::new(
                    502,
                    "resolution_domain_rejected",
                    "The AI resolution was refused by the immutable domain checks. The retained judge output records what it proposed.",
                ),
                "compiler_not_publishable" => AiError::new(
                    422,
                    "specification_needs_review",
                    "The AI review could not approve the resolution criteria. Clarify the subject, the fact to verify, and the deadline before requesting another review.",
                ),
                // Unknown codes and mixed legacy rejection cases are deliberately neutral. Never
                // echo exception text or classify them as user error.
                other if OUTPUT_CODES.contains(&other) => AiError::new(
                    502,
                    "ai_response_invalid",
                    "The AI response was incomplete or did not match the required format. Please try again later.",
                ),
                _ => AiError::new(
                    502,
                    "ai_review_incomplete",
                    "The AI review could not be completed. Please try again later.",
                ),
            }
        }
    }
}

/// `_ai_error`'s last line: anything that is neither an `AIRejected` nor an `AIUnavailable`.
pub fn ai_validation_failed() -> AiError {
    AiError::new(
        502,
        "ai_validation_failed",
        "The AI result failed validation. No result was finalized.",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai::coordinator::Artifact;
    use serde_json::Value;

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-error-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("ai error golden")).expect("json")
    }

    fn rejected(code: &str) -> CoordinatorError {
        CoordinatorError::Rejected {
            code: code.to_string(),
            message: "model text".to_string(),
            artifacts: Vec::new(),
        }
    }

    fn unavailable(artifacts: Vec<Artifact>) -> CoordinatorError {
        CoordinatorError::Unavailable {
            providers: Vec::new(),
            artifacts,
        }
    }

    fn artifact(kind: &'static str) -> Artifact {
        Artifact {
            hash: "0".repeat(64),
            kind,
            body: "{}".to_string(),
        }
    }

    #[test]
    fn the_reference_ai_error_table_is_reproduced_case_for_case() {
        // Every branch, including the ones a port would not think to write: the deliberately
        // neutral fallthrough, the two code *sets*, and both ways a source failure is recognised.
        let document = golden();
        for entry in document["cases"].as_array().expect("cases") {
            let name = entry["name"].as_str().unwrap();
            let expected = &entry["result"];
            let produced = match name {
                "not-an-ai-error" => ai_validation_failed(),
                "unavailable:source-cause" => ai_error(&unavailable(Vec::new()), true),
                "unavailable:source-artifact" => ai_error(&unavailable(vec![artifact("source-failure")]), false),
                "unavailable:provider" => ai_error(&unavailable(Vec::new()), false),
                other => {
                    let code = other.strip_prefix("rejected:").unwrap();
                    let code = if code == "<empty>" { "" } else { code };
                    ai_error(&rejected(code), false)
                }
            };
            assert_eq!(
                produced.status as i64,
                expected["status"].as_i64().unwrap(),
                "{name}: status"
            );
            assert_eq!(produced.code, expected["code"].as_str().unwrap(), "{name}: code");
            assert_eq!(
                produced.message,
                expected["message"].as_str().unwrap(),
                "{name}: message"
            );
        }
    }

    #[test]
    fn an_unrecognised_code_never_reaches_the_caller_as_text() {
        // The reference's own reason for the neutral fallthrough: echoing model output turns a
        // diagnostic into an oracle, and the neutrality has to hold for a code nobody wrote a
        // branch for.
        let mapped = ai_error(&rejected("something_new"), false);
        assert_eq!(mapped.code, "ai_review_incomplete");
        assert!(
            !mapped.message.contains("model text"),
            "the exception's text reached the caller"
        );
    }
}
