//! Structure-preserving Rust port of jsmn (default mode), the first C→Rust port.
//! See examples/jsmn/PROVENANCE.md for scope. Verified against the same
//! golden_parse.json the C reference produced.

pub mod jsmn;

pub use jsmn::{parse_json, Token};
