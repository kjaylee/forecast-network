//! `html.unescape`, ported. The reference is CPython's `html/__init__.py`, not the HTML5
//! specification it cites: the two agree on valid references and differ on what they do with
//! the malformed ones a real page is full of, and it is the reference the resolution guard
//! was built against.
//!
//! The tables live in `html_entities.rs`. Three rules are worth stating because they are
//! where a plausible-looking port goes wrong:
//!
//! - A numeric reference the spec maps to a *different* character (`&#0;` is U+FFFD) is not
//!   an error and is not dropped.
//! - A numeric reference the spec maps to nothing at all (`&#1;`) decodes to the empty
//!   string, so it silently disappears from the text.
//! - A name that is not in the table is retried as its longest known prefix, and whatever
//!   follows the prefix is kept. `&notin;` is one name; `&notit;` is `¬` followed by `it;`.

use crate::html_entities::{INVALID_CHARREFS, INVALID_CODEPOINTS, NAMED};

const REPLACEMENT: char = '\u{fffd}';
const MAX_NAME: usize = 32;

fn named(name: &str) -> Option<&'static str> {
    NAMED
        .binary_search_by(|(key, _)| key.cmp(&name))
        .ok()
        .map(|index| NAMED[index].1)
}

fn invalid_charref(num: u32) -> Option<&'static str> {
    INVALID_CHARREFS
        .binary_search_by(|(code, _)| code.cmp(&num))
        .ok()
        .map(|index| INVALID_CHARREFS[index].1)
}

/// A character that ends a name rather than continuing one.
fn ends_name(ch: char) -> bool {
    matches!(ch, '\t' | '\n' | '\u{c}' | ' ' | '<' | '&' | '#' | ';')
}

/// Decode one reference. `body` is everything the scanner matched after the `&`, including a
/// trailing semicolon if it took one, because the reference looks names up with it: `amp;`
/// and `amp` are two entries and `Amp;` is neither.
fn decode(body: &str) -> String {
    let Some(first) = body.chars().next() else {
        return "&".to_string();
    };
    if first != '#' {
        if let Some(value) = named(body) {
            return value.to_string();
        }
        // The longest known prefix wins. The remainder keeps everything the prefix did not
        // consume, semicolon included: `&notit;` is `not` plus `it;`, not `not` plus `it`.
        let mut cut = body.len();
        while cut > 2 {
            cut -= 1;
            if !body.is_char_boundary(cut) {
                continue;
            }
            if let Some(value) = named(&body[..cut]) {
                return format!("{value}{}", &body[cut..]);
            }
        }
        return format!("&{body}");
    }
    // Numeric. The leading `#` and, for hex, the radix marker are not digits.
    let rest = &body[1..];
    let (digits, radix) = match rest.chars().next() {
        Some('x') | Some('X') => (&rest[1..], 16),
        _ => (rest, 10),
    };
    let mut num: u32 = 0;
    for digit in digits.chars() {
        let Some(value) = digit.to_digit(radix) else { break };
        // A reference longer than the code space is out of range, and saturating says so
        // without the arithmetic that a hundred digits would otherwise need.
        num = num.saturating_mul(radix).saturating_add(value);
    }
    if let Some(value) = invalid_charref(num) {
        return value.to_string();
    }
    if (0xd800..=0xdfff).contains(&num) || num > 0x10ffff {
        return REPLACEMENT.to_string();
    }
    if INVALID_CODEPOINTS.binary_search(&num).is_ok() {
        return String::new();
    }
    char::from_u32(num)
        .map(String::from)
        .unwrap_or_else(|| REPLACEMENT.to_string())
}

/// `html.unescape`.
pub fn unescape(text: &str) -> String {
    if !text.contains('&') {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let bytes = text.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'&' {
            let start = index;
            while index < bytes.len() && bytes[index] != b'&' {
                index += 1;
            }
            out.push_str(&text[start..index]);
            continue;
        }
        // At an `&`. The three alternatives, in the order the reference tries them.
        let after = index + 1;
        let mut matched: Option<(usize, String)> = None;
        if bytes.get(after) == Some(&b'#') {
            let hex = matches!(bytes.get(after + 1), Some(b'x') | Some(b'X'));
            let digits_from = after + 1 + usize::from(hex);
            let mut end = digits_from;
            while end < bytes.len() && is_digit(bytes[end], hex) {
                end += 1;
            }
            if end > digits_from {
                let terminated = bytes.get(end) == Some(&b';');
                let stop = end + usize::from(terminated);
                matched = Some((stop, decode(&text[after..stop])));
            }
        }
        if matched.is_none() {
            let mut end = after;
            while end < bytes.len() && end - after < MAX_NAME && !ends_name(text[end..].chars().next().unwrap_or(' ')) {
                end += text[end..].chars().next().map(char::len_utf8).unwrap_or(1);
            }
            if end > after {
                let stop = end + usize::from(bytes.get(end) == Some(&b';'));
                matched = Some((stop, decode(&text[after..stop])));
            }
        }
        match matched {
            Some((stop, decoded)) => {
                out.push_str(&decoded);
                index = stop;
            }
            None => {
                out.push('&');
                index = after;
            }
        }
    }
    out
}

fn is_digit(byte: u8, hex: bool) -> bool {
    if hex {
        byte.is_ascii_hexdigit()
    } else {
        byte.is_ascii_digit()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn a_name_is_case_sensitive() {
        assert_eq!(unescape("&AMP;"), "&");
        assert_eq!(unescape("&amp;"), "&");
        assert_eq!(unescape("&Amp;"), "&Amp;");
    }

    #[test]
    fn a_name_without_its_semicolon_is_still_a_name_when_the_table_says_so() {
        assert_eq!(unescape("&amp"), "&");
        assert_eq!(unescape("&copy"), "\u{a9}");
        // 106 of the 2231 names have a semicolonless form; the rest do not.
        assert_eq!(unescape("&notin"), "\u{ac}in");
        assert_eq!(unescape("&notin;"), "\u{2209}");
    }

    #[test]
    fn an_unknown_name_falls_back_to_its_longest_known_prefix() {
        assert_eq!(unescape("&notit;"), "\u{ac}it;");
        assert_eq!(unescape("&notarealthing;"), "\u{ac}arealthing;");
    }

    #[test]
    fn an_ampersand_that_starts_nothing_is_kept() {
        assert_eq!(unescape("AT&T"), "AT&T");
        assert_eq!(unescape("& &amp;"), "& &");
    }

    #[test]
    fn numeric_references_decode_in_both_radixes() {
        assert_eq!(unescape("&#65;&#x41;&#X41;"), "AAA");
        assert_eq!(unescape("&#65"), "A");
    }

    #[test]
    fn a_reference_the_spec_maps_elsewhere_is_not_dropped() {
        assert_eq!(unescape("&#0;"), "\u{fffd}");
        assert_eq!(unescape("&#128;"), "\u{20ac}");
    }

    #[test]
    fn a_reference_the_spec_maps_to_nothing_disappears() {
        assert_eq!(unescape("a&#1;b"), "ab");
        assert_eq!(unescape("a&#x0;b"), "a\u{fffd}b");
    }

    #[test]
    fn surrogates_and_beyond_the_code_space_are_replacements() {
        assert_eq!(unescape("&#xD800;"), "\u{fffd}");
        assert_eq!(unescape("&#1114112;"), "\u{fffd}");
        assert_eq!(unescape(&format!("&#{};", "9".repeat(100))), "\u{fffd}");
    }

    /// The corpus is exported from CPython and committed; the one-off that produced it ran
    /// 20,000 cases against this port before being cut down to a size worth keeping.
    fn golden() -> serde_json::Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/html-unescape-golden.json");
        serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{} is missing: {error}", path.display())),
        )
        .expect("golden JSON")
    }

    #[test]
    fn every_reference_the_reference_accepts_and_rejects_matches_it() {
        let golden = golden();
        let cases = golden["cases"].as_array().expect("cases");
        assert!(cases.len() > 1000, "the corpus must actually be there");
        let mut wrong = Vec::new();
        for case in cases {
            let input = case[0].as_str().expect("input");
            let expected = case[1].as_str().expect("expected");
            let got = unescape(input);
            if got != expected && wrong.len() < 8 {
                wrong.push(format!("{input:?} -> {got:?}, expected {expected:?}"));
            }
        }
        assert!(
            wrong.is_empty(),
            "unescape disagrees with Python:\n{}",
            wrong.join("\n")
        );
    }

    #[test]
    fn text_without_an_ampersand_is_untouched() {
        assert_eq!(unescape("plain text"), "plain text");
        assert_eq!(unescape(""), "");
    }
}
