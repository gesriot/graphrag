//! Prerelease identifier ordering. Mirrors NumericIdentifier / AlphaIdentifier /
//! MaxIdentifier in the Python reference.
//!
//! SemVer precedence rule: a numeric identifier is always lower than an
//! alphanumeric one, and an *absent* prerelease (a release) outranks any
//! prerelease. We model that as `Numeric < Alpha < Max`, which is exactly the
//! variant declaration order below (derived `Ord` compares the variant first,
//! then the contained value).

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum Identifier {
    Numeric(u64),
    Alpha(String),
    Max,
}

impl Identifier {
    /// Build the identifier for one dotted prerelease part: numeric if it is all
    /// digits (matching Python `str.isdigit()`), otherwise alphanumeric.
    pub fn from_part(part: &str) -> Identifier {
        if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) {
            // Prerelease numeric parts are validated to have no leading zeros,
            // so a plain parse matches Python's int().
            Identifier::Numeric(part.parse::<u64>().expect("validated numeric identifier"))
        } else {
            Identifier::Alpha(part.to_string())
        }
    }
}
