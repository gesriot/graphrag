# Provenance — vendored `jsmn` (Phase 6 C frontend bootstrap)

First C target for Plan Phase 6. The goal of this checkpoint is not yet a C→Rust
port; it is to prove that a non-Python frontend can publish a BYOG graph that
the existing audit/context-pack rails can consume unchanged.

## Source
- Package: `jsmn`, a small C JSON tokenizer/parser.
- Upstream project: https://github.com/zserge/jsmn
- Vendored in repo commit `002344e` with `jsmn.h`, `tests.c`, `LICENSE`, and
  `compile_commands.json`.
- Exact upstream commit/tag is not recorded in the vendored files; the local
  repository commit pins the reproducible source snapshot for this experiment.
- The upstream test harness headers are not vendored yet. Before the C→Rust
  golden step, either vendor the complete upstream test harness or generate a
  dedicated golden runner from the local `jsmn.h` implementation.

## License — gate step 1 (captured)
- **MIT**. Full text in `LICENSE` (verbatim).

## Build metadata
- `compile_commands.json` is present and captures the bootstrap configuration:
  `cc -DJSMN_HEADER=0 -I. -x c -fsyntax-only jsmn.h`.
- This is a syntax-checkable header-only compilation database entry for the
  first C frontend experiment, and records the intended macro mode
  (`JSMN_HEADER=0`). It is not yet a full upstream test build. The current
  extractor still uses tree-sitter-c only; clang/compile-database semantic facts
  are a later Phase 6 layer.

## C frontend status — graph bootstrap
- Frontend: `scripts/extract_c.py` + `scripts/index_c.py` using `tree-sitter-c`.
- Entity scope: file, function, struct, enum, typedef.
- Relationship scope: contains + conservative deterministic package-internal
  calls. Same-file function definitions win when duplicate C names exist;
  otherwise ambiguous duplicate-name calls are demoted to observations.
- External/undefined calls (`check`, `parse`, libc calls, macro-like calls) are
  preserved as `call_observations`, not promoted into core deterministic edges.

Verified graph result (`byog_jsmn`, snapshot `20260625-073812-343a1c7e`):
- 30 entities, 68 relationships, 30 text units, 157 call observations.
- Entity mix: 23 functions, 3 typedefs, 2 files, 1 enum, 1 struct.
- Relationship mix: 40 `calls`, 28 `contains`.
- `audit_call_edges`: 40 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- Manual call-graph check: `jsmn_parse` resolves to
  `jsmn_alloc_token`, `jsmn_parse_string`, and `jsmn_parse_primitive`; helper
  calls such as `jsmn_fill_token` are also deterministic package calls.

## Known frontend friction
- tree-sitter-c reports 4 `ERROR` nodes around the `JSMN_API` macro on function
  declarations/definitions, but the functions are still extracted. This is the
  expected Phase 6 boundary: clang + `compile_commands.json` is needed later for
  reliable macro/type facts.
- Include relationships are not published yet. The bootstrap graph focuses on
  file/symbol entities, contains, deterministic internal calls, and external
  call observations.
- Function pointers/member calls are out of scope for this first frontend pass.
- Multiple macro configurations (`JSMN_STRICT`, `JSMN_PARENT_LINKS`,
  `JSMN_STATIC`, etc.) are not modeled yet.

## Regression
- `examples/jsmn/tests/test_c_extract.py` locks the key graph facts:
  function/type extraction, `jsmn_parse -> helper` calls, and external calls
  remaining observations.
- The regression also locks the C duplicate-name rule: same-file calls resolve,
  while ambiguous duplicate-name calls stay observations instead of becoming
  false deterministic edges.
- Full repository suite after review hardening: 350 Python tests passed.

## Next port scope
First C→Rust pilot should target the bounded `jsmn_parse` API:
`jsmn_init` + `jsmn_parse` over JSON inputs, returning the C-compatible return
code and token sequence (`type`, `start`, `end`, `size`, and parent only if that
macro mode is intentionally enabled). Capture this golden contract from the
vendored C implementation before writing Rust.
