//! SQL lexer. Mirrors examples/sqlparse/lexer.py `Lexer.get_tokens`.
//!
//! Each SQL_REGEX rule is compiled with `fancy-regex` (a superset of `regex`
//! supporting the lookbehind/lookahead/backreference rules) under `(?i)`
//! (re.IGNORECASE; \w is Unicode by default). At each position we emulate
//! Python's `re.match(text, pos)` via `captures_from_pos(text, pos)` and require
//! the match to start exactly at `pos` -- this keeps full-text context so
//! lookbehind rules see the characters before `pos`. Positions are byte offsets
//! kept on char boundaries; emitted token values are exact source slices.

use std::sync::OnceLock;

use fancy_regex::Regex;

use crate::keywords::keyword_token;
use crate::lexer_rules::RULES;
use crate::tokens::{TokenType, ERROR, NAME};

struct CompiledRule {
    re: Regex,
    action: Option<TokenType>, // None == PROCESS_AS_KEYWORD
}

fn compiled() -> &'static Vec<CompiledRule> {
    static C: OnceLock<Vec<CompiledRule>> = OnceLock::new();
    C.get_or_init(|| {
        RULES
            .iter()
            .map(|(pat, action)| CompiledRule {
                re: Regex::new(&format!("(?i){}", pat)).expect("valid SQL_REGEX rule"),
                action: *action,
            })
            .collect()
    })
}

/// Tokenize `sql` into `(token_type, value)` pairs, mirroring `lexer.tokenize`.
pub fn tokenize(sql: &str) -> Vec<(TokenType, String)> {
    let rules = compiled();
    let mut out: Vec<(TokenType, String)> = Vec::new();
    let n = sql.len();
    let mut pos = 0usize;
    while pos < n {
        let mut matched = false;
        for rule in rules {
            // captures_from_pos searches from `pos` with full-text context; we
            // accept only a match anchored exactly at `pos` (== re.match(text, pos)).
            if let Ok(Some(caps)) = rule.re.captures_from_pos(sql, pos) {
                let m = caps.get(0).expect("group 0 always present");
                if m.start() != pos || m.end() == pos {
                    continue;
                }
                let value = &sql[pos..m.end()];
                let tt = match rule.action {
                    Some(tt) => tt,
                    None => keyword_token(&value.to_uppercase()).unwrap_or(NAME),
                };
                out.push((tt, value.to_string()));
                pos = m.end();
                matched = true;
                break;
            }
        }
        if !matched {
            let ch = sql[pos..].chars().next().expect("pos on a char boundary");
            let len = ch.len_utf8();
            out.push((ERROR, sql[pos..pos + len].to_string()));
            pos += len;
        }
    }
    out
}
