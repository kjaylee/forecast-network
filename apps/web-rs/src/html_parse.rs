//! `html.parser.HTMLParser`, ported far enough to feed `_Article`.
//!
//! The reference is not an HTML5 parser and does not recover from malformed markup the way a
//! browser does. It is a state machine with its own tolerances, and a page that a browser
//! repairs is a page this one may cut short: `<!-->` opens a comment nothing closes, so the
//! reference drops the rest of the document and so does this. Reproducing the tolerances
//! matters because the text it produces is what the publication-time rules run against.
//!
//! Only `feed()` is modelled, never `close()`, because `article_content` only calls `feed`.
//! That is not a simplification: with no `close()` the reference never flushes, so text after
//! the last tag — and any span it was waiting to complete — is not delivered at all.
//!
//! The two locating patterns are hand-written because Rust's `regex` has no lookbehind and
//! both need one. They are transcribed from `locatestarttagend_tolerant` and
//! `attrfind_tolerant` in `html/parser.py`, including the backtracking that makes an
//! unterminated quoted value parse as a bare name.

use crate::html::unescape;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tag {
    pub name: String,
    pub attrs: Vec<(String, Option<String>)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    Start(Tag),
    End(String),
    Data(String),
}

const CDATA_ELEMENTS: [&str; 2] = ["script", "style"];

/// The two places the reference does not recover. It raises, so a page containing either is
/// a page `article_content` refuses to describe — and a port that recovered instead would
/// publish text the reference declines to produce.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParseError {
    /// `_scan_name` found no name token after `<![`.
    ExpectedNameToken,
    /// A marked section with a status keyword the reference does not know.
    UnknownStatusKeyword,
}

/// The marked-section closers, `]\s*]\s*>` and `]\s*>`, matched leftmost like the reference.
fn find_marked_close(s: &[char], from: usize, pattern: &str) -> Option<usize> {
    let two_brackets = pattern.matches(']').count() == 2;
    let n = s.len();
    let mut at = from;
    while at < n {
        if s[at] == ']' {
            let mut probe = at + 1;
            while probe < n && is_space(s[probe]) {
                probe += 1;
            }
            if !two_brackets || s.get(probe) == Some(&']') {
                if two_brackets {
                    probe += 1;
                }
                while probe < n && is_space(s[probe]) {
                    probe += 1;
                }
                if s.get(probe) == Some(&'>') {
                    return Some(probe + 1);
                }
            }
        }
        at += 1;
    }
    None
}

fn is_space(ch: char) -> bool {
    ch.is_whitespace()
}

fn name_char(ch: char) -> bool {
    !matches!(ch, '\t' | '\n' | '\r' | '\u{c}' | ' ' | '/' | '>' | '\0')
}

/// `tagfind_tolerant`, returning the end of the name *and* of the separators after it, which
/// is where the reference starts looking for attributes.
fn tag_name_end(s: &[char], start: usize) -> Option<(usize, String)> {
    let n = s.len();
    if !s.get(start).is_some_and(|c| c.is_ascii_alphabetic()) {
        return None;
    }
    let mut k = start + 1;
    while k < n && name_char(s[k]) {
        k += 1;
    }
    let name: String = s[start..k].iter().collect::<String>().to_lowercase();
    // `(?:\s|/(?!>))*`
    while k < n && (is_space(s[k]) || (s[k] == '/' && s.get(k + 1) != Some(&'>'))) {
        k += 1;
    }
    Some((k, name))
}

/// `attrfind_tolerant` at `start`, whose lookbehind requires the preceding character to be a
/// quote, whitespace or slash — which is why `<div a=1>` has an attribute and `<diva=1>` does
/// not (the tag name ate it).
fn attribute(s: &[char], start: usize) -> Option<(usize, String, Option<String>)> {
    let n = s.len();
    if start == 0 || start >= n {
        return None;
    }
    let before = s[start - 1];
    if !(before == '\'' || before == '"' || is_space(before) || before == '/') {
        return None;
    }
    if is_space(s[start]) || s[start] == '>' || s[start] == '/' {
        return None;
    }
    let mut k = start + 1;
    while k < n && !is_space(s[k]) && !matches!(s[k], '>' | '=' | '/') {
        k += 1;
    }
    let name: String = s[start..k].iter().collect::<String>().to_lowercase();

    // The value is optional, and a quoted one that never closes is not a value at all: the
    // whole group fails and the name stands alone.
    let mut value = None;
    let mut end = k;
    let mut probe = k;
    while probe < n && is_space(s[probe]) {
        probe += 1;
    }
    if probe < n && s[probe] == '=' {
        while probe < n && s[probe] == '=' {
            probe += 1;
        }
        while probe < n && is_space(s[probe]) {
            probe += 1;
        }
        let quoted = matches!(s.get(probe), Some('\'') | Some('"'));
        let raw = if quoted {
            let quote = s[probe];
            s[probe + 1..].iter().position(|c| *c == quote).map(|offset| {
                let end = probe + 1 + offset;
                (end + 1, s[probe + 1..end].iter().collect::<String>())
            })
        } else if probe < n {
            let mut w = probe;
            while w < n && !is_space(s[w]) && s[w] != '>' {
                w += 1;
            }
            Some((w, s[probe..w].iter().collect::<String>()))
        } else {
            None
        };
        if let Some((value_end, text)) = raw {
            value = Some(text);
            end = value_end;
            while end < n && is_space(s[end]) {
                end += 1;
            }
        }
    }
    // `(?:\s|/(?!>))*`
    while end < n && (is_space(s[end]) || (s[end] == '/' && s.get(end + 1) != Some(&'>'))) {
        end += 1;
    }
    Some((end, name, value))
}

/// `locatestarttagend_tolerant`: where the reference decides a start tag ends, before it
/// looks at what follows.
fn locate_start_tag_end(s: &[char], i: usize) -> Option<usize> {
    let n = s.len();
    if s.get(i) != Some(&'<') || !s.get(i + 1).is_some_and(|c| c.is_ascii_alphabetic()) {
        return None;
    }
    let mut k = i + 2;
    while k < n && name_char(s[k]) {
        k += 1;
    }
    // The optional group: separators, then attributes, repeated.
    loop {
        let mut probe = k;
        while probe < n && (is_space(s[probe]) || s[probe] == '/') {
            probe += 1;
        }
        match attribute(s, probe) {
            Some((end, _, _)) if probe > k || end > probe => {
                k = end;
            }
            _ => {
                if probe > k {
                    k = probe;
                }
                break;
            }
        }
    }
    while k < n && is_space(s[k]) {
        k += 1;
    }
    Some(k)
}

struct Tokenizer<'a> {
    source: &'a [char],
    pos: usize,
    cdata: Option<String>,
    events: Vec<Event>,
    /// Set where the reference raises; the scan stops there, as it would on the exception.
    error: Option<ParseError>,
}

impl<'a> Tokenizer<'a> {
    fn starts_with(&self, at: usize, text: &str) -> bool {
        text.chars()
            .enumerate()
            .all(|(offset, ch)| self.source.get(at + offset) == Some(&ch))
    }

    /// The `interesting` pattern in CDATA mode, `</\s*elem\s*>` case-insensitively. Returns
    /// the offset of its start, or the length of the remainder when it is not there.
    fn cdata_end(&self) -> usize {
        let element = self.cdata.clone().unwrap_or_default();
        let n = self.source.len();
        let mut at = self.pos;
        while at < n {
            if self.source[at] == '<' && self.source.get(at + 1) == Some(&'/') {
                let mut probe = at + 2;
                while probe < n && is_space(self.source[probe]) {
                    probe += 1;
                }
                let start = probe;
                while probe < n && probe - start < element.chars().count() {
                    if self.source[probe].to_ascii_lowercase() != element.chars().nth(probe - start).unwrap_or(' ') {
                        break;
                    }
                    probe += 1;
                }
                if probe - start == element.chars().count() {
                    while probe < n && is_space(self.source[probe]) {
                        probe += 1;
                    }
                    if self.source.get(probe) == Some(&'>') {
                        return at;
                    }
                }
            }
            at += 1;
        }
        n
    }

    fn parse_starttag(&mut self, i: usize) -> Option<usize> {
        let endpos = self.check_for_whole_start_tag(i)?;
        let (mut k, name) = tag_name_end(self.source, i + 1)?;
        let mut attrs: Vec<(String, Option<String>)> = Vec::new();
        while k < endpos {
            match attribute(self.source, k) {
                Some((end, attr_name, value)) => {
                    // The reference unescapes an attribute value only when it is non-empty.
                    let value = match value {
                        Some(text) if !text.is_empty() => Some(unescape(&text)),
                        other => other,
                    };
                    attrs.push((attr_name, value));
                    k = end;
                }
                None => break,
            }
        }
        let tail: String = self.source[k..endpos].iter().collect::<String>();
        let tail = tail.trim();
        if tail != ">" && tail != "/>" {
            let data: String = self.source[i..endpos].iter().collect();
            self.events.push(Event::Data(data));
            return Some(endpos);
        }
        let self_closing = tail.ends_with("/>");
        self.events.push(Event::Start(Tag {
            name: name.clone(),
            attrs,
        }));
        if self_closing {
            self.events.push(Event::End(name));
        } else if CDATA_ELEMENTS.contains(&name.as_str()) {
            self.cdata = Some(name);
        }
        Some(endpos)
    }

    fn check_for_whole_start_tag(&self, i: usize) -> Option<usize> {
        let j = locate_start_tag_end(self.source, i)?;
        match self.source.get(j) {
            Some('>') => Some(j + 1),
            Some('/') => {
                if self.starts_with(j, "/>") {
                    Some(j + 2)
                } else if j > i {
                    Some(j)
                } else {
                    Some(i + 1)
                }
            }
            None => None,
            Some(ch) if ch.is_ascii_alphabetic() || matches!(ch, '=' | '/') => None,
            _ if j > i => Some(j),
            _ => Some(i + 1),
        }
    }

    fn parse_endtag(&mut self, i: usize) -> Option<usize> {
        let n = self.source.len();
        // `endendtag = re.compile('>')`, searched from just after the `</`.
        let gt = self.source[i + 1..].iter().position(|c| *c == '>')? + i + 1;
        // `endtagfind = r'</\s*([a-zA-Z][-.a-zA-Z0-9:_]*)\s*>'`
        let mut probe = i + 2;
        while probe < n && is_space(self.source[probe]) {
            probe += 1;
        }
        let start = probe;
        if probe < n && self.source[probe].is_ascii_alphabetic() {
            probe += 1;
            while probe < n
                && (self.source[probe].is_ascii_alphanumeric() || matches!(self.source[probe], '-' | '.' | ':' | '_'))
            {
                probe += 1;
            }
        }
        if probe > start && self.source[probe..gt].iter().all(|c| is_space(*c)) {
            let name: String = self.source[start..probe].iter().collect::<String>().to_lowercase();
            if let Some(element) = self.cdata.clone() {
                if name != element {
                    // Inside CDATA, a closing tag for something else is content, not markup.
                    let data: String = self.source[i..gt + 1].iter().collect();
                    self.events.push(Event::Data(data));
                    return Some(gt + 1);
                }
            }
            self.cdata = None;
            self.events.push(Event::End(name));
            return Some(gt + 1);
        }
        if self.cdata.is_some() {
            let data: String = self.source[i..gt + 1].iter().collect();
            self.events.push(Event::Data(data));
            return Some(gt + 1);
        }
        // Not a closing tag the reference recognises: take the name from `tagfind_tolerant`
        // and ignore whatever sits between it and the `>`.
        if self.source.get(i + 2).is_some_and(|c| c.is_ascii_alphabetic()) {
            let mut end = i + 3;
            while end < n && !is_space(self.source[end]) && !matches!(self.source[end], '/' | '>' | '\0') {
                end += 1;
            }
            let name: String = self.source[i + 2..end].iter().collect::<String>().to_lowercase();
            let close = self.source[end..].iter().position(|c| *c == '>')? + end + 1;
            self.events.push(Event::End(name));
            return Some(close);
        }
        if self.starts_with(i, "</>") {
            return Some(i + 3);
        }
        self.parse_bogus_comment(i)
    }

    fn parse_bogus_comment(&mut self, i: usize) -> Option<usize> {
        let position = self.source[i + 2..].iter().position(|c| *c == '>')? + i + 2;
        Some(position + 1)
    }

    fn parse_comment(&mut self, i: usize) -> Option<usize> {
        // `commentclose = r'--\s*>'`, searched from just after `<!--`.
        let n = self.source.len();
        let mut at = i + 4;
        while at + 1 < n {
            if self.source[at] == '-' && self.source[at + 1] == '-' {
                let mut probe = at + 2;
                while probe < n && is_space(self.source[probe]) {
                    probe += 1;
                }
                if self.source.get(probe) == Some(&'>') {
                    return Some(probe + 1);
                }
            }
            at += 1;
        }
        None
    }

    fn parse_pi(&mut self, i: usize) -> Option<usize> {
        self.source[i + 2..]
            .iter()
            .position(|c| *c == '>')
            .map(|off| off + i + 2 + 1)
    }

    fn parse_html_declaration(&mut self, i: usize) -> Option<usize> {
        if self.starts_with(i, "<!--") {
            return self.parse_comment(i);
        }
        if self.starts_with(i, "<![") {
            // `parse_marked_section`. Both failure modes are the reference raising, not
            // returning, so they are recorded and the scan stops.
            let n = self.source.len();
            let at = i + 3;
            if at >= n {
                // `_scan_name` reports the end of the buffer rather than a bad name, so this
                // is a wait for a continuation that one `feed()` never brings. Not a refusal.
                return None;
            }
            if !self.source[at].is_ascii_alphabetic() {
                self.error = Some(ParseError::ExpectedNameToken);
                return None;
            }
            let mut probe = at + 1;
            while probe < n
                && (self.source[probe].is_ascii_alphanumeric() || matches!(self.source[probe], '-' | '_' | '.'))
            {
                probe += 1;
            }
            let name: String = self.source[at..probe].iter().collect::<String>().to_lowercase();
            // The name pattern ends with `\s*`, and a match that reaches the end of the
            // buffer leaves the reference waiting rather than concluding anything.
            while probe < n && is_space(self.source[probe]) {
                probe += 1;
            }
            if probe >= n {
                return None;
            }
            let close = if ["temp", "cdata", "ignore", "include", "rcdata"].contains(&name.as_str()) {
                find_marked_close(self.source, i + 3, r"]\s*]\s*>")
            } else if ["if", "else", "endif"].contains(&name.as_str()) {
                find_marked_close(self.source, i + 3, r"]\s*>")
            } else {
                self.error = Some(ParseError::UnknownStatusKeyword);
                return None;
            };
            return close;
        }
        let lowered: String = self.source[i..n_min(self.source.len(), i + 9)]
            .iter()
            .collect::<String>()
            .to_lowercase();
        if lowered == "<!doctype" {
            return self.source[i + 9..]
                .iter()
                .position(|c| *c == '>')
                .map(|off| off + i + 9 + 1);
        }
        self.parse_bogus_comment(i)
    }

    /// `goahead(0)` — the only mode the reference is ever called in here.
    fn goahead(&mut self) {
        let n = self.source.len();
        while self.pos < n {
            let (stop, raw) = match self.cdata {
                Some(_) => {
                    let at = self.cdata_end();
                    if at >= n {
                        break;
                    }
                    (at, true)
                }
                None => match self.source[self.pos..].iter().position(|c| *c == '<') {
                    Some(offset) => (self.pos + offset, false),
                    None => {
                        // The reference holds back text whose tail could be a reference split
                        // across two feeds. There is one feed, so that text is never
                        // delivered at all rather than delivered twice.
                        let from = self.pos.max(n.saturating_sub(34));
                        let amp = self.source[from..]
                            .iter()
                            .rposition(|c| *c == '&')
                            .map(|off| off + from);
                        if let Some(amp) = amp {
                            if !self.source[amp..].iter().any(|c| is_space(*c) || *c == ';') {
                                break;
                            }
                        }
                        (n, false)
                    }
                },
            };
            if self.pos < stop {
                let text: String = self.source[self.pos..stop].iter().collect();
                self.events.push(Event::Data(if raw { text } else { unescape(&text) }));
            }
            self.pos = stop;
            if self.pos >= n {
                break;
            }
            if self.source[self.pos] != '<' {
                continue;
            }
            let advanced = if self.starts_with(self.pos, "<!--") {
                self.parse_comment(self.pos)
            } else if self.starts_with(self.pos, "<?") {
                self.parse_pi(self.pos)
            } else if self.starts_with(self.pos, "<!") {
                self.parse_html_declaration(self.pos)
            } else if self.source.get(self.pos + 1).is_some_and(|c| c.is_ascii_alphabetic()) {
                self.parse_starttag(self.pos)
            } else if self.starts_with(self.pos, "</") {
                self.parse_endtag(self.pos)
            } else if self.pos + 1 < n {
                self.events.push(Event::Data("<".to_string()));
                Some(self.pos + 1)
            } else {
                break;
            };
            match advanced {
                Some(next) => self.pos = next,
                None => break,
            }
        }
    }
}

fn n_min(a: usize, b: usize) -> usize {
    if a < b {
        a
    } else {
        b
    }
}

/// Every event the reference would deliver for one `feed()`.
pub fn tokenize(body: &str) -> Result<Vec<Event>, ParseError> {
    let mut tokenizer = Tokenizer {
        source: &body.chars().collect::<Vec<char>>(),
        pos: 0,
        cdata: None,
        events: Vec::new(),
        error: None,
    };
    tokenizer.goahead();
    match tokenizer.error {
        Some(error) => Err(error),
        None => Ok(tokenizer.events),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data(body: &str) -> Vec<String> {
        tokenize(body)
            .expect("no error")
            .into_iter()
            .filter_map(|event| match event {
                Event::Data(text) => Some(text),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn a_plain_document_delivers_its_text() {
        assert_eq!(data("<p>hello</p>"), vec!["hello"]);
    }

    #[test]
    fn text_after_the_last_tag_is_delivered_when_the_feed_ends() {
        assert_eq!(data("<p>a</p>b"), vec!["a", "b"]);
    }

    #[test]
    fn script_content_is_not_decoded_and_is_not_text_to_a_browser() {
        let events = tokenize("<script>a &amp; b</script>x").expect("no error");
        assert!(events.contains(&Event::Data("a &amp; b".to_string())), "{events:?}");
    }

    #[test]
    fn an_unclosed_comment_swallows_the_rest() {
        // Not a bug being reproduced for its own sake: the reference does this, so the text
        // it produces for such a page is the text the rules were written against.
        assert_eq!(data("a<!-->b"), vec!["a"]);
    }

    #[test]
    fn an_attribute_without_a_value_has_none_and_one_with_an_equals_has_an_empty_string() {
        let events = tokenize("<a hidden href=></a>").expect("no error");
        let Some(Event::Start(tag)) = events.first() else {
            panic!("{events:?}")
        };
        assert_eq!(
            tag.attrs,
            vec![("hidden".to_string(), None), ("href".to_string(), Some(String::new()))]
        );
    }

    #[test]
    fn a_marked_section_the_reference_refuses_is_an_error_not_a_recovery() {
        // The reference raises here. Recovering instead would produce article text it
        // declines to produce, which on this path means publishing evidence it would not.
        assert_eq!(tokenize("<![<"), Err(ParseError::ExpectedNameToken));
        assert_eq!(tokenize("<![foo]"), Err(ParseError::UnknownStatusKeyword));
        // A name that reaches the end of the buffer is a wait, not a refusal.
        assert_eq!(tokenize("<![cdata"), Ok(vec![]));
    }

    #[test]
    fn a_known_marked_section_is_skipped_and_its_content_is_not_text() {
        assert_eq!(data("a<![cdata[x]]>b"), vec!["a", "b"]);
    }

    #[test]
    fn an_unterminated_quoted_value_takes_the_rest_of_the_document_with_it() {
        // Not a value that fails to parse, and not an attribute without one: the quoted
        // alternative cannot match, the bare alternative is forbidden a quote, so the whole
        // start tag never completes and the reference waits for a continuation that its one
        // `feed()` never brings. Confirmed against CPython, which returns nothing at all.
        assert_eq!(tokenize("<a href=\"x></a>").expect("no error"), vec![]);
    }

    #[test]
    fn attribute_names_and_tags_are_lowercased() {
        let events = tokenize("<DIV CLASS=\"X\"></DIV>").expect("no error");
        let Some(Event::Start(tag)) = events.first() else {
            panic!("{events:?}")
        };
        assert_eq!(tag.name, "div");
        assert_eq!(tag.attrs, vec![("class".to_string(), Some("X".to_string()))]);
        assert!(events.contains(&Event::End("div".to_string())));
    }
}
