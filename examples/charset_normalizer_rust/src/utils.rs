pub const TOO_SMALL_SEQUENCE: usize = 32;
pub fn iana_name(s: &str, _b: bool) -> String {
    s.to_ascii_lowercase().replace("-", "_")
}
pub fn is_multi_byte_encoding(_: &str) -> bool {
    false
}
pub fn unicode_range(_: char) -> Option<&'static str> {
    None
}
pub fn is_accentuated(_: char) -> bool {
    false
}
pub fn is_latin(_: char) -> bool {
    false
}
pub fn is_unicode_range_secondary(_: &str) -> bool {
    false
}
pub fn identify_sig_or_bom(_: &[u8]) -> (Option<String>, Vec<u8>) {
    (None, vec![])
}
pub fn cut_sequence_chunks(
    _: &[u8],
    _: &str,
    _: std::ops::Range<usize>,
    _: usize,
    _: bool,
    _: bool,
    _: &[u8],
    _: bool,
    _: Option<&str>,
) -> Vec<String> {
    vec![]
}
pub const KO_NAMES: [&str; 0] = [];
pub const ZH_NAMES: [&str; 0] = [];
pub fn is_suspiciously_successive_range(_: Option<&str>, _: Option<&str>) -> bool {
    true
}
pub fn remove_accent(c: char) -> char {
    c
}
