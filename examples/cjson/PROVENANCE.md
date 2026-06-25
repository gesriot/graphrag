# Provenance — vendored `cJSON` (Phase 6 C frontend, third C target)

Third C target for Plan Phase 6 and the first **struct/pointer/ownership-heavy**
one (~3.2k LOC). This checkpoint is the graph bootstrap (frontend + audit clean);
the bounded C→Rust ownership-slice port is the next checkpoint.

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

## Verified graph result (`byog_cjson`, snapshot `20260625-115555-029441e8`)
- 125 entities, 311 relationships, 125 text units, 112 call observations.
- Entity mix: 116 functions, 7 typedefs (`cJSON`, `cJSON_Hooks`, `cJSON_bool`,
  `parse_buffer`, `printbuffer`, `internal_hooks`, `error`), 2 files.
- Relationship mix: 188 `calls`, 123 `contains`.
- `audit_call_edges`: 188 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions.
- Resolved entry chains: `cJSON_Parse -> cJSON_ParseWithOpts`,
  `cJSON_ParseWithLength -> cJSON_ParseWithLengthOpts`.

## Regression
- `examples/cjson/tests/test_cjson_extract.py` locks the struct graph, the
  ownership-slice API surface, the parse chain, the recursive ownership self-edges
  (`cJSON_Delete`), and that allocation primitives stay observations.

## Next port scope (proposed, not yet built)
First cJSON C→Rust port should target a narrow **ownership-bearing slice** rather
than the full API: `parse -> inspect tree -> print -> delete` over a bounded JSON
corpus. This first exercises the struct graph, heap ownership, and free semantics
without spreading into the mutation/builder API. Golden contract = for each JSON
input, the parsed tree's observable shape (via inspection getters) plus the
printed output, with allocation/free behavior exercised end-to-end. The full
mutation API, custom hooks, and printing edge cases are deferred.
