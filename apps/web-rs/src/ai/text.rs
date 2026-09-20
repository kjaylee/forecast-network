//! The two policies a model's prose has to satisfy before anyone reads it as public text.
//!
//! Both are heuristics and both say so in the reference's own docstrings: script detection does
//! not establish meaning, and a product identifier is not a calendar date. They are the cheap
//! gate in front of the judge that does establish it, and they are ported because a gate that
//! passes different text is a gate that lets different text through.

use regex::Regex;
use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextError {
    pub message: &'static str,
    pub code: &'static str,
}

const MONTHS: &str = "January|February|March|April|May|June|July|August|September|October|November|December";
const UNITS: &str = "years?|months?|weeks?|days?|hours?|quarters?|seasons?";

fn temporal() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        let pattern = format!(
            concat!(
                r"\b\d{{4}}[-/]\d{{1,2}}(?:[-/]\d{{1,2}})?\b",
                r"|\b\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:\s*(?:UTC|GMT|KST|Z))?\b",
                r"|\b(?:today|tomorrow|tonight|yesterday|soon|year[- ]end|mid[- ]year)\b",
                r"|\b(?:this|next|last|coming|upcoming|current)\s+(?:{units}|spring|summer|autumn|fall|winter)\b",
                r"|\b(?:end|start|beginning|close)\s+of\s+(?:(?:the|this|next|last)\s+)?(?:{units}|20\d{{2}}|{months})\b",
                r"|\b(?:by|before|after|until|within|during|in|on|at)\s+(?:the\s+)?(?:",
                r"20\d{{2}}|end|start|beginning|midnight|noon|year['\u{{2019}}]s\s+end|Q[1-4]|H[12]|",
                r"\d+\s+(?:{units})|{months})\b",
                r"|\b(?:Q[1-4]|H[12])\s+20\d{{2}}\b",
                r"|\b(?:{months})\s+\d{{1,4}}\b",
            ),
            units = UNITS,
            months = MONTHS,
        );
        // Case-insensitive, as the reference compiles it. The pattern is a literal, so a
        // failure here is a build mistake rather than input the caller could have avoided.
        Regex::new(&format!("(?i){pattern}")).expect("the reference's temporal pattern compiles")
    })
}

/// Deadlines belong in the canonical question and the dedicated date display.
///
/// Product identifiers such as M6, iPhone 17 and RTX 5090 are not calendar dates, and the
/// existing ambiguity judge additionally checks title meaning and identity.
pub fn require_timeless_share_title(title: &str) -> Result<(), TextError> {
    if temporal().is_match(title) {
        return Err(TextError {
            message: "Share titles must omit deadlines; the closing time is displayed separately.",
            code: "compiler_title_deadline",
        });
    }
    Ok(())
}

/// Whether a character is a letter that is not Latin script.
///
/// The reference asks `unicodedata.name` for "LATIN", which is a question about the character's
/// name rather than its script. They agree wherever a name exists, which is everywhere a real
/// page or a real model answer puts a letter.
fn is_non_latin_letter(character: char) -> bool {
    if !character.is_alphabetic() {
        return false;
    }
    use unicode_script::{Script, UnicodeScript};
    character.script() != Script::Latin
}

/// Reject untranslated prose; the existing AI judge verifies English meaning.
///
/// Quoted names may retain their original script inside surrounding English. This is not a claim
/// that script detection alone establishes English meaning.
pub fn require_english_public_text(texts: &[String]) -> Result<(), TextError> {
    let error = TextError {
        message: "Public forecast prose must be translated into English before publication.",
        code: "ai_output_language",
    };
    for text in texts {
        if !text.chars().any(is_non_latin_letter) {
            continue;
        }
        let prose = strip_quoted_names(text);
        if prose.chars().any(is_non_latin_letter) || !has_english_context(&prose) {
            return Err(error);
        }
    }
    Ok(())
}

/// `"[^"\n]{1,160}"|“[^”\n]{1,160}”|‘[^’\n]{1,160}’|(?<!\w)'[^'\n]{1,160}'(?!\w)`
///
/// The last alternative needs lookaround, which Rust's `regex` does not have, so both boundaries
/// are checked by hand — and they are around the *whole* quoted run: no word character before the
/// opening quote and none after the closing one. Reading the lookahead as applying just inside the
/// opening quote left `'Samsung'` in the prose, which is the word that made an untranslated title
/// look English.
fn strip_quoted_names(text: &str) -> String {
    let characters: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut index = 0;
    while index < characters.len() {
        let opening = characters[index];
        let closing = match opening {
            '"' => Some('"'),
            '\u{201c}' => Some('\u{201d}'),
            '\u{2018}' => Some('\u{2019}'),
            '\'' => Some('\''),
            _ => None,
        };
        let Some(closing) = closing else {
            out.push(opening);
            index += 1;
            continue;
        };
        // The straight quote counts only when no word character sits before it; the typographic
        // ones are not word characters themselves and need no such check.
        let before_ok = opening != '\'' || index == 0 || !characters[index - 1].is_alphanumeric();
        if !before_ok {
            out.push(opening);
            index += 1;
            continue;
        }
        let mut end = index + 1;
        while end < characters.len() && characters[end] != closing && characters[end] != '\n' && end - index <= 160 {
            end += 1;
        }
        let closed = end < characters.len() && characters[end] == closing && end > index + 1;
        // The other boundary is after the closing quote, not after the opening one.
        let after_ok = opening != '\'' || end + 1 >= characters.len() || !characters[end + 1].is_alphanumeric();
        if !closed || !after_ok {
            out.push(opening);
            index += 1;
            continue;
        }
        // The run is replaced by a space, so the words either side do not join.
        out.push(' ');
        index = end + 1;
    }
    out
}

/// `re.search(r"[A-Za-z]{2,}", prose)`.
fn has_english_context(prose: &str) -> bool {
    let mut run = 0;
    for character in prose.chars() {
        if character.is_ascii_alphabetic() {
            run += 1;
            if run >= 2 {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}

#[cfg(test)]
mod golden {
    use super::*;
    use serde_json::Value;
    use std::path::PathBuf;

    fn cases(name: &str) -> Vec<Value> {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/ai-text-golden.json");
        let corpus: Value = serde_json::from_str(
            &std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())),
        )
        .expect("golden JSON");
        corpus[name].as_array().cloned().expect("cases")
    }

    #[test]
    fn both_policies_decide_the_same_as_python() {
        // 8,000 generated cases agreed before this subset was kept. The language policy found a
        // real transcription error: the reference's lookahead bounds the closing quote, not the
        // opening one, and reading it the other way left `'Samsung'` in the prose.
        fn title(value: &str) -> Option<&'static str> {
            require_timeless_share_title(value).err().map(|error| error.code)
        }
        fn language(value: &str) -> Option<&'static str> {
            require_english_public_text(&[value.to_string()])
                .err()
                .map(|error| error.code)
        }
        /// The corpus column name and the policy that decides it.
        type Policy = fn(&str) -> Option<&'static str>;
        let policies: [(&str, Policy); 2] = [("titles", title), ("texts", language)];
        for (name, decide) in policies {
            let mut wrong = Vec::new();
            for case in cases(name) {
                let value = case[0].as_str().expect("value");
                let accepted = case[1].as_bool().expect("accepted");
                let expected = case[2].as_str();
                let got = decide(value);
                let matches = match (&got, accepted) {
                    (None, true) => true,
                    (Some(code), false) => Some(*code) == expected,
                    _ => false,
                };
                if !matches && wrong.len() < 8 {
                    wrong.push(format!(
                        "{value:?} -> {got:?}, Python said accepted={accepted} code={expected:?}"
                    ));
                }
            }
            assert!(
                wrong.is_empty(),
                "the {name} policy disagrees with Python:\n{}",
                wrong.join("\n")
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_deadline_in_a_title_is_refused_and_a_product_identifier_is_not() {
        // Each of these is what the reference refuses; the list was read off it rather than
        // guessed, because two of my first guesses were wrong.
        for title in [
            "Will it happen in 2027",
            "Announcement by midnight",
            "Q1 2026 earnings",
            "March 2026 launch",
            "sometime next week",
            "year-end results",
            "Launch before December",
            "closes 2026-09-15",
        ] {
            assert!(require_timeless_share_title(title).is_err(), "{title} has a deadline");
        }
        // Product identifiers look like dates and are not: the reference names M6, iPhone 17 and
        // RTX 5090, and none of them may be refused for that resemblance.
        for title in [
            "iPhone 17 demand",
            "RTX 5090 stock",
            "M6 chip performance",
            "Does Acme ship Product X",
            "BTC below 61,000 by Friday",
        ] {
            assert!(require_timeless_share_title(title).is_ok(), "{title} has no deadline");
        }
    }

    #[test]
    fn untranslated_prose_is_refused_and_a_quoted_name_is_allowed_through_it() {
        assert!(require_english_public_text(&["Product X will be announced.".to_string()]).is_ok());
        let error = require_english_public_text(&["제품 X가 발표될 예정입니다.".to_string()]).unwrap_err();
        assert_eq!(error.code, "ai_output_language");
        // A quoted name may keep its script; the prose around it still has to be English.
        assert!(require_english_public_text(&["Will \"삼성전자\" announce it?".to_string()]).is_ok());
        assert!(require_english_public_text(&["제품 \"X\"가 발표".to_string()]).is_err());
    }

    #[test]
    fn prose_with_no_english_words_at_all_is_refused() {
        // Two Latin letters in a row are the reference's test for English context; a title of
        // only punctuation and digits has none.
        assert!(
            require_english_public_text(&["12 34 56".to_string()]).is_ok(),
            "no non-Latin letters at all"
        );
        assert!(require_english_public_text(&["Ω 12 34".to_string()]).is_err());
    }
}
