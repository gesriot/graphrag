Expose from the crate root a module `split` containing:

```rust
/// Split a SQL string into individual statements (the `sqlparse.split` behavior).
/// `strip_semicolon` controls whether a trailing ';' is removed from each
/// returned statement. Returned statements preserve the original text/whitespace;
/// splitting must respect string/quote/comment/parenthesis nesting and SQL block
/// constructs (e.g. BEGIN...END, CASE...END, DECLARE) rather than naive ';' split.
pub fn split(sql: &str, strip_semicolon: bool) -> Vec<String>;
```

So `arm::split::split(sql, strip)` must compile and run. Derive the exact splitting
behaviour (whitespace handling, semicolon stripping, block/keyword handling) from
your source of truth. You may build whatever internal lexer/tokenizer/state-machine
you need; only the `split` function above is graded.
