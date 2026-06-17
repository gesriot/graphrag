//! Range matcher. Mirrors `Range` in examples/semantic_version/base.py, including
//! the prerelease/build policies (the subtlest part of SimpleSpec matching).

use std::cmp::Ordering;

use crate::version::Version;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Op {
    Eq,
    Gt,
    Gte,
    Lt,
    Lte,
    Neq,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PrereleasePolicy {
    Always,
    Natural,
    SamePatch,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BuildPolicy {
    Implicit,
    Strict,
}

#[derive(Debug, Clone)]
pub struct Range {
    pub operator: Op,
    pub target: Version,
    pub prerelease_policy: PrereleasePolicy,
    pub build_policy: BuildPolicy,
}

impl Range {
    pub fn new(
        operator: Op,
        target: Version,
        prerelease_policy: PrereleasePolicy,
        build_policy: BuildPolicy,
    ) -> Range {
        // Upstream raises if target has build and op is ordered; SimpleSpec.parse_block
        // rejects that earlier ("Invalid simple spec"), so it's unreachable here.
        // A target carrying build always forces strict build matching.
        let build_policy = if !target.build.is_empty() {
            BuildPolicy::Strict
        } else {
            build_policy
        };
        Range {
            operator,
            target,
            prerelease_policy,
            build_policy,
        }
    }

    pub fn match_version(&self, version: &Version) -> bool {
        // Drop build unless we match it strictly.
        let truncated;
        let version: &Version = if self.build_policy != BuildPolicy::Strict {
            truncated = version.truncate_to_prerelease();
            &truncated
        } else {
            version
        };

        if !version.prerelease.is_empty() {
            let same_patch = self.target.truncate_to_patch() == version.truncate_to_patch();
            if self.prerelease_policy == PrereleasePolicy::SamePatch && !same_patch {
                return false;
            }
        }

        match self.operator {
            Op::Eq => {
                if self.build_policy == BuildPolicy::Strict {
                    self.target.truncate_to_prerelease() == version.truncate_to_prerelease()
                        && version.build == self.target.build
                } else {
                    *version == self.target
                }
            }
            Op::Gt => version.precedence_cmp(&self.target) == Ordering::Greater,
            Op::Gte => version.precedence_cmp(&self.target) != Ordering::Less,
            Op::Lt => {
                if !version.prerelease.is_empty()
                    && self.prerelease_policy == PrereleasePolicy::Natural
                    && version.truncate_to_patch() == self.target.truncate_to_patch()
                    && self.target.prerelease.is_empty()
                {
                    return false;
                }
                version.precedence_cmp(&self.target) == Ordering::Less
            }
            Op::Lte => version.precedence_cmp(&self.target) != Ordering::Greater,
            Op::Neq => {
                if self.build_policy == BuildPolicy::Strict {
                    return !(self.target.truncate_to_prerelease()
                        == version.truncate_to_prerelease()
                        && version.build == self.target.build);
                }
                if !version.prerelease.is_empty()
                    && self.prerelease_policy == PrereleasePolicy::Natural
                    && version.truncate_to_patch() == self.target.truncate_to_patch()
                    && self.target.prerelease.is_empty()
                {
                    return false;
                }
                *version != self.target
            }
        }
    }
}
