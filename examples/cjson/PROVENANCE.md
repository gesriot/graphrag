# Provenance — vendored `cJSON` (Phase 6 C frontend + ownership-slice Rust port)

Third C target for Plan Phase 6 and the first **struct/pointer/ownership-heavy**
one (~3.2k LOC). This checkpoint includes the graph bootstrap (frontend + audit
clean), the golden-before-Rust ownership/mutation contract, and the bounded
C→Rust ownership + owned-builder port.

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

## Closed Rust-port scope (2026-07-26)

This port is closed at its current safe, exclusive-`Box` representation. It is
a typed Rust adaptation of the vendored cJSON API, not a drop-in C ABI or an
FFI replacement. Use `API_SURFACE_AUDIT.md` to decide a specific call: its
header-derived table is authoritative and fails closed if `cJSON.h` changes.

**Covered.** The current header has **78 functions** and **23 public
constants/macros/limits/types**. Of those functions, **68 are covered** by
safe Rust equivalents: parsing/printing, getters and predicates, constructors,
owned builders and structural mutation, duplicate/compare, parse/print options,
setters, minify, and node-address detach/replace. The latter two use a
compared-never-dereferenced `*const CJson` identity token; they do not require
an aliasing redesign. **59 golden cases** are re-derived from vendored C, and
the C/Rust safe trace is byte-compared under the C-oracle test. C ownership
traces run under ASan when the toolchain supports it; the **11 Rust ownership
properties** run under Miri in the full gate. The end-to-end `port_eval` gate is
**59 golden / 0 manual fixes / OVERALL PASS**.

**Deliberately refused.** **6 are genuinely blocked by the
exclusive `Box` representation** for the exact C behavior:
`cJSON_CreateStringReference`, `cJSON_CreateObjectReference`,
`cJSON_CreateArrayReference`, `cJSON_AddItemToObjectCS`,
`cJSON_AddItemReferenceToArray`, and `cJSON_AddItemReferenceToObject`.
Each has both a reached C trace that observes a later source mutation and a
checked-in Rust candidate whose primary `E0502` span is required to fall on
that trace mutation. The candidates prove that ordinary safe borrowing does not
express this behavior; they do not claim no Rust representation can. Closing
them means shared mutable bytes (`Rc<RefCell<Vec<u8>>>`) or shared child nodes/
an arena, changing current signatures, traversal, mutation, parsing/printing,
and `Drop`. **4 are excluded for C process-global allocator/error state
rather than ownership**: `cJSON_InitHooks`, `cJSON_GetErrorPtr`,
`cJSON_malloc`, and `cJSON_free`. They need an explicit allocator/global
error policy, not another tree helper.

**Do not assume.** This port does not preserve C pointer aliasing, C global
allocator/error state, the cJSON ABI, or a mutable borrowed-key/string API. It
also does not claim locale-enabled decimal behavior, exhaustive malformed
`strtod` behavior, cross-libc float-golden invariance, concurrency, or
unbounded fuzz coverage. The non-function `prev`, `cJSON_IsReference`, and
`cJSON_StringIsConst` boundaries follow from the same missing aliasing model.

**Reproduce.** From a clean checkout run:

```bash
examples/cjson/tools/check_port.sh --full
```

It runs the header audit, compiler-rejection location checks, C oracle/ASan,
Cargo tests, `port_eval`, and Miri. The default `--quick` mode omits only
Miri; unavailable `rustc`, C/ASan, or nightly-Miri tooling is printed as an
explicit skip and the final status says `PASS WITH SKIPS`.

## Compile metadata
- `compile_commands.json` records the default build: `cc -c -I. cJSON.c`.
- The published extractor path is tree-sitter-c only. Two **optional**,
  independently selectable compiler overlays (both default **off**) can extend
  it from `compile_commands.json` using the compiler named by each entry:
  - `--compiler-dependencies`: flattened TU `depends_on` edges
    (`translation_unit_dependency`) via `compiler -M` + package-path filtering
    (may include transitive headers).
  - `--compiler-includes`: direct `includes` edges
    (`configured_direct_include`) via `compiler -E -H` hierarchy reconstruction
    (including file → only its direct includes; not flattened).
  Neither is clang AST types, multi-config coverage, or production C/C++
  completeness. `-M`/`-H` are GNU/Clang-specific. When either flag is on,
  missing `compile_commands.json` or a broken/unsupported compiler fails
  explicitly; wrappers, response files, and MSVC are unsupported. The default
  off path keeps published graph counts unchanged.
- **Clang AST function-definition audit (standalone diagnostic):**
  `scripts/c_clang_ast_audit.py --package examples/cjson` runs the recorded
  Clang from `compile_commands.json` with
  `-fsyntax-only -Xclang -ast-dump=json` and compares package-local function
  *definitions* to tree-sitter-c entities. Expected shape for this package:
  **113** library definitions in `cJSON.c` match with Clang `qualType`
  metadata; **19** golden `tests/parse/runner.c` functions are
  `out_of_compile_db_scope` (not false misses); **3** MSVC-only static helpers
  (`internal_malloc` / `internal_free` / `internal_realloc`) remain
  `tree_sitter_only` with preprocessor unknown evidence. Clang only;
  GCC/MSVC/wrappers fail closed.
- **Optional `--clang-signatures` graph fields (default off):**
  `scripts/index_c.py --clang-signatures` attaches the audit’s **matched +
  line-confirmed** signature metadata onto existing function entities only
  (entity/relationship counts unchanged). Expected for cJSON when enabled:
  **113** signature facts; the 3 `tree_sitter_only` and 19 out-of-scope rows
  stay residual without invented signatures. Unclean residuals
  (`clang_only` / `ambiguous` / macro multi-file locations) fail the overlay
  explicitly. This is not full type resolution, ABI verification, multi-config
  coverage, or C++ support.
- **Clang AST call-site audit (standalone diagnostic):**
  `scripts/c_clang_call_audit.py --package examples/cjson` compares
  package-local `CallExpr` sites (callee subtree only) to tree-sitter `calls`
  edges. Direct internal matches require a `DeclRefExpr` → `FunctionDecl` with
  unambiguous package definition/entity mapping; external and indirect
  (parm/var/member function-pointer) calls stay observations — not points-to
  analysis. Physical sites use Clang/tree-sitter byte offsets first and exact
  line/normalized-column only when an offset is unavailable; column-only
  matching is forbidden. Nested calls produced by one macro expansion remain
  a multiset instead of being collapsed at the shared expansion offset.
  Measured on this host under the default compile DB (counts may move with
  toolchain/config): matched_internal=188, tree_sitter_only_internal=0,
  out_of_compile_db_scope=307 (runner-dominated), external_direct=71,
  indirect=26, ambiguous=0, clang_only_internal=0. The tree-sitter accounting
  is complete (188 + 0 + 307 = 495 calls).
- **Optional `--clang-calls` relationship fields (default off):**
  `scripts/index_c.py --clang-calls` attaches the audit’s **matched_internal**
  rows as `clang_call_*` metadata onto existing tree-sitter `calls`
  relationships only (relationship count/IDs/endpoints/types unchanged; base
  `confidence=0.9` / `extractor=tree-sitter-c` unchanged). Attachment requires
  one-to-one exact evidence: caller/target titles + package-relative path +
  exact `tree_sitter_span` + exact byte offset. Expected for cJSON when
  enabled: **188** call facts; unconfirmed/out-of-scope edges stay residual
  without invented metadata. Fail-closed residuals
  (`clang_only_internal` / `ambiguous` / macro multi-file locations /
  `covered_by_noninternal_clang_observation`) and inconsistent compiler,
  digest, or compile-entry provenance abort the overlay before mutation. Allowed
  residuals (`tree_sitter_only_internal`, `out_of_compile_db_scope`,
  `external_direct`, `indirect`) are manifest-only. This is not points-to
  analysis, macro-complete call proof, multi-config coverage, C++, or ABI
  verification. `clang_call_confidence=1.0` is relative only to the recorded
  Clang + `compile_commands.json` configuration.
- **Shared AST capture (execution only):** enabling `--clang-signatures` and
  `--clang-calls` together uses one in-memory capture for both overlays (one
  AST dump per compile entry for this package’s single-entry compile DB). No
  disk AST cache; standalone audit CLIs remain available; confidence
  boundaries and independent manifest blocks are unchanged.
- **C symbol identity (tree-sitter extractor, kind-aware titles):** within one
  module key, when `function` / `struct` / `enum` / `typedef` share a bare
  name, every colliding kind is titled `module_key:entity_kind:name` (no
  silent title-only winner). Non-colliding symbols keep `module_key:name`.
  **Source-derived full-graph counts after this correction:** 148 entities /
  640 relationships / **495** calls (call IDs and function endpoints
  unchanged). Intentional delta vs historical published full-graph counts
  (145 / 637 / 495): three named complete struct entities
  (`cJSON:struct:cJSON`, `cJSON:struct:cJSON_Hooks`,
  `cJSON:struct:internal_hooks`) plus matching `contains` edges; colliding
  typedefs use `cJSON:typedef:…` titles. Historical published snapshot rows
  in the table below remain historical.
- **Clang AST type-declaration audit (diagnostic only, no graph mutation):**
  `scripts/c_clang_type_audit.py --package examples/cjson` compares package-
  local named complete structs, named complete enums, and typedefs to
  tree-sitter `struct` / `enum` / `typedef` entities. Identity is
  kind + path + name + exact start line/column (never bare title). Measured
  on this host under the default compile DB after the kind-aware extractor
  fix: matched=10 (7 typedefs + 3 named complete structs), clang_only=0,
  anonymous_declarations=3, outside_package_declarations=212 (each row keeps
  its resolved outside source path; different headers are not collapsed),
  tree_sitter_only=0, ambiguous=0, macro_location_unsupported=0,
  unsupported_declarations=0, out_of_compile_db_scope=0. **No** `uses_type`
  edges, Clang type fields, `index_c` flag, or overlay.
  `--fail-on-mismatch` fails only on tree_sitter_only / clang_only /
  ambiguous / macro residuals — not on anonymous/outside-package alone.
  Not layout/ABI, type-use analysis, points-to, C++, or multi-config.

## C frontend result — clean on the first pass
Unlike `inih`, cJSON does not fragment function bodies with `#if`/`#endif`, so the
tree-sitter-c extractor parsed all 116 functions without phantom/keyword
misparses. The audit is clean on the first index — the largest and most
pointer-heavy C target so far passes the same rails unchanged.

The bootstrap captures the facts that matter for ownership analysis:
- **Struct graph:** named complete structs and typedefs are both entities when
  they share a C name (kind-qualified titles). Anonymous-struct typedefs
  (`error` / `parse_buffer` / `printbuffer`) remain typedef-only.
- **Recursive ownership/traversal:** `cJSON_Delete`, `cJSON_Compare`, and
  `cJSON_Duplicate_rec` are captured as deterministic self-edges — the recursive
  free/compare/duplicate that define cJSON's tree ownership.
- **Allocation primitives stay observations:** `malloc`/`free`/`realloc`/
  `memcpy`/`memset`/`strlen` are weak observations, never core deterministic
  edges, so heap ownership is visible but not silently promoted.

## Verified graph result (`byog_cjson`, snapshot `20260726-040744-fcee0a70`)

Two figures matter; do not mix them:

| scope | entities | relationships | calls | observations | when to quote |
|---|---:|---:|---:|---:|---|
| **Full graph (source-derived, kind-aware titles)** — library + golden runner | **148** | **640** | **495** | 144 | live extract/`build_c_byog`, type audit, post-identity-fix claims |
| **Full graph (historical published snapshot `20260726-040744-fcee0a70`)** | 145 | 637 | **495** | 144 | frozen snapshot identity; do not rewrite |
| **Library subgraph** — `cJSON.c` / `cJSON.h` only (cJSON→cJSON calls) | — | — | **188** | — | ownership/API claims about the ported library itself |
| **Pre-mutation-runner snapshot** (historical; e.g. `20260625-123603` / provenance-stamped `20260726-030425`) | 131 | 367 | **239** | 125 | bootstrap and pre-mutation evidence; ownership slice before mutation traces landed |

The source-derived full graph co-indexes `tests/parse/runner.c` the same way `jsmn`/`inih` do, now including the mutation-scenario helpers:
- Entity mix: 135 functions (116 library + 19 runner), 10 type entities
  (7 typedefs + 3 named complete structs with kind-qualified titles where they
  collide with same-name typedefs), 3 files (cJSON.c, cJSON.h, runner.c).
- Relationship mix: 495 `calls`, 145 `contains` (historical published snapshot
  had 142 `contains` before the three struct entities).
- `audit_call_edges`: 495 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- Of the 495 calls, 188 are library-internal (cJSON→cJSON) and 307 have a
  `runner:` source; the +256 call delta from the pre-mutation snapshot is
  entirely runner-source edges (library keys identical).
- Resolved entry chains: `cJSON_Parse -> cJSON_ParseWithOpts`,
  `cJSON_ParseWithLength -> cJSON_ParseWithLengthOpts`.
- Preprocessor provenance: 6/145 entities, **0/495** trusted call edges, 71/144
  observations flagged. Library trusted calls remain **0/188** flagged — the
  port rests on unconditional internal library calls; the mutation runner did
  not introduce preprocessor-dependent call edges.
- Branch liveness (2026-07-26): under `compile_commands.json` (no `-D`) plus
  simple header defaults, each overlapping non-guard region is labelled
  `live` / `dead` / `unknown` on entities as weak provenance
  (`preprocessor_branches`). Example: `cJSON:get_decimal_point` has
  `ifdef(ENABLE_LOCALES)=dead` and its `else=live` (returns `'.'`).
  **Published graphs default to `eval_mode=no_compiler`** (host-independent:
  platform macros stay **unknown**; labels are byte-identical across machines).
  Local analysis may opt into `compiler -E -dM` builtins plus include-provided
  macros (`eval_mode=compiler_builtins`, basis like `builtin:__GNUC__='4'`) via
  `index_c.py --compiler-builtins`. Every stamp records
  `preprocessor_eval_mode` and `preprocessor_macro_seed_digest`; the snapshot
  `manifest.json` carries a `preprocessor_liveness` block
  (`eval_mode`, `compiler_id` / `compiler_version` when host-specific,
  `macro_seed_digest`, `host_independent`). Re-stamping when the recorded
  digest/mode does not match this host refuses unless
  `--allow-toolchain-drift` is set. Context packs surface
  `preprocessor.branch_liveness` and the digest. A compiler-oracle check
  (`--vs-compiler`) locks agreement on scored regions (typically run under
  `compiler_builtins` for denser coverage).
- What counts as a "header default" is deliberately narrow, because the first
  implementation was not: a `#define` is a default only when it sits outside
  every conditional (include guards excepted) or forms the `#ifndef X` /
  `#define X v` idiom, and it is applied in source order within its own file.
  Harvesting nested defines made `#define __WINDOWS__` — which lives inside
  `#if !defined(__WINDOWS__) && (defined(WIN32) || …)` — read as set on a POSIX
  build, and made every `#ifndef INI_*` default region in `inih` read as dead
  because the `#define` it guards had already been scraped. Liveness also
  propagates: a branch inside a dead region is dead, and inside an undecidable
  region it is undecidable however evaluable its own condition is.
- Checked against the real compiler (`scripts/c_preprocessor.py --vs-compiler`,
  test `examples/cjson/tests/test_c_liveness_vs_compiler.py`). The oracle is
  `clang -E` with the `compile_commands.json` flags — line survival via the
  `# linenum "file"` markers, plus `-E -dM` for directive-only regions, whose
  `#define` leaves no output line to count. **0 disagreements** on all three
  compile-database packages, but the count that matters is how much is actually
  checked:

  | package | regions | mode | scored (line / macro) | vacuous | unknown |
  |---|---:|---|---:|---:|---:|
  | `cjson` | 34 | `no_compiler` | 7 (5 / 2) | 8 | 19 (55.9%) |
  | `cjson` | 34 | `compiler_builtins` | 25 (16 / 9) | 9 | 0 (0.0%) |
  | `inih` | 52 | `no_compiler` | 39 (23 / 16) | 4 | 9 (17.3%) |
  | `inih` | 52 | `compiler_builtins` | 44 (25 / 19) | 8 | 0 (0.0%) |
  | `jsmn` | 20 | `no_compiler` | 18 (16 / 2) | 0 | 2 (10.0%) |
  | `jsmn` | 20 | `compiler_builtins` | 20 (18 / 2) | 0 | 0 (0.0%) |

  *Vacuous* is a separate column on purpose. Directive-only regions that define
  nothing attributable — function-like macros, and names several exclusive
  branches define to the same replacement (`INI_API`, the `CJSON_PUBLIC` arms) —
  cannot be judged in either direction, and counting them as agreements made
  cJSON read 15/15 when 7 regions were really checked. *Unknown* is what
  `no_compiler` mode cannot decide without the toolchain's macro environment:
  on cJSON it is dominated by `_MSC_VER`, `__GNUC__`, `_WIN32`, `__WINDOWS__`,
  `__clang__` and `__cplusplus`.

  Two macro sources feed `compiler_builtins` mode, and both are needed. The
  empty-TU probe (`-E -dM -x c -`) supplies the toolchain's own macros. It does
  **not** supply what the translation unit gets from its `#include`s, and
  treating those as undefined is not a harmless gap: it labelled `#ifndef isinf`,
  `#ifndef isnan` and `#ifndef NAN` in `cJSON.c` *live* when `math.h` had
  already defined all three, so those blocks never run. The labeller therefore
  also reads the real compile command's `-E -dM` table, attributing a name to an
  include only when no package `#define` of it matches the final replacement —
  which keeps the inference non-circular, since our own macros can never make
  themselves look external. Scoring, not excusing, is the rule here: an earlier
  revision detected exactly this mismatch and filed it as an unscoreable "model
  gap", which hid the three wrong labels behind a note.

**History of this reindex (2026-07-26):** preprocessor labels were first stamped
in place on the 131/239 snapshot so a provenance commit would not fold runner
growth into a label-only change. A separate, deliberate reindex then published
the mutation runner into `byog_cjson` after confirming the library subgraph was
unchanged (125 entities / 188 cJSON→cJSON calls, zero call-key or confidence
diffs).

## Regression
- `examples/cjson/tests/test_cjson_extract.py` locks the struct graph, the
  ownership-slice API surface, the parse chain, the recursive ownership self-edges
  (`cJSON_Delete`), and that allocation primitives stay observations (scoped to
  the library subgraph, so it is stable against runner changes).
- `examples/cjson/tests/test_cjson_parse_contract.py` recompiles the C golden
  runner, re-derives the contract, and — when the toolchain supports it —
  recompiles under AddressSanitizer to verify the parse+print+delete and
  mutation ownership paths are leak/double-free clean (skips and records if
  ASan is unavailable).

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
- **Owned builder/mutation and structural API** (`golden_mutation.json`, 7 fixed
  scenarios):
  the runner's `mutation` mode reads a *named scenario*, not JSON. This avoids
  adding a second, unverified script parser to the oracle: each operation
  sequence is compiled into `runner.c`, runs directly against vendored cJSON,
  and has a C-derived canonical trace of every intermediate tree. The scenarios
  cover owned scalar/container constructors (`Null`, `True`, `False`, `Bool`,
  `Number`, `String`, `Raw`, `Array`, `Object`), typed `Int`/`Float`/`Double`/
  `String` arrays, `AddItemToArray`/`AddItemToObject`, array and object
  detach/delete/replace, and case-sensitive versus ASCII-case-insensitive object
  lookup. The detach traces record the returned caller-owned item *and* the
  parent after detachment; the scenario explicitly deletes the returned item.
  Insert is captured at the front, middle, end, beyond the end (where the C
  oracle appends), and a negative index (where it fails and leaves the incoming
  item caller-owned). Recursive and non-recursive `Duplicate`, `Compare`
  key-case and member-order behavior, plus `GetNumberValue` / `GetStringValue`
  on number and string nodes are also captured. The duplicate trace mutates and
  deletes the source while retaining the recursive copy. Thus the oracle
  exercises the claims that add/insert transfer ownership in, detach transfers
  it back, delete frees in place, replace frees the old item, and recursive
  duplication owns no shared child or string storage.
- **Platform coupling (honest scope):** both the C oracle and the Rust port call
  the *platform* libc for `%1.15g`/`%1.17g`, so byte-parity between them holds on
  any single machine by construction. The committed golden, however, is a capture
  from one platform (macOS/Darwin). A libc whose `%g` rounding differs would make
  the checked-in expectations fail rather than silently diverge — the Python
  contract re-derives every case from the C runner, so such a platform shows up
  as a test failure and the golden would need regeneration there. Cross-libc
  invariance is **not** claimed or tested.
- ASan over the 52 parse/print cases (all three modes) and all 7 mutation
  scenarios is leak/double-free clean.

## Rust ownership + owned-builder port (built)
- Port crate: `examples/cjson_rust`.
- Scope: `parse -> inspect tree -> print -> drop/delete` plus the captured
  owned builders, typed arrays, add/insert/detach/delete/replace operations,
  recursive/non-recursive duplication, structural comparison, value accessors,
  parse-end options, buffered/preallocated printing, minification, type/object
  predicates, object-construction helpers, and setters. The Rust side
  reproduces the C-derived unformatted/inspect/formatted and mutation-trace
  oracles (59 golden cases: 22 ownership + 30 float-print + 7 mutation).
- Representation: structure-preserving `CJson` node with a cJSON-style type tag,
  `child`/`next` `Box`-owned singly linked list, `valuestring`, `valueint`,
  `valuedouble`, and object key `string`. It deliberately avoids an idiomatic
  enum so the milestone exercises C tree ownership rather than hiding it behind
  a different representation.
- Ownership: `Drop` mirrors `cJSON_Delete` by iterating the sibling `next` chain
  while child trees drop recursively. The port uses safe Rust and no raw
  pointers; parse failures clean up partially built children through ordinary
  ownership. Builders return `Box<CJson>`; add consumes that box, detach returns
  it to the caller with `next` cleared, delete drops it, and replace drops the
  old item. Insert consumes its box and inserts before an index or appends past
  the end; a recursive duplicate creates independent boxes for all descendants.
  A failed Rust replace returns its incoming box in `Err`, preserving the C rule
  that a failed replacement leaves it caller-owned.
- Getter/inspect surface: `Is*`, `GetArraySize`, `GetArrayItem`,
  `GetObjectItem`, `GetStringValue`, and `GetNumberValue` equivalents are
  ported for the ownership slice (`GetNumberValue` returns NaN on a non-number,
  as captured from C), and the inspect descriptor carries `valueint` plus
  `valuedouble` IEEE-754 bits exactly like the C runner.
- Float printing: `print_number` matches cJSON's two-step `%1.15g` then
  `%1.17g` (with the same relative `compare_double` recovery check) by calling
  libc `snprintf`/`sscanf` — Rust's `Display` for `f64` is a different algorithm
  and does not agree byte-for-byte with the C oracle. Default build (no
  `ENABLE_LOCALES`) keeps the decimal point as `'.'`.
- `port_eval`: graph pass rate 1.0 (495 calls full graph / 188 library-only,
  144 observations, 0 anomalies, 0 dangling, 0 semantic suspicions), context
  packs 3/3 for `cJSON_Delete`, runner `emit_raw`, and `cJSON_New_Item`;
  Rust fmt/check/golden_test/run all ok; 59 golden cases (22 ownership + 30
  float-print + 7 mutation); `manual_fixes=0`; `OVERALL PASS=True`.
- **Rust hardening (Miri + properties):**
  - **Miri:** `cargo +nightly miri test --test ownership_props` — 11 property
    tests, all pass. Covers parse → walk → print → drop of randomly shaped
    trees, wide sibling lists, deep-nesting rejection, garbage inputs, and
    add → insert → detach → delete → replace ownership transfer plus independent
    recursive duplicate/compare behavior under Miri's aliasing/UB model. **Not
    covered by Miri:** the libc
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
      sibling chain, parse+print+drop no-panic, garbage no-panic, mutation
      add/insert/detach/delete/replace ownership transfer, and independent
      recursive duplicate/compare behavior
    - *inferred* — printed output is UTF-8 and re-parsable (not byte-identical
      to the input); wide-array drop is iterative-safe
    - *human-approved* — nesting past the default limit (1000) is rejected
  - **Still untested:** concurrent use, Miri over the FFI float path, fuzzing
    against adversarial multi-MB inputs, and cross-libc float golden invariance.

## Audit derivation and evidence (2026-07-26)

`API_SURFACE_AUDIT.md` is generated from `cJSON.h`, not from the port. Reproduce
the inventory and its corruption check with:

```bash
uv run python examples/cjson/tools/api_surface_audit.py --check
PYTHONPATH=. uv run pytest examples/cjson/tests/test_cjson_extract.py examples/cjson/tests/test_cjson_parse_contract.py -q
```

The second command compiles a temporary C oracle linked to vendored `cJSON.c`,
compares its safe-surface trace byte-for-byte with Rust `api_trace`, executes
every refusal trace, and runs those C traces under ASan where available. It is
kept out of the indexed golden runner so the published `byog_cjson` snapshot
remains the declared library + golden-runner graph.

For the complete evidence chain rather than these audit-only steps, use the
single `check_port.sh --full` command in the closed scope statement above.

The current header has **78 functions** and **23 public constants/macros/limits/
types**. Of the functions, **68 are covered**, **6 are genuinely blocked by the
exclusive `Box` representation**, and **4 are excluded for C process-global
allocator/error state rather than ownership**. There are no merely unimplemented
functions. The six ownership-blocked entries are
`CreateStringReference`, `CreateObjectReference`, `CreateArrayReference`,
`AddItemToObjectCS`, `AddItemReferenceToArray`, and `AddItemReferenceToObject`.
Their C traces prove, respectively, observing caller string/key mutation or
aliasing a child chain. That is only the C half of the boundary. Each entry now
has a minimal candidate in `examples/cjson_rust/compiler_rejections/` that
first stores the ordinary borrowed/Cow form successfully, then reproduces the
same later source mutation. `rustc --edition=2021 --crate-type=lib` rejects
that mutation with the recorded `E0502` error; the pytest audit compiles every
candidate, checks the exact diagnostic, and parses rustc JSON to require its
primary span to fall inside `c_oracle_mutation_trace`. This proves safe shared
borrows do not close these C traces over the present owned tree — not that
another Rust representation is impossible. The generated audit names the
concrete closure and cost for each: shared mutable bytes for string/key aliases,
and shared nodes or a handle arena for child aliases; each changes existing
signatures, traversal, parsing/printing, mutations, and/or `Drop` rather than
adding an afternoon-scale helper.

**Correction (2026-07-26, same day):** `DetachItemViaPointer` and
`ReplaceItemViaPointer` were first classified ownership-blocked on the reasoning
that a `&CJson` borrowed from the tree cannot coexist with `&mut parent`. That
is true of a reference and false of an address. Both are now covered by
`detach_item_via_pointer` / `replace_item_via_pointer`, which take a
`*const CJson` identity token that is compared and never dereferenced — no
`unsafe`, no handle arena, no interior mutability — and both appear in the
byte-compared safe C/Rust trace, including detach identity (`detached == item`)
and the surviving array shape. This is the second time an "impossible under
exclusive `Box` ownership" claim on this port covered more ground than the
evidence supported; the first was insert/duplicate/compare.

The four non-ownership exclusions are `InitHooks`,
`GetErrorPtr`, `cJSON_malloc`, and `cJSON_free`; they need a global/parameterized
allocator and error-state policy. `prev`, `cJSON_IsReference`, and
`cJSON_StringIsConst` are the matching non-function structural boundary.

The C-compatible safe closure includes the functions that were previously only
absent: version metadata; parse options with end/error offsets; buffered and
preallocated printing; case-sensitive/object-presence getters; invalid/false
predicates; minification; all nine `Add*ToObject` helpers; numeric/bool/string
setters; and a borrowed child iterator for `cJSON_ArrayForEach`. The extended
deterministic ownership property exercises the non-aliasing helpers under Miri.

## Vendored whitespace
- `cJSON.h` and `LICENSE` contain upstream whitespace that fails vanilla
  `git diff --check`. The local `.gitattributes` disables only those vendored
  whitespace checks so provenance-preserving bytes can stay verbatim while
  project-authored files remain checked normally.

## Closed handoff

The header inventory is closed at the current representation: all safe
non-aliasing functions are covered, and every remaining function has an
executable, quantified refusal. Further cJSON work would be a deliberate
representation/policy change (shared/borrowed nodes or handles; allocator/error
state), locale decimal-point support, or partial-`strtod` malformation work —
not a hidden API-parity TODO. The Phase 6 checkpoint can stand while the project
moves to productization/benchmarking or clang-backed C/C++ semantic extraction.
