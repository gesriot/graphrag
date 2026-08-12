# Provenance – vendored `inih` (Phase 6 C frontend + second C→Rust port)

Second C target for Plan Phase 6, chosen to surface the next C-specific unknowns
*before* the ownership-heavy `cJSON` milestone: callbacks, file/string input
variants, line-number/error behavior, and compile-time options. This checkpoint
now includes both the C frontend graph bootstrap and the bounded default-config
string parser C→Rust port.

## Source
- Package: `inih` ("INI Not Invented Here"), a small C INI-file parser by Ben Hoyt.
- Upstream: https://github.com/benhoyt/inih (vendored `ini.c`, `ini.h`).
- Vendored verbatim from `master`; exact upstream commit/tag is not recorded in
  the files, the local repository commit pins the reproducible snapshot.

## License – gate step 1 (captured)
- **New BSD (BSD-3-Clause)**. Full text in `LICENSE.txt` (verbatim); SPDX
  `BSD-3-Clause` header is present in both `ini.c` and `ini.h`.

## Common evidence gate

Run `uv run python scripts/port_eval.py --gate inih` from the repository root.
It regenerates the C graph and context packs, runs the C-oracle golden contract,
then runs the Rust port-eval stages. See `examples/PORT_EVIDENCE.md` for the
complete manifest and tool-skip policy.

## Compile metadata
- `compile_commands.json` records the default-config build: `cc -c -I. ini.c`.
- "Default config" means the header defaults: `INI_HANDLER_LINENO=0`,
  `INI_ALLOW_MULTILINE=1`, `INI_ALLOW_BOM=1`, `INI_START_COMMENT_PREFIXES=";#"`,
  `INI_ALLOW_INLINE_COMMENTS=1`, `INI_INLINE_COMMENT_PREFIXES=";"`,
  `INI_USE_STACK=1`, `INI_MAX_LINE=200`, `INI_STOP_ON_FIRST_ERROR=0`,
  `INI_CALL_HANDLER_ON_NEW_SECTION=0`, `INI_ALLOW_NO_VALUE=0`.
- The current extractor is tree-sitter-c only; clang/compile-database semantic
  facts remain a later Phase 6 layer.

## C frontend finding – preprocessor fragmentation (and the fix)
inih is preprocessor-heavy (configurability is implemented with `#if`/`#endif`).
tree-sitter-c does **not** evaluate the preprocessor, so a function body split by
`#if INI_ALLOW_MULTILINE ... #endif` is misparsed: the `else if (cond) { body }`
blocks inside `ini_parse_stream` were read as top-level `function_definition`s
whose "name" is the control keyword `if`. This produced three phantom `ini:if`
entities and 9 `span_outside_caller` anomalies (audit pass rate 0.68).

Fix (means layer, `scripts/extract_c.py`): `_func_name` now rejects C reserved
words. A real C function can never be named `if`/`for`/`while`/…, so this drops
the phantoms; the calls inside those `#if`-guarded blocks then re-attribute to the
real enclosing `ini_parse_stream` and land within its span. After the fix the
audit is clean. This change also leaves the `jsmn` graph unchanged (it has no
`#if`-fragmented function bodies).

Honest limit: tree-sitter-c still sees calls inside *every* `#if INI_*` block
regardless of whether that option is enabled in a given build. For the
**default config** (which the first port targets), the deterministic internal
CALLS promoted into the core graph are compatible with the enabled code path,
but observations still include some disabled-branch calls (for example the
`!INI_USE_STACK` allocation path). The graph is therefore not yet
configuration-aware. Config-aware C facts are the motivation for the
clang/preprocessor layer in Plan Phase 6.

**Diagnostic (2026-07-26):** `scripts/c_preprocessor.py` labels these
preprocessor-dependent facts as provenance (`preprocessor_dependent` /
`preprocessor_reasons`) without demoting `is_deterministic` or changing audit
pass rates. On inih it flags the `HANDLER` function-like macro observations and
trusted calls inside `INI_ALLOW_MULTILINE` / related `#if` regions – the known
failure mode from this PROVENANCE note. A behaviour-preserving reindex
published those columns into the live graph (counts unchanged: 19/54/38/35).
Context packs for `ini:ini_parse_stream` now surface a top-level
`preprocessor_warning`. See also
`examples/inih/tests/test_c_preprocessor_flags.py`.

## Verified graph result (`byog_inih`, snapshot `20260726-030424-9e3862f6`)
The published graph now also contains the co-located golden runner
(`tests/parse/runner.c`) as package code, the same way `jsmn` indexes its runner:
- **Historical published snapshot** (`20260726-030424-9e3862f6`): 19 entities,
  54 relationships, 19 text units, 35 call observations (frozen identity).
- **Source-derived counts after declarator-aware typedef extraction:** **21**
  entities, **56** relationships, **21 text units**, 35 call observations;
  **38 calls unchanged** (IDs/endpoints stable). The +2 entities/+2 contains
  are function-pointer typedefs `ini_handler` and `ini_reader` from `ini.h`.
- Entity mix (source-derived): 15 functions (10 library + 5 runner), 3 files,
  3 typedefs (`ini_parse_string_ctx`, `ini_handler`, `ini_reader`).
- Relationship mix: 38 `calls`, 18 `contains`.
- **Type-declaration audit (measured, multi-site exact matching):** matched=3
  (`ini_parse_string_ctx`, `ini_handler`, `ini_reader`);
  `ambiguous=0`, `clang_only=0`, `tree_sitter_only=0`,
  `alternate_declaration_sites=1`. The graph still publishes one canonical
  `ini_handler` entity at the first walk-order span (line 58). The diagnostic
  audit also owns the `#else` declaration site (line 62) and matches
  configured Clang (line 62) against that exact site without rewriting the
  graph. The unselected canonical site is reported as an alternate site
  (not proven dead/inactive). `--fail-on-mismatch` exits 0.
- **Optional `--clang-types` entity fields (default off):** when enabled,
  attaches those 3 matched type-declaration rows as `clang_type_*` fields on
  the existing typedef entities (graph remains 21/56/21/38/35). For
  `ini_handler`, fields record `clang_type_graph_canonical_line=58` and
  `clang_type_matched_site_line=62` while the entity `span` stays line 58.
  Alternate sites are observation-only (not separate entities). Standard
  mismatch residuals fail closed. Independent `extra_manifest["clang_types"]`
  block (`mode=configured_clang_type_declarations` when on, `mode=off` when
  disabled). Shares the in-memory AST capture with signatures/calls/type-uses/
  type-shapes (N dumps for N entries), and its report is reused rather than
  rebuilt by the type-use and type-shape builders.
- **Clang AST type-use audit (diagnostic):** measured matched_internal=**14**
  (ini_handler / ini_reader / ini_parse_string_ctx parameter and local uses),
  external_or_system=72, unsupported_type_form=2, owner_unmatched=0,
  target_unresolved=0, ambiguous_target=0, macro_location_unsupported=0,
  unowned_context=0. `--fail-on-mismatch` exits 0.
- **Optional `--clang-type-uses` graph edges (default off):** aggregates those
  14 matched observations into **8** `uses_type` relationships (one per
  owner/target entity-id pair). Entity count stays 21; pre-existing
  relationship IDs/endpoints/types are preserved. Independent
  `extra_manifest["clang_type_uses"]` block. Confidence is
  configuration-relative only. Query via `types-used-by` /
  `type-users` / bounded `type-closure`; context packs expose bounded
  type-dependency evidence (e.g. `ini:ini_parse` → `ini:ini_handler`)
  and optional multi-hop `type_*_closure` when `--type-depth > 1`.
  Read-only integrity: `scripts/c_clang_type_use_graph_audit.py`
  validates persisted edges without Clang re-run (legacy/off default
  graphs pass; enabled temporary snapshots with 8 edges pass;
  corruption fails closed). Port gate: profile `type_context` rebuilds
  disposable `output/port_gates/inih/graph` with `--clang-type-uses`
  and requires non-empty untruncated `type_dependency_closure` for
  `ini:ini_parse` (depth 2); published `byog_inih` is not rewritten.
- **Type-shape audit (diagnostic):** configured type-declaration matches are
  typedef-only under the default compile DB, so shape owners classified = 0
  (no struct/enum shapes to compare). Outside-package residuals remain
  observation-only (`outside_package_declarations=12`). Not ABI/layout; the
  audit CLI performs no graph mutation.
- **Optional `--clang-type-shapes` entity fields (default off):** enabling it
  on inih is a clean no-op overlay: **0** decorated entities and **0**
  `clang_shape_*` fields, because there are no matched struct/enum owners
  under this compile DB (typedefs are not independent shapes). The graph
  remains 21/56/21/38/35 and the independent
  `extra_manifest["clang_type_shapes"]` block records
  `mode=configured_clang_type_shapes` with all bucket counts, including the
  observation-only `outside_package_declarations`. Hard equality would be
  ordered direct member names only – never ABI, layout, FFI, or Rust `repr`
  claims.
- `audit_call_edges`: 38 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- The **library** subgraph (ini.c/ini.h) is 15 entities and 17 deterministic
  calls; the remaining edges are the runner's own internal helpers (resolved,
  same-file).
- Resolved intra-library call graph:
  - `ini_parse -> ini_parse_file -> ini_parse_stream`
  - `ini_parse_string -> ini_parse_string_length -> ini_parse_stream`
  - `ini_parse_stream -> {ini_rstrip, ini_lskip, ini_find_chars_or_comment,
    ini_strncpy0}`
- Callbacks and libc stay weak observations (never core edges): `HANDLER` (the
  macro wrapping the `handler` callback – tree-sitter sees the macro name), the
  `reader` function pointer, plus `fopen`/`fclose`/`strlen`/`strchr`/`isspace`/
  `assert` and the `#if !INI_USE_STACK`-guarded `ini_malloc`/`ini_free`/
  `ini_realloc`.

## Regression
- `examples/inih/tests/test_inih_extract.py` locks the library function set, the
  library call graph, the callback/libc observations, and – importantly – that no
  phantom keyword "function" leaks in from preprocessor fragmentation (incl. a
  focused unit test on a `#if`-split body).
- `examples/inih/tests/test_inih_parse_contract.py` recompiles the dedicated C
  golden runner and re-derives the parse contract, and asserts string<->file
  input parity (`ini_parse_string_length` vs `ini_parse_file`).

## Golden contract (captured before Rust)
- Runner: `tests/parse/runner.c`; golden: `tests/parse/golden_parse.json`.
- 21 cases in default config, each pinning an INI input to inih's return code
  (0 / first-error line number) plus the ordered `(section, name, value)`
  callback sequence: sections and the implicit empty section, `=`/`:` separators,
  whitespace stripping, start-of-line (`;`/`#`) and inline (`;` after space)
  comments, multiline continuation, UTF-8 BOM, empty/space-bearing values, blank
  lines, CRLF, malformed lines and section headers, mid-file error recovery, and
  C-string truncation at embedded NUL bytes.
- string<->file input parity holds for every case (measured, not ported).

## C→Rust port status
- Rust port: `examples/inih_rust`.
- Scope: default-config `ini_parse_string` / `ini_parse_string_length`
  behavior over byte input, driving a recording handler.
- The Rust port is string-only: file I/O (`ini_parse` / `ini_parse_file`) is
  measured by the C runner's string<->file parity checks, but not ported.
- The implementation mirrors C-string semantics inside `ini_parse_stream`: each
  fixed line buffer is processed only up to the first `\0`, matching inih's
  internal `strlen(line)` behavior.
- Deferred: `INI_HANDLER_LINENO`, `INI_ALLOW_NO_VALUE`, heap/realloc mode,
  custom allocator, non-default compile-time option matrix, and full C ABI/file
  I/O preservation.
- `port_eval`: `OVERALL PASS=True`, `manual_fixes=0`, 3/3 explicit library
  context packs (`ini_parse_stream`, `ini_parse_string_length`, `ini_rstrip`),
  21/21 golden cases, cargo fmt/check/test/run all ok.

## Next target
Move to `cJSON` for the next Phase 6 step: struct/pointer ownership,
allocation/free behavior, and a broader API surface.
