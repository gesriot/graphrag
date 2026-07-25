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

## Pre-run corrections (2026-07-25, before any N=3 run)
Found by review of the mini-gate artifacts + the pre-registered dry-prep material
audit. All four are protocol/spec fixes made **before** any arm was run, so no
result is retrofitted; the frozen decisions above are unchanged.

1. **Adequacy scope now matches the scored run.** `isodate_adequacy.json` gated
   the *union* closure (`parse_duration` + `duration_isoformat`, 20 entities)
   while the frozen API spec scopes this run to the parser. The spec is now
   parser-only: closure **16**, **13/13 must-reach**, 0 must-exclude leaked,
   adequate — and the formatter entities (`isostrf:strftime`/`_strfduration`,
   `duration_isoformat`) moved to must-exclude, since packing them would hand the
   graph arm material the scored task does not need. Re-gating the formatter
   separately is only required if the secondary check is ever run.
2. **`arm_raw` was contaminated.** `prep` copied `examples/isodate/PROVENANCE.md`
   into the raw kit — our own experiment note, which names the slice under test
   and spells out the exact `timedelta`-vs-`Duration` type nuance the golden
   measures. Now excluded from the copy and flagged by the leak check.
3. **Kit isolation in the graph arm.** Every pack embedded the absolute path of
   the original source (`/…/examples/isodate/isodates.py`) and a `usage_hint`
   telling the agent to consult "the original source of the listed files" —
   jointly an invitation to break the kit-isolation rule the protocol relies on.
   Packs now carry the bare module name as provenance and no `usage_hint`; the
   leak check fails any kit file containing an absolute repo path.
4. **Underspecified interface (the v1 strip-contract lesson).** The result shape
   did not define `days`/`seconds`/`microseconds` normalization, but the golden
   pins Python's `timedelta` carry (`-PT1H` → `days=-1, seconds=82800`, from
   `timedelta(0) - ret`). That rule lives in the CPython stdlib, so it is
   invisible to **both** arms in their material — 3 of 24 cases would have scored
   a shared guess about `datetime` internals rather than graph-vs-raw. The
   normalization invariant is now stated in the API spec, given identically to
   both arms, exactly as the strip contract was after v1.

## Reproduce (pinned — use these exact commands for all six runs)
`fancy-regex` is pinned to `0.13` (the version the earlier `sqlparse` arms were
given) so the pre-provided-dependency variable is identical across v1 and v3.

```bash
uv run python scripts/ablation.py adequacy --graph byog_isodate \
  --spec scripts/ablation_specs/isodate_adequacy.json          # adequate: true, closure 16
uv run python scripts/ablation.py prep --target isodate_duration --graph byog_isodate \
  --source examples/isodate --closure-root isoduration:parse_duration \
  --dep 'fancy-regex = "0.13"' \
  --api scripts/ablation_specs/isodate_duration_api.md --out /tmp/ablation/isodate
uv run python scripts/ablation.py audit --out /tmp/ablation/isodate --graph byog_isodate \
  --spec scripts/ablation_specs/isodate_adequacy.json          # all six checks, exit 0
# (fill each kit with a cold sub-agent, then score each filled kit:)
uv run python scripts/ablation.py eval --kit /tmp/ablation/isodate/arm_graph \
  --golden-dir examples/isodate/tests/duration \
  --contract-test examples/isodate/tests/duration_contract.rs --crate-name arm
```

`eval` reports `cases_passed`/`cases_total` per run (the `X/24` the result table
needs) plus the failing case ids. A case whose port panics costs one case and is
reported as `"<input> (panic)"`, never the whole score.

## Backup
`packaging.SpecifierSet.contains` (20 modules, static, high spread) — strong, but
its version domain overlaps the earlier `semantic_version` port; kept as backup.
