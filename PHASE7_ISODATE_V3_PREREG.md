# Phase 7 Ablation v3 — pre-registration (isodate `parse_duration`)

**Written before running.** Locks target, scope, golden descriptor, adequacy
criteria, and go/no-go before results, so v3 cannot be retrofit. Grounded in the
step-2 mini-probe (`examples/isodate/PROVENANCE.md`): `parse_duration` closure =
17 entities across 9 modules, focus ratio ~0.17, audit clean — the high
raw-assembly-cost + statically-adequate target v1/jsonpatch/v2 never hit together.

## Why v3 needs this target
- v1 `sqlparse.split`: efficiency-only, familiar.
- `jsonpatch`: dynamic-dispatch boundary (call graph under-captures).
- v2 `humanize.number`: near-parity, because raw assembly was easy (~2 modules).
The binding lesson: need a slice whose implementation is genuinely spread across
many interdependent modules (high raw-assembly cost) yet statically adequacy-clean.
isodate's `parse_duration` meets both.

## Target / license
- `isodate` (github.com/gweis/isodate), BSD-3-Clause, vendored `src/isodate/*.py`
  (10 modules) + `version.py` shim. Already in `examples/isodate/`.

## Scope (frozen — design decision 1)
- **Primary API under test: `parse_duration(datestring: str) -> timedelta | Duration`**
  (the parser; `as_timedelta_if_possible` stays at its default True).
- **`duration_isoformat` is SECONDARY (formatter only), scored separately.** The
  headline capability metric is parser parity; the formatter is a distinct,
  smaller check so we never blend parser capability with formatter normalization.
- Out of scope: `parse_date`/`parse_time`/`parse_datetime` as *public* entries
  (they are reached internally by the duration parser and stay in the closure, but
  are not separate scored APIs), tz formatting, `Duration` arithmetic API beyond
  what the parser uses, CLI.

## Golden descriptor (frozen — design decision 2)
`parse_duration` returns a `datetime.timedelta` (fixed-only ISO strings, e.g.
`PT1H30M`) or a `Duration` (years/months present). The golden pins an **explicit
oracle descriptor**, never a blind string round-trip:

```json
{
  "input": "P3Y6M4DT12H30M5S",
  "kind": "duration" | "timedelta" | "error",
  "years": <num>, "months": <num>,          // 0 for timedelta
  "days": <int>, "seconds": <int>, "microseconds": <int>,   // for a Duration these
                                                            // are tdelta.days/…
  "total_seconds": <float|null>             // timedelta only (Duration has no
                                            // fixed total due to years/months)
}
```

- For a `timedelta`: `kind="timedelta"`, `years=months=0`, `days/seconds/microseconds`
  = the timedelta's own fields, `total_seconds` set.
- For a `Duration`: `kind="duration"`, `years`/`months` set, `days/seconds/microseconds`
  = `tdelta.days/seconds/microseconds` (Duration delegates fixed fields to `tdelta`
  via `__getattr__`), `total_seconds=null`.
- Parse errors: `kind="error"` (the Rust API returns `Result`/`Err`).
- Corpus dimensions: weeks (`P2W`), fixed-only → timedelta (`PT1H30M`, `P4D`),
  years/months → Duration (`P1Y`, `P3Y6M4DT12H30M5S`), fractional seconds,
  negative sign (`-P1Y`, `-PT1H` → `Duration(0)-ret` / `timedelta(0)-ret`),
  alternative datetime form (`P0003-06-04T12:30:05`), zero, and a few invalids.
- A Python contract test re-derives the descriptor from the vendored oracle to
  keep the golden in sync.

## Adequacy criteria (frozen — design decision 3)
Roots: `isoduration:parse_duration` (+ `isoduration:duration_isoformat` for the
secondary formatter closure).
- **must-reach:** the parser regex data + builders (`isoduration:ISO8601_PERIOD_REGEX`,
  `isodates:DATE_REGEX_CACHE`/`build_date_regexps`, `isotime:TIME_REGEX_CACHE`/
  `build_time_regexps`), `isodatetime:parse_datetime`, `isodates:parse_date`,
  `isotime:parse_time`, `isoerror:ISO8601Error`, and — the mini-probe watch item —
  **`duration:Duration.__init__`** and **`duration:Duration.__sub__`** (negative
  paths do `Duration(0) - ret`). A reached `Duration` *class* alone is NOT
  sufficient (classes are pack-excluded as broad spans).
- If `Duration.__init__`/`__sub__` are unreached, fix with a **general
  constructor→__init__ / used-class-member edge** in the resolver (like the
  humanize aliased-import / data-reference fixes), NOT a broad class pack.
- **must-exclude (no overpack):** `duration:Duration.__mul__`, formatter-only
  constants not used by the parser slice, `isotzinfo`/`tzinfo` formatting entries
  not needed, `version`.
- adequate = all must-reach present AND zero must-exclude leaked.

## Rust API shape (frozen before agent work)
Both arms implement (exact final form fixed at mini-gate from the descriptor):
```rust
pub enum Parsed { Timedelta { days:i64, seconds:i64, micros:i64 },
                  Duration  { years:f64, months:f64, days:i64, seconds:i64, micros:i64 } }
pub fn parse_duration(s: &str) -> Result<Parsed, ()>;
// secondary: pub fn duration_isoformat(...) -> String;
```
A **regex crate is pre-provided to both arms** (per the sqlparse lesson — removes
hand-rolled-regex variance); exact crate (`regex` vs `fancy-regex`) chosen at
mini-gate after checking the ISO regexes for lookbehind/backrefs.

## Pre-registered go/no-go
1. Mini-gate: freeze signatures → golden descriptor → index/audit (pass 1.0,
   0/0/0) → adequacy → dry-prep manual material audit (no broad spans; must-reach
   packs present; no out-of-slice/leak; packed==closure; API spec hint-fair).
2. **N=3 only after adequacy is clean AND the material audit passes.** If adequacy
   is dirty from tractable static edges, fix generally and re-measure; if dirty
   from dynamic/semantic weirdness, record as a boundary and do not force.
3. N=3 per arm, batched (graph×3 → raw×3, never six at once), corrected-spec
   discipline, hidden golden, infra-only invalidation; report all six + medians
   (golden / build attempts / tools / wall), straight reading.

## Backup
`packaging.SpecifierSet.contains` (20 modules, static, high spread) — strong, but
its version domain overlaps the earlier `semantic_version` port; kept as backup.
