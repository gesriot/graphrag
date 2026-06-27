use encoding_rs::Encoding;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

pub(crate) const PYTHON_ENCODING_ALIASES: &[(&str, &str)] = &[
    ("646", "ascii"),
    ("ansi_x3.4_1968", "ascii"),
    ("ansi_x3_4_1968", "ascii"),
    ("ansi_x3.4_1986", "ascii"),
    ("cp367", "ascii"),
    ("csascii", "ascii"),
    ("ibm367", "ascii"),
    ("iso646_us", "ascii"),
    ("iso_646.irv_1991", "ascii"),
    ("iso_ir_6", "ascii"),
    ("us", "ascii"),
    ("us_ascii", "ascii"),
    ("base64", "base64_codec"),
    ("base_64", "base64_codec"),
    ("big5_tw", "big5"),
    ("csbig5", "big5"),
    ("big5_hkscs", "big5hkscs"),
    ("hkscs", "big5hkscs"),
    ("bz2", "bz2_codec"),
    ("037", "cp037"),
    ("csibm037", "cp037"),
    ("ebcdic_cp_ca", "cp037"),
    ("ebcdic_cp_nl", "cp037"),
    ("ebcdic_cp_us", "cp037"),
    ("ebcdic_cp_wt", "cp037"),
    ("ibm037", "cp037"),
    ("ibm039", "cp037"),
    ("1026", "cp1026"),
    ("csibm1026", "cp1026"),
    ("ibm1026", "cp1026"),
    ("1125", "cp1125"),
    ("ibm1125", "cp1125"),
    ("cp866u", "cp1125"),
    ("ruscii", "cp1125"),
    ("1140", "cp1140"),
    ("ibm1140", "cp1140"),
    ("1250", "cp1250"),
    ("windows_1250", "cp1250"),
    ("1251", "cp1251"),
    ("windows_1251", "cp1251"),
    ("1252", "cp1252"),
    ("windows_1252", "cp1252"),
    ("1253", "cp1253"),
    ("windows_1253", "cp1253"),
    ("1254", "cp1254"),
    ("windows_1254", "cp1254"),
    ("1255", "cp1255"),
    ("windows_1255", "cp1255"),
    ("1256", "cp1256"),
    ("windows_1256", "cp1256"),
    ("1257", "cp1257"),
    ("windows_1257", "cp1257"),
    ("1258", "cp1258"),
    ("windows_1258", "cp1258"),
    ("273", "cp273"),
    ("ibm273", "cp273"),
    ("csibm273", "cp273"),
    ("424", "cp424"),
    ("csibm424", "cp424"),
    ("ebcdic_cp_he", "cp424"),
    ("ibm424", "cp424"),
    ("437", "cp437"),
    ("cspc8codepage437", "cp437"),
    ("ibm437", "cp437"),
    ("500", "cp500"),
    ("csibm500", "cp500"),
    ("ebcdic_cp_be", "cp500"),
    ("ebcdic_cp_ch", "cp500"),
    ("ibm500", "cp500"),
    ("775", "cp775"),
    ("cspc775baltic", "cp775"),
    ("ibm775", "cp775"),
    ("850", "cp850"),
    ("cspc850multilingual", "cp850"),
    ("ibm850", "cp850"),
    ("852", "cp852"),
    ("cspcp852", "cp852"),
    ("ibm852", "cp852"),
    ("855", "cp855"),
    ("csibm855", "cp855"),
    ("ibm855", "cp855"),
    ("857", "cp857"),
    ("csibm857", "cp857"),
    ("ibm857", "cp857"),
    ("858", "cp858"),
    ("csibm858", "cp858"),
    ("ibm858", "cp858"),
    ("860", "cp860"),
    ("csibm860", "cp860"),
    ("ibm860", "cp860"),
    ("861", "cp861"),
    ("cp_is", "cp861"),
    ("csibm861", "cp861"),
    ("ibm861", "cp861"),
    ("862", "cp862"),
    ("cspc862latinhebrew", "cp862"),
    ("ibm862", "cp862"),
    ("863", "cp863"),
    ("csibm863", "cp863"),
    ("ibm863", "cp863"),
    ("864", "cp864"),
    ("csibm864", "cp864"),
    ("ibm864", "cp864"),
    ("865", "cp865"),
    ("csibm865", "cp865"),
    ("ibm865", "cp865"),
    ("866", "cp866"),
    ("csibm866", "cp866"),
    ("ibm866", "cp866"),
    ("869", "cp869"),
    ("cp_gr", "cp869"),
    ("csibm869", "cp869"),
    ("ibm869", "cp869"),
    ("874", "cp874"),
    ("ms874", "cp874"),
    ("windows_874", "cp874"),
    ("932", "cp932"),
    ("ms932", "cp932"),
    ("mskanji", "cp932"),
    ("ms_kanji", "cp932"),
    ("windows_31j", "cp932"),
    ("949", "cp949"),
    ("ms949", "cp949"),
    ("uhc", "cp949"),
    ("950", "cp950"),
    ("ms950", "cp950"),
    ("jisx0213", "euc_jis_2004"),
    ("eucjis2004", "euc_jis_2004"),
    ("euc_jis2004", "euc_jis_2004"),
    ("eucjisx0213", "euc_jisx0213"),
    ("eucjp", "euc_jp"),
    ("ujis", "euc_jp"),
    ("u_jis", "euc_jp"),
    ("euckr", "euc_kr"),
    ("korean", "euc_kr"),
    ("ksc5601", "euc_kr"),
    ("ks_c_5601", "euc_kr"),
    ("ks_c_5601_1987", "euc_kr"),
    ("ksx1001", "euc_kr"),
    ("ks_x_1001", "euc_kr"),
    ("cseuckr", "euc_kr"),
    ("gb18030_2000", "gb18030"),
    ("chinese", "gb2312"),
    ("csiso58gb231280", "gb2312"),
    ("euc_cn", "gb2312"),
    ("euccn", "gb2312"),
    ("eucgb2312_cn", "gb2312"),
    ("gb2312_1980", "gb2312"),
    ("gb2312_80", "gb2312"),
    ("iso_ir_58", "gb2312"),
    ("936", "gbk"),
    ("cp936", "gbk"),
    ("ms936", "gbk"),
    ("hex", "hex_codec"),
    ("roman8", "hp_roman8"),
    ("r8", "hp_roman8"),
    ("csHPRoman8", "hp_roman8"),
    ("cp1051", "hp_roman8"),
    ("ibm1051", "hp_roman8"),
    ("hzgb", "hz"),
    ("hz_gb", "hz"),
    ("hz_gb_2312", "hz"),
    ("csiso2022jp", "iso2022_jp"),
    ("iso2022jp", "iso2022_jp"),
    ("iso_2022_jp", "iso2022_jp"),
    ("iso2022jp_1", "iso2022_jp_1"),
    ("iso_2022_jp_1", "iso2022_jp_1"),
    ("iso2022jp_2", "iso2022_jp_2"),
    ("iso_2022_jp_2", "iso2022_jp_2"),
    ("iso_2022_jp_2004", "iso2022_jp_2004"),
    ("iso2022jp_2004", "iso2022_jp_2004"),
    ("iso2022jp_3", "iso2022_jp_3"),
    ("iso_2022_jp_3", "iso2022_jp_3"),
    ("iso2022jp_ext", "iso2022_jp_ext"),
    ("iso_2022_jp_ext", "iso2022_jp_ext"),
    ("csiso2022kr", "iso2022_kr"),
    ("iso2022kr", "iso2022_kr"),
    ("iso_2022_kr", "iso2022_kr"),
    ("csisolatin6", "iso8859_10"),
    ("iso_8859_10", "iso8859_10"),
    ("iso_8859_10_1992", "iso8859_10"),
    ("iso_ir_157", "iso8859_10"),
    ("l6", "iso8859_10"),
    ("latin6", "iso8859_10"),
    ("thai", "iso8859_11"),
    ("iso_8859_11", "iso8859_11"),
    ("iso_8859_11_2001", "iso8859_11"),
    ("iso_8859_13", "iso8859_13"),
    ("l7", "iso8859_13"),
    ("latin7", "iso8859_13"),
    ("iso_8859_14", "iso8859_14"),
    ("iso_8859_14_1998", "iso8859_14"),
    ("iso_celtic", "iso8859_14"),
    ("iso_ir_199", "iso8859_14"),
    ("l8", "iso8859_14"),
    ("latin8", "iso8859_14"),
    ("iso_8859_15", "iso8859_15"),
    ("l9", "iso8859_15"),
    ("latin9", "iso8859_15"),
    ("iso_8859_16", "iso8859_16"),
    ("iso_8859_16_2001", "iso8859_16"),
    ("iso_ir_226", "iso8859_16"),
    ("l10", "iso8859_16"),
    ("latin10", "iso8859_16"),
    ("csisolatin2", "iso8859_2"),
    ("iso_8859_2", "iso8859_2"),
    ("iso_8859_2_1987", "iso8859_2"),
    ("iso_ir_101", "iso8859_2"),
    ("l2", "iso8859_2"),
    ("latin2", "iso8859_2"),
    ("csisolatin3", "iso8859_3"),
    ("iso_8859_3", "iso8859_3"),
    ("iso_8859_3_1988", "iso8859_3"),
    ("iso_ir_109", "iso8859_3"),
    ("l3", "iso8859_3"),
    ("latin3", "iso8859_3"),
    ("csisolatin4", "iso8859_4"),
    ("iso_8859_4", "iso8859_4"),
    ("iso_8859_4_1988", "iso8859_4"),
    ("iso_ir_110", "iso8859_4"),
    ("l4", "iso8859_4"),
    ("latin4", "iso8859_4"),
    ("csisolatincyrillic", "iso8859_5"),
    ("cyrillic", "iso8859_5"),
    ("iso_8859_5", "iso8859_5"),
    ("iso_8859_5_1988", "iso8859_5"),
    ("iso_ir_144", "iso8859_5"),
    ("arabic", "iso8859_6"),
    ("asmo_708", "iso8859_6"),
    ("csisolatinarabic", "iso8859_6"),
    ("ecma_114", "iso8859_6"),
    ("iso_8859_6", "iso8859_6"),
    ("iso_8859_6_1987", "iso8859_6"),
    ("iso_ir_127", "iso8859_6"),
    ("csisolatingreek", "iso8859_7"),
    ("ecma_118", "iso8859_7"),
    ("elot_928", "iso8859_7"),
    ("greek", "iso8859_7"),
    ("greek8", "iso8859_7"),
    ("iso_8859_7", "iso8859_7"),
    ("iso_8859_7_1987", "iso8859_7"),
    ("iso_ir_126", "iso8859_7"),
    ("csisolatinhebrew", "iso8859_8"),
    ("hebrew", "iso8859_8"),
    ("iso_8859_8", "iso8859_8"),
    ("iso_8859_8_1988", "iso8859_8"),
    ("iso_ir_138", "iso8859_8"),
    ("iso_8859_8_i", "iso8859_8"),
    ("iso_8859_8_e", "iso8859_8"),
    ("csisolatin5", "iso8859_9"),
    ("iso_8859_9", "iso8859_9"),
    ("iso_8859_9_1989", "iso8859_9"),
    ("iso_ir_148", "iso8859_9"),
    ("l5", "iso8859_9"),
    ("latin5", "iso8859_9"),
    ("cp1361", "johab"),
    ("ms1361", "johab"),
    ("cskoi8r", "koi8_r"),
    ("kz_1048", "kz1048"),
    ("rk1048", "kz1048"),
    ("strk1048_2002", "kz1048"),
    ("8859", "latin_1"),
    ("cp819", "latin_1"),
    ("csisolatin1", "latin_1"),
    ("ibm819", "latin_1"),
    ("iso8859", "latin_1"),
    ("iso8859_1", "latin_1"),
    ("iso_8859_1", "latin_1"),
    ("iso_8859_1_1987", "latin_1"),
    ("iso_ir_100", "latin_1"),
    ("l1", "latin_1"),
    ("latin", "latin_1"),
    ("latin1", "latin_1"),
    ("maccyrillic", "mac_cyrillic"),
    ("macgreek", "mac_greek"),
    ("maciceland", "mac_iceland"),
    ("maccentraleurope", "mac_latin2"),
    ("mac_centeuro", "mac_latin2"),
    ("maclatin2", "mac_latin2"),
    ("macintosh", "mac_roman"),
    ("macroman", "mac_roman"),
    ("macturkish", "mac_turkish"),
    ("ansi", "mbcs"),
    ("dbcs", "mbcs"),
    ("csptcp154", "ptcp154"),
    ("pt154", "ptcp154"),
    ("cp154", "ptcp154"),
    ("cyrillic_asian", "ptcp154"),
    ("quopri", "quopri_codec"),
    ("quoted_printable", "quopri_codec"),
    ("quotedprintable", "quopri_codec"),
    ("rot13", "rot_13"),
    ("csshiftjis", "shift_jis"),
    ("shiftjis", "shift_jis"),
    ("sjis", "shift_jis"),
    ("s_jis", "shift_jis"),
    ("shiftjis2004", "shift_jis_2004"),
    ("sjis_2004", "shift_jis_2004"),
    ("s_jis_2004", "shift_jis_2004"),
    ("shiftjisx0213", "shift_jisx0213"),
    ("sjisx0213", "shift_jisx0213"),
    ("s_jisx0213", "shift_jisx0213"),
    ("tis620", "tis_620"),
    ("tis_620_0", "tis_620"),
    ("tis_620_2529_0", "tis_620"),
    ("tis_620_2529_1", "tis_620"),
    ("iso_ir_166", "tis_620"),
    ("u16", "utf_16"),
    ("utf16", "utf_16"),
    ("unicodebigunmarked", "utf_16_be"),
    ("utf_16be", "utf_16_be"),
    ("unicodelittleunmarked", "utf_16_le"),
    ("utf_16le", "utf_16_le"),
    ("u32", "utf_32"),
    ("utf32", "utf_32"),
    ("utf_32be", "utf_32_be"),
    ("utf_32le", "utf_32_le"),
    ("u7", "utf_7"),
    ("utf7", "utf_7"),
    ("unicode_1_1_utf_7", "utf_7"),
    ("u8", "utf_8"),
    ("utf", "utf_8"),
    ("utf8", "utf_8"),
    ("utf8_ucs2", "utf_8"),
    ("utf8_ucs4", "utf_8"),
    ("cp65001", "utf_8"),
    ("uu", "uu_codec"),
    ("zip", "zlib_codec"),
    ("zlib", "zlib_codec"),
    ("x_mac_japanese", "shift_jis"),
    ("x_mac_korean", "euc_kr"),
    ("x_mac_simp_chinese", "gb2312"),
    ("x_mac_trad_chinese", "big5"),
];

#[derive(Debug, Clone, PartialEq)]
pub struct CharsetMatch {
    pub encoding: String,
    pub language: Option<String>,
    pub language_ratios: Vec<(String, f64)>,
    pub chaos: f64,
    pub coherence: f64,
    pub bom: bool,
    pub raw: Vec<u8>,
    pub preemptive_declaration: Option<String>,
    pub submatches: Vec<CharsetMatch>,
}

pub(crate) fn canonical_encoding_name(cp_name: &str, strict: bool) -> Option<String> {
    let cp_name = cp_name.to_ascii_lowercase().replace('-', "_");

    for (alias, canonical) in PYTHON_ENCODING_ALIASES {
        if cp_name == *alias || cp_name == *canonical {
            return Some((*canonical).to_string());
        }
    }

    if strict {
        None
    } else {
        Some(cp_name)
    }
}

pub(crate) fn encoding_declaration_value_span(text: &str) -> Option<(usize, usize)> {
    const KEYWORDS: [&[u8]; 3] = [b"encoding", b"charset", b"coding"];

    let bytes = text.as_bytes();
    let mut index = 0usize;

    while index < bytes.len() {
        for keyword in KEYWORDS {
            if !ascii_starts_with_ignore_case(&bytes[index..], keyword) {
                continue;
            }

            let mut pos = index + keyword.len();
            let mut separators = 0usize;

            while pos < bytes.len() && separators < 10 && matches!(bytes[pos], b':' | b'=' | b' ') {
                pos += 1;
                separators += 1;
            }

            if separators == 0 {
                continue;
            }

            if pos < bytes.len() && matches!(bytes[pos], b'"' | b'\'') {
                pos += 1;
            }

            let value_start = pos;

            while pos < bytes.len()
                && (bytes[pos].is_ascii_alphanumeric() || bytes[pos] == b'-' || bytes[pos] == b'_')
            {
                pos += 1;
            }

            if pos > value_start {
                return Some((value_start, pos));
            }
        }

        index += 1;
    }

    None
}

fn ascii_starts_with_ignore_case(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.len() >= needle.len()
        && haystack
            .iter()
            .zip(needle.iter())
            .take(needle.len())
            .all(|(a, b)| a.eq_ignore_ascii_case(b))
}

impl CharsetMatch {
    pub fn decoded(&self) -> Option<String> {
        let (sig_e, sig_p) = crate::identify_sig_or_bom(&self.raw);
        let bom_or = sig_e.as_deref() == Some(self.encoding.as_str());
        let strip = bom_or && crate::should_strip_sig_or_bom(&self.encoding);
        crate::decode_strict(&self.raw, &self.encoding, bom_or, strip, &sig_p)
    }

    pub fn alphabets(&self) -> Vec<String> {
        let decoded = match self.decoded() {
            Some(s) => s,
            None => return vec![],
        };
        let mut ranges: Vec<String> = decoded
            .chars()
            .filter_map(|c| crate::md::unicode_range(c))
            .map(|s| s.to_string())
            .collect();
        ranges.sort();
        ranges.dedup();
        ranges
    }

    pub fn languages(&self) -> Vec<String> {
        self.language_ratios
            .iter()
            .map(|(language, _)| language.clone())
            .collect()
    }

    pub fn percent_chaos(&self) -> f64 {
        round3(self.chaos * 100.0)
    }

    pub fn percent_coherence(&self) -> f64 {
        round3(self.coherence * 100.0)
    }

    pub fn multi_byte_usage(&self) -> Option<f64> {
        let decoded = self.decoded()?;
        if self.raw.is_empty() {
            return Some(0.0);
        }
        Some(1.0 - (decoded.chars().count() as f64 / self.raw.len() as f64))
    }

    pub fn fingerprint(&self) -> Option<u64> {
        let decoded = self.decoded()?;
        let mut hasher = DefaultHasher::new();
        decoded.hash(&mut hasher);
        Some(hasher.finish())
    }

    pub fn encoding_aliases(&self) -> Vec<String> {
        PYTHON_ENCODING_ALIASES
            .iter()
            .filter_map(|(alias, canonical)| {
                if self.encoding == *alias {
                    Some((*canonical).to_string())
                } else if self.encoding == *canonical {
                    Some((*alias).to_string())
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn submatch(&self) -> &[CharsetMatch] {
        &self.submatches
    }

    pub fn has_submatch(&self) -> bool {
        !self.submatches.is_empty()
    }

    pub fn could_be_from_charset(&self) -> Vec<String> {
        let mut charsets = Vec::with_capacity(1 + self.submatches.len());
        charsets.push(self.encoding.clone());
        charsets.extend(self.submatches.iter().map(|m| m.encoding.clone()));
        charsets
    }

    pub fn output(&self, encoding: &str) -> Option<Vec<u8>> {
        let mut decoded = self.decoded()?;
        let norm = encoding.to_ascii_lowercase().replace('-', "_");
        if let Some(patched) = self.patch_preemptive_declaration(&decoded, &norm) {
            decoded = patched;
        }
        if matches!(norm.as_str(), "utf8" | "utf" | "utf_8") {
            return Some(decoded.into_bytes());
        }

        if crate::python_codecs::is_charmap_encoding(&norm) {
            return crate::python_codecs::encode_charmap_strict(&norm, &decoded);
        }

        if let Some(bytes) = crate::python_codecs::encode_utf32_strict(&norm, &decoded) {
            return Some(bytes);
        }

        if norm == "utf_7" {
            return crate::python_codecs::encode_utf7_strict(&decoded);
        }

        if norm == "hz" {
            return crate::python_codecs::encode_hz_strict(&decoded);
        }

        if norm == "johab" {
            return crate::korean_codecs::encode_johab_strict(&decoded);
        }

        if norm == "iso2022_kr" {
            return crate::korean_codecs::encode_iso2022_kr_strict(&decoded);
        }

        let label = if let Some(l) = crate::encoding_label(&norm, false, &[]) {
            l
        } else {
            encoding
        };
        let enc = if let Some(e) = Encoding::for_label(label.as_bytes()) {
            e
        } else {
            Encoding::for_label(norm.as_bytes())?
        };
        let (bytes, _, _had_errors) = enc.encode(&decoded);
        if _had_errors {
            return None;
        }
        Some(bytes.into_owned())
    }

    pub fn output_utf8(&self) -> Option<Vec<u8>> {
        self.output("utf_8")
    }

    fn patch_preemptive_declaration(&self, decoded: &str, target_encoding: &str) -> Option<String> {
        let declaration = self.preemptive_declaration.as_deref()?;
        let declaration = declaration.to_ascii_lowercase();

        if matches!(declaration.as_str(), "utf-8" | "utf8" | "utf_8") {
            return None;
        }

        let target = canonical_encoding_name(target_encoding, false)?.replace('_', "-");
        let mut split = decoded.len().min(8192);

        while split > 0 && !decoded.is_char_boundary(split) {
            split -= 1;
        }

        let header = &decoded[..split];
        let tail = &decoded[split..];
        let (value_start, value_end) = encoding_declaration_value_span(header)?;
        let mut patched = String::with_capacity(decoded.len() + target.len());
        patched.push_str(&header[..value_start]);
        patched.push_str(&target);
        patched.push_str(&header[value_end..]);
        patched.push_str(tail);
        Some(patched)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CharsetMatches {
    pub results: Vec<CharsetMatch>,
}

impl CharsetMatches {
    pub fn best(&self) -> Option<&CharsetMatch> {
        self.results.first()
    }

    pub fn first(&self) -> Option<&CharsetMatch> {
        self.best()
    }

    pub fn len(&self) -> usize {
        self.results.len()
    }

    pub fn is_empty(&self) -> bool {
        self.results.is_empty()
    }

    pub fn iter(&self) -> std::slice::Iter<'_, CharsetMatch> {
        self.results.iter()
    }

    pub fn get(&self, index: usize) -> Option<&CharsetMatch> {
        self.results.get(index)
    }

    pub fn get_by_encoding(&self, encoding: &str) -> Option<&CharsetMatch> {
        let encoding = canonical_encoding_name(encoding, false)?;
        self.results.iter().find(|result| {
            result
                .could_be_from_charset()
                .iter()
                .any(|e| e == &encoding)
        })
    }

    pub fn append(&mut self, item: CharsetMatch) {
        if item.raw.len() < crate::constant::TOO_BIG_SEQUENCE {
            for m in self.results.iter_mut() {
                if m.chaos == item.chaos {
                    if let (Some(dm), Some(di)) = (m.decoded(), item.decoded()) {
                        if dm == di {
                            m.submatches.push(item);
                            return;
                        }
                    }
                }
            }
        }
        self.results.push(item);
        sort_matches(&mut self.results);
    }
}

impl<'a> IntoIterator for &'a CharsetMatches {
    type Item = &'a CharsetMatch;
    type IntoIter = std::slice::Iter<'a, CharsetMatch>;

    fn into_iter(self) -> Self::IntoIter {
        self.results.iter()
    }
}

fn round3(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

impl CharsetMatches {
    pub fn new(r: Option<Vec<CharsetMatch>>) -> Self {
        let mut results = r.unwrap_or_default();
        sort_matches(&mut results);
        Self { results }
    }
}

fn sort_matches(results: &mut [CharsetMatch]) {
    results.sort_by(|a, b| {
        let chaos_difference = (a.chaos - b.chaos).abs();
        let coherence_difference = (a.coherence - b.coherence).abs();

        if chaos_difference < 0.005 && coherence_difference > 0.02 {
            b.coherence
                .partial_cmp(&a.coherence)
                .unwrap_or(std::cmp::Ordering::Equal)
        } else if chaos_difference < 0.005 && coherence_difference <= 0.02 {
            if a.raw.len() >= crate::constant::TOO_BIG_SEQUENCE {
                a.chaos
                    .partial_cmp(&b.chaos)
                    .unwrap_or(std::cmp::Ordering::Equal)
            } else {
                let a_usage = a.multi_byte_usage().unwrap_or(0.0);
                let b_usage = b.multi_byte_usage().unwrap_or(0.0);
                b_usage
                    .partial_cmp(&a_usage)
                    .unwrap_or(std::cmp::Ordering::Equal)
            }
        } else {
            a.chaos
                .partial_cmp(&b.chaos)
                .unwrap_or(std::cmp::Ordering::Equal)
        }
    });
}

#[derive(Debug, Clone, PartialEq)]
pub struct LegacyDetectionResult {
    pub encoding: Option<String>,
    pub language: String,
    pub confidence: Option<f64>,
}
