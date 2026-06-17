//! Error types. `formatted()` yields the stable `"<label>: <message>"` string
//! that the golden contract matches byte-for-byte against the Python source.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MiniLangError {
    Lex(String),
    Parse(String),
    UndefinedVariable(String),
    DivisionByZero(String),
}

impl MiniLangError {
    pub fn label(&self) -> &'static str {
        match self {
            MiniLangError::Lex(_) => "LexError",
            MiniLangError::Parse(_) => "ParseError",
            MiniLangError::UndefinedVariable(_) => "UndefinedVariable",
            MiniLangError::DivisionByZero(_) => "DivisionByZero",
        }
    }

    pub fn message(&self) -> &str {
        match self {
            MiniLangError::Lex(m)
            | MiniLangError::Parse(m)
            | MiniLangError::UndefinedVariable(m)
            | MiniLangError::DivisionByZero(m) => m,
        }
    }

    pub fn formatted(&self) -> String {
        format!("{}: {}", self.label(), self.message())
    }
}

/// Python `repr()` of the small strings we embed in error messages (single chars
/// or the empty string). Matches `{x!r}` for these cases: `''`, `'='`, `'4'`.
pub fn pyrepr(s: &str) -> String {
    format!("'{}'", s)
}
