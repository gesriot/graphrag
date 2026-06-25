//! Structure-preserving Rust port of the cJSON ownership slice
//! (parse -> inspect -> print -> delete) in default config.
//!
//! This deliberately mirrors cJSON's C node layout rather than using an
//! idiomatic Rust enum, because the point of this milestone is to carry C-style
//! tree ownership across the port:
//! - a node is a struct with a type tag, `child`/`next` links, and the
//!   `valuestring`/`valueint`/`valuedouble`/`string` fields;
//! - the child/sibling chain is a `Box`-owned singly linked list (no raw
//!   pointers, no `prev`);
//! - `Drop` mirrors `cJSON_Delete`: it iterates the `next` chain (so a long
//!   sibling list does not recurse) while each node's `child` drops recursively.
//!
//! Scope: parse + the public getter API + unformatted/formatted printing over a
//! bounded corpus (objects/arrays/strings/escapes/integers/bool/null/nesting).
//! Number *printing* fidelity for non-integers (the `%g` paths) is a deferred
//! sub-stage; the corpus uses integers, which take cJSON's exact `%d` path.

// jsmn-style bit-flag type tags (cJSON.h).
pub const CJSON_INVALID: i32 = 0;
pub const CJSON_FALSE: i32 = 1 << 0;
pub const CJSON_TRUE: i32 = 1 << 1;
pub const CJSON_NULL: i32 = 1 << 2;
pub const CJSON_NUMBER: i32 = 1 << 3;
pub const CJSON_STRING: i32 = 1 << 4;
pub const CJSON_ARRAY: i32 = 1 << 5;
pub const CJSON_OBJECT: i32 = 1 << 6;
pub const CJSON_RAW: i32 = 1 << 7;

const NESTING_LIMIT: usize = 1000;

/// Mirror of the C `cJSON` node. Strings are kept as bytes (cJSON's `char*`),
/// since decoded values and keys may hold arbitrary UTF-8.
pub struct CJson {
    pub child: Option<Box<CJson>>,
    pub next: Option<Box<CJson>>,
    pub type_: i32,
    pub valuestring: Option<Vec<u8>>,
    pub valueint: i32,
    pub valuedouble: f64,
    pub string: Option<Vec<u8>>,
}

impl CJson {
    fn new() -> Self {
        CJson {
            child: None,
            next: None,
            type_: CJSON_INVALID,
            valuestring: None,
            valueint: 0,
            valuedouble: 0.0,
            string: None,
        }
    }
}

impl Drop for CJson {
    fn drop(&mut self) {
        // Mirror cJSON_Delete: iterate the sibling (`next`) chain instead of
        // recursing through it, so freeing a long array/object does not blow the
        // stack. Each node's `child` still drops recursively as the node falls
        // out of scope, matching cJSON's recursive child delete.
        let mut next = self.next.take();
        while let Some(mut node) = next {
            next = node.next.take();
        }
    }
}

/* ---------- getter API (ported 1:1 over the struct) ---------- */

pub fn is_null(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_NULL
}
pub fn is_bool(item: &CJson) -> bool {
    (item.type_ & (CJSON_TRUE | CJSON_FALSE)) != 0
}
pub fn is_true(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_TRUE
}
pub fn is_number(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_NUMBER
}
pub fn is_string(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_STRING
}
pub fn is_array(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_ARRAY
}
pub fn is_object(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_OBJECT
}

/// `cJSON_GetArraySize`: number of children (works for arrays and objects).
pub fn get_array_size(item: &CJson) -> i32 {
    let mut n = 0i32;
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        n += 1;
        cur = node.next.as_deref();
    }
    n
}

/// `cJSON_GetArrayItem`: the child at `index`, or None.
pub fn get_array_item(item: &CJson, index: i32) -> Option<&CJson> {
    if index < 0 {
        return None;
    }
    let mut remaining = index;
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if remaining == 0 {
            return Some(node);
        }
        remaining -= 1;
        cur = node.next.as_deref();
    }
    None
}

/// `cJSON_GetObjectItem`: case-insensitive lookup by key.
pub fn get_object_item<'a>(item: &'a CJson, name: &[u8]) -> Option<&'a CJson> {
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if let Some(key) = node.string.as_deref() {
            if key.eq_ignore_ascii_case(name) {
                return Some(node);
            }
        }
        cur = node.next.as_deref();
    }
    None
}

pub fn get_string_value(item: &CJson) -> Option<&[u8]> {
    if is_string(item) {
        item.valuestring.as_deref()
    } else {
        None
    }
}

/* ---------- parse ---------- */

struct ParseBuffer<'a> {
    content: &'a [u8],
    length: usize,
    offset: usize,
    depth: usize,
}

impl<'a> ParseBuffer<'a> {
    // All index math is wrapping to mirror cJSON's `size_t` arithmetic: a few
    // malformed-input paths transiently set offset to SIZE_MAX before stepping
    // it back, exactly as the C does.
    fn can_read(&self, size: usize) -> bool {
        self.offset.wrapping_add(size) <= self.length
    }
    fn can_access(&self, index: usize) -> bool {
        self.offset.wrapping_add(index) < self.length
    }
    fn byte(&self, index: usize) -> u8 {
        self.content[self.offset.wrapping_add(index)]
    }
    fn dec(&mut self) {
        self.offset = self.offset.wrapping_sub(1);
    }
    fn inc(&mut self) {
        self.offset = self.offset.wrapping_add(1);
    }

    fn skip_whitespace(&mut self) {
        while self.can_access(0) && self.byte(0) <= 32 {
            self.inc();
        }
        if self.offset == self.length {
            self.dec();
        }
    }

    fn skip_utf8_bom(&mut self) {
        if self.offset == 0 && self.can_access(4) && self.content[0..3] == [0xEF, 0xBB, 0xBF] {
            self.offset += 3;
        }
    }

    fn parse_value(&mut self, item: &mut CJson) -> bool {
        if self.can_read(4) && &self.content[self.offset..self.offset + 4] == b"null" {
            item.type_ = CJSON_NULL;
            self.offset += 4;
            return true;
        }
        if self.can_read(5) && &self.content[self.offset..self.offset + 5] == b"false" {
            item.type_ = CJSON_FALSE;
            self.offset += 5;
            return true;
        }
        if self.can_read(4) && &self.content[self.offset..self.offset + 4] == b"true" {
            item.type_ = CJSON_TRUE;
            item.valueint = 1;
            self.offset += 4;
            return true;
        }
        if self.can_access(0) && self.byte(0) == b'"' {
            return self.parse_string(item);
        }
        if self.can_access(0) && (self.byte(0) == b'-' || self.byte(0).is_ascii_digit()) {
            return self.parse_number(item);
        }
        if self.can_access(0) && self.byte(0) == b'[' {
            return self.parse_array(item);
        }
        if self.can_access(0) && self.byte(0) == b'{' {
            return self.parse_object(item);
        }
        false
    }

    fn parse_number(&mut self, item: &mut CJson) -> bool {
        let start = self.offset;
        let mut end = self.offset;
        while end < self.length {
            match self.content[end] {
                b'0'..=b'9' | b'+' | b'-' | b'e' | b'E' | b'.' => end += 1,
                _ => break,
            }
        }
        let text = match std::str::from_utf8(&self.content[start..end]) {
            Ok(t) => t,
            Err(_) => return false,
        };
        let number: f64 = match text.parse() {
            Ok(n) => n,
            Err(_) => return false,
        };
        item.valuedouble = number;
        if number >= i32::MAX as f64 {
            item.valueint = i32::MAX;
        } else if number <= i32::MIN as f64 {
            item.valueint = i32::MIN;
        } else {
            item.valueint = number as i32;
        }
        item.type_ = CJSON_NUMBER;
        self.offset = end;
        true
    }

    fn parse_string(&mut self, item: &mut CJson) -> bool {
        if !self.can_access(0) || self.byte(0) != b'"' {
            return false;
        }
        // Find the closing quote, honoring escapes.
        let mut e = self.offset + 1;
        while e < self.length && self.content[e] != b'"' {
            if self.content[e] == b'\\' {
                if e + 1 >= self.length {
                    return false;
                }
                e += 1;
            }
            e += 1;
        }
        if e >= self.length || self.content[e] != b'"' {
            return false;
        }
        let end = e; // index of the closing quote

        let mut out: Vec<u8> = Vec::new();
        let mut i = self.offset + 1;
        while i < end {
            if self.content[i] != b'\\' {
                out.push(self.content[i]);
                i += 1;
            } else {
                match self.content[i + 1] {
                    b'b' => {
                        out.push(0x08);
                        i += 2;
                    }
                    b'f' => {
                        out.push(0x0c);
                        i += 2;
                    }
                    b'n' => {
                        out.push(b'\n');
                        i += 2;
                    }
                    b'r' => {
                        out.push(b'\r');
                        i += 2;
                    }
                    b't' => {
                        out.push(b'\t');
                        i += 2;
                    }
                    b'"' | b'\\' | b'/' => {
                        out.push(self.content[i + 1]);
                        i += 2;
                    }
                    b'u' => {
                        let seq = utf16_literal_to_utf8(self.content, i, end, &mut out);
                        if seq == 0 {
                            return false;
                        }
                        i += seq;
                    }
                    _ => return false,
                }
            }
        }

        item.type_ = CJSON_STRING;
        item.valuestring = Some(out);
        self.offset = end + 1;
        true
    }

    fn parse_array(&mut self, item: &mut CJson) -> bool {
        if self.depth >= NESTING_LIMIT {
            return false;
        }
        self.depth += 1;
        // caller verified '['
        self.inc();
        self.skip_whitespace();
        if self.can_access(0) && self.byte(0) == b']' {
            self.depth -= 1;
            item.type_ = CJSON_ARRAY;
            self.inc();
            return true;
        }
        if !self.can_access(0) {
            self.dec();
            self.depth -= 1;
            return false;
        }
        self.dec(); // step back before first element
        let mut children: Vec<CJson> = Vec::new();
        loop {
            let mut new_item = CJson::new();
            self.inc();
            self.skip_whitespace();
            if !self.parse_value(&mut new_item) {
                self.depth -= 1;
                return false; // children drop here, freeing what was parsed
            }
            children.push(new_item);
            self.skip_whitespace();
            if !(self.can_access(0) && self.byte(0) == b',') {
                break;
            }
        }
        if !self.can_access(0) || self.byte(0) != b']' {
            self.depth -= 1;
            return false;
        }
        self.depth -= 1;
        item.type_ = CJSON_ARRAY;
        item.child = link(children);
        self.inc();
        true
    }

    fn parse_object(&mut self, item: &mut CJson) -> bool {
        if self.depth >= NESTING_LIMIT {
            return false;
        }
        self.depth += 1;
        if !self.can_access(0) || self.byte(0) != b'{' {
            self.depth -= 1;
            return false;
        }
        self.inc();
        self.skip_whitespace();
        if self.can_access(0) && self.byte(0) == b'}' {
            self.depth -= 1;
            item.type_ = CJSON_OBJECT;
            self.inc();
            return true;
        }
        if !self.can_access(0) {
            self.dec();
            self.depth -= 1;
            return false;
        }
        self.dec(); // step back before first element
        let mut children: Vec<CJson> = Vec::new();
        loop {
            let mut new_item = CJson::new();
            if !self.can_access(1) {
                self.depth -= 1;
                return false;
            }
            self.inc();
            self.skip_whitespace();
            if !self.parse_string(&mut new_item) {
                self.depth -= 1;
                return false;
            }
            self.skip_whitespace();
            // swap valuestring -> string (we parsed the key)
            new_item.string = new_item.valuestring.take();
            if !self.can_access(0) || self.byte(0) != b':' {
                self.depth -= 1;
                return false;
            }
            self.inc();
            self.skip_whitespace();
            if !self.parse_value(&mut new_item) {
                self.depth -= 1;
                return false;
            }
            children.push(new_item);
            self.skip_whitespace();
            if !(self.can_access(0) && self.byte(0) == b',') {
                break;
            }
        }
        if !self.can_access(0) || self.byte(0) != b'}' {
            self.depth -= 1;
            return false;
        }
        self.depth -= 1;
        item.type_ = CJSON_OBJECT;
        item.child = link(children);
        self.inc();
        true
    }
}

/// Chain a vector of children into a `Box`-owned `next` list, preserving order.
fn link(mut children: Vec<CJson>) -> Option<Box<CJson>> {
    let mut head: Option<Box<CJson>> = None;
    while let Some(mut node) = children.pop() {
        node.next = head;
        head = Some(Box::new(node));
    }
    head
}

fn parse_hex4(input: &[u8]) -> u32 {
    let mut h: u32 = 0;
    for (i, &c) in input.iter().take(4).enumerate() {
        h += match c {
            b'0'..=b'9' => (c - b'0') as u32,
            b'A'..=b'F' => 10 + (c - b'A') as u32,
            b'a'..=b'f' => 10 + (c - b'a') as u32,
            _ => return 0,
        };
        if i < 3 {
            h <<= 4;
        }
    }
    h
}

/// Port of `utf16_literal_to_utf8`. `idx` points at the backslash of `\uXXXX`,
/// `end` is the closing-quote index. Returns input bytes consumed (6 or 12), or
/// 0 on failure.
fn utf16_literal_to_utf8(c: &[u8], idx: usize, end: usize, out: &mut Vec<u8>) -> usize {
    if end < idx + 6 {
        return 0;
    }
    let first = parse_hex4(&c[idx + 2..]);
    if (0xDC00..=0xDFFF).contains(&first) {
        return 0;
    }
    let (codepoint, seq_len) = if (0xD800..=0xDBFF).contains(&first) {
        let second = idx + 6;
        if end < second + 6 {
            return 0;
        }
        if c[second] != b'\\' || c[second + 1] != b'u' {
            return 0;
        }
        let second_code = parse_hex4(&c[second + 2..]);
        if !(0xDC00..=0xDFFF).contains(&second_code) {
            return 0;
        }
        (
            0x10000 + (((first & 0x3FF) << 10) | (second_code & 0x3FF)),
            12,
        )
    } else {
        (first, 6)
    };
    match char::from_u32(codepoint) {
        Some(ch) => {
            let mut buf = [0u8; 4];
            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
            seq_len
        }
        None => 0,
    }
}

/// `cJSON_ParseWithLength` (default opts: not requiring null termination).
pub fn parse(input: &[u8]) -> Option<Box<CJson>> {
    if input.is_empty() {
        return None;
    }
    let mut buffer = ParseBuffer {
        content: input,
        length: input.len(),
        offset: 0,
        depth: 0,
    };
    let mut item = Box::new(CJson::new());
    buffer.skip_utf8_bom();
    buffer.skip_whitespace();
    if buffer.parse_value(&mut item) {
        Some(item)
    } else {
        None
    }
}

/* ---------- print ---------- */

fn print_number(item: &CJson, out: &mut Vec<u8>) {
    let d = item.valuedouble;
    if d.is_nan() || d.is_infinite() {
        out.extend_from_slice(b"null");
    } else if d == item.valueint as f64 {
        out.extend_from_slice(item.valueint.to_string().as_bytes());
    } else {
        // Non-integer doubles take cJSON's %1.15g/%1.17g path; matching its byte
        // output is a deferred float-fidelity sub-stage. The bounded corpus is
        // integers, so this branch is not exercised by the golden contract.
        out.extend_from_slice(format!("{d}").as_bytes());
    }
}

fn print_string_ptr(s: Option<&[u8]>, out: &mut Vec<u8>) {
    let input = match s {
        None => {
            out.extend_from_slice(b"\"\"");
            return;
        }
        Some(b) => b,
    };
    out.push(b'"');
    for &c in input {
        if c > 31 && c != b'"' && c != b'\\' {
            out.push(c);
        } else {
            out.push(b'\\');
            match c {
                b'\\' => out.push(b'\\'),
                b'"' => out.push(b'"'),
                0x08 => out.push(b'b'),
                0x0c => out.push(b'f'),
                b'\n' => out.push(b'n'),
                b'\r' => out.push(b'r'),
                b'\t' => out.push(b't'),
                _ => out.extend_from_slice(format!("u{c:04x}").as_bytes()),
            }
        }
    }
    out.push(b'"');
}

fn print_value(item: &CJson, out: &mut Vec<u8>, format: bool, depth: usize) {
    match item.type_ & 0xff {
        CJSON_NULL => out.extend_from_slice(b"null"),
        CJSON_FALSE => out.extend_from_slice(b"false"),
        CJSON_TRUE => out.extend_from_slice(b"true"),
        CJSON_NUMBER => print_number(item, out),
        CJSON_RAW => {
            if let Some(raw) = item.valuestring.as_deref() {
                out.extend_from_slice(raw);
            }
        }
        CJSON_STRING => print_string_ptr(item.valuestring.as_deref(), out),
        CJSON_ARRAY => print_array(item, out, format, depth),
        CJSON_OBJECT => print_object(item, out, format, depth),
        _ => {}
    }
}

fn print_array(item: &CJson, out: &mut Vec<u8>, format: bool, depth: usize) {
    out.push(b'[');
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        print_value(node, out, format, depth + 1);
        if node.next.is_some() {
            out.push(b',');
            if format {
                out.push(b' ');
            }
        }
        cur = node.next.as_deref();
    }
    out.push(b']');
}

fn print_object(item: &CJson, out: &mut Vec<u8>, format: bool, depth: usize) {
    let d = depth + 1;
    out.push(b'{');
    if format {
        out.push(b'\n');
    }
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if format {
            for _ in 0..d {
                out.push(b'\t');
            }
        }
        print_string_ptr(node.string.as_deref(), out);
        out.push(b':');
        if format {
            out.push(b'\t');
        }
        print_value(node, out, format, d);
        if node.next.is_some() {
            out.push(b',');
        }
        if format {
            out.push(b'\n');
        }
        cur = node.next.as_deref();
    }
    if format {
        for _ in 0..(d - 1) {
            out.push(b'\t');
        }
    }
    out.push(b'}');
}

/// `cJSON_PrintUnformatted`.
pub fn print_unformatted(item: &CJson) -> Vec<u8> {
    let mut out = Vec::new();
    print_value(item, &mut out, false, 0);
    out
}

/// `cJSON_Print` (formatted).
pub fn print_formatted(item: &CJson) -> Vec<u8> {
    let mut out = Vec::new();
    print_value(item, &mut out, true, 0);
    out
}

/* ---------- inspect (canonical descriptor via the getter API) ---------- */

fn json_escape(s: &[u8], out: &mut Vec<u8>) {
    out.push(b'"');
    for &c in s {
        match c {
            b'"' => out.extend_from_slice(b"\\\""),
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\r' => out.extend_from_slice(b"\\r"),
            b'\t' => out.extend_from_slice(b"\\t"),
            0..=0x1f => out.extend_from_slice(format!("\\u{c:04x}").as_bytes()),
            _ => out.push(c),
        }
    }
    out.push(b'"');
}

fn describe(item: &CJson, out: &mut Vec<u8>) {
    if is_null(item) {
        out.extend_from_slice(b"{\"t\":\"null\"}");
    } else if is_bool(item) {
        out.extend_from_slice(b"{\"t\":\"bool\",\"v\":");
        out.extend_from_slice(if is_true(item) { b"true" } else { b"false" });
        out.push(b'}');
    } else if is_number(item) {
        out.extend_from_slice(
            format!(
                "{{\"t\":\"num\",\"i\":{},\"bits\":{}}}",
                item.valueint,
                item.valuedouble.to_bits()
            )
            .as_bytes(),
        );
    } else if is_string(item) {
        out.extend_from_slice(b"{\"t\":\"str\",\"v\":");
        json_escape(get_string_value(item).unwrap_or(b""), out);
        out.push(b'}');
    } else if is_array(item) {
        let n = get_array_size(item);
        out.extend_from_slice(format!("{{\"t\":\"arr\",\"n\":{n},\"items\":[").as_bytes());
        for i in 0..n {
            if i > 0 {
                out.push(b',');
            }
            describe(get_array_item(item, i).expect("index < size"), out);
        }
        out.extend_from_slice(b"]}");
    } else if is_object(item) {
        let n = get_array_size(item);
        out.extend_from_slice(format!("{{\"t\":\"obj\",\"n\":{n},\"members\":[").as_bytes());
        for i in 0..n {
            if i > 0 {
                out.push(b',');
            }
            let child = get_array_item(item, i).expect("index < size");
            out.extend_from_slice(b"{\"k\":");
            json_escape(child.string.as_deref().unwrap_or(b""), out);
            out.extend_from_slice(b",\"v\":");
            describe(child, out);
            out.push(b'}');
        }
        out.extend_from_slice(b"]}");
    } else {
        out.extend_from_slice(b"{\"t\":\"invalid\"}");
    }
}

/// Canonical tree descriptor built from the public getter API, matching the C
/// golden runner's `inspect` oracle byte-for-byte (numbers carry valueint plus
/// the IEEE-754 bits of valuedouble).
pub fn inspect(item: &CJson) -> Vec<u8> {
    let mut out = Vec::new();
    describe(item, &mut out);
    out
}
