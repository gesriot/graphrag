# graphrag-code

`graphrag-code` is a reproducible research repository for deterministic code
graphs and bounded Python/C → Rust ports. For each declared target it captures a
source-language behavior contract, rebuilds and audits a graph, assembles context
packs, and checks the Rust port against that contract. The goal is auditable
evidence, not a claim that a graph makes a model intrinsically better at coding.

## What this demonstrates — and what it does not

The declared port profiles show that this process can produce repeatable,
high-fidelity results **within explicit target contracts**. The evidence is
golden-first: behavior comes from the source-language oracle, not from a
hand-written Rust expectation. Fresh graph audits, context packs, source
contracts, Rust checks, and `port_eval` make each result inspectable.

The closed, pre-registered graph-vs-raw ablation series found **no measured
accuracy or efficiency advantage** for the graph arm on its tested benchmark
class. The graph is therefore supported here as a discipline for auditability,
provenance, adequacy gates, and focused context assembly — **not** as a measured
accuracy multiplier or a demonstrated general porting advantage. It also does
not claim full upstream-library parity, complete static semantics, or a full C
ABI migration beyond each target's stated boundary.

## Verify the repository

From the repository root, run the portable full-evidence gate:

```bash
uv run python scripts/port_eval.py --all-gates --full
```

It rebuilds disposable graphs under `output/port_gates/`, runs each declared
source contract and Rust port evaluation, and reports named source-only gaps.
`--full` additionally health-checks each mutable published local graph against
the current extractor without rewriting it. Some live diagnostic and
frozen-record checks deliberately need local graph evidence; the durability
inventory names each one and whether absence is a failure or an explicit skip.
An unavailable optional tool or an opt-in check not requested produces an
explicit `SKIP`; an installed tool that fails makes the gate fail. See [the gate guide](examples/PORT_EVIDENCE.md)
for clean-machine prerequisites, the fast/pre-release split, and the opt-in
long differential and scale checks.

## Read in this order

| When you need… | Read… | Why |
| --- | --- | --- |
| A short orientation and one verification command | This README | The supported conclusion, its limit, and the target map below. |
| To operate or debug the portfolio gate | [examples/PORT_EVIDENCE.md](examples/PORT_EVIDENCE.md) | Manifest rules, clean-checkout behavior, tool skip policy, and CI tiers. |
| The project design, research inputs, and future frontier | [Plan.md](Plan.md) | The roadmap and design decisions; treat its current-status language as bounded by the write-up. |
| The results and their interpretation | [PHASE7_WRITEUP.md](PHASE7_WRITEUP.md) | What the ports and closed ablation series support, including the negative result. |
| Why current prose says exactly what it says | [docs/EVIDENCE_AUDIT_20260728.md](docs/EVIDENCE_AUDIT_20260728.md) | A dated claim → evidence → edit ledger, with runnable checks. |
| The shared oracle discipline and live residuals | [docs/ORACLE_CONTRACT.md](docs/ORACLE_CONTRACT.md) | The independent-oracle rules, their regression scars, the compliance review, and one combined command. |
| Whether a graph-backed claim survives a clean checkout | [docs/EVIDENCE_DURABILITY.md](docs/EVIDENCE_DURABILITY.md) | The manifest-derived split between disposable gate output, local frozen snapshots, published oracle inputs, and historical records. |
| Raw ablation protocol and results | [PHASE7_ABLATION.md](PHASE7_ABLATION.md) | The closed experiment record; read it for tables or preregistration detail, not as a mutable how-to. |
| One target's source, scope, and exceptions | Its `examples/*/PROVENANCE.md` below | The local contract and provenance. cJSON and charset-normalizer also have separate mechanical API audits. |

**Proposed future cleanup, not performed here:** [Plan.md](Plan.md)'s current
evidence snapshot partly duplicates [PHASE7_WRITEUP.md](PHASE7_WRITEUP.md).
On a future roadmap revision, move the outcome/status narrative into the
write-up and leave the plan focused on design and future work. Do not merge this
README with `PORT_EVIDENCE.md`: one is an orientation, the other an operations
manual. Likewise, keep the cJSON and charset API audits separate from their
provenance records because the audits are mechanical evidence, not summaries.

## Target guide

| Target | Reader's view |
| --- | --- |
| `mini_game` | A small deterministic game simulation. Its Rust port covers the recorded simulation-state trace; the Python golden trace, fresh graph/context packs, and port evaluation prove that contract. It makes no claim beyond the exercised simulation behavior. |
| `mini_lang` | A compact language interpreter. Its Rust port covers the declared lexer/parser/evaluator golden contract, proved by the source contract, fresh graph/context packs, and port evaluation. It does not claim a general-purpose language implementation beyond that contract. |
| [`jsmn`](examples/jsmn/PROVENANCE.md) | A C JSON tokenizer/parser. Rust covers default-mode byte-oriented `jsmn_init`/`jsmn_parse`; a compiled C-oracle golden contract, fresh C graph, and port evaluation prove it. Alternate macro configurations and parent links are outside the port. |
| [`inih`](examples/inih/PROVENANCE.md) | A C INI parser. Rust covers default-configuration string parsing and recording-handler behavior, proved by C-oracle goldens and the port gate. File I/O, full C ABI/callback surface, custom allocation, and non-default option matrices remain out. |
| [`sqlparse`](examples/sqlparse/PROVENANCE.md) | A multi-module Python SQL package. Rust covers the lex → statement-splitting → optional-semicolon-strip path, with source goldens, lexer differential checks, fresh graph/context packs, and port evaluation. It does not claim the full grouping or formatting surface. |
| [`semantic_version`](examples/semantic_version/PROVENANCE.md) | A Python semantic-version library. Rust covers `Version`, `SimpleSpec`, and `NpmSpec`, proved by source goldens, graph/context packs, and port evaluation. Other upstream surface is not claimed. |
| [`diff_match_patch`](examples/diff_match_patch/PROVENANCE.md) | A Python diff/match/patch library. Rust covers the selected staged algorithmic core under its source contracts and port gate. It does not claim unscoped upstream behavior outside that selected core. |
| [`charset_normalizer`](examples/charset_normalizer_rust/PORT_STATUS.md) | A data-heavy Python encoding detector with a bounded Rust product surface. Targeted Rust/Python parity, a live source-oracle differential, API audit, and port gate prove the declared detector/API/codec scope. Stateful ISO-2022 extension profiles and the other itemized API exclusions remain explicit boundaries. |
| [`cJSON`](examples/cjson/PROVENANCE.md) | A C JSON tree library. Rust covers the closed exclusive-ownership parse/inspect/print/owned-mutation surface, proved by C-oracle traces under ASan, a header-derived API audit, compiler-anchored refusal candidates, and Miri properties. Source-sharing reference APIs and process-global allocator/error-state calls require a different representation or policy and are refused. |
| [`jsonpatch`](examples/jsonpatch/PROVENANCE.md) | An RFC 6902 source-only extractor boundary case. Its Python apply contract, dynamic-dispatch diagnostics, runtime registry oracle, and a current adequate apply-slice closure are evidence. The closure uses narrow static registry and same-file inherited-member facts; no Rust port or new ablation outcome is claimed. |
| [`humanize`](examples/humanize/PROVENANCE.md) | A source-only number-formatting slice. Python goldens and an adequacy-clean graph support the closed ablation record, which found no graph advantage; there is no Rust port or Rust-port claim. |
| [`isodate`](examples/isodate/PROVENANCE.md) | A source-only duration parsing/formatting slice used by the closed ablation record. Its protected archive remains evidence of the experiment; there is no Rust port, and the aggregate gate deliberately does not regenerate the frozen archive. |

The gate also carries focused diagnostics rather than treating them as silent
implementation details: C preprocessor liveness is checked against compiler
output, Python registry extraction is checked against imported runtime objects,
runtime call observations are compared with published graph calls, and cJSON's
public surface/refusals are checked against its real header, C traces, and
compiler locations. Their scope and residuals are recorded in the target
provenance files and the [evidence audit](docs/EVIDENCE_AUDIT_20260728.md).

**Optional C compiler overlays (each default off):** independently selectable
flags on `scripts/index_c.py`:

- `--compiler-dependencies` — flattened TU `depends_on` edges
  (`fact_kind=translation_unit_dependency`) via per-entry `compiler -M` and
  package-path filtering (may be transitive).
- `--compiler-includes` — direct `includes` edges
  (`fact_kind=configured_direct_include`) via per-entry `compiler -E -H`
  hierarchy reconstruction (parent → only its direct includes).
- `--clang-signatures` — attach configured Clang `qualType` / storage metadata
  to **existing** tree-sitter function entities from the AST audit’s
  unambiguously matched, line-confirmed rows only (no new entities/edges).
- `--clang-calls` — attach configured Clang direct-call evidence metadata to
  **existing** tree-sitter `calls` relationships from the AST call-site audit’s
  `matched_internal` rows only (exact span + byte-offset attachment; no new
  entities/edges; base `confidence`/`extractor` unchanged).
- `--clang-types` — attach configured Clang type-declaration evidence metadata
  to **existing** tree-sitter `struct` / `enum` / `typedef` entities from the
  AST type-declaration audit’s matched rows only (exact `tree_sitter_title` +
  entity type + `symbol_name` + path + canonical graph span; no new entities,
  no alternate-site entities).
- `--clang-type-uses` — publish aggregated `uses_type` relationships from the
  type-use audit’s `matched_internal` rows only (one edge per owner/target
  entity-id pair; recursive self-edges allowed). Observation counts and edge
  counts differ; fail-closed residuals abort. Default off. When present, query
  with `graph_query.py types-used-by` / `type-users` (or `graphrag_code.py`
  equivalents); both CLI surfaces support `--json`. Context packs surface bounded
  `type_dependencies` / `type_dependency_edges` / `type_user_edges` from the
  full relationship set (not the 30-neighbor cap).

When any non-empty combination of `--clang-signatures`, `--clang-calls`,
`--clang-types`, and `--clang-type-uses` is enabled, `index_c` builds **one
shared in-memory AST capture** (one `-ast-dump=json` per
`compile_commands.json` entry) and every enabled overlay consumes it. There is
**no** persistent AST cache and AST JSON is never written to manifests or
parquet. Enabling any non-empty subset still dumps once per entry (N dumps for
N entries, never 2N–4N); none of the flags dumps nothing. Trust/confidence
boundaries are independent per overlay manifest block.

These are narrow configuration-derived layers on top of tree-sitter-c — not
full type resolution, ABI verification, multi-config coverage, points-to
analysis, type-use / `uses_type` proof, macro-complete call proof, or
production C/C++ completeness. `-M` / `-H` / AST-dump are GNU/Clang-specific
adapters, not a universal compiler API. Wrappers, response files, `--config`,
modules, plugins, and PCH fail explicitly. See
[docs/graph_schema.md](docs/graph_schema.md).

**Clang AST audits (standalone diagnostics):**

- `scripts/c_clang_ast_audit.py` — function definitions / signatures vs
  tree-sitter entities. Publishing selected matched signature *fields* into
  BYOG requires the separate explicit `--clang-signatures` flag.
- `scripts/c_clang_call_audit.py` — call sites vs tree-sitter `calls` edges
  (direct internal matches; external/indirect remain observations). Call-site
  matching is byte-offset-first with strict line/column fallback and complete
  tree-sitter edge accounting. Publishing selected matched call *evidence
  fields* into BYOG requires the separate explicit `--clang-calls` flag.
- `scripts/c_clang_type_audit.py` — type *declarations* (named complete
  structs, named complete enums, package-local typedefs) vs tree-sitter
  `struct` / `enum` / `typedef` entities. Collision-safe identity is
  kind + path + name + exact start line/column. The CLI remains diagnostic
  (no graph mutation). Publishing selected matched type-declaration *fields*
  into BYOG requires the separate explicit `--clang-types` flag (no
  `uses_type` edges). Anonymous / union / incomplete / outside-package /
  alternate-site residuals are counted explicitly. `--fail-on-mismatch` exits
  1 only for `tree_sitter_only` / `clang_only` / `ambiguous` /
  `macro_location_unsupported` (not for out-of-scope, anonymous, unsupported,
  outside-package, or alternate sites alone).
- `scripts/c_clang_type_use_audit.py` — type *uses* on declaration-bearing
  AST nodes (function returns, parameters, locals, fields, globals, typedef
  underlying types). The CLI is diagnostic; publishing aggregated `uses_type`
  edges requires the separate explicit `--clang-type-uses` flag. Owners/targets
  reuse the function and type-declaration audits. Locations are the
  declaration-bearing node (not proven exact type-token spans). C's tag
  namespace stays distinct: bare names resolve only as unique typedef spellings,
  while `struct T` / `enum T` use explicit tag spelling. `--fail-on-mismatch`
  exits 1 for `owner_unmatched` / `target_unresolved` / `ambiguous_target` /
  `macro_location_unsupported` only.
- `scripts/c_clang_type_use_graph_audit.py` — **read-only integrity audit** for
  already-persisted configured `uses_type` edges and the
  `clang_type_uses` manifest block. Does not invoke Clang, reindex, publish,
  or rewrite graphs. Exit 0 = valid (including legacy/off with zero configured
  edges), 1 = integrity anomalies, 2 = unreadable graph/snapshot/manifest.
  Shared producer-contract helpers live in `c_clang_type_uses.py`
  (`relationship_id`, `validate_persisted_type_use_overlay`). Published C
  graph health also runs this pure check without changing extractor comparison.

All four AST CLIs remain available (each captures once internally). They are
**Clang only** (`cc` accepted only when `--version` proves Clang/Apple Clang).
None is points-to analysis, layout/ABI proof, multi-config coverage, or
production C/C++ completeness.

**C symbol identity (tree-sitter extractor):** within one collision-safe module
key, when two or more of `function` / `struct` / `enum` / `typedef` share a
bare C name, every colliding kind uses the qualified title
`module_key:entity_kind:name` (for example `cJSON:struct:cJSON` and
`cJSON:typedef:cJSON`). Non-colliding symbols keep the legacy
`module_key:name`. Typedef aliases nested in declarators (including
function-pointer typedefs such as `typedef int (*handler)(...)`) are extracted
by walking only declarator structure — not by scanning parameter lists. The
type audit / `--clang-types` overlay may match any exact tree-sitter
declaration site owned by a semantic graph entity (the graph still keeps one
canonical source-derived span; fields record both graph-canonical and
matched-site coordinates). Alternate unselected sites are observation-only
and are not claimed dead/inactive. This is single-config declaration evidence
— not a multi-config type graph and not a `uses_type` overlay.
