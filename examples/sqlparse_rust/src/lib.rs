//! Structure-preserving Rust port of `sqlparse` (staged, Phase 5 scale milestone).
//! Stage 1: token type tree + keyword tables. Later stages: lexer parity ->
//! StatementSplitter/filter pipeline -> `split` golden/port_eval.
//! See examples/sqlparse/PROVENANCE.md for scope.

pub mod keywords;
pub mod lexer;
pub mod lexer_rules;
pub mod split;
pub mod tokens;

#[cfg(test)]
mod tests {
    use crate::keywords::keyword_token;
    use crate::tokens::{is_subtype, KEYWORD, KEYWORD_DML, NAME, NAME_BUILTIN};

    #[test]
    fn keyword_tree_and_table_basics() {
        // Subtype (prefix) semantics mirror Python `_TokenType.__contains__`.
        assert!(is_subtype(KEYWORD_DML, KEYWORD));
        assert!(!is_subtype(KEYWORD, KEYWORD_DML));
        assert!(!is_subtype(NAME, KEYWORD));
        // Keyword table (uppercase lookup), first-match-wins across dialects.
        assert_eq!(keyword_token("SELECT"), Some(KEYWORD_DML));
        assert_eq!(keyword_token("FROM"), Some(KEYWORD));
        assert_eq!(keyword_token("DEFINITELY_NOT_A_KEYWORD"), None);
        // Conflicting duplicate keys across dialect dictionaries must keep the
        // first Python add-order match.
        assert_eq!(keyword_token("CHARACTER"), Some(KEYWORD));
        assert_eq!(keyword_token("MAP"), Some(NAME_BUILTIN));
        assert_eq!(keyword_token("TIMESTAMP"), Some(NAME_BUILTIN));
    }
}
