//! Typed, callable counterparts to the vendored Python `utils.py` helpers.
//!
//! These helpers remain module-local API (`charset_normalizer_rust::utils`),
//! just as their Python counterparts are importable but not package-root
//! re-exports. They deliberately use concrete Rust types instead of Python's
//! dynamic bytes/bytearray and generator protocols.

use std::ops::Range;

use unicode_general_category::{get_general_category, GeneralCategory};
use unicode_normalization::UnicodeNormalization;

/// Python's `utils.iana_name(..., strict=True)` failure mode in a typed form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IanaNameError {
    pub normalized_name: String,
}

impl std::fmt::Display for IanaNameError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "unable to retrieve IANA for '{}'",
            self.normalized_name
        )
    }
}

impl std::error::Error for IanaNameError {}

/// Python's `remove_accent` can raise when a Unicode decomposition starts with
/// a compatibility tag rather than a hexadecimal scalar value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoveAccentError {
    pub character: char,
}

impl std::fmt::Display for RemoveAccentError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "cannot remove accent from U+{:04X}",
            self.character as u32
        )
    }
}

impl std::error::Error for RemoveAccentError {}

pub fn unicode_range(character: char) -> Option<&'static str> {
    crate::md::unicode_range(character)
}

pub fn remove_accent(character: char) -> Result<char, RemoveAccentError> {
    let decomposition: String = character.nfd().collect();
    let first = decomposition.chars().next().unwrap_or(character);
    if matches!(character, '\u{fefb}'..='\u{fefc}') {
        return Err(RemoveAccentError { character });
    }
    Ok(first)
}

pub fn is_accentuated(character: char) -> bool {
    if character.is_ascii() || !character.is_alphabetic() {
        return false;
    }
    if !unicode_range(character).is_some_and(|range| range.contains("Latin")) {
        return false;
    }
    character.nfd().skip(1).any(|mark| {
        matches!(
            mark,
            '\u{0300}'
                | '\u{0301}'
                | '\u{0302}'
                | '\u{0303}'
                | '\u{0304}'
                | '\u{0308}'
                | '\u{030a}'
                | '\u{0327}'
        )
    })
}

pub fn is_latin(character: char) -> bool {
    character.is_ascii_alphabetic()
        || (character.is_alphabetic()
            && unicode_range(character).is_some_and(|range| range.contains("Latin")))
}

pub fn is_punctuation(character: char) -> bool {
    matches!(
        get_general_category(character),
        GeneralCategory::ConnectorPunctuation
            | GeneralCategory::DashPunctuation
            | GeneralCategory::OpenPunctuation
            | GeneralCategory::ClosePunctuation
            | GeneralCategory::InitialPunctuation
            | GeneralCategory::FinalPunctuation
            | GeneralCategory::OtherPunctuation
    ) || unicode_range(character).is_some_and(|range| range.contains("Punctuation"))
}

pub fn is_symbol(character: char) -> bool {
    match get_general_category(character) {
        GeneralCategory::MathSymbol
        | GeneralCategory::CurrencySymbol
        | GeneralCategory::ModifierSymbol
        | GeneralCategory::OtherSymbol
        | GeneralCategory::DecimalNumber
        | GeneralCategory::LetterNumber
        | GeneralCategory::OtherNumber => true,
        category => unicode_range(character).is_some_and(|range| {
            range.contains("Forms") && category != GeneralCategory::OtherLetter
        }),
    }
}

pub fn is_emoticon(character: char) -> bool {
    unicode_range(character)
        .is_some_and(|range| range.contains("Emoticons") || range.contains("Pictographs"))
}

pub fn is_separator(character: char) -> bool {
    if character.is_whitespace() || matches!(character, '｜' | '+' | '<' | '>') {
        return true;
    }
    matches!(
        get_general_category(character),
        GeneralCategory::SpaceSeparator
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
            | GeneralCategory::OtherPunctuation
            | GeneralCategory::DashPunctuation
            | GeneralCategory::ConnectorPunctuation
    )
}

pub fn is_case_variable(character: char) -> bool {
    character.is_lowercase() != character.is_uppercase()
}

pub fn is_cjk(character: char) -> bool {
    unicode_range(character).is_some_and(|range| range.contains("CJK"))
}

pub fn is_hiragana(character: char) -> bool {
    unicode_range(character).is_some_and(|range| range.contains("Hiragana"))
}

pub fn is_katakana(character: char) -> bool {
    unicode_range(character).is_some_and(|range| range.contains("Katakana"))
}

pub fn is_hangul(character: char) -> bool {
    unicode_range(character).is_some_and(|range| range.contains("Hangul"))
}

pub fn is_thai(character: char) -> bool {
    unicode_range(character).is_some_and(|range| range.contains("Thai"))
}

pub fn is_arabic(character: char) -> bool {
    character != '\u{feff}'
        && unicode_range(character).is_some_and(|range| range.contains("Arabic"))
}

pub fn is_arabic_isolated_form(character: char) -> bool {
    character != '\u{feff}'
        && unicode_range(character).is_some_and(|range| {
            range.contains("Arabic Presentation Forms-A")
                || range.contains("Arabic Presentation Forms-B")
        })
}

pub fn is_cjk_uncommon(character: char) -> bool {
    !crate::constant::COMMON_CJK_CHARACTERS.contains(character.to_string().as_str())
}

pub fn is_unicode_range_secondary(range_name: &str) -> bool {
    const KEYWORDS: [&str; 15] = [
        "Supplement",
        "Extended",
        "Extensions",
        "Modifier",
        "Marks",
        "Punctuation",
        "Symbols",
        "Forms",
        "Operators",
        "Miscellaneous",
        "Drawing",
        "Block",
        "Shapes",
        "Supplemental",
        "Tags",
    ];
    KEYWORDS.iter().any(|keyword| range_name.contains(keyword))
}

pub fn is_unprintable(character: char) -> bool {
    !character.is_whitespace()
        && matches!(
            get_general_category(character),
            GeneralCategory::Control
                | GeneralCategory::Format
                | GeneralCategory::Surrogate
                | GeneralCategory::PrivateUse
                | GeneralCategory::Unassigned
                | GeneralCategory::LineSeparator
                | GeneralCategory::ParagraphSeparator
                | GeneralCategory::SpaceSeparator
        )
        && !matches!(character, '\u{001a}' | '\u{feff}')
}

pub fn any_specified_encoding(sequence: &[u8], search_zone: usize) -> Option<String> {
    let limit = sequence.len().min(search_zone);
    let ascii_header: String = sequence[..limit]
        .iter()
        .filter_map(|byte| byte.is_ascii().then_some(*byte as char))
        .collect();

    let mut offset = 0usize;
    while offset < ascii_header.len() {
        let (start, end) = crate::models::encoding_declaration_value_span(&ascii_header[offset..])?;
        let value = &ascii_header[offset + start..offset + end];
        if let Some(canonical) = crate::models::canonical_encoding_name(value, true) {
            return Some(canonical);
        }
        offset += end.max(1);
    }
    None
}

pub fn is_multi_byte_encoding(name: &str) -> bool {
    let normalized =
        iana_name(name, false).unwrap_or_else(|_| name.to_ascii_lowercase().replace('-', "_"));
    crate::is_multi_byte_encoding_name(&normalized)
}

pub fn identify_sig_or_bom(sequence: &[u8]) -> (Option<String>, Vec<u8>) {
    crate::identify_sig_or_bom(sequence)
}

pub fn should_strip_sig_or_bom(iana_encoding: &str) -> bool {
    crate::should_strip_sig_or_bom(iana_encoding)
}

pub fn iana_name(cp_name: &str, strict: bool) -> Result<String, IanaNameError> {
    let normalized_name = cp_name.to_ascii_lowercase().replace('-', "_");
    match crate::models::canonical_encoding_name(&normalized_name, strict) {
        Some(name) => Ok(name),
        None => Err(IanaNameError { normalized_name }),
    }
}

pub fn cp_similarity(iana_name_a: &str, iana_name_b: &str) -> f64 {
    if is_multi_byte_encoding(iana_name_a) || is_multi_byte_encoding(iana_name_b) {
        return 0.0;
    }

    let left = iana_name(iana_name_a, false).unwrap_or_else(|_| iana_name_a.to_string());
    let right = iana_name(iana_name_b, false).unwrap_or_else(|_| iana_name_b.to_string());
    let matching = (0u8..=255)
        .filter(|byte| {
            let payload = [*byte];
            crate::decode_strict(&payload, &left, false, false, &[]).unwrap_or_default()
                == crate::decode_strict(&payload, &right, false, false, &[]).unwrap_or_default()
        })
        .count();
    matching as f64 / 256.0
}

pub fn is_cp_similar(iana_name_a: &str, iana_name_b: &str) -> bool {
    let left = iana_name(iana_name_a, false).unwrap_or_else(|_| iana_name_a.to_string());
    let right = iana_name(iana_name_b, false).unwrap_or_else(|_| iana_name_b.to_string());
    crate::constant::IANA_SUPPORTED_SIMILAR
        .get(left.as_str())
        .is_some_and(|similar| similar.contains(&right.as_str()))
}

/// Materialized counterpart to Python's lazy `cut_sequence_chunks` generator.
pub fn cut_sequence_chunks(
    sequences: &[u8],
    encoding_iana: &str,
    offsets: Range<usize>,
    chunk_size: usize,
    bom_or_sig_available: bool,
    strip_sig_or_bom: bool,
    sig_payload: &[u8],
    is_multi_byte_decoder: bool,
    decoded_payload: Option<&str>,
) -> Vec<String> {
    let mut chunks = Vec::new();

    if let Some(decoded_payload) = decoded_payload.filter(|_| !is_multi_byte_decoder) {
        for offset in offsets {
            let chunk: String = decoded_payload
                .chars()
                .skip(offset)
                .take(chunk_size)
                .collect();
            if chunk.is_empty() {
                break;
            }
            chunks.push(chunk);
        }
        return chunks;
    }

    for offset in offsets {
        let chunk_end = offset.saturating_add(chunk_size);
        if chunk_end > sequences.len().saturating_add(8) {
            continue;
        }
        let end = chunk_end.min(sequences.len());
        let mut source = Vec::new();
        if bom_or_sig_available && !strip_sig_or_bom {
            source.extend_from_slice(sig_payload);
        }
        if offset < end {
            source.extend_from_slice(&sequences[offset..end]);
        }
        chunks.push(
            crate::decode_strict(&source, encoding_iana, false, false, &[]).unwrap_or_default(),
        );
    }

    chunks
}
