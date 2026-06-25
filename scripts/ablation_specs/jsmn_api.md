Expose these public items from the crate root (`src/lib.rs`):

```rust
// jsmn token type tags (bit flags) and error codes — values matter:
pub const JSMN_UNDEFINED: i32 = 0;
pub const JSMN_OBJECT: i32 = 1;
pub const JSMN_ARRAY: i32 = 2;
pub const JSMN_STRING: i32 = 4;
pub const JSMN_PRIMITIVE: i32 = 8;
pub const JSMN_ERROR_NOMEM: i32 = -1;
pub const JSMN_ERROR_INVAL: i32 = -2;
pub const JSMN_ERROR_PART: i32 = -3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Token {
    pub ttype: i32,  // one of the JSMN_* type tags above
    pub start: i32,  // byte offset of token start (-1 if unset)
    pub end: i32,    // byte offset one past token end (-1 if unset)
    pub size: i32,   // number of child tokens
}

/// Parse JSON bytes `js` with a token capacity of `cap`.
/// A negative `cap` means count-only mode (jsmn's `tokens == NULL`): return the
/// token count but produce no tokens.
/// Returns `(result, tokens)` where `result` is the number of tokens parsed, or a
/// negative `JSMN_ERROR_*` code; `tokens` is the parsed tokens on success (length
/// == result) and empty otherwise.
pub fn parse_json(js: &[u8], cap: i32) -> (i32, Vec<Token>);
```

Behaviour notes you must derive from your source of truth (do not assume a
particular JSON dialect — match what your material specifies): token typing,
object/array/string/primitive handling, the count-only mode, capacity exhaustion
(NOMEM), partial input (PART), and invalid input (INVAL).
