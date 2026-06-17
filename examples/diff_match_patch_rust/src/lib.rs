//! Structure-preserving Rust port of diff-match-patch, v1 diff + v2 match scope.
//! See examples/diff_match_patch/PROVENANCE.md for staged scope and API boundaries.
//!
//! Verified against the same golden_*.json contract as the Python reference.

pub mod diff;
pub mod matching;

pub use diff::{DiffMatchPatch, DELETE, EQUAL, INSERT};
