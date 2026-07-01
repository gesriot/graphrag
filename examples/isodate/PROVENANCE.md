# Provenance — vendored `isodate` (Phase 7 ablation v3 mini-probe)

Step-2 cheap mini-probe candidate for v3 (see `PHASE7_ABLATION.md` Next). Goal: a
target with **genuinely high raw-assembly cost** (slice spread across many small
interdependent modules) that is still **statically adequacy-clean** — the
combination v1/jsonpatch/v2 never hit together.

## Source / license
- `isodate` (github.com/gweis/isodate), vendored `src/isodate/*.py` (10 modules).
- **BSD-3-Clause** (`LICENSE`, verbatim). `version.py` is a vendored-snapshot shim
  (`0+vendored-snapshot`); upstream is build-generated.

## Candidate slice
- Entry: `parse_duration` (+ formatter `duration_isoformat`) from `isoduration.py`.
- **Important type nuance:** `parse_duration` returns a `Duration` when the ISO
  string has years/months, but a `datetime.timedelta` when only fixed components
  (e.g. `PT1H30M` -> `timedelta`). A v3 golden must capture the **type + fields**
  (years/months/days/seconds/microseconds), not a blind string round-trip, or it
  would measure formatter normalization instead of parser parity.

## Mini-probe results
- `byog_isodate` audit: 36 calls, pass_rate 1.0, 0 anomalies/dangling/suspicions.
- Closure from `parse_duration`+`duration_isoformat`: **17 entities across 9
  modules** (isoduration, isodatetime, isodates, isotime, isotzinfo, tzinfo,
  duration, isostrf, isoerror) — reaches the real logic: `Duration`, the regex
  caches + builders (`ISO8601_PERIOD_REGEX`, `DATE_REGEX_CACHE`/`build_date_regexps`,
  `TIME_REGEX_CACHE`/`build_time_regexps`), `parse_date`/`parse_time`/
  `parse_datetime`, `strftime`/`_strfduration`/`_strfdt`, `FixedOffset`.
- **Focus ratio ~0.17** (17 of 98 fn/method/data/class entities): the graph hands
  ~1/6 of the package; the raw arm must trace/assemble across 9 files. High
  raw-assembly cost with real focus advantage (vs humanize's ~2 modules).

## Go/no-go: GO (pre-register v3 on isodate)
- clean closure ✓, spread 9 meaningful modules (>= ~4 guard) ✓, deterministic
  domain, weak model prior, permissive, pure-Python.
- Watch item for the full mini-gate (tractable, not a boundary): `parse_duration`
  constructs `Duration(...)`, but the closure reaches the `Duration` *class*, not
  its `__init__`/fields (classes are excluded from packs as broad spans). Likely a
  small constructor->fields resolver edge, general like the humanize aliased-import
  / data-reference fixes.

## Backup
- `packaging.SpecifierSet.contains` (20 modules, high spread, static) — technically
  strong but its version-domain overlaps the earlier `semantic_version` port,
  weakening freshness; kept as v3 backup.
