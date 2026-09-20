//! The measurement interval a compiled question has to name exactly once, if it names one.
//!
//! "Never infer time roles" is the whole rule: the interval is recognised from explicit half-open
//! ISO UTC brackets and nothing is concluded from a bare date. A question that says "during
//! September" has no measurement window, and one that says `[a, b)` has exactly that one.

use regex::Regex;
use serde_json::{json, Value};
use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowError {
    pub message: &'static str,
    pub code: &'static str,
}

fn error(message: &'static str) -> WindowError {
    WindowError {
        message,
        code: "compiler_measurement_window",
    }
}

fn utc_window() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"\[\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*,\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\)")
            .expect("the reference's window pattern compiles")
    })
}

fn window_open() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"[\[(]\s*\d{4}-\d{2}-\d{2}T[^,\]\)]{0,100},").expect("the reference's pattern compiles")
    })
}

/// The one explicit interval the text names, or none.
///
/// A second interval, or a bracket that opens without closing, is refused rather than resolved:
/// two windows in one question is a question about which one governs.
pub fn measurement_window(text: &str) -> Result<Option<Value>, WindowError> {
    let matches: Vec<_> = utc_window().captures_iter(text).collect();
    if matches.len() > 1 || window_open().find_iter(text).count() != matches.len() {
        return Err(error("Use one exact half-open UTC measurement interval [start, end)."));
    }
    let Some(captures) = matches.first() else {
        return Ok(None);
    };
    let start = captures.get(1).map(|value| value.as_str()).unwrap_or_default();
    let end = captures.get(2).map(|value| value.as_str()).unwrap_or_default();
    let (Some(start_ms), Some(end_ms)) = (
        crate::article::instant_from_str(start),
        crate::article::instant_from_str(end),
    ) else {
        return Err(error("The measurement interval has an invalid UTC date."));
    };
    if start_ms >= end_ms {
        return Err(error("Measurement start must precede its exclusive end."));
    }
    Ok(Some(json!({
        "version": "single-bracket-utc-window-v1",
        "start_at_utc": start,
        "end_at_utc": end,
        "start_at_ms": start_ms,
        "end_at_ms": end_ms,
        "start_inclusive": true,
        "end_exclusive": true,
        "input_expression": captures.get(0).map(|value| value.as_str()).unwrap_or_default(),
        "canonical_expression": format!("[{start}, {end})"),
    })))
}

/// Split the interval out of a criterion, insisting on the one the question declared.
///
/// Returns the text either side of it and whether it was there at all. `required` is what makes
/// the absence a refusal rather than an absence.
pub fn outside_measurement_window(
    text: &str,
    window: &Value,
    required: bool,
) -> Result<(String, String, bool), WindowError> {
    let matches: Vec<_> = utc_window().captures_iter(text).collect();
    if matches.len() > 1 || window_open().find_iter(text).count() != matches.len() || (required && matches.len() != 1) {
        return Err(error(
            "Preserve the exact measurement interval once in each question and YES/NO criterion.",
        ));
    }
    let Some(captures) = matches.first() else {
        return Ok((text.to_string(), String::new(), false));
    };
    let groups = (
        captures.get(1).map(|value| value.as_str()).unwrap_or_default(),
        captures.get(2).map(|value| value.as_str()).unwrap_or_default(),
    );
    if groups
        != (
            window["start_at_utc"].as_str().unwrap_or_default(),
            window["end_at_utc"].as_str().unwrap_or_default(),
        )
    {
        return Err(error("The compiler changed measurement start or end."));
    }
    let whole = captures.get(0).expect("the whole match");
    Ok((text[..whole.start()].to_string(), text[whole.end()..].to_string(), true))
}

/// A retained candidate's own explicit interval, if it declares exactly one.
pub fn candidate_window(candidate: &Value) -> Option<Value> {
    let question = candidate["specification"]["canonical_question"].as_str()?;
    measurement_window(question).ok().flatten()
}

#[cfg(test)]
mod golden {
    use super::*;
    use std::path::PathBuf;

    fn corpus() -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-window-golden.json");
        serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
        )
        .expect("golden JSON")
    }

    #[test]
    fn the_window_policy_decides_the_same_as_python() {
        // 4,000 generated cases agreed before this subset was kept.
        let corpus = corpus();
        let mut wrong = Vec::new();
        for case in corpus["windows"].as_array().expect("windows") {
            let body = case[0].as_str().expect("text");
            let accepted = case[2].as_bool().expect("accepted");
            let expected = case[3].as_str();
            let got = measurement_window(body);
            let matches = match (&got, accepted) {
                (Ok(value), true) => value.as_ref() == Some(&case[1]),
                (Err(error), false) => Some(error.code) == expected,
                _ => false,
            };
            if !matches && wrong.len() < 8 {
                wrong.push(format!(
                    "{body:?} -> {got:?}, Python said accepted={accepted} code={expected:?} value={}",
                    case[1]
                ));
            }
        }
        for case in corpus["outer"].as_array().expect("outer") {
            let body = case[0].as_str().expect("text");
            let window = &case[1];
            let required = case[2].as_bool().expect("required");
            let accepted = case[4].as_bool().expect("accepted");
            let expected = case[5].as_str();
            let got = outside_measurement_window(body, window, required);
            let matches = match (&got, accepted) {
                (Ok((before, after, present)), true) => {
                    let want = case[3].as_array().expect("expected parts");
                    before == want[0].as_str().unwrap()
                        && after == want[1].as_str().unwrap()
                        && *present == want[2].as_bool().unwrap()
                }
                (Err(error), false) => Some(error.code) == expected,
                _ => false,
            };
            if !matches && wrong.len() < 8 {
                wrong.push(format!(
                    "outer {body:?} -> {got:?}, Python said accepted={accepted} code={expected:?}"
                ));
            }
        }
        assert!(
            wrong.is_empty(),
            "the measurement window disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_half_open_interval_is_recognized_and_its_bounds_are_exclusive_at_the_end() {
        let window = measurement_window("During [2026-09-16T00:00:00Z, 2026-09-18T00:00:00Z), something.")
            .unwrap()
            .unwrap();
        assert_eq!(window["start_at_utc"], "2026-09-16T00:00:00Z");
        assert_eq!(window["end_at_utc"], "2026-09-18T00:00:00Z");
        assert_eq!(window["start_inclusive"], true);
        assert_eq!(window["end_exclusive"], true);
        assert_eq!(
            window["canonical_expression"],
            "[2026-09-16T00:00:00Z, 2026-09-18T00:00:00Z)"
        );
        assert_eq!(
            window["end_at_ms"].as_i64().unwrap() - window["start_at_ms"].as_i64().unwrap(),
            2 * 86_400_000
        );
    }

    #[test]
    fn prose_without_an_interval_has_none_and_is_not_an_error() {
        assert!(measurement_window("Will it rain in September?").unwrap().is_none());
    }

    #[test]
    fn two_intervals_or_an_unclosed_bracket_are_refused_rather_than_resolved() {
        let double = "[2026-09-16T00:00:00Z, 2026-09-18T00:00:00Z) and [2026-09-19T00:00:00Z, 2026-09-20T00:00:00Z)";
        assert!(measurement_window(double).is_err());
        assert!(measurement_window("During [2026-09-16T00:00:00Z, sometime later").is_err());
    }

    #[test]
    fn a_backwards_or_impossible_interval_is_refused() {
        assert!(measurement_window("[2026-09-18T00:00:00Z, 2026-09-16T00:00:00Z)").is_err());
        assert!(measurement_window("[2026-09-18T00:00:00Z, 2026-09-18T00:00:00Z)").is_err());
        assert!(measurement_window("[2026-13-01T00:00:00Z, 2026-13-02T00:00:00Z)").is_err());
    }

    #[test]
    fn a_criterion_has_to_keep_the_interval_the_question_declared() {
        let window = measurement_window("[2026-09-16T00:00:00Z, 2026-09-18T00:00:00Z)")
            .unwrap()
            .unwrap();
        let (before, after, present) =
            outside_measurement_window("Within [2026-09-16T00:00:00Z, 2026-09-18T00:00:00Z) yes", &window, true)
                .unwrap();
        assert_eq!((before.as_str(), after.as_str(), present), ("Within ", " yes", true));
        // A criterion that omits the interval is refused when the question declared one.
        assert!(outside_measurement_window("Within the period yes", &window, true).is_err());
        // A criterion that changes it is refused.
        assert!(
            outside_measurement_window("Within [2026-09-16T00:00:00Z, 2026-09-19T00:00:00Z) yes", &window, true)
                .is_err()
        );
        // And when none was declared, its absence is simply an absence.
        let (_, _, present) = outside_measurement_window("no interval here", &Value::Null, false).unwrap();
        assert!(!present);
    }
}
