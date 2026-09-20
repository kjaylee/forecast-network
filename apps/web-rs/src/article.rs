//! What a publication timestamp is allowed to mean, ported from `source_watch`.
//!
//! `_publication_date` and `_jsonld_publication` are the two places a page can state when it
//! was published, and the distinction they draw is the one the resolution guard turns on:
//! only an `instant` can be placed before or after the moment participation closed, and a
//! bare calendar day never can. A port that called `2026-09-15` an instant would place
//! evidence it cannot place.
//!
//! `article_content` above them ties the two to the tokenizer in `html_parse`, which is
//! Python's `HTMLParser` — not an HTML5 parser, and not one that recovers from malformed
//! markup the way a browser does. All of it is held to vectors exported from Python by the
//! tests below, and to a fuzz corpus that found the two divergences this port started with.

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

const MAX_JSONLD_SCRIPTS: usize = 8;
const MAX_JSONLD_BYTES: usize = 65536;
const HIDDEN: [&str; 6] = ["script", "style", "noscript", "nav", "footer", "header"];

/// `_Article`: the observer, not the parser. It decides what counts as the article, where a
/// publication time is stated, and which scripts hold structured data.
#[derive(Default)]
struct Article {
    links: Vec<String>,
    parts: Vec<String>,
    main_parts: Vec<String>,
    hidden: usize,
    main: usize,
    publication_date: Option<String>,
    jsonld: Vec<String>,
    jsonld_active: bool,
    jsonld_parts: Vec<String>,
    jsonld_count: usize,
    jsonld_bytes: usize,
}

impl Article {
    fn start(&mut self, tag: &crate::html_parse::Tag) {
        // A later duplicate wins, which is what `dict(attrs)` does.
        let value = |name: &str| -> Option<String> {
            tag.attrs
                .iter()
                .rev()
                .find(|(key, _)| key == name)
                .and_then(|(_, value)| value.clone())
        };
        if tag.name == "script"
            && (value("type")
                .unwrap_or_default()
                .to_lowercase()
                .split(';')
                .next()
                .unwrap_or("")
                .trim()
                == "application/ld+json")
        {
            self.jsonld_count += 1;
            self.jsonld_active = self.jsonld_count <= MAX_JSONLD_SCRIPTS && self.jsonld_bytes < MAX_JSONLD_BYTES;
            self.jsonld_parts.clear();
        }
        if HIDDEN.contains(&tag.name.as_str()) {
            self.hidden += 1;
        }
        if tag.name == "main" || tag.name == "article" {
            self.main += 1;
        }
        if (tag.name == "a" || tag.name == "link") && value("href").is_some() {
            self.links.push(value("href").unwrap_or_default());
        }
        let key = value("property").or_else(|| value("name"));
        if tag.name == "meta"
            && key
                .as_deref()
                .is_some_and(|k| ["article:published_time", "date", "datePublished"].contains(&k))
        {
            self.publication_date = value("content");
        }
        if tag.name == "time" && self.publication_date.is_none() {
            self.publication_date = value("datetime");
        }
    }

    fn end(&mut self, tag: &str) {
        if tag == "script" {
            if self.jsonld_active {
                self.jsonld.push(self.jsonld_parts.concat());
            }
            self.jsonld_active = false;
            self.jsonld_parts.clear();
        }
        if HIDDEN.contains(&tag) {
            self.hidden = self.hidden.saturating_sub(1);
        }
        if tag == "main" || tag == "article" {
            self.main = self.main.saturating_sub(1);
        }
    }

    fn data(&mut self, text: &str) {
        if self.jsonld_active {
            self.jsonld_bytes += text.len();
            if self.jsonld_bytes <= MAX_JSONLD_BYTES {
                self.jsonld_parts.push(text.to_string());
            } else {
                self.jsonld_active = false;
                self.jsonld_parts.clear();
            }
        }
        if self.hidden == 0 {
            self.parts.push(text.to_string());
            if self.main > 0 {
                self.main_parts.push(text.to_string());
            }
        }
    }
}

/// `article_content`: the article text, and what the page says about when it was published.
///
/// Errs where the reference raises. A page with a marked section it cannot read is a page the
/// reference refuses to describe, and describing it anyway would put text into the timing
/// decision that the reference would have failed the poll over.
pub fn article_content(body: &str) -> Result<(String, Publication), crate::html_parse::ParseError> {
    let mut article = Article::default();
    for event in crate::html_parse::tokenize(body)? {
        match event {
            crate::html_parse::Event::Start(tag) => article.start(&tag),
            crate::html_parse::Event::End(tag) => article.end(&tag),
            crate::html_parse::Event::Data(text) => article.data(&text),
        }
    }
    let chosen = if article.main_parts.is_empty() {
        &article.parts
    } else {
        &article.main_parts
    };
    let text = chosen.join(" ").split_whitespace().collect::<Vec<_>>().join(" ");
    let publication = match &article.publication_date {
        Some(raw) => publication_date(Some(&Value::String(raw.clone()))),
        None => jsonld_publication(&article.jsonld),
    };
    Ok((text, publication))
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

    /// Exported from the Python parser by the same one-off that produced the corpus above:
    /// 20,000 generated documents agreed before this 2,000-case subset was kept. It carries
    /// the cases the reference raises on, because a port that recovers where it raises is
    /// producing text the reference would have failed the poll over.
    fn parse_golden() -> serde_json::Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/article-parse-golden.json");
        serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{} is missing: {error}", path.display())),
        )
        .expect("golden JSON")
    }

    #[test]
    fn the_reference_article_parsing_is_reproduced_including_its_refusals() {
        let golden = parse_golden();
        let cases = golden["cases"].as_array().expect("cases");
        assert!(cases.len() > 1000, "the corpus must actually be there");
        let mut wrong = Vec::new();
        for case in cases {
            let html = case["html"].as_str().expect("html");
            let refused = case
                .get("refused")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            match (article_content(html), refused) {
                (Err(_), true) => {}
                (Ok(_), true) => wrong.push(format!("{html:?}: Python refused, the port answered")),
                (Err(error), false) => wrong.push(format!("{html:?}: the port refused ({error:?})")),
                (Ok((text, publication)), false) => {
                    let want = (
                        case["text"].as_str().expect("text"),
                        case["date"].as_str(),
                        case["precision"].as_str().expect("precision"),
                    );
                    if text != want.0 || publication.0.as_deref() != want.1 || publication.1 != want.2 {
                        wrong.push(format!("{html:?}: got ({text:?}, {publication:?}), expected {want:?}"));
                    }
                }
            }
            if wrong.len() >= 8 {
                break;
            }
        }
        assert!(
            wrong.is_empty(),
            "article parsing disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }

    #[test]
    fn the_reference_article_text_is_reproduced() {
        let golden = golden();
        let cases = golden["cases"].as_array().expect("cases");
        assert!(cases.len() > 30, "the corpus must actually be there");
        let mut wrong = Vec::new();
        for case in cases {
            let html = case["html"].as_str().expect("html");
            let name = case["name"].as_str().unwrap_or("?");
            let (text, publication) = article_content(html).expect("the corpus has no refused page");
            let want_text = case["text"].as_str().expect("text");
            if text != want_text || publication != expected(case) {
                wrong.push(format!(
                    "{name}: got ({text:?}, {publication:?}), expected ({want_text:?}, {:?})",
                    expected(case)
                ));
            }
        }
        assert!(
            wrong.is_empty(),
            "article_content disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }
}
