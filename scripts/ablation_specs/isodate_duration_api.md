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

**Field normalization (part of the result shape, not a behavioural hint).** The
`days`/`seconds`/`microseconds` triple is always carried in normalized form:
`0 <= microseconds < 1_000_000` and `0 <= seconds < 86_400`, with `days` holding
the sign and any whole-day carry. A negative fixed duration is therefore
represented with a negative `days` and non-negative `seconds`/`microseconds`
(e.g. minus one hour is `days: -1, seconds: 82_800, microseconds: 0`), never as
`days: 0, seconds: -3_600`. The same normalization applies to the
`days`/`seconds`/`microseconds` of the `Duration` variant; its `years`/`months`
carry their own sign independently.

A regex crate (`fancy-regex`) is pre-provided in `Cargo.toml` (the ISO regexes use
lookahead); use it rather than hand-rolling a regex engine. Do not add other deps.
