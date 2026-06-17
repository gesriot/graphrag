//! NpmSpec port (v2b). Mirrors `NpmSpec` + its `Parser` in
//! examples/semantic_version/base.py: the npm dialect (`||` OR groups, ` - `
//! hyphen ranges, space-AND, x/X wildcards, npm `^`/`~`), and the prerelease
//! clause expansion. Reuses the shared Range/Clause/Version from v1/v2a.

use std::cmp::Ordering;
use std::sync::OnceLock;

use regex::Regex;

use crate::clause::Clause;
use crate::range::{BuildPolicy, Op, PrereleasePolicy, Range};
use crate::version::Version;

fn npm_spec_block() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        let number = r"x|X|\*|0|[1-9][0-9]*";
        let part = r"[a-zA-Z0-9.-]*";
        let pattern = format!(
            r"^(?:v)?(?P<op><=|>=|<|>|=|\^|~)?(?P<major>{n})(?:\.(?P<minor>{n})(?:\.(?P<patch>{n}))?)?(?:-(?P<prerel>{p}))?(?:\+(?P<build>{p}))?$",
            n = number,
            p = part
        );
        Regex::new(&pattern).unwrap()
    })
}

fn pyrepr(s: &str) -> String {
    format!("'{}'", s)
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

fn component(text: Option<&str>) -> Option<u64> {
    match text {
        None | Some("*") | Some("x") | Some("X") => None,
        Some(d) => Some(d.parse::<u64>().expect("regex guarantees digits")),
    }
}

/// npm `cls.range`: every NpmSpec range uses the same-patch prerelease policy.
fn npm_range(op: Op, target: Version) -> Range {
    Range::new(
        op,
        target,
        PrereleasePolicy::SamePatch,
        BuildPolicy::Implicit,
    )
}

fn parse_simple(simple: &str) -> Result<Vec<Range>, String> {
    let caps = match npm_spec_block().captures(simple) {
        Some(c) => c,
        // Reachable only via the hyphen path with an invalid operand (untested by
        // the golden); upstream would raise AttributeError here.
        None => return Err(format!("Invalid NPM block: {}", pyrepr(simple))),
    };

    let raw_prefix = caps.name("op").map(|m| m.as_str()).unwrap_or("");
    let prefix = if raw_prefix.is_empty() {
        "="
    } else {
        raw_prefix
    };

    let major = component(caps.name("major").map(|m| m.as_str()));
    let minor = component(caps.name("minor").map(|m| m.as_str()));
    let patch = component(caps.name("patch").map(|m| m.as_str()));
    let prerel = caps.name("prerel").map(|m| m.as_str());
    let mut build = caps.name("build").map(|m| m.as_str());
    // Ignore the build part unless comparing to a specific version (`=`).
    if build.is_some() && prefix != "=" {
        build = None;
    }
    let nonempty = |o: Option<&str>| o.map(|s| !s.is_empty()).unwrap_or(false);

    let mut eff_prefix = prefix;
    let target = match (major, minor, patch) {
        (None, _, _) => {
            if eff_prefix != "=" && eff_prefix != ">=" {
                return Err(format!("Invalid expression {}", pyrepr(simple)));
            }
            eff_prefix = ">=";
            core(0, 0, 0)
        }
        (Some(maj), None, _) => core(maj, 0, 0),
        (Some(maj), Some(min), None) => core(maj, min, 0),
        (Some(maj), Some(min), Some(pat)) => full_target(maj, min, pat, prerel, build),
    };

    if (major.is_none() || minor.is_none() || patch.is_none())
        && (nonempty(prerel) || nonempty(build))
    {
        return Err(format!("Invalid NPM spec: {}", pyrepr(simple)));
    }

    let ranges = match eff_prefix {
        "^" => {
            let high = if target.major != 0 {
                target.truncate_to_patch().next_major()
            } else if target.minor != 0 {
                target.truncate_to_patch().next_minor()
            } else if minor.is_none() {
                target.truncate_to_patch().next_major()
            } else if patch.is_none() {
                target.truncate_to_patch().next_minor()
            } else {
                target.truncate_to_patch().next_patch()
            };
            vec![npm_range(Op::Gte, target), npm_range(Op::Lt, high)]
        }
        "~" => {
            let high = if minor.is_none() {
                target.next_major()
            } else {
                target.next_minor()
            };
            vec![npm_range(Op::Gte, target), npm_range(Op::Lt, high)]
        }
        "=" => {
            if major.is_none() {
                vec![npm_range(Op::Gte, target)]
            } else if minor.is_none() {
                let high = target.next_major();
                vec![npm_range(Op::Gte, target), npm_range(Op::Lt, high)]
            } else if patch.is_none() {
                let high = target.next_minor();
                vec![npm_range(Op::Gte, target), npm_range(Op::Lt, high)]
            } else {
                vec![npm_range(Op::Eq, target)]
            }
        }
        ">" => {
            if minor.is_none() {
                vec![npm_range(Op::Gte, target.next_major())]
            } else if patch.is_none() {
                vec![npm_range(Op::Gte, target.next_minor())]
            } else {
                vec![npm_range(Op::Gt, target)]
            }
        }
        ">=" => vec![npm_range(Op::Gte, target)],
        "<" => vec![npm_range(Op::Lt, target)],
        "<=" => {
            if minor.is_none() {
                vec![npm_range(Op::Lt, target.next_major())]
            } else if patch.is_none() {
                vec![npm_range(Op::Lt, target.next_minor())]
            } else {
                vec![npm_range(Op::Lte, target)]
            }
        }
        _ => return Err(format!("Invalid expression {}", pyrepr(simple))),
    };
    Ok(ranges)
}

fn all_of(ranges: Vec<Range>) -> Clause {
    Clause::AllOf(ranges.into_iter().map(Clause::Range).collect())
}

fn parse(expression: &str) -> Result<Clause, String> {
    // result starts as Never(); `Never | X == X`, so we collect the OR-groups.
    let mut groups_out: Vec<Clause> = Vec::new();

    for raw_group in expression.split("||") {
        let trimmed = raw_group.trim();
        let group = if trimmed.is_empty() {
            ">=0.0.0"
        } else {
            trimmed
        };

        let mut subclauses: Vec<Range> = Vec::new();
        if let Some((low, high)) = group.split_once(" - ") {
            subclauses.extend(parse_simple(&format!(">={}", low))?);
            subclauses.extend(parse_simple(&format!("<={}", high))?);
        } else {
            for block in group.split(' ') {
                if !npm_spec_block().is_match(block) {
                    return Err(format!(
                        "Invalid NPM block in {}: {}",
                        pyrepr(expression),
                        pyrepr(block)
                    ));
                }
                subclauses.extend(parse_simple(block)?);
            }
        }

        let mut prerelease_clauses: Vec<Range> = Vec::new();
        let mut non_prerel_clauses: Vec<Range> = Vec::new();
        for clause in &subclauses {
            if !clause.target.prerelease.is_empty() {
                match clause.operator {
                    Op::Gt | Op::Gte => prerelease_clauses.push(Range::new(
                        Op::Lt,
                        core(
                            clause.target.major,
                            clause.target.minor,
                            clause.target.patch + 1,
                        ),
                        PrereleasePolicy::Always,
                        BuildPolicy::Implicit,
                    )),
                    Op::Lt | Op::Lte => prerelease_clauses.push(Range::new(
                        Op::Gte,
                        core(clause.target.major, clause.target.minor, 0),
                        PrereleasePolicy::Always,
                        BuildPolicy::Implicit,
                    )),
                    _ => {}
                }
                prerelease_clauses.push(clause.clone());
                non_prerel_clauses.push(npm_range(
                    clause.operator,
                    clause.target.truncate_to_patch(),
                ));
            } else {
                non_prerel_clauses.push(clause.clone());
            }
        }

        if !prerelease_clauses.is_empty() {
            groups_out.push(all_of(prerelease_clauses));
        }
        groups_out.push(all_of(non_prerel_clauses));
    }

    Ok(if groups_out.len() == 1 {
        groups_out.pop().unwrap()
    } else {
        Clause::AnyOf(groups_out)
    })
}

#[derive(Debug, Clone)]
pub struct NpmSpec {
    pub expression: String,
    clause: Clause,
}

impl NpmSpec {
    pub fn new(expression: &str) -> Result<NpmSpec, String> {
        let clause = parse(expression)?;
        Ok(NpmSpec {
            expression: expression.to_string(),
            clause,
        })
    }

    pub fn match_version(&self, version: &Version) -> bool {
        self.clause.match_version(version)
    }

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
