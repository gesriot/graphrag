//! Structure-preserving Rust port of the vendored Python `semantic_version`
//! (v1 scope: `Version` only). See examples/semantic_version/PROVENANCE.md.
//!
//! Verified against the same golden_*.json contract as the Python reference.

pub mod clause;
pub mod identifier;
pub mod npm;
pub mod range;
pub mod spec;
pub mod version;

pub use npm::NpmSpec;
pub use spec::SimpleSpec;
pub use version::{compare, validate, Version};
