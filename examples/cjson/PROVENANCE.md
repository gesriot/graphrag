# Provenance — vendored `cJSON` (Phase 6 C frontend + ownership-slice Rust port)

Third C target for Plan Phase 6 and the first **struct/pointer/ownership-heavy**
one (~3.2k LOC). This checkpoint includes the graph bootstrap (frontend + audit
clean), the golden-before-Rust ownership-slice contract, and the bounded C→Rust
ownership-slice port.

## Source
- Package: `cJSON`, an ultralightweight C JSON parser by Dave Gamble and
  contributors.
- Upstream: https://github.com/DaveGamble/cJSON (vendored `cJSON.c`, `cJSON.h`).
- Vendored verbatim from `master`; exact upstream commit/tag is not recorded in
  the files, the local repository commit pins the reproducible snapshot.

## License — gate step 1 (captured)
- **MIT**. Full text in `LICENSE` (verbatim); the copyright header
  (`Copyright (c) 2009-2017 Dave Gamble and cJSON contributors`) and the MIT
  permission notice are present in both `cJSON.c` and `cJSON.h`.

## Compile metadata
- `compile_commands.json` records the default build: `cc -c -I. cJSON.c`.
- The current extractor is tree-sitter-c only; clang/compile-database semantic
  facts remain a later Phase 6 layer.

## C frontend result — clean on the first pass
Unlike `inih`, cJSON does not fragment function bodies with `#if`/`#endif`, so the
tree-sitter-c extractor parsed all 116 functions without phantom/keyword
misparses. The audit is clean on the first index — the largest and most
pointer-heavy C target so far passes the same rails unchanged.

The bootstrap captures the facts that matter for ownership analysis:
- **Struct graph:** the node struct and the internal buffers are entities. (They
  appear as `typedef` entities because cJSON uses the `typedef struct {..} T;`
  idiom; the typedef name is the captured title.)
- **Recursive ownership/traversal:** `cJSON_Delete`, `cJSON_Compare`, and
  `cJSON_Duplicate_rec` are captured as deterministic self-edges — the recursive
  free/compare/duplicate that define cJSON's tree ownership.
- **Allocation primitives stay observations:** `malloc`/`free`/`realloc`/
  `memcpy`/`memset`/`strlen` are weak observations, never core deterministic
  edges, so heap ownership is visible but not silently promoted.

## Verified graph result (`byog_cjson`, snapshot `20260625-123603-a5400f50`)
The published graph also contains the co-located golden runner
(`tests/parse/runner.c`) as package code, the same way `jsmn`/`inih` do:
- 131 entities, 367 relationships, 131 text units, 125 call observations.
- Entity mix: 121 functions (116 library + 5 runner), 7 typedefs (`cJSON`,
  `cJSON_Hooks`, `cJSON_bool`, `parse_buffer`, `printbuffer`, `internal_hooks`,
  `error`), 3 files (cJSON.c, cJSON.h, runner.c).
- Relationship mix: 239 `calls`, 128 `contains`.
- `audit_call_edges`: 239 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- The **library** subgraph (cJSON.c/cJSON.h) is 125 entities and 188
  deterministic calls; the remaining edges are the runner's own helpers.
- Resolved entry chains: `cJSON_Parse -> cJSON_ParseWithOpts`,
  `cJSON_ParseWithLength -> cJSON_ParseWithLengthOpts`.

## Regression
- `examples/cjson/tests/test_cjson_extract.py` locks the struct graph, the
  ownership-slice API surface, the parse chain, the recursive ownership self-edges
  (`cJSON_Delete`), and that allocation primitives stay observations (scoped to
  the library subgraph, so it is stable against runner changes).
- `examples/cjson/tests/test_cjson_parse_contract.py` recompiles the C golden
  runner, re-derives the contract, and — when the toolchain supports it —
  recompiles under AddressSanitizer to verify the parse+print+delete path is
  leak/double-free clean (skips and records if ASan is unavailable).

## Golden contract (captured before Rust)
- Runner: `tests/parse/runner.c`; goldens under `tests/parse/golden_*.json`.
- **Ownership slice** (`golden_parse.json`, 22 cases): objects, arrays, strings
  with escapes, `\u` unicode → UTF-8, integers incl. zero/negative/max-int32,
  bool, null, nesting, empty containers, top-level scalars, whitespace, duplicate
  keys, and two parse-error inputs. Each case pins:
  - `unformatted`: `cJSON_PrintUnformatted` output (or `__PARSE_ERROR__`),
  - `inspect`: a canonical descriptor built from cJSON's public API and public
    struct fields (`Is*`, `GetArraySize`/`GetArrayItem`, object key walking via
    `string`, `valuestring`/`valueint`); numbers carry `valueint` + the IEEE-754
    bits of `valuedouble`, so number-parse fidelity is checked exactly without
    depending on float *printing*,
  - `formatted`: `cJSON_Print` output, for a few cases.
- **Float-printing fidelity** (`golden_float_print.json`, 30 cases): non-integer
  doubles via the same parse→print path. Covers the `%1.15g` path, the `%1.17g`
  round-trip fallback (e.g. π, 1/3, min-normal, DBL_EPSILON), exponent forms,
  precision boundaries that round under 15 digits, negative zero (prints `0`),
  very large/small magnitudes, and overflow→±inf→`null` (`1e400` / `-1e400`).
  Deliberately omitted: `ENABLE_LOCALES` decimal-point variants (default build
  has locales off); bare JSON `NaN` tokens (parser rejects them — overflow
  exponents cover the print-null path); malformed-number partial `strtod`
  consumption.
- **Platform coupling (honest scope):** both the C oracle and the Rust port call
  the *platform* libc for `%1.15g`/`%1.17g`, so byte-parity between them holds on
  any single machine by construction. The committed golden, however, is a capture
  from one platform (macOS/Darwin). A libc whose `%g` rounding differs would make
  the checked-in expectations fail rather than silently diverge — the Python
  contract re-derives every case from the C runner, so such a platform shows up
  as a test failure and the golden would need regeneration there. Cross-libc
  invariance is **not** claimed or tested.
- ASan over the full combined corpus (all three modes) is leak/double-free clean.

## Rust ownership-slice port (built)
- Port crate: `examples/cjson_rust`.
- Scope: `parse -> inspect tree -> print -> drop/delete` over the captured
  bounded corpus. The Rust side reproduces the C-derived
  unformatted/inspect/formatted oracles.
- Representation: structure-preserving `CJson` node with a cJSON-style type tag,
  `child`/`next` `Box`-owned singly linked list, `valuestring`, `valueint`,
  `valuedouble`, and object key `string`. It deliberately avoids an idiomatic
  enum so the milestone exercises C tree ownership rather than hiding it behind
  a different representation.
- Ownership: `Drop` mirrors `cJSON_Delete` by iterating the sibling `next` chain
  while child trees drop recursively. The port uses safe Rust and no raw
  pointers; parse failures clean up partially built children through ordinary
  ownership.
- Getter/inspect surface: `Is*`, `GetArraySize`, `GetArrayItem`,
  `GetObjectItem`, and `GetStringValue` equivalents are ported for the
  ownership slice, and the inspect descriptor carries `valueint` plus
  `valuedouble` IEEE-754 bits exactly like the C runner.
- Float printing: `print_number` matches cJSON's two-step `%1.15g` then
  `%1.17g` (with the same relative `compare_double` recovery check) by calling
  libc `snprintf`/`sscanf` — Rust's `Display` for `f64` is a different algorithm
  and does not agree byte-for-byte with the C oracle. Default build (no
  `ENABLE_LOCALES`) keeps the decimal point as `'.'`.
- `port_eval`: graph pass rate 1.0 (239 calls, 125 observations, 0 anomalies,
  0 dangling, 0 semantic suspicions), context packs 3/3 for
  `cJSON_ParseWithLength`, `cJSON_PrintUnformatted`, and `cJSON_Delete`;
  Rust fmt/check/golden_test/run all ok; 52 golden cases (22 ownership + 30
  float-print); `manual_fixes=0`; `OVERALL PASS=True`.
- **Rust hardening (Miri + properties):**
  - **Miri:** `cargo +nightly miri test --test ownership_props` — 8 property
    tests, all pass. Covers parse → walk → print → drop of randomly shaped
    trees, wide sibling lists, deep-nesting rejection, and garbage inputs under
    Miri's aliasing/UB model. **Not covered by Miri:** the libc
    `snprintf`/`sscanf` float-print path. That code is compiled only under
    `cfg(not(miri))`; under Miri a pure-Rust `Display` stand-in is used so
    ownership tests can print numbers without foreign calls. The stand-in is
    **not** golden-faithful and is never linked into a normal build — float
    byte-parity remains a non-Miri, libc check (`parse_contract` is
    `#[ignore]` under Miri for that reason). No ownership UB was found.
  - **Property tests:** `tests/ownership_props.rs` with `proptest` (32 cases
    per randomised property on normal builds, 16 under Miri; seed printed by
    proptest on failure). Invariants are labelled in-file as Phase 5 requires:
    - *deterministic* — public type tags, `Is*` partition, array-size vs
      sibling chain, parse+print+drop no-panic, garbage no-panic
    - *inferred* — printed output is UTF-8 and re-parsable (not byte-identical
      to the input); wide-array drop is iterative-safe
    - *human-approved* — nesting past the default limit (1000) is rejected
  - **Still untested:** custom allocators/hooks, concurrent use, full mutation
    API, Miri over the FFI float path, fuzzing against adversarial multi-MB
    inputs, and cross-libc float golden invariance.
- Deferred: full mutation/builder API, custom hooks/allocators, reference flags,
  `prev` links, `ENABLE_LOCALES` decimal-point printing, and malformed-number
  edge cases that depend on `strtod` partial consumption.

## Vendored whitespace
- `cJSON.h` and `LICENSE` contain upstream whitespace that fails vanilla
  `git diff --check`. The local `.gitattributes` disables only those vendored
  whitespace checks so provenance-preserving bytes can stay verbatim while
  project-authored files remain checked normally.

## Next scope
The ownership-bearing slice and the bounded float-printing fidelity suite are
complete. Remaining cJSON depth (mutation/builder API, custom hooks, locale
decimal points, partial-strtod malformation) is optional; the Phase 6 ownership
+ print-fidelity checkpoint can stand while the project moves to
productization/benchmarking or clang-backed C/C++ semantic extraction.
