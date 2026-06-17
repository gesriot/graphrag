//! Structure-preserving Rust port of the vendored Python `semantic_version`
//! (v1 scope: `Version` only). See examples/semantic_version/PROVENANCE.md.
//!
//! Verified against the same golden_*.json contract as the Python reference.

pub mod identifier;
pub mod version;

pub use version::{compare, validate, Version};
