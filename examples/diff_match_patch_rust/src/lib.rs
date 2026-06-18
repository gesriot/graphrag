//! Structure-preserving Rust port of the staged diff-match-patch algorithmic
//! core: v1 diff + v2 match + v3 patch.
//! See examples/diff_match_patch/PROVENANCE.md for staged scope and API boundaries.
//!
//! Verified against the same golden_*.json contract as the Python reference.

pub mod diff;
pub mod matching;
pub mod patch;

pub use diff::{DiffMatchPatch, DELETE, EQUAL, INSERT};
pub use patch::Patch;
