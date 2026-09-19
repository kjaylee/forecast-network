//! What a publication timestamp is allowed to mean, ported from `source_watch`.
//!
//! `_publication_date` and `_jsonld_publication` are the two places a page can state when it
//! was published, and the distinction they draw is the one the resolution guard turns on:
//! only an `instant` can be placed before or after the moment participation closed, and a
//! bare calendar day never can. A port that called `2026-09-15` an instant would place
//! evidence it cannot place.
//!
//! The caller, `article_content`, is not ported: it needs Python's `HTMLParser`, which is
//! not an HTML5 parser and does not recover from malformed markup the way a browser does.
//! Reproducing it means reproducing that state machine, and `html.unescape`'s several
//! thousand named references, neither of which Rust's `regex` crate can express — `regex`
//! has no lookbehind, and both `locatestarttagend_tolerant` and `attrfind_tolerant` need it.
//!
//! These two are the part of the answer that does not depend on the tokenizer, and they are
//! held to vectors exported from Python by the tests below.

use serde_json::Value;

/// One distinct publication time was found among the structured data; more than one is not
/// a tie to break, it is a page that disagrees with itself.
const ARTICLE_TYPES: [&str; 3] = ["Article", "NewsArticle", "BlogPosting"];
const SCHEMA_PREFIXES: [&str; 2] = ["https://schema.org/", "http://schema.org/"];
const MAX_NODES: usize = 128;
const MAX_DEPTH: usize = 8;

pub const UNKNOWN: &str = "unknown";
pub const DATE: &str = "date";
pub const INSTANT: &str = "instant";

/// `(date, precision)`, exactly as the Python reference returns it.
pub type Publication = (Option<String>, &'static str);

fn unknown() -> Publication {
    (None, UNKNOWN)
}

fn leap_year(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap_year(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn two_digits(bytes: &[u8]) -> Option<i64> {
    if bytes.len() == 2 && bytes.iter().all(u8::is_ascii_digit) {
        Some((bytes[0] - b'0') as i64 * 10 + (bytes[1] - b'0') as i64)
    } else {
        None
    }
}

fn four_digits(bytes: &[u8]) -> Option<i64> {
    if bytes.len() == 4 && bytes.iter().all(u8::is_ascii_digit) {
        Some(bytes.iter().fold(0i64, |acc, b| acc * 10 + (b - b'0') as i64))
    } else {
        None
    }
}

/// A calendar day that exists. `2026-02-29` does not, and neither does `2026-09-32`.
///
/// Year zero is also not a day: `datetime.MINYEAR` is 1, so the reference refuses it and
/// `0000-10-28T02:00:00Z` is `unknown` there while it is a perfectly shaped instant here.
/// The fuzz corpus is what found that, and it is the kind of difference that would
/// otherwise have placed evidence the reference could not place.
fn valid_day(year: &[u8], month: &[u8], day: &[u8]) -> bool {
    let (Some(year), Some(month), Some(day)) = (four_digits(year), two_digits(month), two_digits(day)) else {
        return false;
    };
    year >= 1 && (1..=12).contains(&month) && day >= 1 && day <= days_in_month(year, month)
}

/// A clock time and zone that exist. Python validates these rather than trusting the shape.
fn valid_clock(hour: &[u8], minute: &[u8], second: &[u8], zone: &str) -> bool {
    let (Some(hour), Some(minute), Some(second)) = (two_digits(hour), two_digits(minute), two_digits(second)) else {
        return false;
    };
    if hour > 23 || minute > 59 || second > 59 {
        return false;
    }
    if zone == "Z" {
        return true;
    }
    let offset = &zone.as_bytes()[1..];
    if offset.len() != 5 || offset[2] != b':' {
        return false;
    }
    let (Some(offset_hour), Some(offset_minute)) = (two_digits(&offset[..2]), two_digits(&offset[3..])) else {
        return false;
    };
    // The reference builds a `timedelta` from these and only rejects a total of 24 hours or
    // more, so `+09:60` is ten hours and perfectly valid while `+24:00` is not. Validating
    // the fields against a clock instead of against the sum refuses instants the reference
    // accepts -- and the fuzz corpus found exactly that.
    offset_hour * 60 + offset_minute < 24 * 60
}

/// The reference's two accepted shapes, matched over the whole string and nothing else.
///
/// `re.fullmatch` is the whole of this: a fractional second, a missing zone, or a space
/// where the `T` belongs is not a near miss, it is a different string, and the reference
/// answers `unknown` for it.
pub fn publication_date(raw: Option<&Value>) -> Publication {
    let Some(Value::String(text)) = raw else {
        return unknown();
    };
    let bytes = text.as_bytes();
    // `\d{4}-\d{2}-\d{2}Z?`
    if bytes.len() == 10 || (bytes.len() == 11 && bytes[10] == b'Z') {
        let day = &bytes[..10];
        if day[4] == b'-' && day[7] == b'-' && valid_day(&day[..4], &day[5..7], &day[8..10]) {
            return (Some(text[..10].to_string()), DATE);
        }
        return unknown();
    }
    // `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})`
    if bytes.len() < 20 {
        return unknown();
    }
    let day = &bytes[..10];
    let shape = day[4] == b'-' && day[7] == b'-' && bytes[10] == b'T' && bytes[13] == b':' && bytes[16] == b':';
    let zone = &text[19..];
    let zone_ok = zone == "Z" || (zone.len() == 6 && (zone.starts_with('+') || zone.starts_with('-')));
    if shape
        && zone_ok
        && valid_day(&day[..4], &day[5..7], &day[8..10])
        && valid_clock(&bytes[11..13], &bytes[14..16], &bytes[17..19], zone)
    {
        return (Some(text.clone()), INSTANT);
    }
    unknown()
}

fn article_type(value: &Value) -> bool {
    let kinds: Vec<&Value> = match value {
        Value::String(_) => vec![value],
        Value::Array(items) => items.iter().collect(),
        _ => return false,
    };
    kinds.iter().any(|kind| {
        kind.as_str().is_some_and(|text| {
            let stripped = SCHEMA_PREFIXES
                .iter()
                .find_map(|prefix| text.strip_prefix(prefix))
                .unwrap_or(text);
            ARTICLE_TYPES.contains(&stripped)
        })
    })
}

/// Search the structured data for a publication time, bounded like the reference.
///
/// The bounds are not decoration: a page that buries a date past them is a page whose date
/// is not meant to be found, and the reference refuses to find it.
pub fn jsonld_publication(scripts: &[String]) -> Publication {
    let mut found: Vec<Publication> = Vec::new();
    for script in scripts {
        let Ok(document) = serde_json::from_str::<Value>(script) else {
            continue;
        };
        let mut pending: Vec<(Value, usize)> = vec![(document, 0)];
        let mut nodes = 0usize;
        while let Some((item, depth)) = pending.pop() {
            if nodes >= MAX_NODES {
                break;
            }
            nodes += 1;
            if depth > MAX_DEPTH {
                continue;
            }
            match item {
                Value::Array(entries) => {
                    for entry in entries.into_iter().take(MAX_NODES) {
                        pending.push((entry, depth + 1));
                    }
                }
                Value::Object(mut fields) => {
                    if fields.get("@type").is_some_and(article_type) {
                        let published = publication_date(fields.get("datePublished"));
                        if published.0.is_some() && !found.contains(&published) {
                            found.push(published);
                        }
                    }
                    if let Some(graph) = fields.remove("@graph") {
                        pending.push((graph, depth + 1));
                    }
                }
                _ => {}
            }
        }
    }
    // Conflicting article publication metadata remains unknown for review.
    if found.len() == 1 {
        found.pop().expect("checked")
    } else {
        unknown()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// Exported from the Python parser by `scripts/generate_article_golden.py`, which
    /// `check.py` also runs with `--check` so a change there fails rather than leaving
    /// these vectors describing a parser that no longer exists.
    fn golden() -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/article-content-golden.json");
        let text = std::fs::read_to_string(&path).unwrap_or_else(|error| {
            panic!(
                "{} is generated by scripts/generate_article_golden.py: {error}",
                path.display()
            )
        });
        serde_json::from_str(&text).expect("golden JSON")
    }

    fn expected(entry: &Value) -> Publication {
        let date = entry["date"].as_str().map(str::to_string);
        let precision = match entry["precision"].as_str().expect("precision") {
            "unknown" => UNKNOWN,
            "date" => DATE,
            "instant" => INSTANT,
            other => panic!("unknown precision in the golden: {other}"),
        };
        (date, precision)
    }

    #[test]
    fn the_reference_dates_are_reproduced() {
        let golden = golden();
        let entries = golden["datetimes"].as_array().expect("datetimes");
        assert!(entries.len() > 15, "the vectors must actually be there");
        for entry in entries {
            let raw = entry["input"].clone();
            let input = if raw.is_null() { None } else { Some(&raw) };
            assert_eq!(
                publication_date(input),
                expected(entry),
                "publication date disagrees with Python for {:?}",
                entry["input"]
            );
        }
    }

    #[test]
    fn the_reference_structured_data_is_reproduced() {
        let golden = golden();
        let entries = golden["jsonld"].as_array().expect("jsonld");
        assert!(entries.len() > 10, "the vectors must actually be there");
        for entry in entries {
            let scripts: Vec<String> = entry["scripts"]
                .as_array()
                .expect("scripts")
                .iter()
                .map(|script| script.as_str().expect("script text").to_string())
                .collect();
            assert_eq!(
                jsonld_publication(&scripts),
                expected(entry),
                "structured-data date disagrees with Python for {scripts:?}"
            );
        }
    }

    #[test]
    fn the_reference_dates_are_reproduced_at_the_boundaries() {
        // Hand-written vectors cover the rules someone thought of. These cover the
        // boundaries between them, which is where a port goes wrong: a leap day, an offset
        // of `+24:00`, a zone in lowercase, a valid shape with one digit changed.
        let golden = golden();
        let fuzz = &golden["fuzz"];
        let entries = fuzz["datetimes"].as_array().expect("fuzz datetimes");
        assert!(entries.len() > 400, "the fuzz corpus must actually be there");
        for entry in entries {
            let raw = entry["input"].clone();
            let input = if raw.is_null() { None } else { Some(&raw) };
            assert_eq!(
                publication_date(input),
                expected(entry),
                "publication date disagrees with Python for {:?}",
                entry["input"]
            );
        }
    }

    #[test]
    fn the_reference_structured_data_is_reproduced_at_the_boundaries() {
        let golden = golden();
        let entries = golden["fuzz"]["jsonld"].as_array().expect("fuzz jsonld");
        assert!(entries.len() > 150, "the fuzz corpus must actually be there");
        for entry in entries {
            let scripts: Vec<String> = entry["scripts"]
                .as_array()
                .expect("scripts")
                .iter()
                .map(|script| script.as_str().expect("script text").to_string())
                .collect();
            assert_eq!(
                jsonld_publication(&scripts),
                expected(entry),
                "structured-data date disagrees with Python for {scripts:?}"
            );
        }
    }

    #[test]
    fn the_article_text_vectors_are_waiting_for_the_tokenizer() {
        // Recorded so the gap is visible rather than implied: these are the cases the port
        // still owes, and none can be answered without Python's HTMLParser.
        let golden = golden();
        let cases = golden["cases"].as_array().expect("cases");
        assert!(
            cases.len() > 30,
            "the article cases must be there for the port that needs them"
        );
        assert!(cases
            .iter()
            .all(|case| case["html"].is_string() && case["text"].is_string()));
    }
}
