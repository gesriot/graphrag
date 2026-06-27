//! Port of charset_normalizer/md.py mess detection logic (CharInfo + 9 plugins + helpers + mess_ratio).
//! See examples/charset_normalizer/md.py and utils.py for the Python reference.

use unicode_general_category::{get_general_category, GeneralCategory};
use unicode_normalization::UnicodeNormalization;

use crate::constant::UNICODE_RANGES_COMBINED;

const _LATIN: u32 = 1;
const _ACCENTUATED: u32 = 1 << 1;
const _CJK: u32 = 1 << 2;
const _HANGUL: u32 = 1 << 3;
const _KATAKANA: u32 = 1 << 4;
const _HIRAGANA: u32 = 1 << 5;
const _THAI: u32 = 1 << 6;
const _ARABIC: u32 = 1 << 7;
const _ARABIC_ISOLATED_FORM: u32 = 1 << 8;
const _GLYPH_MASK: u32 = _CJK | _HANGUL | _KATAKANA | _HIRAGANA | _THAI;
const _ACCENT_MARKS: &[char] = &[
    '\u{0300}', '\u{0301}', '\u{0302}', '\u{0303}', '\u{0304}', '\u{0308}', '\u{030a}', '\u{0327}',
];

const UNICODE_SECONDARY_RANGE_KEYWORD: &[&str] = &[
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

const COMMON_CJK_CHARACTERS: &str = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞日一国年大十二本中長出三時行見月分後前生五間上東四今金九入学高円子外八六下来気小七山話女北午百書先名川千水半男西電校語土木聞食車何南万毎白天母火右読友左休父雨一二三四五六七八九十百千萬上下左右中人女子大小山川日月火水木金土父母天地國名年時文校學生";

#[inline]
fn is_common_safe_ascii(c: char) -> bool {
    matches!(
        c,
        '<' | '>'
            | '='
            | ':'
            | '/'
            | '&'
            | ';'
            | '{'
            | '}'
            | '['
            | ']'
            | ','
            | '|'
            | '"'
            | '-'
            | '('
            | ')'
    )
}

fn is_printable(ch: char) -> bool {
    // Match Python str.isprintable(): excludes Separators (Zs/Zl/Zp) except the ASCII space
    // which is handled in the ASCII fast-path of CharInfo. Non-breaking space etc are non-printable.
    !matches!(
        get_general_category(ch),
        GeneralCategory::Control
            | GeneralCategory::Format
            | GeneralCategory::Surrogate
            | GeneralCategory::PrivateUse
            | GeneralCategory::Unassigned
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
            | GeneralCategory::SpaceSeparator
    )
}
fn is_punctuation(ch: char) -> bool {
    matches!(
        get_general_category(ch),
        GeneralCategory::ConnectorPunctuation
            | GeneralCategory::DashPunctuation
            | GeneralCategory::OpenPunctuation
            | GeneralCategory::ClosePunctuation
            | GeneralCategory::InitialPunctuation
            | GeneralCategory::FinalPunctuation
            | GeneralCategory::OtherPunctuation
    )
}
fn is_symbol(ch: char) -> bool {
    match get_general_category(ch) {
        GeneralCategory::MathSymbol
        | GeneralCategory::CurrencySymbol
        | GeneralCategory::ModifierSymbol
        | GeneralCategory::OtherSymbol
        | GeneralCategory::DecimalNumber
        | GeneralCategory::LetterNumber
        | GeneralCategory::OtherNumber => true,
        cat => unicode_range(ch).map_or(false, |r| {
            r.contains("Forms") && cat != GeneralCategory::OtherLetter
        }),
    }
}
fn is_emoticon(ch: char) -> bool {
    unicode_range(ch).map_or(false, |r| {
        r.contains("Emoticons") || r.contains("Pictographs")
    })
}
fn is_separator(ch: char) -> bool {
    if ch.is_whitespace() || matches!(ch, '｜' | '+' | '<' | '>') {
        return true;
    }
    matches!(
        get_general_category(ch),
        GeneralCategory::SpaceSeparator
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
            | GeneralCategory::OtherPunctuation
            | GeneralCategory::DashPunctuation
            | GeneralCategory::ConnectorPunctuation
    )
}
fn _character_flags(ch: char) -> u32 {
    if !ch.is_alphabetic() {
        return 0;
    }
    let mut f = 0u32;
    if let Some(r) = unicode_range(ch) {
        let c = ch as u32;
        if r.contains("Latin")
            || (r.contains("Halfwidth and Fullwidth Forms")
                && ((0xFF21..=0xFF3A).contains(&c) || (0xFF41..=0xFF5A).contains(&c)))
        {
            f |= _LATIN;
        }
        if r.contains("CJK") {
            f |= _CJK;
        }
        if r.contains("Hangul") {
            f |= _HANGUL;
        }
        if r.contains("Katakana")
            || (r.contains("Halfwidth and Fullwidth Forms") && (0xFF61..=0xFF9F).contains(&c))
        {
            f |= _KATAKANA;
        }
        if r.contains("Hiragana") {
            f |= _HIRAGANA;
        }
        if r.contains("Thai") {
            f |= _THAI;
        }
        if r.contains("Arabic") {
            f |= _ARABIC;
            if r.contains("Presentation Forms-A") || r.contains("Presentation Forms-B") {
                f |= _ARABIC_ISOLATED_FORM;
            }
        }
    }
    if ch.nfd().any(|c| _ACCENT_MARKS.contains(&c)) {
        f |= _ACCENTUATED;
    }
    f
}

pub fn unicode_range(character: char) -> Option<&'static str> {
    let code = character as u32;
    let ranges = &*UNICODE_RANGES_COMBINED;
    let mut lo = 0;
    let mut hi = ranges.len();
    while lo < hi {
        let m = (lo + hi) / 2;
        if ranges[m].0 <= code {
            lo = m + 1;
        } else {
            hi = m;
        }
    }
    if lo > 0 {
        let (_s, stop, nm) = ranges[lo - 1];
        if code < stop {
            return Some(nm);
        }
    }
    None
}

pub fn remove_accent(character: char) -> char {
    if character.is_ascii() {
        return character;
    }
    let d: String = character.nfd().collect();
    if d.is_empty() {
        character
    } else {
        d.chars().next().unwrap_or(character)
    }
}

#[derive(Debug, Clone, Default)]
pub struct CharInfo {
    pub character: char,
    pub printable: bool,
    pub alpha: bool,
    pub upper: bool,
    pub lower: bool,
    pub space: bool,
    pub digit: bool,
    pub is_ascii: bool,
    pub case_variable: bool,
    pub flags: u32,
    pub accentuated: bool,
    pub latin: bool,
    pub is_cjk: bool,
    pub is_arabic: bool,
    pub is_glyph: bool,
    pub punct: bool,
    pub sym: bool,
}
impl CharInfo {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn update(&mut self, character: char) {
        self.character = character;
        let o = character as u32;
        if o < 128 {
            self.is_ascii = true;
            self.accentuated = false;
            self.is_cjk = false;
            self.is_arabic = false;
            self.is_glyph = false;
            if (65..=90).contains(&o) {
                self.alpha = true;
                self.upper = true;
                self.lower = false;
                self.space = false;
                self.digit = false;
                self.printable = true;
                self.case_variable = true;
                self.flags = _LATIN;
                self.latin = true;
                self.punct = false;
                self.sym = false;
            } else if (97..=122).contains(&o) {
                self.alpha = true;
                self.upper = false;
                self.lower = true;
                self.space = false;
                self.digit = false;
                self.printable = true;
                self.case_variable = true;
                self.flags = _LATIN;
                self.latin = true;
                self.punct = false;
                self.sym = false;
            } else if (48..=57).contains(&o) {
                self.alpha = false;
                self.upper = false;
                self.lower = false;
                self.space = false;
                self.digit = true;
                self.printable = true;
                self.case_variable = false;
                self.flags = 0;
                self.latin = false;
                self.punct = false;
                self.sym = false;
            } else if o == 32 || (9..=13).contains(&o) {
                self.alpha = false;
                self.upper = false;
                self.lower = false;
                self.space = true;
                self.digit = false;
                self.printable = o == 32;
                self.case_variable = false;
                self.flags = 0;
                self.latin = false;
                self.punct = false;
                self.sym = false;
            } else {
                self.printable = is_printable(character);
                self.alpha = false;
                self.upper = false;
                self.lower = false;
                self.space = false;
                self.digit = false;
                self.case_variable = false;
                self.flags = 0;
                self.latin = false;
                self.punct = if self.printable {
                    is_punctuation(character)
                } else {
                    false
                };
                self.sym = if self.printable {
                    is_symbol(character)
                } else {
                    false
                };
            }
        } else {
            self.is_ascii = false;
            self.printable = is_printable(character);
            self.alpha = character.is_alphabetic();
            self.upper = character.is_uppercase();
            self.lower = character.is_lowercase();
            self.space = character.is_whitespace();
            self.digit = character.is_numeric();
            self.case_variable = self.lower != self.upper;
            let fl = if self.alpha {
                _character_flags(character)
            } else {
                0
            };
            self.flags = fl;
            self.accentuated = (fl & _ACCENTUATED) != 0;
            self.latin = (fl & _LATIN) != 0;
            self.is_cjk = (fl & _CJK) != 0;
            self.is_arabic = (fl & _ARABIC) != 0;
            self.is_glyph = (fl & _GLYPH_MASK) != 0;
            self.punct = if self.printable {
                is_punctuation(character)
            } else {
                false
            };
            self.sym = if self.printable {
                is_symbol(character)
            } else {
                false
            };
        }
    }
}

pub struct TooManySymbolOrPunctuationPlugin {
    _punctuation_count: usize,
    _symbol_count: usize,
    _character_count: usize,
    _last_printable_char: Option<char>,
    _frenzy_symbol_in_word: bool,
}
impl TooManySymbolOrPunctuationPlugin {
    pub fn new() -> Self {
        Self {
            _punctuation_count: 0,
            _symbol_count: 0,
            _character_count: 0,
            _last_printable_char: None,
            _frenzy_symbol_in_word: false,
        }
    }
    pub fn feed_info(&mut self, ch: char, info: &CharInfo) {
        self._character_count += 1;
        if ch != self._last_printable_char.unwrap_or('\0') && !is_common_safe_ascii(ch) {
            if info.punct {
                self._punctuation_count += 1;
            } else if !info.digit && info.sym && !is_emoticon(ch) {
                self._symbol_count += 2;
            }
        }
        self._last_printable_char = Some(ch);
    }
    pub fn reset(&mut self) {
        self._punctuation_count = 0;
        self._character_count = 0;
        self._symbol_count = 0;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count == 0 {
            0.0
        } else {
            let r = (self._punctuation_count + self._symbol_count) as f64
                / self._character_count as f64;
            if r >= 0.3 {
                r
            } else {
                0.0
            }
        }
    }
}
pub struct TooManyAccentuatedPlugin {
    _character_count: usize,
    _accentuated_count: usize,
}
impl TooManyAccentuatedPlugin {
    pub fn new() -> Self {
        Self {
            _character_count: 0,
            _accentuated_count: 0,
        }
    }
    pub fn feed_info(&mut self, _ch: char, info: &CharInfo) {
        self._character_count += 1;
        if info.accentuated {
            self._accentuated_count += 1;
        }
    }
    pub fn reset(&mut self) {
        self._character_count = 0;
        self._accentuated_count = 0;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count < 8 {
            0.0
        } else {
            let r = self._accentuated_count as f64 / self._character_count as f64;
            if r >= 0.35 {
                r
            } else {
                0.0
            }
        }
    }
}
pub struct UnprintablePlugin {
    _unprintable_count: usize,
    _character_count: usize,
}
impl UnprintablePlugin {
    pub fn new() -> Self {
        Self {
            _unprintable_count: 0,
            _character_count: 0,
        }
    }
    pub fn feed_info(&mut self, ch: char, info: &CharInfo) {
        if !info.space && !info.printable && ch != '\x1a' && ch != '\u{feff}' {
            self._unprintable_count += 1;
        }
        self._character_count += 1;
    }
    pub fn reset(&mut self) {
        self._unprintable_count = 0;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count == 0 {
            0.0
        } else {
            (self._unprintable_count * 8) as f64 / self._character_count as f64
        }
    }
}
pub struct SuspiciousDuplicateAccentPlugin {
    _successive_count: usize,
    _character_count: usize,
    _last_latin_character: Option<char>,
    _last_was_accentuated: bool,
}
impl SuspiciousDuplicateAccentPlugin {
    pub fn new() -> Self {
        Self {
            _successive_count: 0,
            _character_count: 0,
            _last_latin_character: None,
            _last_was_accentuated: false,
        }
    }
    pub fn feed_info(&mut self, ch: char, info: &CharInfo) {
        self._character_count += 1;
        if self._last_latin_character.is_some() && info.accentuated && self._last_was_accentuated {
            if info.upper {
                if let Some(l) = self._last_latin_character {
                    if l.is_uppercase() {
                        self._successive_count += 1;
                    }
                }
            }
            if remove_accent(ch) == remove_accent(self._last_latin_character.unwrap_or(ch)) {
                self._successive_count += 1;
            }
        }
        self._last_latin_character = Some(ch);
        self._last_was_accentuated = info.accentuated;
    }
    pub fn reset(&mut self) {
        self._successive_count = 0;
        self._character_count = 0;
        self._last_latin_character = None;
        self._last_was_accentuated = false;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count == 0 {
            0.0
        } else {
            (self._successive_count * 2) as f64 / self._character_count as f64
        }
    }
}
pub struct SuspiciousRange {
    _suspicious_successive_range_count: usize,
    _character_count: usize,
    _last_printable_seen: Option<char>,
    _last_printable_range: Option<String>,
}
impl SuspiciousRange {
    pub fn new() -> Self {
        Self {
            _suspicious_successive_range_count: 0,
            _character_count: 0,
            _last_printable_seen: None,
            _last_printable_range: None,
        }
    }
    pub fn feed_info(&mut self, ch: char, info: &CharInfo) {
        self._character_count += 1;
        if info.space || info.punct || is_common_safe_ascii(ch) {
            self._last_printable_seen = None;
            self._last_printable_range = None;
            return;
        }
        if self._last_printable_seen.is_none() {
            self._last_printable_seen = Some(ch);
            self._last_printable_range = unicode_range(ch).map(|s| s.to_string());
            return;
        }
        let ra = self._last_printable_range.as_deref();
        let rb = unicode_range(ch);
        if is_suspiciously_successive_range(ra, rb) {
            self._suspicious_successive_range_count += 1;
        }
        self._last_printable_seen = Some(ch);
        self._last_printable_range = rb.map(|s| s.to_string());
    }
    pub fn reset(&mut self) {
        self._character_count = 0;
        self._suspicious_successive_range_count = 0;
        self._last_printable_seen = None;
        self._last_printable_range = None;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count <= 13 {
            0.0
        } else {
            (self._suspicious_successive_range_count * 2) as f64 / self._character_count as f64
        }
    }
}
pub struct SuperWeirdWordPlugin {
    _word_count: usize,
    _bad_word_count: usize,
    _foreign_long_count: usize,
    _is_current_word_bad: bool,
    _foreign_long_watch: bool,
    _character_count: usize,
    _bad_character_count: usize,
    _buffer_length: usize,
    _buffer_last_char: Option<char>,
    _buffer_last_char_accentuated: bool,
    _buffer_accent_count: usize,
    _buffer_glyph_count: usize,
    _buffer_upper_count: usize,
}
impl SuperWeirdWordPlugin {
    pub fn new() -> Self {
        Self {
            _word_count: 0,
            _bad_word_count: 0,
            _foreign_long_count: 0,
            _is_current_word_bad: false,
            _foreign_long_watch: false,
            _character_count: 0,
            _bad_character_count: 0,
            _buffer_length: 0,
            _buffer_last_char: None,
            _buffer_last_char_accentuated: false,
            _buffer_accent_count: 0,
            _buffer_glyph_count: 0,
            _buffer_upper_count: 0,
        }
    }
    pub fn feed_info(&mut self, ch: char, info: &CharInfo) {
        if info.alpha {
            self._buffer_length += 1;
            self._buffer_last_char = Some(ch);
            if info.upper {
                self._buffer_upper_count += 1;
            }
            self._buffer_last_char_accentuated = info.accentuated;
            if info.accentuated {
                self._buffer_accent_count += 1;
            }
            if !self._foreign_long_watch && (!info.latin || info.accentuated) && !info.is_glyph {
                self._foreign_long_watch = true;
            }
            if info.is_glyph {
                self._buffer_glyph_count += 1;
            }
            return;
        }
        if self._buffer_length == 0 {
            return;
        }
        if info.space || info.punct || is_separator(ch) {
            self._word_count += 1;
            let bl = self._buffer_length;
            self._character_count += bl;
            if bl >= 4 {
                if (self._buffer_accent_count as f64) / bl as f64 >= 0.5 {
                    self._is_current_word_bad = true;
                } else if self._buffer_last_char_accentuated {
                    if let Some(l) = self._buffer_last_char {
                        if l.is_uppercase() && self._buffer_upper_count != bl {
                            self._foreign_long_count += 1;
                            self._is_current_word_bad = true;
                        }
                    }
                } else if self._buffer_glyph_count == 1 {
                    self._is_current_word_bad = true;
                    self._foreign_long_count += 1;
                }
            }
            if bl >= 24 && self._foreign_long_watch {
                let camel = self._buffer_upper_count > 0
                    && (self._buffer_upper_count as f64) / bl as f64 <= 0.3;
                if !camel {
                    self._foreign_long_count += 1;
                    self._is_current_word_bad = true;
                }
            }
            if self._is_current_word_bad {
                self._bad_word_count += 1;
                self._bad_character_count += bl;
                self._is_current_word_bad = false;
            }
            self._foreign_long_watch = false;
            self._buffer_length = 0;
            self._buffer_last_char = None;
            self._buffer_last_char_accentuated = false;
            self._buffer_accent_count = 0;
            self._buffer_glyph_count = 0;
            self._buffer_upper_count = 0;
        } else if !matches!(ch, '<' | '>' | '-' | '=' | '~' | '|' | '_') && !info.digit && info.sym
        {
            self._is_current_word_bad = true;
            self._buffer_length += 1;
            self._buffer_last_char = Some(ch);
            self._buffer_last_char_accentuated = false;
        }
    }
    pub fn reset(&mut self) {
        self._buffer_length = 0;
        self._buffer_last_char = None;
        self._buffer_last_char_accentuated = false;
        self._is_current_word_bad = false;
        self._foreign_long_watch = false;
        self._bad_word_count = 0;
        self._word_count = 0;
        self._character_count = 0;
        self._bad_character_count = 0;
        self._foreign_long_count = 0;
        self._buffer_accent_count = 0;
        self._buffer_glyph_count = 0;
        self._buffer_upper_count = 0;
    }
    pub fn ratio(&self) -> f64 {
        if self._word_count <= 10 && self._foreign_long_count == 0 {
            0.0
        } else if self._character_count == 0 {
            0.0
        } else {
            self._bad_character_count as f64 / self._character_count as f64
        }
    }
}
pub struct CjkUncommonPlugin {
    _character_count: usize,
    _uncommon_count: usize,
}
impl CjkUncommonPlugin {
    pub fn new() -> Self {
        Self {
            _character_count: 0,
            _uncommon_count: 0,
        }
    }
    pub fn feed_info(&mut self, ch: char, _info: &CharInfo) {
        self._character_count += 1;
        if !COMMON_CJK_CHARACTERS.contains(ch) {
            self._uncommon_count += 1;
        }
    }
    pub fn reset(&mut self) {
        self._character_count = 0;
        self._uncommon_count = 0;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count < 8 {
            0.0
        } else {
            let u = self._uncommon_count as f64 / self._character_count as f64;
            if u > 0.5 {
                u / 10.0
            } else {
                0.0
            }
        }
    }
}
pub struct ArchaicUpperLowerPlugin {
    _buf: bool,
    _character_count_since_last_sep: usize,
    _successive_upper_lower_count: usize,
    _successive_upper_lower_count_final: usize,
    _character_count: usize,
    _last_alpha_seen: Option<char>,
    _last_alpha_seen_upper: bool,
    _last_alpha_seen_lower: bool,
    _current_ascii_only: bool,
}
impl ArchaicUpperLowerPlugin {
    pub fn new() -> Self {
        Self {
            _buf: false,
            _character_count_since_last_sep: 0,
            _successive_upper_lower_count: 0,
            _successive_upper_lower_count_final: 0,
            _character_count: 0,
            _last_alpha_seen: None,
            _last_alpha_seen_upper: false,
            _last_alpha_seen_lower: false,
            _current_ascii_only: true,
        }
    }
    pub fn feed_info(&mut self, ch: char, info: &CharInfo) {
        let concerned = info.alpha && info.case_variable;
        let sep = !concerned;
        if sep && self._character_count_since_last_sep > 0 {
            if self._character_count_since_last_sep <= 64
                && !info.digit
                && !self._current_ascii_only
            {
                self._successive_upper_lower_count_final += self._successive_upper_lower_count;
            }
            self._successive_upper_lower_count = 0;
            self._character_count_since_last_sep = 0;
            self._last_alpha_seen = None;
            self._buf = false;
            self._character_count += 1;
            self._current_ascii_only = true;
            return;
        }
        if self._current_ascii_only && !info.is_ascii {
            self._current_ascii_only = false;
        }
        if let Some(_l) = self._last_alpha_seen {
            if (info.upper && self._last_alpha_seen_lower)
                || (info.lower && self._last_alpha_seen_upper)
            {
                if self._buf {
                    self._successive_upper_lower_count += 2;
                    self._buf = false;
                } else {
                    self._buf = true;
                }
            } else {
                self._buf = false;
            }
        }
        self._character_count += 1;
        self._character_count_since_last_sep += 1;
        self._last_alpha_seen = Some(ch);
        self._last_alpha_seen_upper = info.upper;
        self._last_alpha_seen_lower = info.lower;
    }
    pub fn reset(&mut self) {
        self._character_count = 0;
        self._character_count_since_last_sep = 0;
        self._successive_upper_lower_count = 0;
        self._successive_upper_lower_count_final = 0;
        self._last_alpha_seen = None;
        self._last_alpha_seen_upper = false;
        self._last_alpha_seen_lower = false;
        self._buf = false;
        self._current_ascii_only = true;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count == 0 {
            0.0
        } else {
            self._successive_upper_lower_count_final as f64 / self._character_count as f64
        }
    }
}
pub struct ArabicIsolatedFormPlugin {
    _character_count: usize,
    _isolated_form_count: usize,
}
impl ArabicIsolatedFormPlugin {
    pub fn new() -> Self {
        Self {
            _character_count: 0,
            _isolated_form_count: 0,
        }
    }
    pub fn feed_info(&mut self, _ch: char, info: &CharInfo) {
        self._character_count += 1;
        if (info.flags & _ARABIC_ISOLATED_FORM) != 0 {
            self._isolated_form_count += 1;
        }
    }
    pub fn reset(&mut self) {
        self._character_count = 0;
        self._isolated_form_count = 0;
    }
    pub fn ratio(&self) -> f64 {
        if self._character_count < 8 {
            0.0
        } else {
            self._isolated_form_count as f64 / self._character_count as f64
        }
    }
}

pub fn is_suspiciously_successive_range(a: Option<&str>, b: Option<&str>) -> bool {
    if a.is_none() || b.is_none() {
        return true;
    }
    let (ua, ub) = (a.unwrap(), b.unwrap());
    if ua == ub {
        return false;
    }
    if ua.contains("Latin") && ub.contains("Latin") {
        return false;
    }
    if ua.contains("Emoticons") || ub.contains("Emoticons") {
        return false;
    }
    if (ua.contains("Latin") || ub.contains("Latin"))
        && (ua.contains("Combining") || ub.contains("Combining"))
    {
        return false;
    }
    let ka: Vec<&str> = ua.split(' ').collect();
    let kb: Vec<&str> = ub.split(' ').collect();
    for el in &ka {
        if UNICODE_SECONDARY_RANGE_KEYWORD.contains(el) {
            continue;
        }
        if kb.contains(el) {
            return false;
        }
    }
    let ajp = matches!(ua, "Hiragana" | "Katakana");
    let bjp = matches!(ub, "Hiragana" | "Katakana");
    if (ajp || bjp) && (ua.contains("CJK") || ub.contains("CJK")) {
        return false;
    }
    if ajp && bjp {
        return false;
    }
    if ua.contains("Hangul") || ub.contains("Hangul") {
        if ua.contains("CJK") || ub.contains("CJK") {
            return false;
        }
        if ua == "Basic Latin" || ub == "Basic Latin" {
            return false;
        }
    }
    if (ua.contains("CJK") || ub.contains("CJK"))
        || (matches!(ua, "Katakana" | "Hiragana") && matches!(ub, "Katakana" | "Hiragana"))
    {
        if ua.contains("Punctuation") || ub.contains("Punctuation") {
            return false;
        }
        if ua.contains("Forms") || ub.contains("Forms") {
            return false;
        }
        if ua == "Basic Latin" || ub == "Basic Latin" {
            return false;
        }
    }
    true
}

pub fn mess_ratio(decoded_sequence: &str, maximum_threshold: f64, debug: bool) -> f64 {
    let chars: Vec<char> = decoded_sequence.chars().collect();
    let len = chars.len();
    let step = if len < 511 {
        32
    } else if len < 1024 {
        64
    } else {
        128
    };
    let mut d_sp = TooManySymbolOrPunctuationPlugin::new();
    let mut d_ta = TooManyAccentuatedPlugin::new();
    let mut d_up = UnprintablePlugin::new();
    let mut d_sda = SuspiciousDuplicateAccentPlugin::new();
    let mut d_sr = SuspiciousRange::new();
    let mut d_sw = SuperWeirdWordPlugin::new();
    let mut d_cu = CjkUncommonPlugin::new();
    let mut d_au = ArchaicUpperLowerPlugin::new();
    let mut d_ai = ArabicIsolatedFormPlugin::new();
    let mut info = CharInfo::new();
    let mut mean = 0.0;
    let mut early = false;
    for bs in (0..len).step_by(step) {
        let be = (bs + step).min(len);
        for &ch in &chars[bs..be] {
            info.update(ch);
            d_up.feed_info(ch, &info);
            d_sw.feed_info(ch, &info);
            d_au.feed_info(ch, &info);
            if info.printable {
                d_sp.feed_info(ch, &info);
                d_sr.feed_info(ch, &info);
            }
            if info.alpha {
                d_ta.feed_info(ch, &info);
                if info.latin {
                    d_sda.feed_info(ch, &info);
                }
                if info.is_cjk {
                    d_cu.feed_info(ch, &info);
                }
                if info.is_arabic {
                    d_ai.feed_info(ch, &info);
                }
            }
        }
        mean = d_sp.ratio()
            + d_ta.ratio()
            + d_up.ratio()
            + d_sda.ratio()
            + d_sr.ratio()
            + d_sw.ratio()
            + d_cu.ratio()
            + d_au.ratio()
            + d_ai.ratio();
        if mean >= maximum_threshold {
            early = true;
            break;
        }
    }
    if !early {
        info.update('\n');
        d_sw.feed_info('\n', &info);
        d_au.feed_info('\n', &info);
        d_up.feed_info('\n', &info);
        mean = d_sp.ratio()
            + d_ta.ratio()
            + d_up.ratio()
            + d_sda.ratio()
            + d_sr.ratio()
            + d_sw.ratio()
            + d_cu.ratio()
            + d_au.ratio()
            + d_ai.ratio();
    }
    if debug { /* no-op */ }
    (mean * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn smoke() {
        assert_eq!(mess_ratio("clean ascii here for test", 1.0, false), 0.0);
        let v = mess_ratio("Voilà accents éè", 1.0, false);
        assert!(v > 0.0);
    }

    #[test]
    fn cp932_garbage_mess_from_arabic_cp1256() {
        // The exact garbage produced by decoding cp1256 Arabic payload as cp932 (shift_jis).
        // Python mess_ratio on this is 0.1 (rounded), which is <0.2 so passes chaos probe
        // and triggers mb_definitive (len=36 < 45*0.98) causing cp932 to win over cp1256.
        // This test captures the value for parity.
        let garbage: String = vec![
            32796u32, 65421, 65416, 65415, 32, 65416, 65415, 30653, 65415, 30716, 46, 32, 32796,
            65421, 65416, 65415, 32, 65416, 65415, 30653, 65415, 30716, 46, 32, 32796, 65421,
            65416, 65415, 32, 65416, 65415, 30653, 65415, 30716, 46, 32,
        ]
        .into_iter()
        .filter_map(char::from_u32)
        .collect();
        assert_eq!(mess_ratio(&garbage, 0.2, false), 0.1);
    }
}
