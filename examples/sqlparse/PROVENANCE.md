# Provenance — vendored `sqlparse` (Phase 5 scale experiment)

First large, multi-package scale target (Plan Phase 5). Earlier targets were 1-3
files / a single class; sqlparse is a real ~4.1k-LOC project with nested
sub-packages (`engine/`, `filters/`), exercising cross-module/cross-package
resolution at scale rather than one-file complexity.

## Source
- Package: `sqlparse` 0.5.5 (PyPI) — a non-validating SQL parser/formatter.
- Upstream: https://github.com/andialbrecht/sqlparse
- Retrieved: 2026-06-18 from the PyPI wheel `sqlparse-0.5.5-py3-none-any.whl`.
- Pure Python, no runtime dependencies.

## License — gate step 1 (captured)
- **BSD-3-Clause** (`License :: OSI Approved :: BSD License`). Full text in `LICENSE` (verbatim).

## What was vendored
- The full `sqlparse/` package (21 modules incl. `engine/` and `filters/`
  sub-packages), verbatim, plus `LICENSE`.
- `__pycache__` removed; no source modifications.

## Purpose (staged)
1. **Scale measurement first:** index + `audit_call_edges` at scale; record LOC,
   timing, graph sizes; classify any new false-edge classes / recall gaps from
   cross-package imports before porting anything.
2. **Then** select one cohesive component (likely the tokenizer + lexer/filter
   pipeline) to port end-to-end with a differential SQL corpus.

## Scale audit result
- Snapshot: `byog_sqlparse/snapshots/20260618-151436-ad7b5954`.
- Size: 4,146 Python LOC across 21 modules (`engine/` + `filters/` included).
- Graph: 243 entities, 454 relationships, 242 text units, 253 call observations.
- Resolved call audit: 229 calls, structural pass rate 1.0, 0 anomalies,
  0 dangling targets, 0 semantic suspicions after the audit heuristic was made
  import-aware for `from pkg import module; module.func()` edges.
- Manual precision sample: 12/12 correct, including cross-package constructors,
  module calls, grouping helper calls, self-methods, and `SQLParseError`.

Interpretation: the first real multi-package graph scale probe is clean. The
measured bottleneck was not resolver precision but audit noise from a legacy
semantic-suspicion heuristic. End-to-end scaled porting remains unproven.

## Split behavior contract — gate step 2
- Golden file: `tests/split/golden_split.json`.
- Contract test: `tests/test_split_contract.py`.
- Scope: `sqlparse.split(sql, strip_semicolon=...) -> list[str]`.
- Coverage: 25 frozen cases covering ordinary semicolon splitting, empty and
  whitespace-only input, semicolons inside strings/comments/parentheses, `GO`
  and `GO 2`, case-sensitive `GO` splitting, transaction `BEGIN`, procedural
  `CREATE ... BEGIN ... END`, `CASE`, unicode strings, repeated semicolons,
  strip-semicolon mode, and the block-comment-after-semicolon edge behavior.

## Port scope
The completed scaled port covers the `sqlparse.split` pipeline rather than the
full formatter:
`__init__.split` → `FilterStack` → `lexer.tokenize` → `StatementSplitter` →
optional semicolon stripping → `sql.Statement` stringification. This keeps the
component cross-module and behavior-heavy while avoiding the whole grouping and
formatting surface in the first scaled port.

Rust lexer caveat: `keywords.SQL_REGEX` contains Python-regex features that
Rust's standard `regex` crate does not support (lookahead/lookbehind and one
backreference for dollar-quoted strings). Use `fancy-regex` selectively or
replace those specific patterns with hand-written scanners; do not assume a
mechanical `re` → `regex` table translation will compile.

Rust port status:
1. **Stage 1 complete:** token type tree + generated keyword dictionaries in
   `examples/sqlparse_rust`, with 811 raw keyword entries and first-match
   behavior matching the Python dictionary add-order.
2. **Stage 2 complete:** lexer parity with the vendored Python implementation.
   The Rust gate compares `(token_type_path, value)` token-by-token across 40
   differential cases / 341 tokens. All 8 rules that require lookaround or
   backreferences are covered; 51/53 SQL regex rules are exercised, with the
   two remaining rules shadowed by earlier Python regex order.
3. **Stage 3 complete:** `StatementSplitter` state machine, minimal
   `sql.Token` / `Statement` string reconstruction, `StripTrailingSemicolon`,
   and the split path of `FilterStack`.
4. **Stage 4 complete:** `port_eval` passes with graph pass rate 1.0
   (229 calls, 0 anomalies, 0 dangling, 0 semantic suspicions), 3/3 context
   packs (`lexer:tokenize`, `engine.filter_stack:FilterStack.run`,
   `engine.statement_splitter:StatementSplitter.process`), rust
   fmt/check/golden_test/run all ok, 65 golden cases across lex + split, and
   `manual_fixes=0`.

Graph caveat: the generic index currently does not expose `__init__.py`'s
top-level `split` as an entity, so the context-pack evidence uses the concrete
pipeline symbols above rather than `__init__:split`.
