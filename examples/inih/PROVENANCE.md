# Provenance — vendored `inih` (Phase 6 C frontend, second C target)

Second C target for Plan Phase 6, chosen to surface the next C-specific unknowns
*before* the ownership-heavy `cJSON` milestone: callbacks, file/string input
variants, line-number/error behavior, and compile-time options. This checkpoint
is the **graph bootstrap** (frontend + audit clean); the C→Rust port is the next
checkpoint.

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

Honest limit: tree-sitter-c still counts calls inside *every* `#if INI_*` block
regardless of whether that option is enabled in a given build. For the **default
config** (which the port targets) all these blocks are enabled, so the graph is
accurate for default mode; it is not yet configuration-aware. Config-aware C
facts are the motivation for the clang/preprocessor layer in Plan §5.

## Verified graph result (`byog_inih`, snapshot `20260625-102840-e341fad2`)
- 13 entities, 28 relationships, 13 text units, 26 call observations.
- Entity mix: 10 functions, 2 files, 1 typedef (`ini_parse_string_ctx`).
- Relationship mix: 17 `calls`, 11 `contains`.
- `audit_call_edges`: 17 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- Resolved intra-package call graph:
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
- `examples/inih/tests/test_inih_extract.py` locks the function set, the call graph,
  the callback/libc observations, and — importantly — that no phantom keyword
  "function" leaks in from preprocessor fragmentation (incl. a focused unit test
  on a `#if`-split body).

## Next port scope (proposed, not yet built)
First inih C→Rust port should target the bounded **string** entry point in
default config: `ini_parse_string` / `ini_parse_string_length` driving
`ini_parse_stream` with a recording handler. Golden contract = for each INI input,
the ordered sequence of `(section, name, value)` handler calls plus the return
code (0, or first-error line number). File I/O (`ini_parse`/`ini_parse_file`) and
`INI_HANDLER_LINENO`/other non-default options are explicitly out of the first
port's scope (measured here, but deferred), mirroring how `jsmn` deferred
`JSMN_STRICT`/`JSMN_PARENT_LINKS`.
