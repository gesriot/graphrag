//! Token kinds and the Token value type. Mirrors examples/mini_lang/tokens.py.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenKind {
    Number,
    Ident,
    Let,
    Plus,
    Minus,
    Star,
    Slash,
    LParen,
    RParen,
    Equals,
    Semicolon,
    Eof,
}

impl TokenKind {
    /// Stable name matching the Python `TokenKind` enum values (used in error text).
    pub fn name(&self) -> &'static str {
        match self {
            TokenKind::Number => "NUMBER",
            TokenKind::Ident => "IDENT",
            TokenKind::Let => "LET",
            TokenKind::Plus => "PLUS",
            TokenKind::Minus => "MINUS",
            TokenKind::Star => "STAR",
            TokenKind::Slash => "SLASH",
            TokenKind::LParen => "LPAREN",
            TokenKind::RParen => "RPAREN",
            TokenKind::Equals => "EQUALS",
            TokenKind::Semicolon => "SEMICOLON",
            TokenKind::Eof => "EOF",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Token {
    pub kind: TokenKind,
    pub text: String,
    pub pos: usize,
}
