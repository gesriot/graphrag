//! mini_lang: structure-preserving Rust port of examples/mini_lang (Python).
//!
//! `run_source` is the single entry the CLI and the golden contract both use, so
//! "source -> (stdout, error)" is verified identically against the Python goldens.

pub mod ast_nodes;
pub mod errors;
pub mod eval;
pub mod lexer;
pub mod parser;
pub mod tokens;

/// Run a program. Returns (output_lines, error). error is None on success,
/// otherwise the stable `"<label>: <message>"` string.
pub fn run_source(source: &str) -> (Vec<String>, Option<String>) {
    let result = lexer::tokenize(source)
        .and_then(parser::parse)
        .and_then(|stmts| eval::run(&stmts));
    match result {
        Ok(outputs) => (outputs, None),
        Err(err) => (Vec::new(), Some(err.formatted())),
    }
}
