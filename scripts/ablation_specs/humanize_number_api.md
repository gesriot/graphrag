Implement these public functions from the crate root (`src/lib.rs`), reproducing
the **default-locale (English)** behavior of the corresponding `humanize.number`
formatters. Derive the exact output rules from your source of truth; only the
signatures below are fixed (they are the interface the hidden contract checks).

```rust
// Thousands grouping. ndigits = None keeps the value's own fractional part;
// Some(n) rounds to n decimal places.
pub fn intcomma(value: f64, ndigits: Option<i64>) -> String;

// Large numbers as words (e.g. "1.2 million"). `format` is the printf-style
// float format applied to the scaled mantissa (default "%.1f").
pub fn intword(value: f64, format: &str) -> String;

// Small integers as words ("four"); >= 10 stays numeric.
pub fn apnumber(value: i64) -> String;

// Ordinal string ("12th", "21st"); `gender` default "male".
pub fn ordinal(value: i64, gender: &str) -> String;

// Number as a vulgar fraction string ("1/2", "1 1/4", "3").
pub fn fractional(value: f64) -> String;

// Scientific notation with superscript exponent ("1.23 x 10³"); `precision`
// default 2 (decimal places of the mantissa).
pub fn scientific(value: f64, precision: i64) -> String;
```

Defaults to assume for the bare calls: `intcomma(v, None)`, `intword(v, "%.1f")`,
`ordinal(v, "male")`, `scientific(v, 2)`. Default locale only: no translation
catalogs; the i18n helpers behave as identity/passthrough.
