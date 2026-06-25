Expose from the crate root a module `split` containing:

```rust
/// Split a SQL string into individual statements (the `sqlparse.split` behavior).
/// Each returned statement is the statement's text with leading/trailing
/// whitespace removed (Python `str(stmt).strip()`); empty results are dropped.
/// If `strip_semicolon` is true, a trailing ';' is removed from each statement
/// (before the strip). Splitting must respect string/quote/comment/parenthesis
/// nesting and SQL block constructs (e.g. BEGIN...END, CASE...END, DECLARE)
/// rather than naive ';' split.
pub fn split(sql: &str, strip_semicolon: bool) -> Vec<String>;
```

So `arm::split::split(sql, strip)` must compile and run. The whitespace-strip and
semicolon contract above is the definition of this public API (given equally to
both arms); derive the *internal* splitting/tokenizing behaviour (block/keyword
handling, nesting) from your source of truth. You may build whatever internal
lexer/tokenizer/state-machine you need; only the `split` function above is graded.
