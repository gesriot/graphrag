//! Structure-preserving Rust port of inih (`ini.c`) in **default config**.
//!
//! Scope (matches the golden contract): the string entry point
//! `ini_parse_string` / `ini_parse_string_length` driving `ini_parse_stream`
//! with a recording handler. Default options only:
//! `INI_ALLOW_MULTILINE=1`, `INI_ALLOW_BOM=1`, `INI_ALLOW_INLINE_COMMENTS=1`
//! (start prefixes `;#`, inline prefix `;`), `INI_USE_STACK=1`,
//! `INI_MAX_LINE=200`, `INI_STOP_ON_FIRST_ERROR=0`,
//! `INI_CALL_HANDLER_ON_NEW_SECTION=0`, `INI_ALLOW_NO_VALUE=0`,
//! `INI_HANDLER_LINENO=0`.
//!
//! inih is byte-oriented (it manipulates `char` buffers and tests `isspace` on
//! bytes), so the port operates on `&[u8]`, exactly like the C reference. File
//! I/O (`ini_parse`/`ini_parse_file`) is out of scope for this port; the C
//! golden runner measures string<->file parity separately.

/// `INI_MAX_LINE` from the default config. Lines longer than this (minus the
/// room C reserves for `\r\n\0`) are truncated and flagged as an error, mirroring
/// the fixed stack buffer in `ini_parse_stream`.
pub const INI_MAX_LINE: usize = 200;

const MAX_SECTION: usize = 50;
const MAX_NAME: usize = 50;
const START_COMMENT_PREFIXES: &[u8] = b";#";
const INLINE_COMMENT_PREFIXES: &[u8] = b";";

/// C `isspace` for the default "C" locale.
fn is_space(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

/// `ini_lskip`: slice past leading whitespace.
fn lskip(s: &[u8]) -> &[u8] {
    let mut i = 0;
    while i < s.len() && is_space(s[i]) {
        i += 1;
    }
    &s[i..]
}

/// `ini_rstrip`: slice without trailing whitespace.
fn rstrip(s: &[u8]) -> &[u8] {
    let mut e = s.len();
    while e > 0 && is_space(s[e - 1]) {
        e -= 1;
    }
    &s[..e]
}

/// `ini_find_chars_or_comment` (with `INI_ALLOW_INLINE_COMMENTS`): index of the
/// first byte in `chars`, or the first inline-comment prefix preceded by
/// whitespace, or the end of `s`.
fn find_chars_or_comment(s: &[u8], chars: Option<&[u8]>) -> usize {
    let mut was_space = false;
    let mut i = 0;
    while i < s.len() {
        let c = s[i];
        if let Some(set) = chars {
            if set.contains(&c) {
                break;
            }
        }
        if was_space && INLINE_COMMENT_PREFIXES.contains(&c) {
            break;
        }
        was_space = is_space(c);
        i += 1;
    }
    i
}

/// `ini_strncpy0`: copy at most `size - 1` bytes (the C buffers are NUL padded).
fn strncpy0(src: &[u8], size: usize) -> Vec<u8> {
    let n = src.len().min(size - 1);
    src[..n].to_vec()
}

/// C `strlen`: inih stores each line in a NUL-terminated `char` buffer, so all
/// later string operations ignore bytes after the first `\0` in that line.
fn c_strlen(s: &[u8]) -> usize {
    s.iter().position(|&c| c == 0).unwrap_or(s.len())
}

/// `ini_reader_string`: fgets-style reader over a byte buffer. Fills `line` with
/// up to `num - 1` bytes, stopping after a `\n`. Returns false at end of input.
struct StringReader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> StringReader<'a> {
    fn read(&mut self, line: &mut Vec<u8>, num: usize) -> bool {
        if self.pos >= self.data.len() || num < 2 {
            return false;
        }
        line.clear();
        let mut n = num;
        while n > 1 && self.pos < self.data.len() {
            let c = self.data[self.pos];
            self.pos += 1;
            line.push(c);
            if c == b'\n' {
                break;
            }
            n -= 1;
        }
        true
    }
}

/// Port of `ini_parse_stream` over a string reader (= `ini_parse_string_length`).
///
/// Calls `handler(section, name, value)` for each parsed pair, in order, with
/// byte slices (inih's `const char*`). The handler returns `true` on success;
/// returning `false` records a parse error. Returns 0 on success, or the line
/// number of the first error (parsing does not stop on first error).
pub fn parse_string<F>(input: &[u8], mut handler: F) -> i32
where
    F: FnMut(&[u8], &[u8], &[u8]) -> bool,
{
    let max_line = INI_MAX_LINE;
    let mut reader = StringReader {
        data: input,
        pos: 0,
    };

    let mut section: Vec<u8> = Vec::new();
    let mut prev_name: Vec<u8> = Vec::new();
    let mut lineno: i32 = 0;
    let mut error: i32 = 0;

    let mut line: Vec<u8> = Vec::new();
    let mut abyss: Vec<u8> = Vec::new();

    while reader.read(&mut line, max_line) {
        let offset = c_strlen(&line);
        let line_view = &line[..offset];
        lineno += 1;

        // If the line filled the buffer without a newline, it was too long:
        // discard input through the end of the line and flag an error.
        if offset == max_line - 1 && line_view[offset - 1] != b'\n' {
            while reader.read(&mut abyss, 16) {
                if error == 0 {
                    error = lineno;
                }
                let abyss_len = c_strlen(&abyss);
                if abyss_len > 0 && abyss[abyss_len - 1] == b'\n' {
                    break;
                }
            }
        }

        // Skip a UTF-8 BOM on the first line.
        let mut base = 0;
        if lineno == 1
            && offset >= 3
            && line_view[0] == 0xEF
            && line_view[1] == 0xBB
            && line_view[2] == 0xBF
        {
            base = 3;
        }

        let after_bom = &line_view[base..];
        let leading = after_bom.len() - lskip(after_bom).len();
        // `start > line` in C: trimmed start is past the line base (BOM or
        // leading whitespace consumed).
        let had_leading = base + leading > 0;
        let start = rstrip(lskip(after_bom));
        let first = start.first().copied();

        // In C, `strchr(START_COMMENT_PREFIXES, *start)` is also true for the NUL
        // terminator, so blank lines fall into the (no-op) comment branch.
        if first.is_none_or(|c| START_COMMENT_PREFIXES.contains(&c)) {
            // start-of-line comment or blank line: ignore
        } else if !prev_name.is_empty() && had_leading {
            // Non-blank line with leading whitespace: continuation of the
            // previous name's value (Python configparser style).
            let end = find_chars_or_comment(start, None);
            let value = rstrip(&start[..end]);
            if !handler(&section, &prev_name, value) && error == 0 {
                error = lineno;
            }
        } else if first == Some(b'[') {
            // "[section]" line.
            let rest = &start[1..];
            let end = find_chars_or_comment(rest, Some(b"]"));
            if end < rest.len() && rest[end] == b']' {
                section = strncpy0(&rest[..end], MAX_SECTION);
                prev_name.clear();
                // INI_CALL_HANDLER_ON_NEW_SECTION = 0: no callback here.
            } else if error == 0 {
                // No ']' found on the section line.
                error = lineno;
            }
        } else {
            // Not a comment: must be a name[=:]value pair.
            let end = find_chars_or_comment(start, Some(b"=:"));
            if end < start.len() && (start[end] == b'=' || start[end] == b':') {
                let name = rstrip(&start[..end]);
                let value_field = &start[end + 1..];
                let vend = find_chars_or_comment(value_field, None);
                let value = rstrip(lskip(&value_field[..vend]));
                prev_name = strncpy0(name, MAX_NAME);
                if !handler(&section, name, value) && error == 0 {
                    error = lineno;
                }
            } else if error == 0 {
                // No '=' or ':' and INI_ALLOW_NO_VALUE = 0: error.
                error = lineno;
            }
        }
    }

    error
}
