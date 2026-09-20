//! Bounded public-source retrieval, ported from `sources.py` and `source_watch.py`.
//!
//! `validate_public_url` is the SSRF boundary: it decides which hosts may be requested at all,
//! and it runs before any network activity. A port that is merely similar here is not similar —
//! it is a different set of hosts, and the difference is a request the Python Worker would
//! refuse. Everything else in this module is bounded reading of what that boundary allowed.

use serde_json::{json, Value};
use worker::Url;

use crate::html_parse::{tokenize, Event};

/// Publisher pages can include large inline assets; retain complete bytes within the same
/// 512 KiB ceiling the immutable artifact store enforces.
pub const MAX_SOURCE_BYTES: usize = 512 * 1024;
pub const MAX_EXCERPT_BYTES: usize = 24000;
pub const MAX_SOURCE_REDIRECTS: usize = 3;
pub const SOURCE_POLICY_VERSION: &str = "public-official-hosts-v2";

/// Host registrations assert authority, not that every page proves every question.
/// `SourceVerifier` and the immutable outcome clauses must still assess each page.
pub const OFFICIAL_HOSTS: &[&str] = &[
    "www.apple.com",
    "apple.com",
    "blogs.nvidia.com",
    "nvidianews.nvidia.com",
    "www.nvidia.com",
    "openai.com",
    "blog.google",
    "deepmind.google",
    "www.microsoft.com",
    "blogs.microsoft.com",
    "news.microsoft.com",
    "news.samsung.com",
    "www.nasa.gov",
    "science.nasa.gov",
    "www.esa.int",
    "www.spacex.com",
    "www.noaa.gov",
    "www.climate.gov",
    "www.who.int",
    "www.un.org",
    "www.federalreserve.gov",
    "www.bls.gov",
    "www.bea.gov",
    "www.ecb.europa.eu",
    "www.bok.or.kr",
    "kostat.go.kr",
    "www.kostat.go.kr",
    "www.kma.go.kr",
    "solana.com",
    "ethereum.org",
    "api.kraken.com",
    "www.bitstamp.net",
    "www.fifa.com",
    "www.olympics.com",
];

pub const FALLBACK_HOSTS: &[&str] = &[
    "www.reuters.com",
    "reuters.com",
    "apnews.com",
    "www.bbc.com",
    "www.bbc.co.uk",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceRejected {
    Malformed,
    NotPublic,
    IpAddress,
    Unregistered,
}

impl SourceRejected {
    /// The message the Python Worker raises with, kept for the error the caller reports.
    pub fn message(&self) -> &'static str {
        match self {
            SourceRejected::Malformed => "Source URL is malformed",
            SourceRejected::NotPublic => "Source requires public HTTPS without credentials or custom ports",
            SourceRejected::IpAddress => "IP source addresses are not permitted",
            SourceRejected::Unregistered => "Source host is not in the approved authoritative-source registry",
        }
    }
}

/// What `urlsplit` reports for a URL: the scheme, the hostname, and whether credentials or a
/// fragment were present.
struct SplitUrl {
    scheme: String,
    host: Option<String>,
    has_userinfo: bool,
    has_fragment: bool,
    /// `Some(n)` when a port is written, and `None` when it is absent or not a number.
    port: Option<PortPart>,
}

enum PortPart {
    Number(u32),
    Invalid,
}

/// `urlsplit`, which is not a URL standard implementation. It does no IDNA check, keeps
/// `xn--apple.com` as written, and raises only for an unclosed IPv6 bracket or an unparseable
/// port. Delegating this to a stricter parser changes which hosts are reachable, and which hosts
/// are reachable is the whole of the guard.
fn split_url(url: &str) -> Option<SplitUrl> {
    let bytes = url.as_bytes();
    // The scheme runs to the first `:` that precedes any `/`, `?` or `#`.
    let mut scheme_end = None;
    for (index, byte) in bytes.iter().enumerate() {
        match byte {
            b':' => {
                scheme_end = Some(index);
                break;
            }
            b'/' | b'?' | b'#' => break,
            _ => {}
        }
    }
    let scheme = match scheme_end {
        Some(end)
            if end > 0
                && bytes[0].is_ascii_alphabetic()
                && bytes[..end]
                    .iter()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.')) =>
        {
            url[..end].to_ascii_lowercase()
        }
        _ => String::new(),
    };
    let after_scheme = match scheme_end.filter(|_| !scheme.is_empty()) {
        Some(end) => &url[end + 1..],
        None => url,
    };
    let mut has_fragment = false;
    let mut without_fragment = after_scheme;
    if let Some(at) = after_scheme.find('#') {
        has_fragment = true;
        without_fragment = &after_scheme[..at];
    }
    let authority = match without_fragment.strip_prefix("//") {
        Some(rest) => rest.split(['/', '?']).next().unwrap_or("").to_string(),
        None => {
            return Some(SplitUrl {
                scheme,
                host: None,
                has_userinfo: false,
                has_fragment,
                port: None,
            })
        }
    };
    let has_userinfo = authority.contains('@');
    let hostinfo = match authority.rfind('@') {
        Some(at) => &authority[at + 1..],
        None => authority.as_str(),
    };
    // An IPv6 literal is bracketed, and an unclosed bracket is the reference raising.
    let (host, port_text) = if let Some(rest) = hostinfo.strip_prefix('[') {
        let close = rest.find(']')?;
        let host = &rest[..close];
        let tail = &rest[close + 1..];
        (host.to_string(), tail.strip_prefix(':').map(str::to_string))
    } else {
        match hostinfo.split_once(':') {
            Some((host, port)) => (host.to_string(), Some(port.to_string())),
            None => (hostinfo.to_string(), None),
        }
    };
    let port = match port_text {
        None => None,
        Some(text) if text.is_empty() => None,
        Some(text) => match text.parse::<u32>() {
            Ok(number) if number <= 65535 => Some(PortPart::Number(number)),
            _ => Some(PortPart::Invalid),
        },
    };
    Some(SplitUrl {
        scheme,
        host: if host.is_empty() {
            None
        } else {
            Some(host.to_lowercase())
        },
        has_userinfo,
        has_fragment,
        port,
    })
}

/// Return the exact registered hostname, or reject before any network activity.
pub fn validate_public_url(url: &str, official: bool) -> Result<String, SourceRejected> {
    if url.len() > 2048
        || url
            .chars()
            .any(|c| c.is_whitespace() || c == '\\' || (c as u32) < 0x20 || c == '\0')
    {
        return Err(SourceRejected::Malformed);
    }
    let Some(parsed) = split_url(url) else {
        return Err(SourceRejected::Malformed);
    };
    if matches!(parsed.port, Some(PortPart::Invalid)) {
        return Err(SourceRejected::Malformed);
    }
    if parsed.scheme != "https" || parsed.has_userinfo || parsed.has_fragment {
        return Err(SourceRejected::NotPublic);
    }
    if matches!(parsed.port, Some(PortPart::Number(port)) if port != 443) {
        return Err(SourceRejected::NotPublic);
    }
    let Some(host) = parsed.host else {
        return Err(SourceRejected::NotPublic);
    };
    if host.ends_with('.') {
        return Err(SourceRejected::NotPublic);
    }
    if host.parse::<std::net::IpAddr>().is_ok() {
        return Err(SourceRejected::IpAddress);
    }
    let registered = OFFICIAL_HOSTS.contains(&host.as_str()) || (!official && FALLBACK_HOSTS.contains(&host.as_str()));
    if !registered {
        return Err(SourceRejected::Unregistered);
    }
    Ok(host)
}

/// `_Text`: the article text without the parts a reader does not see.
fn visible_text(body: &str) -> String {
    let mut hidden = 0usize;
    let mut parts: Vec<String> = Vec::new();
    let Ok(events) = tokenize(body) else {
        return String::new();
    };
    for event in events {
        match event {
            Event::Start(tag) => {
                if matches!(tag.name.as_str(), "script" | "style" | "noscript") {
                    hidden += 1;
                }
            }
            Event::End(tag) => {
                if matches!(tag.as_str(), "script" | "style" | "noscript") {
                    hidden = hidden.saturating_sub(1);
                }
            }
            Event::Data(text) => {
                if hidden == 0 {
                    parts.push(text);
                }
            }
        }
    }
    parts.join(" ").split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Truncation is by bytes, and a character that does not fit is dropped rather than halved.
fn truncate_bytes(text: &str, limit: usize) -> String {
    let mut out = String::new();
    for ch in text.chars() {
        if out.len() + ch.len_utf8() > limit {
            break;
        }
        out.push(ch);
    }
    out
}

pub fn evidence_excerpt(body: &str) -> String {
    truncate_bytes(&visible_text(body), MAX_EXCERPT_BYTES)
}

/// Losslessly remove repeated OHLC field names, never dates or prices.
///
/// Bitstamp's 288 daily five-minute records exceed the ordinary excerpt budget solely because
/// each row repeats its keys. Every field and scalar is preserved in an explicitly labelled
/// column table, and the full original JSON remains the artifact.
pub fn market_json_excerpt(body: &str, url: &str) -> String {
    let Ok(parsed) = Url::parse(url) else {
        return evidence_excerpt(body);
    };
    let path = parsed.path();
    let host_matches = parsed.host_str() == Some("www.bitstamp.net");
    let path_matches = path
        .strip_prefix("/api/v2/ohlc/")
        .map(|rest| {
            !rest.is_empty()
                && rest.ends_with('/')
                && rest[..rest.len() - 1]
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        })
        .unwrap_or(false);
    if !host_matches || !path_matches {
        return evidence_excerpt(body);
    }
    // A decimal JSON number has no exact text form here, and a duplicate key cannot be
    // projected without losing one of them, so both fall back rather than guess.
    let Ok(value) = serde_json::from_str::<Value>(body) else {
        return evidence_excerpt(body);
    };
    if body.contains('.') && json_has_float(&value) {
        return evidence_excerpt(body);
    }
    let Some(rows) = value["data"]["ohlc"].as_array() else {
        return evidence_excerpt(body);
    };
    if rows.is_empty() || rows.len() > 1000 {
        return evidence_excerpt(body);
    }
    let Some(first) = rows[0].as_object() else {
        return evidence_excerpt(body);
    };
    let mut columns: Vec<&str> = first.keys().map(String::as_str).collect();
    columns.sort_unstable();
    if columns.is_empty()
        || !rows.iter().all(|row| {
            row.as_object()
                .map(|fields| {
                    let mut keys: Vec<&str> = fields.keys().map(String::as_str).collect();
                    keys.sort_unstable();
                    keys == columns
                })
                .unwrap_or(false)
        })
    {
        return evidence_excerpt(body);
    }
    let projected: Vec<Value> = rows
        .iter()
        .map(|row| Value::Array(columns.iter().map(|key| row[key].clone()).collect()))
        .collect();
    let mut updated = value.clone();
    updated["data"]["ohlc"] = json!({
        "representation": "lossless-json-columns-v1",
        "columns": columns,
        "rows": projected,
    });
    let compact = serde_json::to_string(&updated).unwrap_or_default();
    if compact.len() <= MAX_EXCERPT_BYTES {
        return compact;
    }
    evidence_excerpt(body)
}

fn json_has_float(value: &Value) -> bool {
    match value {
        Value::Number(number) => number.as_i64().is_none() && number.as_u64().is_none(),
        Value::Array(items) => items.iter().any(json_has_float),
        Value::Object(fields) => fields.values().any(json_has_float),
        _ => false,
    }
}

/// Only publisher article paths on the watched exact official host.
pub fn discover_articles(body: &str, url: &str) -> Vec<String> {
    let Ok(host) = validate_public_url(url, true) else {
        return Vec::new();
    };
    let Ok(base) = Url::parse(url) else {
        return Vec::new();
    };
    let mut links = article_hrefs(body);
    // RSS `<link>` text, unlike Atom href, is plain character data.
    links.extend(rss_links(body));
    let mut found: Vec<String> = Vec::new();
    for value in links {
        let Ok(joined) = base.join(&value) else {
            continue;
        };
        let candidate = joined.as_str().split('#').next().unwrap_or("").to_string();
        if validate_public_url(&candidate, true).ok().as_deref() != Some(host.as_str()) {
            continue;
        }
        let Ok(resolved) = Url::parse(&candidate) else {
            continue;
        };
        let path = resolved.path().to_string();
        // Date folders also appear in WordPress media uploads. Matching any date suffix
        // accidentally registers logos and images as permanent polling jobs, and supported
        // publishers use extensionless article slugs at these roots.
        let folded = path.to_lowercase();
        if folded.contains("/wp-content/") || folded.contains("/uploads/") {
            continue;
        }
        if eligible_article_path(&host, &path) && !found.contains(&candidate) {
            found.push(candidate);
        }
    }
    found.truncate(MAX_DISCOVERED);
    found
}

pub const MAX_DISCOVERED: usize = 6;

/// The date-rooted, extensionless slug each registered publisher uses for articles.
fn eligible_article_path(host: &str, path: &str) -> bool {
    let rest = match host {
        "www.apple.com" | "apple.com" => match path.strip_prefix("/newsroom/") {
            Some(rest) => rest,
            None => return false,
        },
        "blogs.microsoft.com" => match path.strip_prefix("/blog/").or_else(|| path.strip_prefix('/')) {
            Some(rest) => rest,
            None => return false,
        },
        "news.microsoft.com" | "www.microsoft.com" => {
            match path.strip_prefix("/source/").or_else(|| path.strip_prefix('/')) {
                Some(rest) => rest,
                None => return false,
            }
        }
        _ => return false,
    };
    // `\d{4}/\d{2}/` then an optional `\d{2}/`, then the slug and an optional trailing slash.
    let rest = match digits(rest, 4) {
        Some(rest) if rest.starts_with('/') => &rest[1..],
        _ => return false,
    };
    let rest = match digits(rest, 2) {
        Some(rest) if rest.starts_with('/') => &rest[1..],
        _ => return false,
    };
    let rest = match digits(rest, 2) {
        Some(rest) if rest.starts_with('/') => &rest[1..],
        _ => rest,
    };
    let slug = rest.strip_suffix('/').unwrap_or(rest);
    !slug.is_empty()
        && slug.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && slug.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn digits(text: &str, count: usize) -> Option<&str> {
    if text.len() >= count && text[..count].bytes().all(|b| b.is_ascii_digit()) {
        Some(&text[count..])
    } else {
        None
    }
}

fn article_hrefs(body: &str) -> Vec<String> {
    let Ok(events) = tokenize(body) else {
        return Vec::new();
    };
    let mut links = Vec::new();
    for event in events {
        if let Event::Start(tag) = event {
            if matches!(tag.name.as_str(), "a" | "link") {
                // `dict(attrs)` keeps the last occurrence of a repeated attribute.
                if let Some((_, Some(href))) = tag.attrs.iter().rev().find(|(key, _)| key == "href") {
                    links.push(href.clone());
                }
            }
        }
    }
    links
}

/// `<link>https://…</link>` appears in RSS as character data rather than an attribute.
fn rss_links(body: &str) -> Vec<String> {
    let mut found = Vec::new();
    let mut rest = body;
    while let Some(start) = rest.find("<link>") {
        let after = &rest[start + 6..];
        let Some(end) = after.find("</link>") else {
            break;
        };
        let inner = after[..end].trim();
        if inner.starts_with("https://") && !inner.contains(char::is_whitespace) {
            found.push(inner.to_string());
        }
        rest = &after[end..];
    }
    found
}

/// Whether an article mentions any of the question's families, as a whole word.
///
/// The reference casefolds both sides and then requires a non-word character on each side, so
/// `arm` does not match `warm`. Casefolding is approximated by lowercasing, which differs for
/// the handful of characters (`ß` and friends) where the two disagree.
pub fn relevant(text: &str, families: &[String]) -> bool {
    let lowered = text.to_lowercase();
    families
        .iter()
        .any(|family| contains_word(&lowered, &family.to_lowercase()))
}

fn contains_word(haystack: &str, needle: &str) -> bool {
    let bytes = haystack.as_bytes();
    if needle.is_empty() {
        // The reference searches for the empty pattern, which matches at any position with no
        // word character on either side — including the end of any string that does not end in
        // one. `relevant("anything!", [""])` is true there, and a port that said false would
        // report a question as unrelated when the reference reports it as related.
        return (0..=bytes.len())
            .any(|at| (at == 0 || !is_word_byte(bytes[at - 1])) && (at == bytes.len() || !is_word_byte(bytes[at])));
    }
    let mut from = 0;
    while from < haystack.len() {
        let Some(offset) = haystack[from..].find(needle) else {
            return false;
        };
        let at = from + offset;
        let end = at + needle.len();
        let before_ok = at == 0 || !is_word_byte(bytes[at - 1]);
        let after_ok = end >= bytes.len() || !is_word_byte(bytes[end]);
        if before_ok && after_ok {
            return true;
        }
        from = at + needle.len().max(1);
    }
    false
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_lowercase() || byte.is_ascii_digit()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// Exported from the Python source policy by the same one-off that produced the corpus
    /// above: 8,000 URLs and 2,500 of each of the others agreed before this subset was kept.
    /// A case the reference raises on carries a leading `!`.
    fn golden() -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/source-policy-golden.json");
        serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{} is missing: {error}", path.display())),
        )
        .expect("golden JSON")
    }

    #[test]
    fn the_reference_source_policy_is_reproduced() {
        let golden = golden();
        let mut wrong = Vec::new();
        let mut note = |message: String| {
            if wrong.len() < 8 {
                wrong.push(message);
            }
        };
        let urls = golden["urls"].as_array().expect("urls");
        assert!(urls.len() > 500, "the corpus must actually be there");
        for case in urls {
            let url = case[0].as_str().expect("url");
            let official = case[1].as_bool().expect("official");
            let expected = case[2].as_str().expect("expected");
            match (validate_public_url(url, official), case[3].as_bool().expect("rejected")) {
                (Ok(host), false) if host == expected => {}
                (Err(error), true) if error.message() == expected => {}
                (got, rejected) => note(format!(
                    "{url:?} official={official} rejected={rejected}: got {got:?}, expected {expected:?}"
                )),
            }
        }
        for case in golden["excerpts"].as_array().expect("excerpts") {
            let body = case[0].as_str().expect("body");
            if let Some(expected) = case[1].as_str() {
                if evidence_excerpt(body) != expected {
                    note(format!("excerpt {body:?}"));
                }
            }
        }
        for case in golden["discover"].as_array().expect("discover") {
            let body = case[0].as_str().expect("body");
            let base = case[1].as_str().expect("base");
            if let Some(expected) = case[2].as_array() {
                let expected: Vec<String> = expected.iter().map(|v| v.as_str().expect("url").to_string()).collect();
                if discover_articles(body, base) != expected {
                    note(format!("discover {body:?} base={base:?}"));
                }
            }
        }
        for case in golden["relevant"].as_array().expect("relevant") {
            let text = case[0].as_str().expect("text");
            let families: Vec<String> = case[1]
                .as_array()
                .expect("families")
                .iter()
                .map(|v| v.as_str().expect("family").to_string())
                .collect();
            if relevant(text, &families) != case[2].as_bool().expect("expected") {
                note(format!("relevant {text:?} {families:?}"));
            }
        }
        assert!(
            wrong.is_empty(),
            "the source policy disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }

    fn rejected(url: &str, official: bool) -> Option<SourceRejected> {
        validate_public_url(url, official).err()
    }

    #[test]
    fn only_registered_hosts_are_reachable() {
        assert_eq!(
            validate_public_url("https://www.apple.com/newsroom/x", true).unwrap(),
            "www.apple.com"
        );
        assert_eq!(
            rejected("https://evil.test/x", true),
            Some(SourceRejected::Unregistered)
        );
        // A fallback host is registered for the ordinary path and not for the official one.
        assert_eq!(
            validate_public_url("https://www.reuters.com/x", false).unwrap(),
            "www.reuters.com"
        );
        assert_eq!(
            rejected("https://www.reuters.com/x", true),
            Some(SourceRejected::Unregistered)
        );
    }

    #[test]
    fn an_address_literal_is_never_a_source() {
        assert_eq!(rejected("https://127.0.0.1/x", false), Some(SourceRejected::IpAddress));
        assert_eq!(rejected("https://[::1]/x", false), Some(SourceRejected::IpAddress));
    }

    #[test]
    fn credentials_ports_and_fragments_are_refused() {
        for url in [
            "https://user:pass@www.apple.com/x",
            "https://www.apple.com:8443/x",
            "https://www.apple.com/x#fragment",
            "http://www.apple.com/x",
            "https://www.apple.com./x",
        ] {
            assert!(rejected(url, true).is_some(), "{url} should be refused");
        }
    }

    #[test]
    fn malformed_input_is_refused_before_it_is_parsed() {
        assert_eq!(
            rejected("https://www.apple.com/a b", true),
            Some(SourceRejected::Malformed)
        );
        assert_eq!(
            rejected("https://www.apple.com/\\x", true),
            Some(SourceRejected::Malformed)
        );
        assert_eq!(
            rejected(&format!("https://www.apple.com/{}", "a".repeat(2100)), true),
            Some(SourceRejected::Malformed)
        );
    }

    #[test]
    fn the_excerpt_is_collapsed_and_bounded() {
        assert_eq!(evidence_excerpt("<p>a</p>  <p>b</p>"), "a b");
        assert_eq!(evidence_excerpt("<script>x</script>visible"), "visible");
        // Truncation is by bytes and a character that does not fit is dropped whole.
        let text = "é".repeat(MAX_EXCERPT_BYTES);
        let excerpt = evidence_excerpt(&text);
        assert!(excerpt.len() <= MAX_EXCERPT_BYTES);
        assert!(excerpt.chars().all(|c| c == 'é'));
    }

    #[test]
    fn the_ohlc_projection_keeps_every_field_and_only_for_bitstamp() {
        let body =
            r#"{"data":{"ohlc":[{"high":"1","low":"2","timestamp":"3"},{"high":"4","low":"5","timestamp":"6"}]}}"#;
        let projected = market_json_excerpt(body, "https://www.bitstamp.net/api/v2/ohlc/btcusd/");
        assert!(projected.contains("lossless-json-columns-v1"), "{projected}");
        assert!(
            projected.contains("\"rows\":[[\"1\",\"2\",\"3\"],[\"4\",\"5\",\"6\"]]"),
            "{projected}"
        );
        // Another host gets the ordinary excerpt, not the projection.
        let other = market_json_excerpt(body, "https://api.kraken.com/0/public/OHLC");
        assert!(!other.contains("lossless-json-columns-v1"));
    }

    #[test]
    fn an_article_link_is_kept_only_when_it_stays_on_the_watched_host() {
        let body = "<a href=\"/newsroom/2026/09/product-announced/\">a</a>\
                    <a href=\"https://evil.test/newsroom/x/\">b</a>\
                    <a href=\"/wp-content/uploads/2026/09/logo/\">c</a>\
                    <a href=\"/newsroom/2026/09/photo.jpg\">d</a>";
        let found = discover_articles(body, "https://www.apple.com/newsroom/");
        assert_eq!(
            found,
            vec!["https://www.apple.com/newsroom/2026/09/product-announced/".to_string()]
        );
    }
}
