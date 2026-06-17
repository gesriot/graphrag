//! Structure-preserving port of `semantic_version.Version` (v1 scope).
//! Mirrors examples/semantic_version/base.py: parse / coerce / Display / precedence
//! comparison / exact equality. Spec/range matching (SimpleSpec, NpmSpec) is out of scope.

use std::cmp::Ordering;
use std::fmt;
use std::sync::OnceLock;

use regex::Regex;

use crate::identifier::Identifier;

fn version_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9a-zA-Z.-]+))?(?:\+([0-9a-zA-Z.-]+))?$").unwrap()
    })
}

fn coerce_base_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\d+(?:\.\d+(?:\.\d+)?)?").unwrap())
}

fn coerce_cleanup_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"[^a-zA-Z0-9+.-]").unwrap())
}

/// Python `repr()` of the simple strings embedded in error messages.
fn pyrepr(s: &str) -> String {
    format!("'{}'", s)
}

/// Python `_has_leading_zero`: non-empty, all digits, starts with '0', not "0".
fn has_leading_zero(value: &str) -> bool {
    !value.is_empty()
        && value.starts_with('0')
        && value != "0"
        && value.bytes().all(|b| b.is_ascii_digit())
}

fn is_numeric(part: &str) -> bool {
    !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Vec<String>,
    pub build: Vec<String>,
}

impl Version {
    pub fn parse(version_string: &str) -> Result<Version, String> {
        if version_string.is_empty() {
            return Err(format!(
                "Invalid empty version string: {}",
                pyrepr(version_string)
            ));
        }

        let caps = match version_re().captures(version_string) {
            Some(c) => c,
            None => {
                return Err(format!(
                    "Invalid version string: {}",
                    pyrepr(version_string)
                ))
            }
        };

        let major_s = caps.get(1).unwrap().as_str();
        let minor_s = caps.get(2).unwrap().as_str();
        let patch_s = caps.get(3).unwrap().as_str();

        if has_leading_zero(major_s) {
            return Err(format!(
                "Invalid leading zero in major: {}",
                pyrepr(version_string)
            ));
        }
        if has_leading_zero(minor_s) {
            return Err(format!(
                "Invalid leading zero in minor: {}",
                pyrepr(version_string)
            ));
        }
        if has_leading_zero(patch_s) {
            return Err(format!(
                "Invalid leading zero in patch: {}",
                pyrepr(version_string)
            ));
        }

        let major = major_s
            .parse::<u64>()
            .map_err(|_| "major out of range".to_string())?;
        let minor = minor_s
            .parse::<u64>()
            .map_err(|_| "minor out of range".to_string())?;
        let patch = patch_s
            .parse::<u64>()
            .map_err(|_| "patch out of range".to_string())?;

        let prerelease = match caps.get(4).map(|m| m.as_str()) {
            None | Some("") => Vec::new(),
            Some(s) => {
                let parts: Vec<String> = s.split('.').map(|p| p.to_string()).collect();
                validate_identifiers(&parts, false)?;
                parts
            }
        };

        let build = match caps.get(5).map(|m| m.as_str()) {
            None | Some("") => Vec::new(),
            Some(s) => {
                let parts: Vec<String> = s.split('.').map(|p| p.to_string()).collect();
                validate_identifiers(&parts, true)?;
                parts
            }
        };

        Ok(Version {
            major,
            minor,
            patch,
            prerelease,
            build,
        })
    }

    /// Coerce an arbitrary version string into a semver-compatible one (non-partial).
    pub fn coerce(version_string: &str) -> Result<Version, String> {
        let m = coerce_base_re().find(version_string).ok_or_else(|| {
            format!(
                "Version string lacks a numerical component: {}",
                pyrepr(version_string)
            )
        })?;
        let end = m.end();

        let mut version = version_string[..end].to_string();
        while version.matches('.').count() < 2 {
            version.push_str(".0");
        }
        // Strip leading zeros in each component ('' -> '0').
        version = version
            .split('.')
            .map(|part| {
                let stripped = part.trim_start_matches('0');
                if stripped.is_empty() {
                    "0"
                } else {
                    stripped
                }
            })
            .collect::<Vec<_>>()
            .join(".");

        if end == version_string.len() {
            return Version::parse(&version);
        }

        let rest_raw = &version_string[end..];
        let rest = coerce_cleanup_re().replace_all(rest_raw, "-").into_owned();
        let first = rest.as_bytes()[0] as char;

        let (prerelease, mut build) = if first == '+' || first == '.' {
            // '+' = explicit build; '.' = an extra version component, also build.
            (String::new(), rest[1..].to_string())
        } else if first == '-' {
            let rest = &rest[1..];
            match rest.split_once('+') {
                Some((pre, b)) => (pre.to_string(), b.to_string()),
                None => (rest.to_string(), String::new()),
            }
        } else {
            match rest.split_once('+') {
                Some((pre, b)) => (pre.to_string(), b.to_string()),
                None => (rest.to_string(), String::new()),
            }
        };
        build = build.replace('+', ".");

        if !prerelease.is_empty() {
            version = format!("{}-{}", version, prerelease);
        }
        if !build.is_empty() {
            version = format!("{}+{}", version, build);
        }
        Version::parse(&version)
    }

    /// Precedence key WITHOUT build metadata (used for `<`/`>`), mirroring
    /// `_build_precedence_key(with_build=False)`.
    fn cmp_key(&self) -> (u64, u64, u64, Vec<Identifier>) {
        let prerelease_key = if self.prerelease.is_empty() {
            vec![Identifier::Max]
        } else {
            self.prerelease
                .iter()
                .map(|p| Identifier::from_part(p))
                .collect()
        };
        (self.major, self.minor, self.patch, prerelease_key)
    }

    /// Mirrors Python `Version.__cmp__`: precedence `<`/`>` (build ignored), then
    /// exact `==` (build-sensitive) for 0; otherwise the two are not orderable
    /// (`NotImplemented` upstream) -> `None`.
    pub fn compare(&self, other: &Version) -> Option<i32> {
        match self.cmp_key().cmp(&other.cmp_key()) {
            Ordering::Less => Some(-1),
            Ordering::Greater => Some(1),
            Ordering::Equal => {
                if self == other {
                    Some(0)
                } else {
                    None
                }
            }
        }
    }
}

fn validate_identifiers(identifiers: &[String], allow_leading_zeroes: bool) -> Result<(), String> {
    for item in identifiers {
        if item.is_empty() {
            return Err(format!(
                "Invalid empty identifier {} in {}",
                pyrepr(item),
                pyrepr(&identifiers.join("."))
            ));
        }
        if !allow_leading_zeroes && is_numeric(item) && item.starts_with('0') && item != "0" {
            return Err(format!(
                "Invalid leading zero in identifier {}",
                pyrepr(item)
            ));
        }
    }
    Ok(())
}

impl fmt::Display for Version {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)?;
        if !self.prerelease.is_empty() {
            write!(f, "-{}", self.prerelease.join("."))?;
        }
        if !self.build.is_empty() {
            write!(f, "+{}", self.build.join("."))?;
        }
        Ok(())
    }
}

/// Module-level `compare(v1, v2)` = `Version(v1).__cmp__(Version(v2))`.
/// Inputs are expected to be valid version strings (as in the reference).
pub fn compare(v1: &str, v2: &str) -> Option<i32> {
    let a = Version::parse(v1).expect("compare() expects a valid version string");
    let b = Version::parse(v2).expect("compare() expects a valid version string");
    a.compare(&b)
}

/// Module-level `validate`: True iff the string parses as a full version.
pub fn validate(version_string: &str) -> bool {
    Version::parse(version_string).is_ok()
}
