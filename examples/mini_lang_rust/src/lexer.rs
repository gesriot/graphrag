//! Hand-written lexer: source text -> tokens. Mirrors examples/mini_lang/lexer.py.

use crate::errors::MiniLangError;
use crate::tokens::{Token, TokenKind};

fn single(c: char) -> Option<TokenKind> {
    match c {
        '+' => Some(TokenKind::Plus),
        '-' => Some(TokenKind::Minus),
        '*' => Some(TokenKind::Star),
        '/' => Some(TokenKind::Slash),
        '(' => Some(TokenKind::LParen),
        ')' => Some(TokenKind::RParen),
        '=' => Some(TokenKind::Equals),
        ';' => Some(TokenKind::Semicolon),
        _ => None,
    }
}

fn is_ident_start(c: char) -> bool {
    c.is_alphabetic() || c == '_'
}

fn is_ident_part(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

pub fn tokenize(source: &str) -> Result<Vec<Token>, MiniLangError> {
    let chars: Vec<char> = source.chars().collect();
    let n = chars.len();
    let mut tokens: Vec<Token> = Vec::new();
    let mut i = 0;
    while i < n {
        let c = chars[i];
        if c == ' ' || c == '\t' || c == '\r' {
            i += 1;
            continue;
        }
        if c == '\n' {
            // Newlines separate statements, just like ';'.
            tokens.push(Token {
                kind: TokenKind::Semicolon,
                text: "\n".to_string(),
                pos: i,
            });
            i += 1;
            continue;
        }
        if let Some(kind) = single(c) {
            tokens.push(Token {
                kind,
                text: c.to_string(),
                pos: i,
            });
            i += 1;
            continue;
        }
        if c.is_ascii_digit() || c == '.' {
            let start = i;
            let mut seen_dot = false;
            while i < n && (chars[i].is_ascii_digit() || chars[i] == '.') {
                if chars[i] == '.' {
                    if seen_dot {
                        return Err(MiniLangError::Lex(format!(
                            "malformed number at position {}",
                            start
                        )));
                    }
                    seen_dot = true;
                }
                i += 1;
            }
            let text: String = chars[start..i].iter().collect();
            if text == "." {
                return Err(MiniLangError::Lex(format!(
                    "malformed number at position {}",
                    start
                )));
            }
            tokens.push(Token {
                kind: TokenKind::Number,
                text,
                pos: start,
            });
            continue;
        }
        if is_ident_start(c) {
            let start = i;
            while i < n && is_ident_part(chars[i]) {
                i += 1;
            }
            let text: String = chars[start..i].iter().collect();
            let kind = if text == "let" {
                TokenKind::Let
            } else {
                TokenKind::Ident
            };
            tokens.push(Token {
                kind,
                text,
                pos: start,
            });
            continue;
        }
        return Err(MiniLangError::Lex(format!(
            "unknown token '{}' at position {}",
            c, i
        )));
    }
    tokens.push(Token {
        kind: TokenKind::Eof,
        text: String::new(),
        pos: n,
    });
    Ok(tokens)
}
