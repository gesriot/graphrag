//! SimpleSpec port (v2a). Mirrors `SimpleSpec` + its `Parser` in
//! examples/semantic_version/base.py: comma-AND blocks, the prefix operators
//! (`^ ~ ~= == != > >= < <=`, wildcards), and match/select/filter.

use std::cmp::Ordering;
use std::sync::OnceLock;

use regex::Regex;

use crate::clause::Clause;
use crate::range::{BuildPolicy, Op, PrereleasePolicy, Range};
use crate::version::Version;

fn naive_spec() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        let number = r"\*|0|[1-9][0-9]*";
        let pattern = format!(
            r"^(?P<op><=|>=|==|!=|~=|[<>=^~])?(?P<major>{n})(?:\.(?P<minor>{n})(?:\.(?P<patch>{n}))?)?(?:-(?P<prerel>[a-z0-9A-Z.-]*))?(?:\+(?P<build>[a-z0-9A-Z.-]*))?$",
            n = number
        );
        Regex::new(&pattern).unwrap()
    })
}

fn pyrepr(s: &str) -> String {
    format!("'{}'", s)
}

/// Map a captured numeric component to None (wildcard/absent) or its value.
fn component(text: Option<&str>) -> Option<u64> {
    match text {
        None | Some("*") | Some("x") | Some("X") => None,
        Some(d) => Some(d.parse::<u64>().expect("regex guarantees digits")),
    }
}

fn full_target(
    major: u64,
    minor: u64,
    patch: u64,
    prerel: Option<&str>,
    build: Option<&str>,
) -> Version {
    let split = |s: Option<&str>| -> Vec<String> {
        match s {
            Some(v) if !v.is_empty() => v.split('.').map(|p| p.to_string()).collect(),
            _ => Vec::new(),
        }
    };
    Version {
        major,
        minor,
        patch,
        prerelease: split(prerel),
        build: split(build),
    }
}

fn core(major: u64, minor: u64, patch: u64) -> Version {
    Version {
        major,
        minor,
        patch,
        prerelease: Vec::new(),
        build: Vec::new(),
    }
}

fn r(op: Op, target: Version) -> Clause {
    Clause::Range(Range::new(
        op,
        target,
        PrereleasePolicy::Natural,
        BuildPolicy::Implicit,
    ))
}

fn parse_block(expr: &str) -> Result<Clause, String> {
    let caps = match naive_spec().captures(expr) {
        Some(c) => c,
        None => return Err(format!("Invalid simple spec component: {}", pyrepr(expr))),
    };

    let raw_prefix = caps.name("op").map(|m| m.as_str()).unwrap_or("");
    let prefix = match raw_prefix {
        "" | "=" => "==",
        other => other,
    };

    let major = component(caps.name("major").map(|m| m.as_str()));
    let minor = component(caps.name("minor").map(|m| m.as_str()));
    let patch = component(caps.name("patch").map(|m| m.as_str()));
    let prerel = caps.name("prerel").map(|m| m.as_str());
    let build = caps.name("build").map(|m| m.as_str());
    let build_present = build.is_some();
    let nonempty = |o: Option<&str>| o.map(|s| !s.is_empty()).unwrap_or(false);

    let invalid = || Err(format!("Invalid simple spec: {}", pyrepr(expr)));

    // Build the comparison target, mirroring the upstream None-component handling.
    let target = match (major, minor, patch) {
        (None, _, _) => {
            if prefix != "==" && prefix != ">=" {
                return invalid();
            }
            core(0, 0, 0)
        }
        (Some(maj), None, _) => core(maj, 0, 0),
        (Some(maj), Some(min), None) => core(maj, min, 0),
        (Some(maj), Some(min), Some(pat)) => full_target(maj, min, pat, prerel, build),
    };

    if (major.is_none() || minor.is_none() || patch.is_none())
        && (nonempty(prerel) || nonempty(build))
    {
        return invalid();
    }
    if build_present && prefix != "==" && prefix != "!=" {
        return invalid();
    }

    let clause = match prefix {
        "^" => {
            let high = if target.major != 0 {
                target.next_major()
            } else if target.minor != 0 {
                target.next_minor()
            } else {
                target.next_patch()
            };
            Clause::AllOf(vec![r(Op::Gte, target), r(Op::Lt, high)])
        }
        "~" => {
            let high = if minor.is_none() {
                target.next_major()
            } else {
                target.next_minor()
            };
            Clause::AllOf(vec![r(Op::Gte, target), r(Op::Lt, high)])
        }
        "~=" => {
            let high = if minor.is_none() || patch.is_none() {
                target.next_major()
            } else {
                target.next_minor()
            };
            Clause::AllOf(vec![r(Op::Gte, target), r(Op::Lt, high)])
        }
        "==" => {
            if major.is_none() {
                r(Op::Gte, target)
            } else if minor.is_none() {
                let high = target.next_major();
                Clause::AllOf(vec![r(Op::Gte, target), r(Op::Lt, high)])
            } else if patch.is_none() {
                let high = target.next_minor();
                Clause::AllOf(vec![r(Op::Gte, target), r(Op::Lt, high)])
            } else if build == Some("") {
                Clause::Range(Range::new(
                    Op::Eq,
                    target,
                    PrereleasePolicy::Natural,
                    BuildPolicy::Strict,
                ))
            } else {
                r(Op::Eq, target)
            }
        }
        "!=" => {
            if minor.is_none() {
                let high = target.next_major();
                Clause::AnyOf(vec![r(Op::Lt, target), r(Op::Gte, high)])
            } else if patch.is_none() {
                let high = target.next_minor();
                Clause::AnyOf(vec![r(Op::Lt, target), r(Op::Gte, high)])
            } else if prerel == Some("") {
                Clause::Range(Range::new(
                    Op::Neq,
                    target,
                    PrereleasePolicy::Always,
                    BuildPolicy::Implicit,
                ))
            } else if build == Some("") {
                Clause::Range(Range::new(
                    Op::Neq,
                    target,
                    PrereleasePolicy::Natural,
                    BuildPolicy::Strict,
                ))
            } else {
                r(Op::Neq, target)
            }
        }
        ">" => {
            if minor.is_none() {
                r(Op::Gte, target.next_major())
            } else if patch.is_none() {
                r(Op::Gte, target.next_minor())
            } else {
                r(Op::Gt, target)
            }
        }
        ">=" => r(Op::Gte, target),
        "<" => {
            if prerel == Some("") {
                Clause::Range(Range::new(
                    Op::Lt,
                    target,
                    PrereleasePolicy::Always,
                    BuildPolicy::Implicit,
                ))
            } else {
                r(Op::Lt, target)
            }
        }
        "<=" => {
            if minor.is_none() {
                r(Op::Lt, target.next_major())
            } else if patch.is_none() {
                r(Op::Lt, target.next_minor())
            } else {
                r(Op::Lte, target)
            }
        }
        _ => return invalid(),
    };
    Ok(clause)
}

fn parse(expression: &str) -> Result<Clause, String> {
    let mut clauses: Vec<Clause> = Vec::new();
    for block in expression.split(',') {
        if !naive_spec().is_match(block) {
            return Err(format!("Invalid simple block {}", pyrepr(block)));
        }
        clauses.push(parse_block(block)?);
    }
    Ok(if clauses.len() == 1 {
        clauses.pop().unwrap()
    } else {
        Clause::AllOf(clauses)
    })
}

#[derive(Debug, Clone)]
pub struct SimpleSpec {
    pub expression: String,
    clause: Clause,
}

impl SimpleSpec {
    pub fn new(expression: &str) -> Result<SimpleSpec, String> {
        let clause = parse(expression)?;
        Ok(SimpleSpec {
            expression: expression.to_string(),
            clause,
        })
    }

    pub fn match_version(&self, version: &Version) -> bool {
        self.clause.match_version(version)
    }

    /// Convenience for tests: parse the version string then match.
    pub fn matches(&self, version: &str) -> bool {
        let v = Version::parse(version).expect("valid version string in match");
        self.clause.match_version(&v)
    }

    pub fn filter(&self, versions: &[&str]) -> Vec<String> {
        versions
            .iter()
            .filter_map(|s| {
                let v = Version::parse(s).expect("valid version string in filter");
                if self.clause.match_version(&v) {
                    Some(v.to_string())
                } else {
                    None
                }
            })
            .collect()
    }

    /// Best (max by precedence) matching version, mirroring `select` = max(filter).
    pub fn select(&self, versions: &[&str]) -> Option<String> {
        let mut best: Option<Version> = None;
        for s in versions {
            let v = Version::parse(s).expect("valid version string in select");
            if !self.clause.match_version(&v) {
                continue;
            }
            best = match best {
                Some(b) if b.precedence_cmp(&v) == Ordering::Less => Some(v),
                Some(b) => Some(b),
                None => Some(v),
            };
        }
        best.map(|v| v.to_string())
    }
}
