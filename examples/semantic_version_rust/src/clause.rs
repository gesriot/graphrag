//! Clause tree. Mirrors the Clause hierarchy (AnyOf/AllOf/Always/Never/Matcher)
//! in examples/semantic_version/base.py, but only what `match` needs: the exact
//! frozenset/simplify machinery is irrelevant to match results, so the tree is
//! kept structural (AND/OR of Ranges).

use crate::range::Range;
use crate::version::Version;

#[derive(Debug, Clone)]
pub enum Clause {
    Always,
    Never,
    Range(Range),
    AllOf(Vec<Clause>),
    AnyOf(Vec<Clause>),
}

impl Clause {
    pub fn match_version(&self, version: &Version) -> bool {
        match self {
            Clause::Always => true,
            Clause::Never => false,
            Clause::Range(r) => r.match_version(version),
            Clause::AllOf(clauses) => clauses.iter().all(|c| c.match_version(version)),
            Clause::AnyOf(clauses) => clauses.iter().any(|c| c.match_version(version)),
        }
    }
}
