# Provenance – vendored `sqlparse` (Phase 5 scale experiment)

First large, multi-package scale target (Plan Phase 5). Earlier targets were 1-3
files / a single class; sqlparse is a real ~4.1k-LOC project with nested
sub-packages (`engine/`, `filters/`), exercising cross-module/cross-package
resolution at scale rather than one-file complexity.

## Source
- Package: `sqlparse` 0.5.5 (PyPI) – a non-validating SQL parser/formatter.
- Upstream: https://github.com/andialbrecht/sqlparse
- Retrieved: 2026-06-18 from the PyPI wheel `sqlparse-0.5.5-py3-none-any.whl`.
- Pure Python, no runtime dependencies.

## License – gate step 1 (captured)
- **BSD-3-Clause** (`License :: OSI Approved :: BSD License`). Full text in `LICENSE` (verbatim).

## Common evidence gate

Run `uv run python scripts/port_eval.py --gate sqlparse` from the repository
root. It regenerates the graph and context packs, runs the Python golden
contract, then runs the Rust port-eval stages. See `examples/PORT_EVIDENCE.md`
for the complete manifest and tool-skip policy.

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
- Snapshot: `byog_sqlparse/snapshots/20260618-151436-ad7b5954` – **no longer
  on disk.** Routine `keep_last=5` retention deleted it on 2026-07-28 when a
  cross-module resolver change republished `byog_sqlparse` twice and this
  snapshot became the sixth-oldest. It cannot be regenerated: the extractor
  has moved on. The prior 2026-07-28 replay yielded 279 calls; the
  2026-07-30 direct-initializer reindex yields 289 entities, 1,339
  relationships, and 291 calls. The numbers below are therefore a recorded
  historical result,
  and `scripts/doc_claims.json` carries the claim as `kind: historical`
  rather than a live derivation. `byog_graph.pinned_snapshot_ids` now
  protects every snapshot a doc claim pins, so retention cannot destroy
  another one.
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

## Inherited-member runtime audit (2026-07-30)

The generic same-file inherited-member extractor rule was checked outside the
JSONPatch package that motivated it. A fresh extractor run supplies its complete
candidate population; a separate Python process imports the vendored package
and verifies that the declared base is in `Child.__mro__` and is the first class
whose `__dict__` owns the member. This is runtime evidence, not a second AST
walk.

The result is **671/671** SQLParse and **57/57** semantic-version inherited-member facts; **728/728** total, with **0 mismatches** and **0 runtime errors**. SQLParse's
population includes **66** multiple-inheritance facts, **6** slotted-child facts, and **18** properties; semantic-version contributes **24** slotted-child facts. The
companion regression plants a C3 diamond, a `super()` override, a property, and
`__slots__`; it also plants a class-level assignment shadowing a base method and
asserts that the runtime oracle reports the extractor edge as a mismatch. The
latter shape was absent from the two measured populations, so this is a scoped
zero-error result, not a claim that the AST rule models assignment shadowing.

Run `uv run python scripts/inherited_member_runtime_audit.py --check` from the
repository root. The SQLParse profile runs the same source-only check; it reads
no published `byog_*` graph and makes an empty candidate population fail rather
than report a `0/0` pass.

## Initializer public-API runtime audit (2026-07-30)

The bridge formerly skipped every `__init__.py`, which made the public
`sqlparse.split` entry point absent from the graph. The replacement is narrow:
index an initializer only when it directly defines a public function, async
function, or class; do not manufacture alias entities for bare re-exports.
`scripts/init_api_runtime_audit.py` discovers the Python targets from
`scripts/port_gates.json`, imports every initializer in a clean subprocess, and
compares its runtime public names with entities from a fresh graph. Its
fail-closed contract is only for direct definitions, while re-exports remain a
separate visible population rather than being silently declared covered.

The current runtime census is **9** Python targets, **8** targets with an
initializer, and **11** initializer modules. They expose **117** public names:
**4** direct definitions are **4 present / 0 missing**, with **0 runtime
errors**; the remaining **113** are re-exports (**2** happen to have a
same-titled implementation entity, **111** do not have an alias entity). All
four direct definitions are SQLParse root APIs: `format`, `parse`,
`parsestream`, and `split`. The SQLParse profile runs the same audit. A
re-export-only initializer is intentionally not a graph API alias; representing
one would require a separate alias-edge policy, not duplicate definition nodes.
Indexing all re-exports as definitions would create **2** same-title collisions
and require **111** new alias nodes across the target set, without adding a
source-body owner for those names. The narrow rule therefore retains executable
entry points while leaving alias semantics as an explicit future decision.

## Re-export identity and trace reachability (2026-07-30)

The **113** names called “re-exports” by the direct-initializer audit are not
one uniform static fact. `scripts/reexport_reachability_audit.py` imports every
initializer in a clean process, maps a binding's runtime source identity to a
fresh entity title, then traces each registered golden workload. **73** bindings
resolve uniquely to an existing defining entity; **2** target the skipped
`sqlparse.engine` / `sqlparse.filters` initializers; and **38** are public
scalar values with no source identity to express as an alias edge. There are
**0** ambiguous or outside-package identities.

An `exports` relationship is therefore deliberately not added in this change.
To emit the identity facts without duplicate aliases would first require **8**
initializer-module nodes for export sources (only SQLParse's root one exists
today), then at most **75** identity edges; the 38 scalar values would still
need a different data model. Nothing currently consumes that relationship: the call oracle maps a
profile frame to the defining code title after attribute lookup, context packs
do not start from a non-existent `package.name` alias entity, and closure must
not be widened (its edge list is deliberately out of scope here).

The trace result is an upper bound, not evidence that callers used a package
alias. Four registered workloads execute **194** golden cases: they give a
measured workload to **50** of the 113 bindings, and **9** resolved defining
targets appear in their profile frames (6 humanize, 1 semantic-version, 2
SQLParse-engine). The other
**63** bindings have no registered call workload and are explicitly
**unmeasured**, not assumed unreachable. SQLParse's three remaining unmapped
raw pairs are all local generator-expression frames; none names a package alias.
An `exports` edge would therefore leave the call-oracle mapper unchanged.

This is a measured static-namespace boundary rather than a claim that export
relationships are useless in general. Add them only with a consumer that can
query or traverse package aliases and an oracle that observes the lookup path.

## SQLParse call-graph observation (2026-07-30)

`uv run python scripts/call_graph_oracle.py --package sqlparse` profiles the
same source corpus consumed by the Rust contract: **40** lexer cases and **25**
split cases, validating each Python result before tracing. Against the current
local published graph it reports **16 confirmed**, **9 missed**, and **132
unconfirmed** edges from **25 mapped** observed pairs (**28 raw**); the nested
`engine.filter_stack` module frames are mapped by relative module title rather
than silently dropped to their basename. This is a call-only coverage
measurement, not a precision rate and not an extractor reindex: the misses are
visible residuals, while the unconfirmed graph edges were not exercised by this
65-case workload.

Indexing the four direct initializer APIs moves the observed mapping from 22/28
to **25/28** and confirms `sqlparse:split → FilterStack` plus
`sqlparse:split → FilterStack.run`. The remaining three unmapped raw pairs are
all `<locals>.<genexpr>` frames, which have no graph entity. The scoreable
population also exposes one previously invisible miss,
`sqlparse:split → sql.TokenList.__str__`; that is why confirmed rises by two
while misses rise from eight to nine. No comparison was narrowed to improve the
number.

The nine misses are a different shape from JSONPatch's registry residuals:
operator-protocol dispatch (`_TokenType.__contains__`, `__getattr__`,
`TokenList.__str__`) and cross-module instance-method calls through the filter
stack. Measuring a second package was worth it for that alone – JSONPatch's
30/3/83 is not representative.

## Call-oracle recall composition (2026-07-31)

The preceding 2026-07-30 paragraph is retained as the original measurement.
It armed `sys.setprofile` before package import, so one mapped pair was class
body execution (`TypedLiteral → _TokenType.__getattr__`), not a golden-workload
call. The tracer now imports before profiling; independently, a narrow
same-file `super().__init__` lexical candidate adds the observed
`TokenList.__init__ → Token.__init__` pair to the graph. It is marked
non-deterministic because a runtime receiver subclass can alter the next MRO
owner. The current live result is **17 confirmed**, **7 missed**, and **132
unconfirmed** from **24 mapped** observed pairs (**27 raw**) across the same
**40** lexer and **25** split cases: **17/24 = 70.8%** observed recall.

Those seven misses are heterogeneous: two `_TokenType.__contains__` protocol
calls, two `TokenList.__str__` protocol calls, one mutable-filter-list element
dispatch, one profiler generator-resume frame (`process → Lexer.get_tokens`),
and one imported-subclass constructor whose executing body belongs to
`TokenList`. The full cross-package table in `docs/ORACLE_CONTRACT.md` is the
meaning of this recall: it is a workload construct mix, not a score comparable
to JSONPatch or semantic-version without those categories.

## Split behavior contract – gate step 2
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

Graph boundary: direct public definitions in an initializer are entities under
their package title (`sqlparse:split`), so the selected port target is now in
the graph. Bare initializer re-exports are deliberately not duplicate alias
entities; the runtime audit above reports that residual population explicitly.
