# Provenance — vendored `inih` (Phase 6 C frontend + second C→Rust port)

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

## License — gate step 1 (captured)
- **New BSD (BSD-3-Clause)**. Full text in `LICENSE.txt` (verbatim); SPDX
  `BSD-3-Clause` header is present in both `ini.c` and `ini.h`.

## Compile metadata
- `compile_commands.json` records the default-config build: `cc -c -I. ini.c`.
- "Default config" means the header defaults: `INI_HANDLER_LINENO=0`,
  `INI_ALLOW_MULTILINE=1`, `INI_ALLOW_BOM=1`, `INI_START_COMMENT_PREFIXES=";#"`,
  `INI_ALLOW_INLINE_COMMENTS=1`, `INI_INLINE_COMMENT_PREFIXES=";"`,
  `INI_USE_STACK=1`, `INI_MAX_LINE=200`, `INI_STOP_ON_FIRST_ERROR=0`,
  `INI_CALL_HANDLER_ON_NEW_SECTION=0`, `INI_ALLOW_NO_VALUE=0`.
- The current extractor is tree-sitter-c only; clang/compile-database semantic
  facts remain a later Phase 6 layer.

## C frontend finding — preprocessor fragmentation (and the fix)
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

## Verified graph result (`byog_inih`, snapshot `20260625-112030-39d0cdd0`)
The published graph now also contains the co-located golden runner
(`tests/parse/runner.c`) as package code, the same way `jsmn` indexes its runner:
- 19 entities, 54 relationships, 19 text units, 35 call observations.
- Entity mix: 15 functions (10 library + 5 runner), 3 files, 1 typedef
  (`ini_parse_string_ctx`).
- Relationship mix: 38 `calls`, 16 `contains`.
- `audit_call_edges`: 38 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- The **library** subgraph (ini.c/ini.h) is 13 entities and 17 deterministic
  calls; the remaining edges are the runner's own internal helpers (resolved,
  same-file).
- Resolved intra-library call graph:
  - `ini_parse -> ini_parse_file -> ini_parse_stream`
  - `ini_parse_string -> ini_parse_string_length -> ini_parse_stream`
  - `ini_parse_stream -> {ini_rstrip, ini_lskip, ini_find_chars_or_comment,
    ini_strncpy0}`
- Callbacks and libc stay weak observations (never core edges): `HANDLER` (the
  macro wrapping the `handler` callback — tree-sitter sees the macro name), the
  `reader` function pointer, plus `fopen`/`fclose`/`strlen`/`strchr`/`isspace`/
  `assert` and the `#if !INI_USE_STACK`-guarded `ini_malloc`/`ini_free`/
  `ini_realloc`.

## Regression
- `examples/inih/tests/test_inih_extract.py` locks the library function set, the
  library call graph, the callback/libc observations, and — importantly — that no
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
