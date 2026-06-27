use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use unicode_normalization::UnicodeNormalization;

use crate::constant::{
    _FREQUENCIES_RANK, _FREQUENCIES_SET, FREQUENCIES, KO_NAMES, TOO_SMALL_SEQUENCE,
    UNICODE_SECONDARY_RANGE_KEYWORD, ZH_NAMES,
};
use crate::md::{is_suspiciously_successive_range, unicode_range};
use unicode_general_category::{get_general_category, GeneralCategory};

pub type CoherenceMatch = (String, f64);
pub type CoherenceMatches = Vec<CoherenceMatch>;

pub(crate) const LANGUAGE_ORDER: &[&str] = &[
    "English",
    "English—",
    "German",
    "French",
    "Dutch",
    "Italian",
    "Polish",
    "Spanish",
    "Russian",
    "Japanese",
    "Japanese—",
    "Japanese——",
    "Portuguese",
    "Swedish",
    "Chinese",
    "Ukrainian",
    "Norwegian",
    "Finnish",
    "Vietnamese",
    "Czech",
    "Hungarian",
    "Korean",
    "Indonesian",
    "Turkish",
    "Romanian",
    "Farsi",
    "Arabic",
    "Danish",
    "Serbian",
    "Lithuanian",
    "Slovene",
    "Slovak",
    "Hebrew",
    "Bulgarian",
    "Croatian",
    "Hindi",
    "Estonian",
    "Thai",
    "Greek",
    "Tamil",
    "Kazakh",
];

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

fn normalized_encoding_name(name: &str) -> String {
    name.to_ascii_lowercase().replace('-', "_")
}

fn is_latin(character: char) -> bool {
    if character.is_ascii_alphabetic() {
        return true;
    }

    unicode_range(character).is_some_and(|range| range.contains("Latin"))
}

fn is_accentuated(character: char) -> bool {
    if character.is_ascii() || !character.is_alphabetic() {
        return false;
    }
    // Only Latin accented count for accent heuristics (matches Python behavior for non-Latin scripts)
    if !unicode_range(character).map_or(false, |r| r.contains("Latin")) {
        return false;
    }

    character.nfd().skip(1).any(|mark| {
        matches!(
            mark,
            '\u{0300}' // grave
                | '\u{0301}' // acute
                | '\u{0302}' // circumflex
                | '\u{0303}' // tilde
                | '\u{0304}' // macron
                | '\u{0308}' // diaeresis
                | '\u{030a}' // ring above
                | '\u{0327}' // cedilla
        )
    })
}

fn first_char(value: &str) -> Option<char> {
    value.chars().next()
}

fn target_features(language: &str) -> (bool, bool) {
    let Some(characters) = FREQUENCIES.get(language) else {
        return (false, false);
    };

    let mut target_have_accents = false;
    let mut target_pure_latin = true;

    for character in characters {
        let Some(character) = first_char(character) else {
            continue;
        };

        if !target_have_accents && is_accentuated(character) {
            target_have_accents = true;
        }

        if target_pure_latin && !is_latin(character) {
            target_pure_latin = false;
        }

        if target_have_accents && !target_pure_latin {
            break;
        }
    }

    (target_have_accents, target_pure_latin)
}

fn sort_by_ratio_desc<T: AsRef<str>>(items: &mut [(T, f64)]) {
    items.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(Ordering::Equal)
            .then_with(|| {
                let pa = LANGUAGE_ORDER
                    .iter()
                    .position(|&x| x == a.0.as_ref())
                    .unwrap_or(99);
                let pb = LANGUAGE_ORDER
                    .iter()
                    .position(|&x| x == b.0.as_ref())
                    .unwrap_or(99);
                pa.cmp(&pb)
            })
    });
}

fn filter_alt_coherence_matches(results: CoherenceMatches) -> CoherenceMatches {
    let mut order: Vec<String> = Vec::new();
    let mut ratios: HashMap<String, Vec<f64>> = HashMap::new();

    for (language, ratio) in &results {
        let no_em_name = language.replace('—', "");

        if !ratios.contains_key(&no_em_name) {
            order.push(no_em_name.clone());
        }

        ratios.entry(no_em_name).or_default().push(*ratio);
    }

    if ratios
        .values()
        .any(|language_ratios| language_ratios.len() > 1)
    {
        return order
            .into_iter()
            .filter_map(|language| {
                let max_ratio = ratios
                    .get(&language)?
                    .iter()
                    .copied()
                    .fold(f64::NEG_INFINITY, f64::max);
                Some((language, max_ratio))
            })
            .collect();
    }

    results
}

fn is_unicode_range_secondary(range: &str) -> bool {
    UNICODE_SECONDARY_RANGE_KEYWORD
        .iter()
        .any(|keyword| range.contains(keyword))
}

fn unicode_range_languages(primary_range: &str) -> Vec<String> {
    let mut languages = Vec::new();

    for &language in LANGUAGE_ORDER {
        let Some(characters) = FREQUENCIES.get(language) else {
            continue;
        };

        if characters.iter().any(|character| {
            first_char(character)
                .and_then(unicode_range)
                .is_some_and(|range| range == primary_range)
        }) {
            languages.push(language.to_string());
        }
    }

    languages
}

fn is_multi_byte_encoding(name: &str) -> bool {
    let name = normalized_encoding_name(name);

    matches!(
        name.as_str(),
        "utf_8"
            | "utf_8_sig"
            | "utf_16"
            | "utf_16_be"
            | "utf_16_le"
            | "utf_32"
            | "utf_32_be"
            | "utf_32_le"
            | "utf_7"
            | "big5"
            | "big5hkscs"
            | "cp932"
            | "cp949"
            | "cp950"
            | "euc_jis_2004"
            | "euc_jisx0213"
            | "euc_jp"
            | "euc_kr"
            | "gb18030"
            | "gb2312"
            | "gbk"
            | "hz"
            | "iso2022_jp"
            | "iso2022_jp_1"
            | "iso2022_jp_2"
            | "iso2022_jp_2004"
            | "iso2022_jp_3"
            | "iso2022_jp_ext"
            | "iso2022_kr"
            | "johab"
            | "shift_jis"
            | "shift_jis_2004"
            | "shift_jisx0213"
    )
}

fn fallback_encoding_languages(name: &str) -> Option<Vec<String>> {
    let name = normalized_encoding_name(name);
    let primary_range = match name.as_str() {
        "cp855" | "cp866" | "iso8859_5" | "koi8_r" | "koi8_t" | "koi8_u" | "kz1048"
        | "mac_cyrillic" | "ptcp154" => Some("Cyrillic"),
        "cp737" | "cp875" | "cp1253" | "iso8859_7" | "mac_greek" => Some("Greek and Coptic"),
        "cp424" | "cp856" | "cp862" | "cp1255" | "iso8859_8" => Some("Hebrew"),
        "cp720" | "cp864" | "cp1006" | "cp1256" | "iso8859_6" => Some("Arabic"),
        "cp874" | "iso8859_11" | "tis_620" => Some("Thai"),
        _ => None,
    };

    primary_range.map(unicode_range_languages)
}

fn encoding_unicode_range(name: &str) -> Vec<String> {
    if is_multi_byte_encoding(name) {
        return Vec::new();
    }

    let name = normalized_encoding_name(name);

    let mut seen_ranges: HashMap<&'static str, usize> = HashMap::new();
    let mut character_count = 0usize;

    for byte in 0x40u8..0xffu8 {
        let one = [byte];
        let Some(chunk) = crate::decode_strict(&one, &name, false, false, &[]) else {
            continue;
        };

        for character in chunk.chars() {
            let Some(character_range) = unicode_range(character) else {
                continue;
            };

            if !is_unicode_range_secondary(character_range) {
                *seen_ranges.entry(character_range).or_insert(0) += 1;
                character_count += 1;
            }
        }
    }

    if character_count == 0 {
        return Vec::new();
    }

    let mut ranges: Vec<String> = seen_ranges
        .into_iter()
        .filter_map(|(range, count)| {
            if count as f64 / character_count as f64 >= 0.15 {
                Some(range.to_string())
            } else {
                None
            }
        })
        .collect();

    ranges.sort();
    ranges
}

pub fn alpha_unicode_split(decoded_sequence: &str) -> Vec<String> {
    let mut layers: Vec<(&'static str, String)> = Vec::new();
    let mut single_layer_key: Option<&'static str> = None;
    let mut multi_layer = false;
    let mut prev_character_range: Option<&'static str> = None;
    let mut prev_layer_index: Option<usize> = None;

    for character in decoded_sequence.chars() {
        if !character.is_alphabetic()
            || matches!(
                get_general_category(character),
                GeneralCategory::NonspacingMark
                    | GeneralCategory::SpacingMark
                    | GeneralCategory::EnclosingMark
            )
        {
            continue;
        }

        let character_range = if character.is_ascii() {
            Some("Basic Latin")
        } else {
            unicode_range(character)
        };

        let Some(character_range) = character_range else {
            continue;
        };

        if Some(character_range) == prev_character_range {
            if let Some(layer_index) = prev_layer_index {
                if let Some((_, layer)) = layers.get_mut(layer_index) {
                    layer.push(character);
                }
            }
            continue;
        }

        let mut layer_target_range: Option<&'static str> = None;
        let mut layer_target_index: Option<usize> = None;

        if multi_layer {
            for (index, (discovered_range, _)) in layers.iter().enumerate() {
                if !is_suspiciously_successive_range(Some(discovered_range), Some(character_range))
                {
                    layer_target_range = Some(discovered_range);
                    layer_target_index = Some(index);
                    break;
                }
            }
        } else if let Some(single_layer_key) = single_layer_key {
            if !is_suspiciously_successive_range(Some(single_layer_key), Some(character_range)) {
                layer_target_range = Some(single_layer_key);
                layer_target_index = layers
                    .iter()
                    .position(|(discovered_range, _)| *discovered_range == single_layer_key);
            }
        }

        let layer_index = if let Some(layer_index) = layer_target_index {
            layer_index
        } else {
            let layer_target_range = layer_target_range.unwrap_or(character_range);
            if let Some(index) = layers
                .iter()
                .position(|(discovered_range, _)| *discovered_range == layer_target_range)
            {
                index
            } else {
                layers.push((layer_target_range, String::new()));
                if single_layer_key.is_none() {
                    single_layer_key = Some(layer_target_range);
                } else {
                    multi_layer = true;
                }
                layers.len() - 1
            }
        };

        if let Some((_, layer)) = layers.get_mut(layer_index) {
            layer.push(character);
        }

        prev_character_range = Some(character_range);
        prev_layer_index = Some(layer_index);
    }

    layers
        .into_iter()
        .map(|(_, characters)| characters.to_lowercase())
        .collect()
}

pub fn alphabet_languages(characters: &[String], ignore_non_latin: bool) -> Vec<String> {
    let characters_set: HashSet<&str> = characters.iter().map(String::as_str).collect();
    let source_have_accents = characters
        .iter()
        .filter_map(|character| first_char(character))
        .any(is_accentuated);

    let mut languages: Vec<(String, f64)> = Vec::new();

    for &language in LANGUAGE_ORDER {
        let Some(language_characters) = FREQUENCIES.get(language) else {
            continue;
        };

        let (target_have_accents, target_pure_latin) = target_features(language);

        if ignore_non_latin && !target_pure_latin {
            continue;
        }

        if !target_have_accents && source_have_accents {
            continue;
        }

        let Some(frequencies_set) = _FREQUENCIES_SET.get(language) else {
            continue;
        };

        let character_match_count = frequencies_set
            .iter()
            .filter(|character| characters_set.contains(**character))
            .count();

        let character_count = language_characters.len();

        if character_count == 0 {
            continue;
        }

        let ratio = character_match_count as f64 / character_count as f64;

        if ratio >= 0.2 {
            languages.push((language.to_string(), ratio));
        }
    }

    sort_by_ratio_desc(&mut languages);
    languages
        .into_iter()
        .map(|(language, _)| language)
        .collect()
}

pub fn characters_popularity_compare(language: &str, ordered_characters: &[String]) -> f64 {
    let Some(language_characters) = FREQUENCIES.get(language) else {
        return 0.0;
    };
    let Some(frequencies_language_set) = _FREQUENCIES_SET.get(language) else {
        return 0.0;
    };
    let Some(lang_rank) = _FREQUENCIES_RANK.get(language) else {
        return 0.0;
    };

    let ordered_characters_count = ordered_characters.len();

    if ordered_characters_count == 0 {
        return 0.0;
    }

    let target_language_characters_count = language_characters.len();

    if target_language_characters_count == 0 {
        return 0.0;
    }

    let large_alphabet = target_language_characters_count > 26;
    let expected_projection_ratio =
        target_language_characters_count as f64 / ordered_characters_count as f64;

    let ordered_rank: HashMap<&str, usize> = ordered_characters
        .iter()
        .enumerate()
        .map(|(rank, character)| (character.as_str(), rank))
        .collect();

    let mut common_lr: Vec<usize> = Vec::new();
    let mut common_orr: Vec<usize> = Vec::new();

    for (character, language_rank) in lang_rank {
        if let Some(ordered_rank) = ordered_rank.get(character) {
            common_lr.push(*language_rank);
            common_orr.push(*ordered_rank);
        }
    }

    let mut character_approved_count = 0usize;

    for (character_rank, character) in ordered_characters.iter().enumerate() {
        let character = character.as_str();

        if !frequencies_language_set.contains(character) {
            continue;
        }

        let Some(&character_rank_in_language) = lang_rank.get(character) else {
            continue;
        };

        let character_rank_projection =
            (character_rank as f64 * expected_projection_ratio) as usize;
        let rank_distance = character_rank_projection.abs_diff(character_rank_in_language);

        if !large_alphabet && rank_distance > 4 {
            continue;
        }

        if large_alphabet && (rank_distance as f64) < target_language_characters_count as f64 / 3.0
        {
            character_approved_count += 1;
            continue;
        }

        let mut before_match_count = 0usize;
        let mut after_match_count = 0usize;

        for index in 0..common_lr.len() {
            let language_rank = common_lr[index];
            let ordered_rank = common_orr[index];

            if language_rank < character_rank_in_language {
                if ordered_rank < character_rank {
                    before_match_count += 1;
                }
            } else if ordered_rank >= character_rank {
                after_match_count += 1;
            }
        }

        let after_len = target_language_characters_count - character_rank_in_language;

        if character_rank_in_language == 0 && before_match_count <= 4 {
            character_approved_count += 1;
            continue;
        }

        if after_len == 0 && after_match_count <= 4 {
            character_approved_count += 1;
            continue;
        }

        if (character_rank_in_language > 0
            && before_match_count as f64 / character_rank_in_language as f64 >= 0.4)
            || (after_len > 0 && after_match_count as f64 / after_len as f64 >= 0.4)
        {
            character_approved_count += 1;
        }
    }

    character_approved_count as f64 / ordered_characters_count as f64
}

pub fn coherence_ratio(
    decoded_sequence: &str,
    threshold: f64,
    lg_inclusion: Option<&str>,
) -> CoherenceMatches {
    let mut results: CoherenceMatches = Vec::new();
    let mut ignore_non_latin = false;
    let mut sufficient_match_count = 0usize;

    let mut lg_inclusion_list: Vec<String> = lg_inclusion
        .map(|lg_inclusion| {
            lg_inclusion
                .split(',')
                .map(ToString::to_string)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    if let Some(position) = lg_inclusion_list
        .iter()
        .position(|language| language == "Latin Based")
    {
        ignore_non_latin = true;
        lg_inclusion_list.remove(position);
    }

    for layer in alpha_unicode_split(decoded_sequence) {
        let character_count = layer.chars().count();

        if character_count <= TOO_SMALL_SEQUENCE {
            continue;
        }

        let mut sequence_frequencies: HashMap<String, (usize, usize)> = HashMap::new();

        for (index, character) in layer.chars().enumerate() {
            let character = character.to_string();
            let next_order = sequence_frequencies.len();
            let entry = sequence_frequencies
                .entry(character)
                .or_insert((0, next_order));
            entry.0 += 1;
            entry.1 = entry.1.min(index);
        }

        let mut most_common: Vec<(String, usize, usize)> = sequence_frequencies
            .into_iter()
            .map(|(character, (count, first_index))| (character, count, first_index))
            .collect();

        most_common.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| a.2.cmp(&b.2))
                .then_with(|| a.0.cmp(&b.0))
        });

        let popular_character_ordered: Vec<String> = most_common
            .into_iter()
            .map(|(character, _, _)| character)
            .collect();

        let candidate_languages = if lg_inclusion_list.is_empty() {
            alphabet_languages(&popular_character_ordered, ignore_non_latin)
        } else {
            lg_inclusion_list.clone()
        };

        for language in candidate_languages {
            let ratio = characters_popularity_compare(&language, &popular_character_ordered);

            if ratio < threshold {
                continue;
            } else if ratio >= 0.8 {
                sufficient_match_count += 1;
            }

            results.push((language, round4(ratio)));

            if sufficient_match_count >= 3 {
                break;
            }
        }
    }

    let mut results = filter_alt_coherence_matches(results);
    sort_by_ratio_desc(&mut results);
    results
}

pub fn merge_coherence_ratios(results: &[CoherenceMatches]) -> CoherenceMatches {
    let mut order: Vec<String> = Vec::new();
    let mut per_language_ratios: HashMap<String, Vec<f64>> = HashMap::new();

    for result in results {
        for (language, ratio) in result {
            if !per_language_ratios.contains_key(language) {
                order.push(language.clone());
            }
            per_language_ratios
                .entry(language.clone())
                .or_default()
                .push(*ratio);
        }
    }

    let mut merged: CoherenceMatches = order
        .into_iter()
        .filter_map(|language| {
            let ratios = per_language_ratios.get(&language)?;
            if ratios.is_empty() {
                return None;
            }

            Some((
                language,
                round4(ratios.iter().sum::<f64>() / ratios.len() as f64),
            ))
        })
        .collect();

    sort_by_ratio_desc(&mut merged);
    merged
}

pub fn encoding_languages(iana_name: &str) -> Vec<String> {
    let iana_name = normalized_encoding_name(iana_name);

    if is_multi_byte_encoding(&iana_name) {
        return mb_encoding_languages(&iana_name);
    }

    if let Some(languages) = fallback_encoding_languages(&iana_name) {
        if !languages.is_empty() {
            return languages;
        }
    }

    let unicode_ranges = encoding_unicode_range(&iana_name);
    let primary_range = unicode_ranges
        .iter()
        .find(|specified_range| !specified_range.contains("Latin"));

    match primary_range {
        Some(primary_range) => unicode_range_languages(primary_range),
        None => vec!["Latin Based".to_string()],
    }
}

pub fn mb_encoding_languages(iana_name: &str) -> Vec<String> {
    let iana_name = normalized_encoding_name(iana_name);

    if iana_name.starts_with("shift_")
        || iana_name.starts_with("iso2022_jp")
        || iana_name.starts_with("euc_j")
        || iana_name == "cp932"
    {
        return vec!["Japanese".to_string()];
    }

    if iana_name.starts_with("gb") || ZH_NAMES.contains(iana_name.as_str()) {
        return vec!["Chinese".to_string()];
    }

    if iana_name.starts_with("iso2022_kr") || KO_NAMES.contains(iana_name.as_str()) {
        return vec!["Korean".to_string()];
    }

    Vec::new()
}
