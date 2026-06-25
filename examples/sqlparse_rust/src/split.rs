//! `sqlparse.split` pipeline (Stage 3). Mirrors engine/statement_splitter.py,
//! the split path of engine/filter_stack.py, StripTrailingSemicolonFilter, and
//! sql.Statement stringification. No grouping/formatting/parse.
//!
//! Token-type comparisons follow Python's distinction exactly:
//! - `x in T.Keyword` (a `_TokenType`)  -> subtype (prefix) check
//! - `x in (a, b)` (a tuple) / `x is T`  -> exact path equality

use crate::lexer::tokenize;
use crate::tokens::{
    is_subtype, TokenType, COMMENT_MULTILINE, COMMENT_SINGLE, KEYWORD, KEYWORD_DDL, NAME, NEWLINE,
    PUNCTUATION, WHITESPACE,
};

struct Token {
    value: String,
    is_whitespace: bool,
}

impl Token {
    fn new(ttype: TokenType, value: String) -> Self {
        Token {
            is_whitespace: is_subtype(ttype, WHITESPACE),
            value,
        }
    }
}

type Statement = Vec<Token>;

#[derive(Default)]
struct StatementSplitter {
    // Set on `DECLARE` inside CREATE, but never read -- vestigial in upstream too;
    // kept to mirror the splitter's state structure.
    #[allow(dead_code)]
    in_declare: bool,
    in_case: bool,
    is_create: bool,
    begin_depth: i64,
    seen_begin: bool,
    consume_ws: bool,
    level: i64,
    tokens: Statement,
}

const TRANSACTION_WORDS: &[&str] = &[
    "TRANSACTION",
    "WORK",
    "TRAN",
    "DISTRIBUTED",
    "DEFERRED",
    "IMMEDIATE",
    "EXCLUSIVE",
];

impl StatementSplitter {
    fn reset(&mut self) {
        *self = StatementSplitter::default();
    }

    fn change_splitlevel(&mut self, ttype: TokenType, value: &str) -> i64 {
        if ttype == PUNCTUATION && value == "(" {
            return 1;
        }
        if ttype == PUNCTUATION && value == ")" {
            return -1;
        }
        if !is_subtype(ttype, KEYWORD) {
            return 0;
        }
        let unified = value.to_uppercase();
        if ttype == KEYWORD_DDL && unified.starts_with("CREATE") {
            self.is_create = true;
            return 0;
        }
        if unified == "DECLARE" && self.is_create && self.begin_depth == 0 {
            self.in_declare = true;
            return 1;
        }
        if unified == "BEGIN" {
            self.begin_depth += 1;
            self.seen_begin = true;
            if self.is_create {
                return 1;
            }
            return 0;
        }
        if self.seen_begin
            && (ttype == KEYWORD || ttype == NAME)
            && TRANSACTION_WORDS.contains(&unified.as_str())
        {
            self.begin_depth = (self.begin_depth - 1).max(0);
            self.seen_begin = false;
            return 0;
        }
        if unified == "END" {
            if !self.in_case {
                self.begin_depth = (self.begin_depth - 1).max(0);
            } else {
                self.in_case = false;
            }
            return -1;
        }
        if matches!(unified.as_str(), "IF" | "FOR" | "WHILE" | "CASE")
            && self.is_create
            && self.begin_depth > 0
        {
            if unified == "CASE" {
                self.in_case = true;
            }
            return 1;
        }
        if matches!(unified.as_str(), "END IF" | "END FOR" | "END WHILE") {
            return -1;
        }
        0
    }

    fn process(mut self, stream: Vec<(TokenType, String)>) -> Vec<Statement> {
        let mut out: Vec<Statement> = Vec::new();
        for (ttype, value) in stream {
            // EOS_TTYPE = (Whitespace, Comment.Single) -- exact membership.
            let is_eos = ttype == WHITESPACE || ttype == COMMENT_SINGLE;
            if self.consume_ws && !is_eos {
                out.push(std::mem::take(&mut self.tokens));
                self.reset();
            }

            self.level += self.change_splitlevel(ttype, &value);
            self.tokens.push(Token::new(ttype, value.clone()));

            let is_ws_or_comment = ttype == WHITESPACE
                || ttype == NEWLINE
                || ttype == COMMENT_SINGLE
                || ttype == COMMENT_MULTILINE;
            let is_begin_kw = ttype == KEYWORD && value.to_uppercase() == "BEGIN";
            if ttype == PUNCTUATION && value == ";" {
                if self.seen_begin {
                    self.begin_depth = (self.begin_depth - 1).max(0);
                }
                self.seen_begin = false;
                if self.level <= 0 && self.begin_depth == 0 {
                    self.consume_ws = true;
                }
            } else if ttype == KEYWORD && value.split_whitespace().next() == Some("GO") {
                self.consume_ws = true;
            } else if !is_ws_or_comment && !is_begin_kw {
                self.seen_begin = false;
            }
        }
        if !self.tokens.is_empty() && !self.tokens.iter().all(|t| t.is_whitespace) {
            out.push(self.tokens);
        }
        out
    }
}

fn strip_trailing_semicolon(stmt: &mut Statement) {
    while let Some(last) = stmt.last() {
        if last.is_whitespace || last.value == ";" {
            stmt.pop();
        } else {
            break;
        }
    }
}

fn statement_str(stmt: &Statement) -> String {
    stmt.iter().map(|t| t.value.as_str()).collect()
}

/// Mirrors `sqlparse.split(sql, strip_semicolon=...)`.
pub fn split(sql: &str, strip_semicolon: bool) -> Vec<String> {
    let stream = tokenize(sql);
    let statements = StatementSplitter::default().process(stream);
    statements
        .into_iter()
        .map(|mut stmt| {
            if strip_semicolon {
                strip_trailing_semicolon(&mut stmt);
            }
            statement_str(&stmt).trim().to_string()
        })
        .collect()
}
