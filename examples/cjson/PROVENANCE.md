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
- Runner: `tests/parse/runner.c`; golden: `tests/parse/golden_parse.json`.
- 22 cases over a bounded ownership-bearing corpus (objects, arrays, strings with
  escapes, `\u` unicode → UTF-8, integers incl. zero/negative/max-int32, bool,
  null, nesting, empty containers, top-level scalars, whitespace, duplicate keys,
  and two parse-error inputs), each pinning three oracles:
  - `unformatted`: `cJSON_PrintUnformatted` output (or `__PARSE_ERROR__`),
  - `inspect`: a canonical descriptor built from cJSON's public API and public
    struct fields (`Is*`, `GetArraySize`/`GetArrayItem`, object key walking via
    `string`, `valuestring`/`valueint`); numbers carry `valueint` + the IEEE-754
    bits of `valuedouble`, so number-parse fidelity is checked exactly without
    depending on float *printing*,
  - `formatted`: `cJSON_Print` output, for a few cases.
- Float-printing edge cases (NaN/inf/exponent/precision) are deferred to a later
  sub-stage; the primary corpus uses integers so the print oracle stays well
  defined without porting printf/double quirks.
- ASan over the full corpus (all three modes) is leak/double-free clean.

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
- `port_eval`: graph pass rate 1.0 (239 calls, 125 observations, 0 anomalies,
  0 dangling, 0 semantic suspicions), context packs 3/3 for
  `cJSON_ParseWithLength`, `cJSON_PrintUnformatted`, and `cJSON_Delete`;
  Rust fmt/check/golden_test/run all ok; 22 golden cases; `manual_fixes=0`;
  `OVERALL PASS=True`.
- Deferred: full mutation/builder API, custom hooks/allocators, reference flags,
  `prev` links, and non-integer float-printing fidelity (`%1.15g`/`%1.17g`,
  NaN/inf/exponent/precision). The current integer corpus exercises cJSON's
  exact `%d` print path; malformed-number edge cases that depend on `strtod`
  partial consumption are also outside this first slice.

## Vendored whitespace
- `cJSON.h` and `LICENSE` contain upstream whitespace that fails vanilla
  `git diff --check`. The local `.gitattributes` disables only those vendored
  whitespace checks so provenance-preserving bytes can stay verbatim while
  project-authored files remain checked normally.

## Next scope
The ownership-bearing slice is complete. The natural next cJSON sub-stage, if we
stay inside this target, is a bounded float-printing fidelity suite for the
`%1.15g`/`%1.17g` paths. Otherwise this checkpoint can stand as the Phase 6
ownership milestone while the project moves to productization/benchmarking or
clang-backed C/C++ semantic extraction.
