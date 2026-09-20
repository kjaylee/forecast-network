//! Turning the compiler's prose into the instant the lifecycle commits.
//!
//! The model is asked for a human-readable UTC deadline and told never to compute epoch
//! milliseconds. Everything downstream therefore depends on this module being exact: it is where
//! a date the model wrote in Korean, or with slashes, or as a bare time, is checked against the
//! user's own input and reduced to one integer.
//!
//! Two details are worth stating because they look like leniency and are not:
//!
//!   - An equivalent instant written differently is *not* accepted. `2026-09-21T21:00:00+09:00`
//!     is the same moment as the deadline and is still refused, because the comparison is on the
//!     literal spelling. Accepting it would mean parsing an offset, and a model that can write
//!     one deadline two ways can write two deadlines.
//!   - Anything outside a declared measurement interval that looks like a time has to be the
//!     exact closing instant. Inside the interval only the interval's own start and end may
//!     appear, because the bracket is the only thing that says what "during" means.
//!
//! Rust's `regex` has no lookaround, and the reference uses both a lookahead (a UTC spelling must
//! not be the prefix of a longer token) and a lookbehind (a bare time must not be part of a
//! longer number). Both are reproduced by scanning and checking the surrounding bytes, which is
//! what the reference's engine does internally.

use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::OnceLock;

use super::compiler_wire::COMPILER_WIRE_VERSION;
use super::coordinator::CoordinatorError;
use super::window::{measurement_window, outside_measurement_window, WindowError};

const DUPLICATE_FIELDS: [&str; 5] = [
    "schema_version",
    "candidate_ref",
    "similarity_bp",
    "materially_different_rules",
    "explanation",
];

fn refused(code: &str, message: &str) -> CoordinatorError {
    CoordinatorError::Rejected {
        code: code.to_string(),
        message: message.to_string(),
        artifacts: Vec::new(),
    }
}

fn window_refused(error: WindowError) -> CoordinatorError {
    refused(error.code, error.message)
}

fn utc_spelling() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        // The reference's trailing `(?![A-Za-z0-9:+-])` is checked by `accepted` instead.
        Regex::new(
            r"(?i)(\d{4})\s*(?:년\s*|-)(\d{1,2})\s*(?:월\s*|-)(\d{1,2})\s*(?:일\s*)?(?:T|\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:UTC|Z)",
        )
        .expect("the reference's UTC-spelling pattern compiles")
    })
}

fn iso_stamp() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})")
            .expect("the reference's stamp pattern compiles")
    })
}

fn plain_date() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"(\d{4})\s*(?:년\s*|[-/])(\d{1,2})\s*(?:월\s*|[-/])(\d{1,2})(?:일)?")
            .expect("the reference's date pattern compiles")
    })
}

fn bare_time() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"\d{1,2}:\d{2}(?::\d{2})?").expect("the reference's time pattern compiles"))
}

/// Python's `datetime(...)`: the same components, the same refusals, the same epoch.
///
/// Returning `None` where the reference raises `ValueError` is what makes an impossible date
/// (`2026-02-30`) a `compiler_deadline_invalid` rather than a silent roll-forward.
pub(crate) fn civil_ms(year: i64, month: i64, day: i64, hour: i64, minute: i64, second: i64) -> Option<i64> {
    if !(1..=9999).contains(&year) || !(1..=12).contains(&month) || !(0..=23).contains(&hour) {
        return None;
    }
    if !(0..=59).contains(&minute) || !(0..=59).contains(&second) {
        return None;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            if leap {
                29
            } else {
                28
            }
        }
    };
    if !(1..=days).contains(&day) {
        return None;
    }
    let adjusted = if month <= 2 { year - 1 } else { year };
    let era = if adjusted >= 0 { adjusted } else { adjusted - 399 } / 400;
    let year_of_era = adjusted - era * 400;
    let shifted = (month + 9) % 12;
    let day_of_year = (153 * shifted + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days_since_epoch = era * 146097 + day_of_era - 719468;
    Some((days_since_epoch * 86400 + hour * 3600 + minute * 60 + second) * 1000)
}

/// The characters a UTC spelling may not be followed by: anything that would make it part of a
/// longer token. The reference expresses this as a negative lookahead.
fn bounded(text: &str, end: usize) -> bool {
    match text[end..].chars().next() {
        None => true,
        Some(next) => !(next.is_ascii_alphanumeric() || matches!(next, ':' | '+' | '-')),
    }
}

/// The numbers of a UTC spelling, or `None` where the reference raises `ValueError`.
fn components(captures: &regex::Captures<'_>) -> Option<(i64, i64, i64, i64, i64, i64)> {
    let number = |index: usize| -> i64 {
        captures
            .get(index)
            .map(|value| value.as_str().parse::<i64>().unwrap_or(0))
            .unwrap_or(0)
    };
    let values = (number(1), number(2), number(3), number(4), number(5), number(6));
    civil_ms(values.0, values.1, values.2, values.3, values.4, values.5).map(|_| values)
}

/// Every UTC spelling the reference would match: leftmost, non-overlapping, and only when the
/// lookahead holds. A match whose lookahead fails is retried one character later, which is what
/// the reference's engine does when a match attempt at a position fails.
fn accepted<'t>(text: &'t str) -> Vec<(usize, usize, regex::Captures<'t>)> {
    let mut found = Vec::new();
    let mut search = 0;
    while search <= text.len() {
        let Some(captures) = utc_spelling().captures_at(text, search) else {
            break;
        };
        let whole = captures.get(0).expect("the whole match");
        if bounded(text, whole.end()) {
            found.push((whole.start(), whole.end(), captures));
            search = whole.end();
        } else {
            search = whole.start() + text[whole.start()..].chars().next().map_or(1, char::len_utf8);
        }
    }
    found
}

/// The reference's `utc_spelling.sub(equivalent_utc, text)`: every accepted spelling replaced by
/// the exact deadline, or the mismatch that stopped it.
fn rewrite(text: &str, timestamp: &str, instant: i64) -> Result<String, CoordinatorError> {
    let mut result = String::with_capacity(text.len());
    let mut cursor = 0;
    for (start, end, captures) in accepted(text) {
        let Some(values) = components(&captures) else {
            return Err(refused(
                "compiler_deadline_invalid",
                "The input contains an invalid UTC date.",
            ));
        };
        if civil_ms(values.0, values.1, values.2, values.3, values.4, values.5) != Some(instant) {
            return Err(refused(
                "compiler_deadline_mismatch",
                "The compiler changed the requested UTC deadline; publication is blocked.",
            ));
        }
        result.push_str(&text[cursor..start]);
        result.push_str(timestamp);
        cursor = end;
    }
    result.push_str(&text[cursor..]);
    Ok(result)
}

/// The reference's `check_dates`: no other deadline, no other date, and outside a window no other
/// time.
fn check_dates(text: &str, timestamp: &str, instant: i64, window: Option<&Value>) -> Result<(), CoordinatorError> {
    for other in iso_stamp().find_iter(text) {
        if other.as_str() != timestamp {
            return Err(refused(
                "compiler_deadline_mismatch",
                "The criteria contain conflicting deadlines.",
            ));
        }
    }
    let (year, month, day) = civil_parts(instant);
    for date in plain_date().captures_iter(text) {
        let number = |index: usize| {
            date.get(index)
                .map(|value| value.as_str().parse::<i64>().unwrap_or(0))
                .unwrap_or(0)
        };
        if (number(1), number(2), number(3)) != (year, month, day) {
            return Err(refused(
                "compiler_deadline_mismatch",
                "The question and criteria contain conflicting dates.",
            ));
        }
    }
    if window.is_some() {
        let without = text.replace(timestamp, " ");
        let mut search = 0;
        while search <= without.len() {
            let Some(found) = bare_time().find_at(&without, search) else {
                break;
            };
            // The reference's `(?<!\d)`: a bare time may not be the tail of a longer number.
            let preceded_by_digit = without[..found.start()]
                .chars()
                .next_back()
                .is_some_and(|previous| previous.is_ascii_digit());
            if !preceded_by_digit {
                return Err(refused(
                    "compiler_deadline_mismatch",
                    "Additional times outside the measurement interval need the exact closing UTC instant.",
                ));
            }
            search = found.start() + without[found.start()..].chars().next().map_or(1, char::len_utf8);
        }
    }
    Ok(())
}

pub fn civil_parts(instant: i64) -> (i64, i64, i64) {
    let days = instant.div_euclid(86_400_000);
    let shifted = days + 719468;
    let era = if shifted >= 0 { shifted } else { shifted - 146096 } / 146097;
    let day_of_era = shifted - era * 146097;
    let year_of_era = (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    (if month <= 2 { year + 1 } else { year }, month, day)
}

/// One criterion: split out the interval if it must carry one, then rewrite the times in what is
/// left.
fn normalize_time_text(
    text: &str,
    timestamp: &str,
    instant: i64,
    window: Option<&Value>,
    required_window: bool,
) -> Result<String, CoordinatorError> {
    let Some(window) = window else {
        return rewrite(text, timestamp, instant);
    };
    let (before, after, present) = outside_measurement_window(text, window, required_window).map_err(window_refused)?;
    let before = rewrite(&before, timestamp, instant)?;
    let after = rewrite(&after, timestamp, instant)?;
    check_dates(&format!("{before} {after}"), timestamp, instant, Some(window))?;
    Ok(format!(
        "{before}{}{after}",
        if present {
            window["canonical_expression"].as_str().unwrap_or_default()
        } else {
            ""
        }
    ))
}

/// `_normalize_compiler_output`.
pub fn normalize_compiler_output(
    output: &Map<String, Value>,
    original_question: &str,
    candidate_context: &[Value],
    distinct_windows: bool,
) -> Result<Value, CoordinatorError> {
    if output.get("compiler_wire_version").and_then(Value::as_str) != Some(COMPILER_WIRE_VERSION) {
        return Err(refused("compiler_wire_version", "Compiler wire version mismatch"));
    }
    let lookup: Vec<&Value> = candidate_context
        .iter()
        .filter(|item| item["candidate_ref"].is_string())
        .collect();
    let window = measurement_window(original_question).map_err(window_refused)?;

    let mut duplicates = Vec::new();
    let mut distinct: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for item in output["duplicate_candidates"].as_array().cloned().unwrap_or_default() {
        let fields: Vec<&String> = item
            .as_object()
            .map(|fields| fields.keys().collect())
            .unwrap_or_default();
        if fields.len() != DUPLICATE_FIELDS.len() || !DUPLICATE_FIELDS.iter().all(|name| item.get(name).is_some()) {
            return Err(refused(
                "compiler_candidate_fields",
                "Duplicate output must use only the current reference wire",
            ));
        }
        let Some(reference) = item["candidate_ref"].as_str() else {
            return Err(refused(
                "compiler_candidate_reference",
                "Duplicate candidate reference is unknown or repeated",
            ));
        };
        if seen.iter().any(|used| used == reference) {
            return Err(refused(
                "compiler_candidate_reference",
                "Duplicate candidate reference is unknown or repeated",
            ));
        }
        let Some(candidate) = lookup
            .iter()
            .find(|entry| entry["candidate_ref"].as_str() == Some(reference))
        else {
            return Err(refused(
                "compiler_candidate_reference",
                "Duplicate candidate reference is unknown or repeated",
            ));
        };
        seen.push(reference.to_string());
        // The model answered with a reference; the verified identity is substituted back, so the
        // similarity it asserted is bound to a forecast it was actually shown.
        let mut resolved = item.as_object().cloned().unwrap_or_default();
        resolved.remove("candidate_ref");
        resolved.insert("forecast_id".to_string(), candidate["forecast_id"].clone());
        resolved.insert(
            "specification_hash".to_string(),
            candidate["specification_hash"].clone(),
        );
        if distinct_windows {
            if let (Some(window), Some(other)) = (window.as_ref(), super::window::candidate_window(candidate)) {
                if other["canonical_expression"] != window["canonical_expression"] {
                    // A declared canonical series: the same predicate over a different explicit
                    // interval is a separate measurement contract, not a duplicate. The raw
                    // model verdict stays in its artifact; only the normalized record changes.
                    resolved.insert("materially_different_rules".to_string(), json!(true));
                    resolved.insert(
                        "explanation".to_string(),
                        json!(format!(
                            "Distinct measurement interval {} versus {}: separate canonical episode. {}",
                            window["canonical_expression"].as_str().unwrap_or_default(),
                            other["canonical_expression"].as_str().unwrap_or_default(),
                            resolved.get("explanation").and_then(Value::as_str).unwrap_or_default(),
                        )),
                    );
                    distinct.push(
                        candidate["forecast_id"]
                            .as_str()
                            .map(str::to_string)
                            .unwrap_or_else(|| candidate["forecast_id"].to_string()),
                    );
                }
            }
        }
        duplicates.push(Value::Object(resolved));
    }

    let Some(timestamp) = output["close_at_utc"].as_str() else {
        return Err(refused(
            "compiler_deadline_timezone",
            "The deadline must specify an exact UTC time.",
        ));
    };
    if timestamp.len() != 20
        || !{
            let bytes = timestamp.as_bytes();
            bytes[4] == b'-'
                && bytes[7] == b'-'
                && bytes[10] == b'T'
                && bytes[13] == b':'
                && bytes[16] == b':'
                && bytes[19] == b'Z'
                && bytes
                    .iter()
                    .enumerate()
                    .all(|(index, byte)| matches!(index, 4 | 7 | 10 | 13 | 16 | 19) || byte.is_ascii_digit())
        }
    {
        return Err(refused(
            "compiler_deadline_timezone",
            "The deadline must specify an exact UTC time.",
        ));
    }
    let number = |from: usize, to: usize| timestamp[from..to].parse::<i64>().unwrap_or(0);
    let instant = civil_ms(
        number(0, 4),
        number(5, 7),
        number(8, 10),
        number(11, 13),
        number(14, 16),
        number(17, 19),
    )
    .ok_or_else(|| {
        refused(
            "compiler_deadline_invalid",
            "The deadline contains an invalid date or time.",
        )
    })?;

    if let Some(window) = window.as_ref() {
        if window["end_at_utc"].as_str() != Some(timestamp) {
            return Err(refused(
                "compiler_deadline_mismatch",
                "The exclusive measurement end must equal the closing deadline.",
            ));
        }
    }
    let original_outside = match window.as_ref() {
        None => original_question.to_string(),
        Some(window) => {
            let (before, after, _) =
                outside_measurement_window(original_question, window, true).map_err(window_refused)?;
            format!("{before} {after}")
        }
    };
    // The user's own words are checked too: a model can only be held to a deadline the question
    // actually stated, in either language.
    for (_, _, captures) in accepted(&original_outside) {
        let Some(values) = components(&captures) else {
            return Err(refused(
                "compiler_deadline_invalid",
                "The input contains an invalid UTC date.",
            ));
        };
        if civil_ms(values.0, values.1, values.2, values.3, values.4, values.5) != Some(instant) {
            return Err(refused(
                "compiler_deadline_mismatch",
                "The compiler changed the requested UTC deadline; publication is blocked.",
            ));
        }
    }
    if window.is_some() {
        check_dates(
            &rewrite(&original_outside, timestamp, instant)?,
            timestamp,
            instant,
            window.as_ref(),
        )?;
    }

    let mut normalized = output.clone();
    normalized.remove("close_at_utc");
    normalized.remove("compiler_wire_version");
    normalized.insert("duplicate_candidates".to_string(), Value::Array(duplicates));
    normalized.insert(
        "canonical_question".to_string(),
        json!(normalize_time_text(
            output["canonical_question"].as_str().unwrap_or_default(),
            timestamp,
            instant,
            window.as_ref(),
            window.is_some(),
        )?),
    );
    let mut rules = Vec::new();
    for rule in output["rules"].as_array().cloned().unwrap_or_default() {
        // Only a YES/NO criterion has to carry the interval: an INVALID clause is about identity,
        // not about time.
        let required = window.is_some() && matches!(rule["outcome"].as_str(), Some("YES") | Some("NO"));
        let mut rewritten = rule.as_object().cloned().unwrap_or_default();
        rewritten.insert(
            "condition".to_string(),
            json!(normalize_time_text(
                rule["condition"].as_str().unwrap_or_default(),
                timestamp,
                instant,
                window.as_ref(),
                required,
            )?),
        );
        rules.push(Value::Object(rewritten));
    }
    normalized.insert("rules".to_string(), Value::Array(rules));
    if window.is_some() {
        let mut invalidation = Vec::new();
        for text in output["invalidation_rules"].as_array().cloned().unwrap_or_default() {
            invalidation.push(json!(normalize_time_text(
                text.as_str().unwrap_or_default(),
                timestamp,
                instant,
                window.as_ref(),
                false,
            )?));
        }
        normalized.insert("invalidation_rules".to_string(), Value::Array(invalidation));
    }
    if !distinct.is_empty() {
        normalized.insert("_distinct_measurement_windows".to_string(), json!(distinct));
    }

    // Every public text that states the deadline has to state the same one.
    let mut texts = vec![normalized["canonical_question"]
        .as_str()
        .unwrap_or_default()
        .to_string()];
    for rule in normalized["rules"].as_array().cloned().unwrap_or_default() {
        if matches!(rule["outcome"].as_str(), Some("YES") | Some("NO")) {
            texts.push(rule["condition"].as_str().unwrap_or_default().to_string());
        }
    }
    if texts.iter().any(|text| !text.contains(timestamp)) {
        return Err(refused(
            "compiler_deadline_mismatch",
            "The question and YES/NO criteria must use the same deadline.",
        ));
    }
    if window.is_none() {
        let mut every = vec![normalized["canonical_question"]
            .as_str()
            .unwrap_or_default()
            .to_string()];
        for rule in normalized["rules"].as_array().cloned().unwrap_or_default() {
            every.push(rule["condition"].as_str().unwrap_or_default().to_string());
        }
        for text in every {
            check_dates(&text, timestamp, instant, None)?;
        }
    }
    normalized.insert("close_at_ms".to_string(), json!(instant));
    Ok(Value::Object(normalized))
}

/// `datetime.fromtimestamp(ms / 1000, utc).date().isoformat()`.
pub fn date_of(instant_ms: i64) -> String {
    let (year, month, day) = civil_parts(instant_ms);
    format!("{year:04}-{month:02}-{day:02}")
}

/// `datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000`.
///
/// Used for a publication instant the article parser read, which is always either a `Z`-suffixed
/// instant or an offset one; anything it cannot read is no instant at all rather than a guess.
pub fn parse_utc_instant(value: &str) -> Option<i64> {
    let normalized = value.replace('Z', "+00:00");
    let (body, offset) = if normalized.len() > 6
        && (normalized.as_bytes()[normalized.len() - 6] == b'+' || normalized.as_bytes()[normalized.len() - 6] == b'-')
    {
        let sign = if normalized.as_bytes()[normalized.len() - 6] == b'-' {
            -1
        } else {
            1
        };
        let tail = &normalized[normalized.len() - 5..];
        let hours = tail.get(..2)?.parse::<i64>().ok()?;
        let minutes = tail.get(3..5)?.parse::<i64>().ok()?;
        if hours >= 24 || minutes >= 60 {
            return None;
        }
        (
            &normalized[..normalized.len() - 6],
            sign * (hours * 3600 + minutes * 60),
        )
    } else {
        (normalized.as_str(), 0)
    };
    let (date, time) = body.split_once(['T', 't'])?;
    let mut fields = date.split('-');
    let year = fields.next()?.parse::<i64>().ok()?;
    let month = fields.next()?.parse::<i64>().ok()?;
    let day = fields.next()?.parse::<i64>().ok()?;
    if fields.next().is_some() {
        return None;
    }
    let mut parts = time.split(':');
    let hour = parts.next()?.parse::<i64>().ok()?;
    let minute = parts.next()?.parse::<i64>().ok()?;
    let seconds = parts.next().unwrap_or("0");
    let (second, fraction) = seconds.split_once('.').unwrap_or((seconds, ""));
    let second = second.parse::<i64>().ok()?;
    let millis = if fraction.is_empty() {
        0
    } else {
        let places = fraction.len().min(6);
        fraction[..places].parse::<i64>().ok()? * 10i64.pow(3 - places.min(3) as u32)
    };
    Some(civil_ms(year, month, day, hour, minute, second)? + millis - offset * 1000)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-compiler-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("compiler golden")).expect("json")
    }

    #[test]
    fn every_normalization_case_decides_what_the_reference_decided() {
        // The corpus is deliberately adversarial: an equivalent offset, a Korean date that does
        // not match, a moved measurement interval, a bare time left outside the window, and the
        // two sides of the declared-series rule.
        let document = golden();
        let cases = document["normalize"].as_array().expect("cases");
        assert!(cases.len() >= 25, "the corpus lost its breadth");
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let output = case["output"].as_object().expect("output").clone();
            let expected = match super::normalize_compiler_output(
                &output,
                case["question"].as_str().unwrap(),
                case["candidates"].as_array().unwrap(),
                case["distinct_windows"].as_bool().unwrap(),
            ) {
                Ok(value) => value,
                Err(error) => {
                    let wanted = &case["error"]["code"];
                    if wanted.is_null() {
                        panic!("{name}: refused with {:?} but the reference accepted it", error.code());
                    }
                    assert_eq!(error.code(), wanted.as_str(), "{name}: a different refusal");
                    continue;
                }
            };
            assert_eq!(
                expected, case["normalized"],
                "{name}: the normalized record differs from the reference"
            );
        }
    }
}
