//! Token type hierarchy. Mirrors examples/sqlparse/tokens.py.
//!
//! Python uses tuple-subclass `_TokenType` where membership (`a in b`) means "b
//! is a prefix of a" (a is a subtype of b). We represent a token type as its
//! path of names (`&'static [&'static str]`) and model subtyping as a prefix check.

/// A token type is its hierarchy path, e.g. `Keyword.DML` -> `["Keyword", "DML"]`.
pub type TokenType = &'static [&'static str];

/// `child` is a subtype of `parent` iff `parent` is a prefix of `child`
/// (mirrors `parent_tt.__contains__(child_tt)` / `child in parent`).
pub fn is_subtype(child: TokenType, parent: TokenType) -> bool {
    child.len() >= parent.len() && &child[..parent.len()] == parent
}

// Root + common types (mirror tokens.py).
pub const TOKEN: TokenType = &[];
pub const TEXT: TokenType = &["Text"];
pub const WHITESPACE: TokenType = &["Text", "Whitespace"];
pub const NEWLINE: TokenType = &["Text", "Whitespace", "Newline"];
pub const ERROR: TokenType = &["Error"];
pub const OTHER: TokenType = &["Other"];

pub const KEYWORD: TokenType = &["Keyword"];
pub const NAME: TokenType = &["Name"];
pub const NAME_PLACEHOLDER: TokenType = &["Name", "Placeholder"];
pub const NAME_BUILTIN: TokenType = &["Name", "Builtin"];
pub const LITERAL: TokenType = &["Literal"];
pub const STRING: TokenType = &["Literal", "String"];
pub const STRING_SINGLE: TokenType = &["Literal", "String", "Single"];
pub const STRING_SYMBOL: TokenType = &["Literal", "String", "Symbol"];
pub const NUMBER: TokenType = &["Literal", "Number"];
pub const NUMBER_INTEGER: TokenType = &["Literal", "Number", "Integer"];
pub const NUMBER_FLOAT: TokenType = &["Literal", "Number", "Float"];
pub const NUMBER_HEXADECIMAL: TokenType = &["Literal", "Number", "Hexadecimal"];
pub const PUNCTUATION: TokenType = &["Punctuation"];
pub const OPERATOR: TokenType = &["Operator"];
pub const COMPARISON: TokenType = &["Operator", "Comparison"];
pub const WILDCARD: TokenType = &["Wildcard"];
pub const COMMENT: TokenType = &["Comment"];
pub const COMMENT_SINGLE: TokenType = &["Comment", "Single"];
pub const COMMENT_SINGLE_HINT: TokenType = &["Comment", "Single", "Hint"];
pub const COMMENT_MULTILINE: TokenType = &["Comment", "Multiline"];
pub const COMMENT_MULTILINE_HINT: TokenType = &["Comment", "Multiline", "Hint"];
pub const ASSIGNMENT: TokenType = &["Assignment"];

pub const GENERIC: TokenType = &["Generic"];
pub const COMMAND: TokenType = &["Generic", "Command"];

pub const KEYWORD_DML: TokenType = &["Keyword", "DML"];
pub const KEYWORD_DDL: TokenType = &["Keyword", "DDL"];
pub const KEYWORD_CTE: TokenType = &["Keyword", "CTE"];
pub const KEYWORD_ORDER: TokenType = &["Keyword", "Order"];
pub const KEYWORD_TZCAST: TokenType = &["Keyword", "TZCast"];
