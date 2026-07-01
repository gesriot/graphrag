Implement the ISO 8601 **duration parser** from `isodate.parse_duration`,
default behavior (`as_timedelta_if_possible=True`). Derive the exact parsing
rules from your source of truth; only the signature and result shape below are
fixed (the interface the hidden contract checks). Scope: the PARSER only — the
formatter `duration_isoformat` is out of scope for this run.

`parse_duration` returns a fixed `timedelta` when the ISO string has no
years/months (e.g. `PT1H30M`, `P4D`, `P2W`), and a `Duration` (with year/month
components) otherwise (e.g. `P1Y`, `P3Y6M4DT12H30M5S`). Negatives and fractional
components are supported; invalid inputs return an error.

```rust
#[derive(Debug, PartialEq)]
pub enum Parsed {
    // fixed-only durations
    Timedelta { days: i64, seconds: i64, microseconds: i64 },
    // durations with year/month components; days/seconds/microseconds are the
    // NORMALIZED fixed remainder (as Python's Duration.tdelta fields)
    Duration { years: f64, months: f64, days: i64, seconds: i64, microseconds: i64 },
}

pub fn parse_duration(s: &str) -> Result<Parsed, ()>;
```

A regex crate (`fancy-regex`) is pre-provided in `Cargo.toml` (the ISO regexes use
lookahead); use it rather than hand-rolling a regex engine. Do not add other deps.
