//! Structure-preserving Rust port of jsmn (first C→Rust port).
//! Mirrors examples/jsmn/jsmn.h in **default mode**: non-strict, no
//! JSMN_PARENT_LINKS. jsmn is byte-oriented: `start`/`end` are byte offsets into
//! the JSON, so the port operates on `&[u8]` (not chars) exactly like the C code.

// jsmntype_t is a bit-flag enum.
pub const JSMN_UNDEFINED: i32 = 0;
pub const JSMN_OBJECT: i32 = 1;
pub const JSMN_ARRAY: i32 = 2;
pub const JSMN_STRING: i32 = 4;
pub const JSMN_PRIMITIVE: i32 = 8;

// jsmnerr
pub const JSMN_ERROR_NOMEM: i32 = -1;
pub const JSMN_ERROR_INVAL: i32 = -2;
pub const JSMN_ERROR_PART: i32 = -3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Token {
    pub ttype: i32,
    pub start: i32,
    pub end: i32,
    pub size: i32,
}

impl Default for Token {
    fn default() -> Self {
        Token {
            ttype: JSMN_UNDEFINED,
            start: -1,
            end: -1,
            size: 0,
        }
    }
}

struct Parser {
    pos: usize,
    toknext: usize,
    toksuper: i32,
    num_tokens: usize,
    tokens: Vec<Token>,
    collect: bool, // false == jsmn's `tokens == NULL` (count-only)
}

impl Parser {
    fn new(num_tokens: usize, collect: bool) -> Self {
        let tokens = if collect {
            vec![Token::default(); num_tokens]
        } else {
            Vec::new()
        };
        Parser {
            pos: 0,
            toknext: 0,
            toksuper: -1,
            num_tokens,
            tokens,
            collect,
        }
    }

    /// Mirrors jsmn_alloc_token: returns the new token index or None on overflow.
    fn alloc_token(&mut self) -> Option<usize> {
        if self.toknext >= self.num_tokens {
            return None;
        }
        let idx = self.toknext;
        self.toknext += 1;
        self.tokens[idx] = Token::default();
        Some(idx)
    }

    fn parse_primitive(&mut self, js: &[u8]) -> i32 {
        let start = self.pos;
        while self.pos < js.len() && js[self.pos] != 0 {
            match js[self.pos] {
                // non-strict: ':' is also a terminator
                b':' | b'\t' | b'\r' | b'\n' | b' ' | b',' | b']' | b'}' => break,
                _ => {}
            }
            let ch = js[self.pos];
            // jsmn rejects non-printable / non-ASCII bytes inside a primitive.
            if !(32..127).contains(&ch) {
                self.pos = start;
                return JSMN_ERROR_INVAL;
            }
            self.pos += 1;
        }
        // non-strict: reaching end of input is an acceptable primitive end.
        if !self.collect {
            self.pos -= 1;
            return 0;
        }
        match self.alloc_token() {
            None => {
                self.pos = start;
                JSMN_ERROR_NOMEM
            }
            Some(ti) => {
                self.tokens[ti] = Token {
                    ttype: JSMN_PRIMITIVE,
                    start: start as i32,
                    end: self.pos as i32,
                    size: 0,
                };
                self.pos -= 1;
                0
            }
        }
    }

    fn parse_string(&mut self, js: &[u8]) -> i32 {
        let start = self.pos;
        self.pos += 1; // skip opening quote
        while self.pos < js.len() && js[self.pos] != 0 {
            let c = js[self.pos];
            if c == b'"' {
                if !self.collect {
                    return 0;
                }
                return match self.alloc_token() {
                    None => {
                        self.pos = start;
                        JSMN_ERROR_NOMEM
                    }
                    Some(ti) => {
                        self.tokens[ti] = Token {
                            ttype: JSMN_STRING,
                            start: (start + 1) as i32,
                            end: self.pos as i32,
                            size: 0,
                        };
                        0
                    }
                };
            }
            if c == b'\\' && self.pos + 1 < js.len() {
                self.pos += 1;
                match js[self.pos] {
                    b'"' | b'/' | b'\\' | b'b' | b'f' | b'r' | b'n' | b't' => {}
                    b'u' => {
                        self.pos += 1;
                        let mut i = 0;
                        while i < 4 && self.pos < js.len() && js[self.pos] != 0 {
                            let h = js[self.pos];
                            let is_hex = (48..=57).contains(&h)
                                || (65..=70).contains(&h)
                                || (97..=102).contains(&h);
                            if !is_hex {
                                self.pos = start;
                                return JSMN_ERROR_INVAL;
                            }
                            self.pos += 1;
                            i += 1;
                        }
                        self.pos -= 1;
                    }
                    _ => {
                        self.pos = start;
                        return JSMN_ERROR_INVAL;
                    }
                }
            }
            self.pos += 1;
        }
        self.pos = start;
        JSMN_ERROR_PART
    }

    fn parse(&mut self, js: &[u8]) -> i32 {
        let mut count = self.toknext as i32;
        while self.pos < js.len() && js[self.pos] != 0 {
            let c = js[self.pos];
            match c {
                b'{' | b'[' => {
                    count += 1;
                    if self.collect {
                        let ti = match self.alloc_token() {
                            None => return JSMN_ERROR_NOMEM,
                            Some(i) => i,
                        };
                        if self.toksuper != -1 {
                            self.tokens[self.toksuper as usize].size += 1;
                        }
                        self.tokens[ti].ttype = if c == b'{' { JSMN_OBJECT } else { JSMN_ARRAY };
                        self.tokens[ti].start = self.pos as i32;
                        self.toksuper = (self.toknext - 1) as i32;
                    }
                }
                b'}' | b']' => {
                    if self.collect {
                        let type_ = if c == b'}' { JSMN_OBJECT } else { JSMN_ARRAY };
                        let mut i = self.toknext as i64 - 1;
                        while i >= 0 {
                            let t = self.tokens[i as usize];
                            if t.start != -1 && t.end == -1 {
                                if t.ttype != type_ {
                                    return JSMN_ERROR_INVAL;
                                }
                                self.toksuper = -1;
                                self.tokens[i as usize].end = (self.pos + 1) as i32;
                                break;
                            }
                            i -= 1;
                        }
                        if i == -1 {
                            return JSMN_ERROR_INVAL;
                        }
                        while i >= 0 {
                            let t = self.tokens[i as usize];
                            if t.start != -1 && t.end == -1 {
                                self.toksuper = i as i32;
                                break;
                            }
                            i -= 1;
                        }
                    }
                }
                b'"' => {
                    let r = self.parse_string(js);
                    if r < 0 {
                        return r;
                    }
                    count += 1;
                    if self.toksuper != -1 && self.collect {
                        self.tokens[self.toksuper as usize].size += 1;
                    }
                }
                b'\t' | b'\r' | b'\n' | b' ' => {}
                b':' => {
                    self.toksuper = self.toknext as i32 - 1;
                }
                b',' => {
                    if self.collect
                        && self.toksuper != -1
                        && self.tokens[self.toksuper as usize].ttype != JSMN_ARRAY
                        && self.tokens[self.toksuper as usize].ttype != JSMN_OBJECT
                    {
                        let mut i = self.toknext as i64 - 1;
                        while i >= 0 {
                            let t = self.tokens[i as usize];
                            if (t.ttype == JSMN_ARRAY || t.ttype == JSMN_OBJECT)
                                && t.start != -1
                                && t.end == -1
                            {
                                self.toksuper = i as i32;
                                break;
                            }
                            i -= 1;
                        }
                    }
                }
                _ => {
                    // non-strict: every unquoted value is a primitive
                    let r = self.parse_primitive(js);
                    if r < 0 {
                        return r;
                    }
                    count += 1;
                    if self.toksuper != -1 && self.collect {
                        self.tokens[self.toksuper as usize].size += 1;
                    }
                }
            }
            self.pos += 1;
        }

        if self.collect {
            for i in (0..self.toknext).rev() {
                if self.tokens[i].start != -1 && self.tokens[i].end == -1 {
                    return JSMN_ERROR_PART;
                }
            }
        }
        count
    }
}

/// Parse `js` with a token capacity of `cap`. A negative `cap` is jsmn's
/// `tokens == NULL` count-only mode. Returns `(result_code, tokens)` where the
/// token list is `[0..result]` on success and empty otherwise — matching the
/// golden contract derived from the C reference.
pub fn parse_json(js: &[u8], cap: i32) -> (i32, Vec<Token>) {
    if cap < 0 {
        let mut p = Parser::new(0, false);
        (p.parse(js), Vec::new())
    } else {
        let mut p = Parser::new(cap as usize, true);
        let r = p.parse(js);
        let tokens = if r > 0 {
            p.tokens[..r as usize].to_vec()
        } else {
            Vec::new()
        };
        (r, tokens)
    }
}
