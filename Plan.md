# Replicating Microsoft GraphRAG for Large-Scale Codebase Understanding and Automated Language Migration (C/Python → Rust)

**Goal:** Create a practical, open-source system that achieves (or closely approximates) the capabilities Mark Russinovich demonstrated: feed a complex codebase (via structured representation such as AST) into a GraphRAG-style pipeline to produce a hierarchical semantic knowledge graph, then use that graph + original code + LLM agents to port/rewrite the code to another language (e.g. Python→Rust or C/C++→Rust) with high fidelity and minimal errors.

This is **not** a trivial "prompt the LLM with all the code" task. The key innovation highlighted by Russinovich is giving the LLM a *semantic graph* of the whole codebase so it can reason about architecture, relationships, intent, and invariants globally and locally.

**Status of the original work (reviewed 2026-06-15):**
- Microsoft GraphRAG (the base technique and library) is open source: https://github.com/microsoft/graphrag (and docs https://microsoft.github.io/graphrag). It is positioned as a pipeline for extracting structured data from unstructured text, building a knowledge graph, clustering it, summarizing communities, and querying the result.
- The specific "code4llm" demo/tool shown in the talk (Python side-scroller game translated to Rust) and the full internal scalable "code processing infrastructure" (graphs over source at scale + guided AI agents, per Galen Hunt's "1 engineer, 1 month, 1 million lines of code" North Star) are **not publicly released**. Community requested the demo in microsoft/graphrag#1779; that issue is now **closed as not planned**, not open.
- Treat the 2030 / "eliminate C/C++" framing carefully: public reporting says Hunt later clarified this as a research project and not an official Windows rewrite roadmap. The actionable takeaway for this project is the infrastructure pattern: algorithmic source-code graph + AI agents + verification, not a promise of fully autonomous migration.

This plan outlines how to build a strong public equivalent using open components, starting small and scaling.

## 1. Source Material and Core Insights from Research

- **Primary talks (must watch for exact wording and demo):**
  - "Microsoft is Getting Rusty: A Review of Successes and Challenges" – Mark Russinovich (Rust Nation UK / related RustConf 2025 keynotes). Key segment roughly ~28-32 min depending on the recording.
  - "From Blue Screens to Orange Crabs: Microsoft's Rusty Revolution".
  - Quotes below are currently paraphrased/aggregated from reports + transcripts in coverage. Before using them in docs or marketing, archive the exact video/transcript segment and treat the timing as approximate (~28-32 minutes depending on the recording).
    - "If we take the abstract syntax tree, we pass it through the graph rag algorithm and create a graph that semantically represents a large codebase, we can have the LLM start to reason over it and port the code itself, piece by piece from one language to another."
    - Demo of a simple Python side-scrolling game (3 files, ~200 lines): GraphRAG version produced correct, compiling, identically behaving Rust. Plain LLM/ChatGPT produced "garbage"/broken code.
    - "Normal LLM translation gives you garbage. But if you give the AI a semantic understanding of the whole codebase, it can reason about what the code actually does."
- Broader context (Galen Hunt, Distinguished Engineer CoreAI, reporting around Dec 2025): Internal infrastructure combining "algorithmic infrastructure [that] creates a scalable graph over source code at scale" + "AI processing infrastructure [that] enables us to apply AI agents, guided by algorithms, to make code modifications at scale." Public reporting also says this was clarified as a research project rather than an official Windows rewrite plan. North Star: 1 engineer/month/1M LOC.
- Microsoft GraphRAG paper: "From Local to Global: A Graph RAG Approach to Query-Focused Summarization" (arXiv:2404.16130). Core pipeline: LLM entity/relation/claim extraction from text chunks → knowledge graph → Leiden hierarchical community detection → bottom-up community summaries → query-time global (map-reduce over communities) + local search.
- Why it may help for code (beyond vanilla RAG): Code understanding and porting require *holistic sensemaking* (architecture, cross-module invariants, data flows, "why this design") + precise local facts. Vector similarity over raw text/chunks can omit connections and global coherence. Hierarchical graphs + summaries are a design candidate for assembling that material; any advantage over raw source must be measured rather than assumed.

Many independent projects already combine **Tree-sitter** (precise, multi-language AST parsing) + graph databases (Memgraph, Neo4j, FalkorDB, even SQLite) + LLM/GraphRAG layers for "Code Knowledge Graphs" / CodeRAG. Examples:
- Graph-Code / Code-Graph-RAG (tree-sitter → graph in Memgraph → NL-to-Cypher → visualization + surgical edits via AST).
- Codebase-Memory (tree-sitter for 66 languages, call graphs, Louvain communities, SQLite + MCP for agents).
- Various CodeRAG experiments using dependency graphs from AST for better retrieval than pure vector.

The proposed pattern is **hybrid**:
- Deterministic/precise structural graph from AST (functions, types, calls, modules, imports, etc.).
- LLM-powered semantic overlay (intent, summaries, claims, higher-level relationships that static analysis misses) – exactly where the original GraphRAG extraction shines.
- Hierarchical communities + summaries for "subsystem overviews".
- Then rich retrieval (graph traversals + GraphRAG-style global/local queries) to feed translation agents.

Important implementation correction: do **not** rely on generic GraphRAG entity extraction to infer precise code structure from raw source. Use deterministic source tooling for hard facts, then feed the resulting graph into GraphRAG. The official GraphRAG docs support a "bring your own graph" path using `entities.parquet`, `relationships.parquet`, and optional `text_units.parquet`, followed by community creation/report workflows. That should be the primary route for the MVP.

**Current execution strategy (decided 2026-06-15): no external API by default.** The first working system should not require OpenAI/Azure/Anthropic/xAI keys. Build the deterministic pipeline first: extractor → BYOG parquet → schema tests → local graph queries/context packs → manual or local-agent-assisted work in Codex / Claude Code / Grok Build. Official GraphRAG LLM workflows (`create_community_reports`, Global/Local/DRIFT search, embeddings) remain an optional later backend for evaluation or higher-quality summaries, not a blocker for Phase 1-3 progress.

**Means vs. ends – verification boundary (recorded 2026-06-16):** graph-quality auditing is the *means*, Python→Rust fidelity is the *north-star end*. A correct graph is necessary but not sufficient; it must be measured separately from the port outcome. Two repeatable harnesses make this explicit: `scripts/audit_call_edges.py` measures the graph (structural pass rate of CALLS edges, dangling targets, seeded precision sample), and `scripts/port_eval.py` measures the end-to-end port (graph quality → context packs → `cargo fmt/check/test/run` → golden scenarios → manual-fix count) as one comparable report. C/C++ input remains "maybe, later"; Python→Rust is the capability that must work flawlessly and is the primary acceptance metric.

**Common port evidence (2026-07-26; pack gate hardened 2026-08-11):** `uv run python scripts/port_eval.py --all-gates --full` runs the declarative profiles in `scripts/port_gates.json`: it reindexes a fresh disposable graph, audits it, regenerates context packs, runs the source oracle contract, and invokes the normal Rust `port_eval` stages. Context-pack generation is **fail-closed**: every requested pack must be generated and validated (`complete=true`) or `overall_pass` and the declared gate fail – not merely a reduced displayed count. Optional C-only `type_context` in the manifest (strict schema: `clang_type_uses`, depth, edge/observation bounds, per-symbol direction requirements) rebuilds the disposable `output/port_gates/<id>/graph` with `--clang-type-uses` and validates `type_*_closure` evidence; missing Clang is an explicit skip, broken Clang or a failed configured index is a failure. Published `byog_*` roots are never rewritten by this path. C/ASan/Miri and live differential checks remain profile-specific declarations, not copied shell scripts. Missing optional tools are explicit `SKIP`s; a present-but-broken tool or a broken golden is a failing gate. `examples/PORT_EVIDENCE.md` lists the declared source-only gaps rather than treating them as passes. The existing cJSON and charset-normalizer handoff scripts remain supported for their documented specialized modes.

**Evidence-boundary audit (2026-07-28):** The current aggregate gate has
**9 profiles and 3 named source-only gaps**, and its manifest fails closed both
when a Rust port lacks a profile and when a golden-bearing source package is
absent from the registry. Current C evidence is equally bounded: cJSON's
header-derived closure proves the safe-borrow boundary of its exclusive-`Box`
tree, not an impossibility result for Rust; compiler-scored C liveness labels
remain provenance rather than full preprocessing; and the runtime Python
registry oracle proves agreement only for its tested Name/decorator mappings,
leaving lambda values explicitly missed. The closed ablation series does not
show a graph advantage over raw source. See
[`PHASE7_WRITEUP.md` §1.6](PHASE7_WRITEUP.md#16-evidence-boundary-update-2026-07-28)
and [`docs/EVIDENCE_AUDIT_20260728.md`](docs/EVIDENCE_AUDIT_20260728.md) for
the runnable evidence and claim boundaries.

**JSONPatch closure update (2026-07-30):** the source-only `apply_patch` slice
now reaches its scoped adequacy contract through narrow static registry and
same-file inherited-member facts. The latter names only an effective base member
that a subclass does not override; it is not a class-to-all-members expansion
and not a `calls` edge. This does not create a Rust port, revise the closed
ablation outcome, or claim general dynamic-dispatch completeness; see
[`examples/jsonpatch/PROVENANCE.md`](examples/jsonpatch/PROVENANCE.md) for the
live adequacy claim and the remaining observed-call residuals.

In particular, any earlier “thesis is proven” wording is limited to the
target-specific, contract-checked porting rails. It does not prove that graph
material causes those port outcomes or that it outperforms raw source.

**Porting gate (per-project checklist, recorded 2026-06-16):** before porting any project to Rust, pass these gates in order – do not start the port until all are green:
1. **License captured** – project is permissively licensed; license + provenance recorded (esp. for external/third-party code).
2. **Golden/contract captured first** – `golden_*.json` (or trace contract) exists and the Python reference passes them *before* any Rust is written. The contract scope is stated explicitly (e.g. for mini_lang: `run_source` semantics are pinned; CLI file I/O error text is out of scope, only its pass/fail outcome is kept faithful).
3. **Graph clean** – `audit_call_edges` on the project's graph shows `pass_rate=1.0` with no dangling targets, OR every remaining weak/false edge is demoted to `call_observations` (never a high-confidence deterministic edge).
4. **Then `port_eval`** – only after 1–3, run the end-to-end harness and record the report (graph pass rate, golden cases, manual-fix count, `overall_pass`).

Validated end-to-end on eight ports, all with `overall_pass=True` and 0 recorded manual fixes: `mini_game` (greenhouse), `mini_lang` (interpreter; lexer→parser→eval, 28 golden cases), the external BSD-licensed `semantic_version` 2.10.0 core scope (`Version`, `SimpleSpec`, and `NpmSpec`; 147 golden cases across 13 files), the selected staged algorithmic core of the external Apache-2.0 `diff-match-patch` 20241021 package (Myers diff, line mode and cleanups; arbitrary-length Bitap fuzzy match; patch make/apply/split/serialization with Unicode and percent-codec fidelity; 107 golden cases across 9 files), the external BSD-licensed `sqlparse` 0.5.5 `split` pipeline (lexer with Python-regex lookaround/backreference parity, StatementSplitter, strip-semicolon behavior; 65 golden cases across lex + split), the MIT-licensed C `jsmn` default-mode parser/tokenizer (`jsmn_init` + `jsmn_parse`; byte offsets, count-only mode, NOMEM/PART, non-strict unquoted keys, escapes/unicode/nesting; 18 golden cases), the BSD-3-Clause C `inih` default-config string parser (`ini_parse_string` / `ini_parse_string_length`; callback sequence, error-line behavior, multiline/inline comments/BOM/CRLF/embedded-NUL C-string behavior; 21 golden cases), and the MIT-licensed C `cJSON` ownership slice (`parse -> inspect tree -> print -> drop/delete`; structure-preserving node/tree ownership, ASan-clean C oracle, 59 golden cases: the ownership slice, bounded `%1.15g`/`%1.17g` float-print fidelity, and owned mutation/builder traces).

**cJSON API closure over the exclusive-ownership representation (2026-07-26):**
the original 52-case baseline still has seven C-oracle mutation traces (59 cases
total): owned constructors, typed arrays, add/insert, detach (including caller
deletion), delete, replace, duplicate/compare, and value accessors. The Rust
port keeps its `Box` child/next ownership representation and passes the same
traces, 11 labelled ownership properties, Miri, and ASan over the C mutation
corpus. `examples/cjson/API_SURFACE_AUDIT.md` mechanically enumerates the
current `cJSON.h`: 78 functions plus 23 public constants/macros/limits/types;
68 functions are covered, 6 require shared mutable/aliased storage (reference
constructors/adds and the const-key alias), and 4 require a process-global
allocator/error-state policy (`InitHooks`, `GetErrorPtr`, `malloc`, `free`).
Every refusal has a reached C oracle trace under ASan and a checked-in,
compiler-tested safe-borrow candidate: construction type-checks, while the
same later source mutation the C trace observes fails with its recorded E0502.
The audit states the shared-byte/shared-node or handle representation that
would close each behavior and the resulting signatures/traversal/Drop cost;
it does not call another Rust representation impossible. There are no merely
unimplemented functions in this representation.
`DetachItemViaPointer`/`ReplaceItemViaPointer` were initially classified
ownership-blocked and are in fact covered: node identity travels as a
compared-never-dereferenced `*const CJson`, so no `unsafe`, handle arena or
interior mutability is required. This is still not a claim of full cJSON API
migration.

The complete cJSON evidence chain is
`examples/cjson/tools/check_port.sh --full`; `--quick` omits only Miri and
prints unavailable compiler, ASan, or Miri gates as explicit skips.

**Advanced Python→Rust stress-test (added 2026-06-27; hardened 2026-07-26):** vendored MIT `charset-normalizer` is the next-level validation target after `sqlparse.split`, not just another example. It exercises a data-heavy heuristic detector with large Unicode/frequency tables, `uses_data` / `data_dependencies` context packing, codec-policy edge cases, a broader API surface, and a product-style CLI. The scoped Rust port in `examples/charset_normalizer_rust` is backed by the Python reference in `examples/charset_normalizer`, 23 saved context packs under `examples/charset_normalizer_rust/packs`, and a local ignored graph artifact `byog_charset_normalizer` for rerunning graph queries without reindexing. Current gates: graph audit pass rate 1.0 with no anomalies/dangling/semantic suspicions; 18/18 captured golden samples; 83 Rust tests; a live, seeded Python-oracle differential gate with 79 inputs across 15 source-encoding categories and 79/79 strong agreements, plus a 530-input on-demand every-byte/mutation/long sweep with 530/530 agreements (seed `20260725`); full `PYTHONPATH=. uv run pytest examples -q --tb=no` expected summary `1175 passed, 2 xfailed`; repeatable handoff via `examples/charset_normalizer_rust/tools/check_port.sh`. The detector comparison checks canonical selected encoding, strict decoded Unicode, language, BOM, and scores–not merely a label–and reports ambiguous differences rather than suppressing them. `examples/charset_normalizer_rust/API_SURFACE_AUDIT.md` now itemizes the API: covered detector/input/model/CLI surface includes `CliDetectionResult`, direct submatch mutation, Python-style replacement/default output, callable non-root helpers, exact generated Python HZ/GB2312 (7,445 shifted pairs), and exact generated Python EUC-JIS plus Shift-JIS-X-0213 maps; deliberate exclusions remain global logger mutation, same-name legacy `detect` behavior, and hidden Rust-only cp controls; Python-only object-model and argparse callback mechanics are marked not applicable. Codec scope is closed: the only named non-exact boundary is five stateful ISO-2022 profiles, with 3,960–11,365 Python-only scalar encodes per profile relative to `encoding_rs` (and per-profile Rust-only/remapped counts in `PORT_STATUS.md`); implementing five generated stateful codecs is deliberately left unstarted. The port does not claim unbounded fuzzing or exhaustive multibyte variant coverage.

**Readiness snapshot (reviewed through 2026-08-14):** the small-project Python→Rust thesis is proven, and the first real multi-package scaled component is now green on vendored `sqlparse` 0.5.5 (~4.1k LOC): 243 entities / 454 relationships / 253 call observations, 229 resolved calls, structural pass rate 1.0, 0 dangling targets, 0 semantic suspicions after the audit heuristic was made import-aware, and a seeded 12/12 manual precision sample including cross-package constructor/module calls. The `sqlparse.split` Rust port passes the staged gates: generated token tree + 811 keyword entries, lexer parity across 40 differential cases / 341 tokens, StatementSplitter + split pipeline, and `port_eval` with 65 golden cases, 3/3 context packs, and 0 manual fixes. Phase 0 is complete. The Python portion of Phase 1 and the deterministic core of Phase 2 are usable (generic package indexing, provenance, audited call edges, local traversals and context packs), while optional LLM overlays remain open. Phase 3 now has one installable `graphrag-code` / `python -m graphrag_code` product surface over the compatible source-checkout scripts, including a bounded cycle-safe multi-hop `subgraph` query (deterministic structural exploration only; not an alias for one-hop `neighbors`). Visualization and natural-language/optional GraphRAG query backends remain open. MCP remains exactly 16 read-only tools, exposes `subgraph` immediately after `neighbors`, exposes `components` immediately after `subgraph`, exposes `strong_components` immediately after `components`, exposes `condensation` immediately after `strong_components`, and exposes `degree_ranking` immediately after `condensation`. CLI/Python `degree-ranking` is implemented as raw directed relationship-row degree accounting; MCP exposes that existing producer and does not add another degree algorithm. The Phase 4 small-project milestone is exceeded. Phase 5 has a successful scaled-component pilot. Phase 6 now has a working C frontend and three bounded C→Rust ports: `jsmn` (43 C calls, 165 observations, `port_eval` 18 golden / 0 manual fixes), `inih` (38 C calls, 35 observations, `port_eval` 21 golden / 0 manual fixes), and `cJSON` (495 C/runner calls including mutation-runner helpers, 144 observations, library subgraph still 188 cJSON→cJSON calls; `port_eval` 59 golden / 0 manual fixes). `inih` surfaced the expected preprocessor/config-awareness boundary (now labelled as provenance on the published graph and in context packs – detection, not clang expansion), and the Rust port covers callback/error-line string parsing while file I/O is measured but not ported. `cJSON` closes the first ownership-heavy C slice with a structure-preserving Rust tree (`Box`-owned child/next links and `Drop` mirroring recursive `cJSON_Delete`), bounded float-printing fidelity matching cJSON's `%1.15g`/`%1.17g` paths, and an owned mutation/builder API verified by per-step C traces under ASan. Its generated header audit now records 68 covered functions, six C-trace-reached and compiler-backed safe-borrow rejections, and four global allocator/error-state exclusions. The six snippets construct their borrowed candidates but fail at the trace's later source mutation with E0502; the audit identifies the shared-byte/shared-node or handle redesign and its cost instead of calling a different Rust representation impossible. Optional configured include edges, matched Clang signature fields, and matched Clang direct-call evidence fields now exist (each default off), but full macro expansion, type graphs, ABI verification, function-pointer resolution, multi-config semantics, and larger C/C++ production-readiness remain open. Phases 6–7 are not production-ready. In short: a convincing research prototype with strong small-scope evidence, one real repository-scale Python component port, and three C→Rust proofs including a struct/pointer/heap-ownership slice, not yet a production migration product.

**Initializer API closure (2026-07-30):** generic Python indexing now includes
only direct public definitions from `__init__.py`, naming them under the
package title (for example `sqlparse:split`), and retains re-export-only
initializers as an explicit non-alias boundary. A manifest-derived,
runtime-importing initializer audit covers all nine Python targets: four direct
definitions are all present; 113 re-exports are reported separately rather
than inflated into duplicate graph entities. The `sqlparse.split` target named
by the Rust port is therefore an actual graph entity, not merely a module API
outside the graph. See `examples/sqlparse/PROVENANCE.md` for the census and
call-oracle effect. The current full-suite expectation is **2113 passed, 2 xfailed**;
this 2026-08-14 persisted-integrity doctor update supersedes the earlier 721-passed /
2026-07-26 gate snapshot. The product CLI is the installable ``graphrag-code``
console command (`python -m graphrag_code`); source-checkout ``scripts/*.py``
paths remain compatibility entries. Packaging does not bundle published graphs
or claim standalone ``port-eval --all-gates``. ``--reuse-unchanged`` on
``index-python`` / ``index-c`` / ``index`` is opt-in whole-snapshot reuse for
supported deterministic configurations (Python without ``use_advanced``; C
without compiler/Clang overlays). It is not per-file incremental indexing or
a watcher. Unsupported modes rebuild normally or reject explicit reuse with
exit 2. ``corpus_hash`` semantics are unchanged. ``port-eval`` still performs
fresh disposable indexing. ``graphrag-code mcp --graph <root> --indexer auto``
is a local stdio MCP adapter over one existing graph (no network, no HTTP,
no indexing/publishing/port-eval). Tools are read-only and scoped to the
startup graph root. Each call takes a shared `.publish.lock` reader lease
and pins one published snapshot for the duration of the call. Cooperating
publishers wait. MCP rejects managed graphs without an existing regular lock
file instead of silently serving without retention protection; this is not a
distributed lease and not protection against tools that ignore the lock.
`graphrag-code adopt-publication-lock --offline-confirmed` is the explicit
offline migration that creates only `.publish.lock` on a doctor-valid
managed graph published before the protocol existed. It is never an
automatic MCP or doctor side effect. The flag is an operator assertion that
no legacy reader or publisher that ignores the lock is active; the program
cannot prove quiescence, because those processes never open the file.
Automatically touching `.publish.lock` would split the locking domain.
Immutable checked-in `byog_*` evidence remains on the explicitly unleased
compatibility path. ``graphrag-code snapshot-history`` and
``snapshot-diff`` (and MCP ``snapshot_history`` / ``snapshot_diff``) list
or structurally compare retained published snapshots under one shared
reader lease. This is persisted-row comparison, not semantic
equivalence. Staging is not history. The CLI is strict by default and
never creates the lock; staging notices keep the exact count and at most
20 returned names. Missing fields differ from explicit nulls, and JSON
booleans differ from numbers. ``--allow-unlocked-legacy`` is CLI-only
and has no retention guarantee. MCP remains strict and does not expose that
option. ``graphrag-code snapshot-activate --activate-confirmed`` is the
explicit mutating CLI that changes only ``current`` to an
already-published retained snapshot. Confirmation and
``--expected-current`` are mandatory. It requires an already-adopted
publication lock, never creates that file, and is intentionally absent
from MCP. Query, context-pack, doctor, and status tools accept an
optional retained-snapshot selector (``--snapshot`` / MCP ``snapshot``,
default ``current``). Historical reads do not require activation and do
not change ``current``. One shared reader lease pins the selected
published snapshot against cooperating keep-last retention. The existing
regular lock is required for explicit query/context CLI selectors and is
never created by them. Their omitted-selector path keeps legacy-flat and
pre-lock compatibility and has no retention guarantee. ``graphrag-code
snapshot-pins`` / ``snapshot-pin`` / ``snapshot-unpin`` manage operator
retention pins in ``.snapshot-pins.json``. That registry is not
activation, backup, or replication. Listing never creates it; pin/unpin
require confirmation and a registry-revision CAS; unpin does not delete
immediately. Cooperating keep-last protects ``current``, doc-claim pins,
and operator pins. A malformed registry aborts publication before
``current`` changes. ``graphrag-code snapshot-retention-plan
--keep-last <N>`` is the read-only report of that same shared selection
helper: current UNION existing claim pins UNION operator pins, then
newest remaining published snapshots up to the effective keep-last
floor of at least 1. It does not prune or delete. Staging is a notice,
not a candidate. Dangling pins are reported, not invented. The command
holds one shared existing-lock lease, never creates
``.snapshot-pins.json`` or ``.publish.lock``, and is intentionally
absent from MCP. ``plan_revision`` binds the decision inputs, schema,
and exact retained/deletion result. ``graphrag-code snapshot-prune
--keep-last <N> --expected-plan-revision sha256:<hex> --prune-confirmed``
recomputes that plan under one exclusive existing-lock lease and deletes
only the CAS-verified candidates. There is no dry-run. A stale revision
changes nothing. Recursive deletion is not transactionally atomic; a
partial prune reports ``partial=true`` and requires a fresh plan.
``graphrag-code snapshot-staging`` is a read-only structural inventory
of direct ``snapshots/.staging-*`` entries. It holds one shared
existing-lock lease, never creates ``.publish.lock``, and does not
delete, quarantine, or infer ownership. Publishers construct staging
without holding the exclusive graph-root publication lock and hold a dedicated advisory
writer lease on ``.staging-<id>/.staging-writer.lock`` during that
private write. Reacquiring existing writer-lock metadata in an
already-managed graph briefly uses a shared graph-lock gate which ends
before payload construction; reacquisition is nonblocking while gated,
and fresh publisher lock creation is not gated.
This prevents cleanup's release/unlink window from handing the same lock
to a waiting cooperative writer. Observed lease contention is not
ownership or liveness.
Missing writer-lock metadata is legacy/unverifiable. Two-scan agreement
is bounded change detection, not proof that a writer is dead. No age
heuristic is used. Inventory ``cleanup_eligible`` stays false.
``staging_revision`` is informational and is not accepted or applied.
``graphrag-code snapshot-staging-cleanup-plan`` is a separate read-only
schema-2 plan over that inventory. Schema 1 was read-only/pre-apply and
is not accepted by apply. A name is a ``deletion_candidate`` only for
a real directory with a canonical suffix, cooperative writer-lock
metadata, and ``not_held_at_scan``. That is not writer death,
ownership, or permission to delete. Observed non-contention is not
the apply command's exclusive writer-lock claim.
``graphrag-code snapshot-staging-cleanup --expected-plan-revision
sha256:<hex> --cleanup-confirmed`` recomputes that plan under one
exclusive existing-lock lease, claims every selected existing writer
lock, revalidates identities, and deletes only the CAS-verified
candidates. There is no dry-run. Recursive deletion is not
transactionally atomic; a partial result reports ``partial=true`` and
requires a fresh plan. The command is intentionally absent from MCP.
``graphrag-code snapshot-maintenance-plan --keep-last <N>`` is a
read-only composite of the current retention plan and the current
schema-2 staging cleanup plan. It holds one shared existing-lock
lease, does not take a nested lease, and is not another mutation
path. ``actionable_components`` names only the apply commands whose
embedded deletion sets are currently non-empty. Applying either
component requires a fresh plan before another mutation.
``graphrag-code snapshot-maintenance-apply --keep-last <N>
--expected-maintenance-revision sha256:<hex> --maintenance-confirmed``
is the CAS apply for that composite. It holds one exclusive
existing-lock lease, never creates ``.publish.lock``, does not take a
nested lease, and does not call the public prune or staging-cleanup
scopes. After writer-lock claims it revalidates from captured
consistency tokens instead of recomputing the cleanup plan. Internal
order is staging cleanup then prune. Recursive deletion is not
transactionally atomic; a partial result requires a fresh plan.
``graphrag-code snapshot-maintenance-reconcile --plan-file
<saved-plan.json>`` is the read-only aftermath inspection. It holds
one shared existing-lock lease and accepts only bounded regular input
files. Before graph inspection it validates the composite and both
embedded self-hashes, direct candidate names, and the exact ordered
schema-1 apply-result outcome when supplied. It does not recover, roll
back, or prove deletion cause.
``graphrag-code snapshot-export-plan --snapshot <id|current>`` is a
read-only inspection of one retained published snapshot. It holds one
shared existing-lock lease, hashes only direct envelope payload files,
and does not create an archive or mutate the graph. The plan is not a
backup and is not authorization to delete anything.
``graphrag-code snapshot-export-apply --snapshot <id|current>
--destination <new-dir> --expected-export-revision sha256:<hex>
--export-confirmed`` is the CAS copy of that payload set into a newly
created destination. It holds one shared existing-lock lease,
never mutates the graph, never overwrites a pre-existing destination,
and does not claim backup or recoverability. Publication is bound
to the held staging inode. A post-publication parent-fsync or
destination-identity failure emits ``ok=false``, ``partial=true``,
and exit 1 without deleting the destination. A crash may leave the
private sibling staging directory.
``graphrag-code snapshot-export-verify --export-dir <directory>
--expected-export-revision sha256:<hex>`` is the read-only check
that one already-created standalone export still contains exactly
that envelope. It does not inspect a managed graph, acquire a
graph lease, or mutate the export. Observed revision uses the
export-plan canonical contract. A stable mismatch emits the
complete report and exits 1. Unsafe structure or concurrent
change exits 1 with empty stdout.
``graphrag-code snapshot-export-reconcile --plan-file
<saved-plan.json> --destination <path>`` is the read-only aftermath
inspection for a saved export plan and optional saved apply result.
It does not inspect a managed graph, acquire a graph lease, or
mutate the destination. Stable absence and stable revision mismatch
emit a complete report and exit 0. It does not recover or prove
that apply created or deleted the path.
``graphrag-code snapshot-export-apply`` creates
``.export-writer.lock`` immediately after anchoring the private
staging directory and holds an exclusive advisory writer lease
through payload construction, staged verification, and the wait
immediately before publication. The lock pathname is removed while
the lease is still held, then the lease is released before atomic
publication. The published destination never contains that file. The lock is protocol
metadata, not ownership or cleanup eligibility.
``graphrag-code snapshot-export-staging --parent <directory>`` is
the read-only structural inventory of direct
``.graphrag-export-*`` children under one selected parent. For
recognized real directories only it may inspect and
nonblocking-probe ``.export-writer.lock``. It does
not inspect a managed graph, acquire a graph lease, infer
ownership or writer activity, plan cleanup, or delete anything. A
matching name is not proof that apply created the entry.
``held_at_scan`` does not change ``writer_activity``.
Inventory ``cleanup_supported`` stays false.
``graphrag-code snapshot-export-staging-cleanup-plan --parent
<directory>`` is a separate read-only schema-2 classification of
those leftovers. Cleanup-plan schema 1 was read-only/pre-apply
(``apply_supported=false``) and is not accepted by apply. Schema 2
sets ``apply_supported=true`` and keeps ``cleanup_applied=false``.
A candidate requires a recognized real directory
whose writer-lock metadata is present, empty, restrictive-mode,
single-linked, and ``not_held_at_scan`` on both agreeing scans.
Prefixed non-candidates are blocked, not omitted.
``deletion_candidates`` is not authorization to delete.
The plan does not accept an expected revision or confirmation.
``graphrag-code snapshot-export-staging-cleanup --parent
<directory> --expected-plan-revision sha256:<hex>
--cleanup-confirmed`` is the separate CAS apply. There is no
dry-run; the plan command is the preview. Confirmation is required
even when the candidate set is empty. Apply anchors the selected
parent, recomputes the schema-2 plan, compares the caller token,
nonblockingly claims every selected existing writer lock,
revalidates parent/staging/lock identities, and only then deletes.
There is no graph lease because this operates on an arbitrary
export parent. Recursive deletion is not transactionally atomic.
A partial result always requires a fresh schema-2 plan; there is
no rollback, trash, quarantine, or recovery. Advisory locks
protect only cooperating processes. Non-cooperating processes
remain outside the protection boundary. No ownership, liveness,
backup, authenticity, or recovery is claimed.
``graphrag-code snapshot-export-staging-cleanup-reconcile --parent
<directory> --plan-file <saved-cleanup-plan.json>`` is the
read-only aftermath inspection for that saved schema-2 plan and
an optional saved schema-1 apply result. It does not mutate,
claim a writer lease, inspect a managed graph, or emit a retry
token. Absence does not prove apply deleted an entry; presence
does not prove apply failed; matching identity does not prove
ownership or continuous identity. A fresh schema-2 cleanup plan
is required before any later apply.
``graphrag-code snapshot-import-plan --graph <root>
--export-dir <directory>`` is a read-only plan for adding one
standalone snapshot export to an existing managed
``current + snapshots/`` graph. It holds one shared existing-lock
graph lease, keeps the export directory and payload descriptors
open through serialization, validates the language-independent
stored snapshot envelope, and classifies an already-published
matching id or an existing ``.staging-<id>`` as blocked. It does
not import, copy, activate, create staging, or mutate either
tree. ``import_performed`` is always false. ``import_revision`` is
a self-consistency/CAS-ready plan token.
``graphrag-code snapshot-import-apply --graph <root>
--export-dir <directory> --expected-import-revision sha256:<hex>
--import-confirmed`` is the CAS apply. It reproduces that plan
from held source descriptors under one shared existing-lock
lease, copies exact source bytes into ``.staging-<snapshot-id>``,
holds ``.staging-writer.lock`` through copying, then acquires one
exclusive existing-lock graph lease for native no-replace
publication. It preserves the source manifest id, leaves
``current`` unchanged, does not inspect or change pins, and does
not run retention. It does not overwrite an existing snapshot id.
A post-publication identity or fsync failure emits
``ok=false``, ``partial=true``, and exit 1 without rolling back
the published snapshot. Pre-publication cleanup reacquires the
exclusive graph lease and identity-checks/claims this invocation's
writer-lock inode; it leaves replaced or unverifiable metadata in
place. A partial result never infers an unchanged current pointer:
``current_after`` is null and ``current_unchanged=false`` unless the
post-publication proof completed. A crash may leave ``.staging-<id>`` and
its writer-lock metadata. Successful import is not activation,
backup, authenticity, or recoverability.
``graphrag-code snapshot-import-reconcile --graph <root>
--plan-file <saved-import-plan.json>`` is the read-only
aftermath inspection for that saved schema-1 plan and an
optional saved schema-1 apply result. It holds one shared
existing-lock graph lease, validates the saved inputs completely
before observing the graph, and does not retry, recover, copy,
publish, activate, pin, prune, clean staging, run retention, or
mutate either tree. Snapshot absence does not prove apply failed;
presence does not prove apply created it; matching revision
proves only payload-contract equality during the bounded
observation window. A present snapshot receives a final complete
held-payload recheck after target-state observation. A fresh import
plan is required before any later apply.
``graphrag-code snapshot-transfer-plan --source-graph <root>
--snapshot <id|current> --target-graph <root>`` is a read-only
plan for a future direct transfer of one retained snapshot from
one managed ``current + snapshots/`` graph to a different managed
graph, without first creating a standalone export directory. It
holds one shared existing-lock lease on each graph for the
complete joint observation. The two leases are acquired in one
deterministic global order independent of source/target role:
canonical UTF-8 path bytes of the real graph root, then
``(st_dev, st_ino)`` as a stable identity tie-breaker. Same-graph
identity, including path aliases for the same inode, is rejected
before nested leases. It validates the language-independent stored
snapshot envelope, classifies an already-published matching id or
an existing ``.staging-<id>`` as blocked, and does not export,
import, copy, activate, create staging, or mutate either tree.
``transfer_performed`` is always false. ``transfer_revision`` is a
self-consistency/CAS-ready plan token accepted only by
``snapshot-transfer-apply`` after that command freshly reproduces
the same plan. A ready plan does not authorize a later apply
without freshly reproducing the complete plan and matching
``transfer_revision``.
``graphrag-code snapshot-transfer-apply --source-graph <root>
--snapshot <id|current> --target-graph <root>
--expected-transfer-revision sha256:<hex> --transfer-confirmed``
is the CAS apply. It holds one source-shared and one
target-exclusive existing-lock lease acquired in that same global
order, copies exact source bytes into ``.staging-<snapshot-id>``,
holds ``.staging-writer.lock`` through copying, then publishes with
a native no-replace rename. It preserves the source snapshot id,
leaves both ``current`` pointers unchanged, does not inspect or
change pins, and does not run retention. It does not overwrite an
existing snapshot id. A crash may leave ``.staging-<id>`` and its
writer-lock metadata. Successful transfer is not activation,
backup, authenticity, or recoverability.
``graphrag-code snapshot-transfer-reconcile --source-graph <root>
--target-graph <root> --plan-file <saved-transfer-plan.json>``
observes both managed graphs against one saved schema-1
transfer plan and an optional saved schema-1 apply result. It
holds one shared existing-lock lease on each graph in the same
global order, validates saved inputs completely before observing
either graph, and classifies each planned snapshot as absent,
matching, or a stable valid revision mismatch. It does not retry,
copy, recover, publish, activate, delete, clean staging, or
mutate either graph. A saved apply result is declaration-only.
A fresh transfer plan is required before any later apply.
Standalone prune and
staging cleanup remain available. Neither prune, staging
inventory, the staging cleanup plan, staging cleanup apply, the
composite maintenance plan, the composite apply, reconcile, the
export plan, the export apply, the export verify, the export
reconcile, the export staging inventory, the export staging
cleanup plan, the export staging cleanup apply, the export
staging cleanup reconcile, the import plan, the import apply, the
import reconcile, the transfer plan, the transfer apply, nor
the transfer reconcile
is an MCP tool. MCP remains exactly 16
read-only tools and stays strict. Advisory locks do not protect
against non-cooperating programs. No search, UI, HTTP service,
repair, or reindex is added.

**Re-export namespace boundary (2026-07-30):** the 113 non-direct initializer
bindings are now measured separately from direct definitions: 73 have a unique
existing defining entity, 2 target skipped initializer modules, and 38 are
scalar values with no source identity. Four golden traces give a measured workload to 50 bindings
over 194 cases and execute 9 defining targets; 63 bindings have no registered
trace and remain explicitly unmeasured. The graph deliberately adds neither
duplicate alias entities nor `exports` relationships: doing the latter would
first add 8 initializer-module nodes for export sources and at most 75 identity edges, but has no
current call-oracle, context-pack, or closure consumer. The three unmapped
SQLParse pairs are generator-expression frames, not aliases. This is a
documented static-namespace boundary, not an impossibility claim.

## 2. High-Level Architecture (Replicable Version)

```
Source Code (C / C++ / Python / etc.)
        ↓
Tree-sitter (or lang-specific: syn for Rust, etc.) + optional deeper static analysis (call graph resolution, dataflow basics)
        ↓
Structured extraction → "Documents" or direct entities:
  - Nodes: File, Module/Package, Function/Method, Type/Struct/Enum/Class, Variable/Constant, Trait/Interface, etc.
  - Edges: CONTAINS, CALLS (static + heuristic), IMPORTS/DEPENDS, IMPLEMENTS/INHERITS, USES_TYPE, DEFINES, etc.
  - Rich attributes + snippets + docs/comments.
        ↓
(MVP / recommended) Export the deterministic source graph as GraphRAG BYOG artifacts:
  - entities.parquet: symbols/modules/files with descriptions + linked text units.
  - relationships.parquet: structural edges with descriptions, weights, provenance.
  - text_units.parquet: source snippets, docs, tests, build metadata, and extracted facts.
(Primary, no external API) Local deterministic graph layer:
  - schema validation: no dangling endpoints/text units, provenance on every fact.
  - DuckDB/SQLite/NetworkX traversals: callers/callees, modules, impact, dependency order.
  - context-pack generation: symbol neighborhood + source snippets + tests/golden traces + behavior contract notes.
  - optional deterministic/community heuristics until LLM summaries are introduced.
(Optional later) Run official GraphRAG LLM workflows or another LLM summarizer over the BYOG graph to add community reports, Global/Local/DRIFT search, embeddings, and semantic overlays. Validate every hard relation against deterministic facts.
        ↓
Communities/summaries:
  - local first: module-aware groups, graph metrics, deterministic summaries/context packs.
  - optional later: Leiden + bottom-up LLM community summaries.
        ↓
Storage: GraphRAG artifacts (parquet/index) as canonical outputs + DuckDB/SQLite for local queries + optional graph DB (Memgraph/Neo4j/FalkorDB) for visualization and Cypher-style traversals + embeddings for hybrid search.
        ↓
Query layer (local graph-native traversals first; adapted GraphRAG global/local optional later):
  - Local sensemaking: "What are the core modules, dependencies, and data flows?"
  - Symbol-centric + neighborhood.
  - Context packs as portable memory for local agents and manual review.
  - Community summaries as optional large-system memory once an LLM endpoint is configured.
        ↓
Porting/Translation Agent System (multi-step, iterative):
  - Decomposition planner (respect dep graph; bottom-up or community-by-community).
  - Context assembler: pull relevant subgraph + deterministic context packs + original snippets + tests/golden traces + porting rules (Rust idioms, ownership patterns, error handling, unsafe boundaries for C).
  - Translator path: manual/local-agent-assisted first (Codex / Claude Code / Grok Build); optional LLM API/local endpoint later.
  - Verifier: cargo check/build, run tests (migrate or harness original tests), fuzz/differential if applicable, static analysis (clippy, miri for unsafe).
  - Refiner loop: feed errors + more context back; human review gates for critical components.
        ↓
Output: Rust crate(s) mirroring (or improving) original structure + updated graph artifacts (dual C/Rust or migrated facts).
```

**Key success enablers for "zero errors" (as claimed in the anecdote):**
- Extremely rich, low-hallucination context (deterministic AST/static graph first; optional LLM semantic overlay later).
- Incremental + verifiable process (never port everything in one shot).
- Strong verification harness (original tests are gold; add property-based/differential testing).
- Human oversight on architecture and safety-critical pieces.
- For C→Rust specifically: memory model translation is non-trivial; start with higher-level or well-tested components; use Rust's unsafe + FFI bridges where needed initially.

**MVP target (first concrete milestone):**
- Input: one small multi-file Python project with tests and deterministic behavior (CLI/game logic preferred over graphics-only behavior).
- Output graph: BYOG-compatible `entities.parquet`, `relationships.parquet`, `text_units.parquet`.
- Self-contained validation: schema tests generate fresh BYOG in temp dirs; no required pre-generated outputs and no external API.
- Query/context layer: answer at least 10 fixed architecture/behavior questions using local graph traversals and `context-pack` outputs with cited symbols/snippets.
- Porting loop: translate one dependency-ordered unit at a time using local tools/agents + context packs, run `cargo check`, run ported/golden tests, and record every manual intervention.
- Baseline: compare against plain full-context local-agent/manual prompting and vector-RAG-over-code when available. Cloud LLM baselines are optional, not required for MVP.

## 3. Phased Implementation Plan (Actionable, Verifiable)

**Success criteria overall:** Reproduce a high-fidelity port of a non-trivial open-source Python (or small C) project within an explicit behavior contract, where the output compiles, passes original (or ported) tests, and matches the contract's key scenarios. Compare against a raw-source baseline without presuming superiority; document any parity or negative result, scope limits, costs, token usage, and failure modes.

### Phase 0: Foundations & Reproduction Experiments (1-2 weeks)
- Clone and run microsoft/graphrag on sample narrative data + a small multi-file Python codebase. Measure baseline global Q&A quality.
- Watch the key talks in full; transcribe/clip the exact demo segments and quotes. Note any visible UI or output style from the game demo.
- Set up the workspace: Python + Rust toolchains, GraphRAG package/CLI for BYOG compatibility, pyarrow/pandas, tree-sitter (Python bindings or CLI + tree-sitter-language-pack / tree-sitter-cli), DuckDB/SQLite, NetworkX, and optional graph DB (Neo4j/Memgraph via Docker only when visualization or Cypher is needed). Keep LLM access provider-pluggable, but do not require any external API for the first pipeline.
- Pick 2-3 small target projects for experiments:
  1. A tiny public Python game or CLI app similar in spirit to the demo (~few hundred LOC, multiple modules/files, clear structure).
  2. A small well-tested C library or component (e.g. a data structure or parser with tests).
  3. Something from the graphrag repo itself or a simple Rust crate (for round-tripping later).
- Baseline: Use manual/local-agent prompting over raw code and, where available, basic vector RAG over code chunks. External cloud LLM baseline is optional.
- Decide the initial graph schema and provenance model before writing agents. Every node/edge should retain `source_file`, byte/range span, extractor name, confidence, and whether it is deterministic or LLM-inferred.
- **Verification:** One tiny code corpus converted to GraphRAG-compatible BYOG tables; self-contained schema tests; deterministic golden traces; GraphRAG config/key boundary documented if official LLM workflows are not run.

### Phase 1: Robust Multi-Language Code Parser & Structural Knowledge Graph (Core)
- Integrate tree-sitter (primary: Python, C, C++, Rust grammars are mature; add others as needed). Handle error-tolerant parsing (critical for real codebases).
- Add semantic analyzers where Tree-sitter is insufficient:
  - Python: stdlib `ast`, importlib/module resolution, optional Jedi/Pyright/mypy signals for references and types.
  - C/C++: clang tooling over `compile_commands.json`; Tree-sitter alone is not enough for macros, includes, overloads, templates, or reliable type facts.
  - Rust: rust-analyzer or `cargo metadata`/`syn` for crate graph and item-level facts.
- Build extractor that walks AST to produce:
  - Symbol inventory (with signatures, docs, visibility, attributes like `unsafe`, complexity metrics).
  - Containment hierarchy (file → module → item).
  - Call graph (conservative static calls; note limitations on dynamic/indirect). Python dynamic-dispatch *detection* (2026-07-26): `scripts/python_dynamic.py` stamps `dynamic_dependent` / `dynamic_reasons` for registry/dict callable tables, `getattr` with non-literal names, polymorphic receivers from those lookups, `__getattr__`, `importlib` dynamic imports, and **decorator registration** (`@Owner.register_*` writing `cls.REG[key] = subclass`) – provenance labels; **registry promotion** may add non-deterministic `calls` edges for statically named Name-table members at labelled dispatch sites only. Validated on `jsonpatch` (`JsonPatch._get_operation` / `JsonPatch.apply`) and `semantic_version` (`BaseSpec.SYNTAXES`); context packs surface `dynamic_warning`. Runtime registry oracle (`--vs-runtime`, subprocess import, independent discovery): Name + decorator tables **8/8 agree** (jsonpatch 6 + SYNTAXES 2); isodate lambdas **25 missed** (left unguessed); non-callable AST hits (`UNICODE_RANGES_COMBINED`, `_TRANSLATIONS`, `KIND_ALIASES`) are false-positive tables, not misses. **Call-graph oracle** (`scripts/call_graph_oracle.py`, 2026-07-28): golden contracts under `sys.setprofile` vs published `calls` edges – confirmed / missed / unconfirmed kept separate. **Cross-module import resolution**: `from X import Y` + param defaults / `self` attrs – jsonpatch oracle **confirmed 8→21, missed 25→12**. **Registry-dispatch promotion** (2026-07-30): statically named Name table members at labelled dispatch sites become `calls` edges at conf 0.75 / `is_deterministic=False` (never lambda/runtime-only); jsonpatch oracle **confirmed 21→27, missed 12→6**; adequacy missing **29→7** of 37; audit **1.0** at 134 calls. Structural `audit_call_edges` is still not an observed-truth claim by itself.
  - Type/dependency/use edges.
  - Basic control/data flow annotations where cheap.
- Serialize to GraphRAG BYOG tables as the primary contract, and also keep a normalized graph model (nodes/edges + properties) for traversals. Support incremental updates (file hash + watcher or git diff).
- Store: Start with parquet + DuckDB/SQLite + NetworkX. Add Neo4j/Memgraph only when graph-native queries/visualization are clearly useful.
- Add basic embeddings for hybrid (symbol name + signature + summary).
- **Optional but high value:** Simple call-graph resolution heuristics and import resolution.
- **Verification:** For a medium repo (e.g. 10k-50k LOC), produce accurate "list all public functions calling X transitively", "module dependency graph", "most complex functions". Compare precision/recall manually or against known structure. Track false edges separately from unknown edges; do not let uncertain dynamic calls masquerade as ground truth.

### Phase 2: Local Query/Context Layer + Optional GraphRAG Workflows
- Primary track: build local, no-external-API graph operations on the BYOG outputs:
  - schema validation and provenance audits.
  - graph traversals: callers/callees, direct/transitive dependencies, modules, import graph, affected symbols.
  - deterministic context packs for porting/review: entity + neighbors + source snippets + test/golden contract + confidence/provenance.
  - simple local community/grouping heuristics (module/package grouping, connected components, centrality) before any LLM summarization. Weakly connected `components` is **implemented** as a bounded topology summary (`--edge-type`, `--max-components`, `--max-nodes-per-component`): not semantic community detection, Leiden, clustering, centrality, architecture inference, GraphRAG, or natural-language analysis. MCP exposes that existing producer immediately after `subgraph` and does not expose DOT or output-format selection. Representatives remain smallest UTF-8 titles, not leaders. Component size remains topology, not importance. Directed `strong-components` is **implemented** as exact mutual-reachability SCCs over selected persisted rows (`--edge-type`, `--max-components`, `--max-nodes-per-component`): not weak components, semantic communities, Leiden, architecture, hierarchy, importance, dependency/build order, or a runtime recursion/deadlock proof. MCP exposes that existing producer as `strong_components` immediately after `components`. Structural `degree-ranking` is **implemented** as raw directed relationship-row degree accounting (`--rank-by total|incoming|outgoing`, `--edge-type`, `--max-nodes`): self-loops contribute in=1/out=1/total=2; parallel rows each count; isolates remain; endpoint-only nodes are marked non-entities. This is not PageRank, betweenness, closeness, eigenvector centrality, importance, architecture, community detection, GraphRAG, or semantic meaning. MCP exposes that existing producer immediately after `condensation` as `degree_ranking` and does not expose DOT, a metric, a normalized score, or an ordinal rank. Structural `dependency-order` is **implemented** as a deterministic containment order over persisted `contains` rows: source-before-target across SCCs, UTF-8 presentation inside cycles, full unbounded `List[str]`. It is not a build/import/call/semantic order, hierarchy, architecture, GraphRAG, or a porting plan, and MCP does not expose it. Directed `condensation` is **implemented** as a bounded SCC condensation DAG over selected persisted rows (`--edge-type`, `--max-components`, `--max-nodes-per-component`, `--max-edges`): Kahn order heap-keyed by representative UTF-8 bytes, aggregated condensation edges, exact totals. It is not weak components, cycle enumeration, transitive closure, a unique rank, build/import/call order, Leiden, architecture, GraphRAG, or a runtime-cycle proof. MCP exposes that existing producer as `condensation`, the sixteenth read-only tool added, immediately after `strong_components` and immediately before `degree_ranking`. Strong-components, dependency-order, and condensation share one iterative SCC engine; condensation and dependency-order share one condensation-DAG helper. MCP remains exactly 16 tools.
- Optional track: prefer a thin wrapper over microsoft/graphrag before forking. Use the official BYOG path for deterministic graph ingestion when an API key or local OpenAI-compatible endpoint is available:
  - `entities.parquet` for files/modules/symbols.
  - `relationships.parquet` for structural edges.
  - optional `text_units.parquet` for source snippets, docs, tests, and build context.
  - workflows: start with `[create_communities, create_community_reports]`; add `generate_text_embeddings` for Local/DRIFT/Basic search.
- Domain-specific prompts (critical!):
  - Entity types tailored: `function`, `struct`, `enum`, `trait`, `module`, `file`, `constant`, `type_alias`, etc.
  - Relationship types or rich descriptions: `calls`, `is_called_by`, `defines`, `uses_type`, `imports`, `implements`, `overrides`, `contains`, semantic "related_to" or "depends_on_semantically".
  - Claims/covariates: "assumes non-null", "thread-safe", "performance critical path", "error handling strategy: returns Result", "porting note: uses raw pointers here".
- If running official GraphRAG workflows, run the index pipeline on the Phase 1 BYOG tables plus enriched symbol "documents". Keep deterministic and LLM-inferred facts in separate columns/tables so provenance is visible.
- If running official GraphRAG workflows, leverage existing community detection (Leiden) and bottom-up summarization. Tune or add code-aware summarization prompts: "Describe the responsibilities, invariants, data flows, and architectural role of this community/subsystem. Note any cross-cutting concerns or porting considerations."
- Generate local first "global" views: top-level architecture outline, key interfaces, dependency order, error models, and behavior-contract inventory. Upgrade these with GraphRAG community reports only when an LLM endpoint is configured.
- Add code-specific query modes (e.g. "impact analysis" subgraph).
- **Hybrid boost (optional later):** Keep the precise AST-derived edges as ground truth. If LLM extraction is added, use it only for semantics, summaries, and soft relations. This addresses known weaknesses of pure LLM-extracted graphs on code (hallucinated calls, missed edges).
- **Verification:** On the small demo project, local context-pack queries like "explain the overall architecture and main data flow" or "what are the invariants around the game state?" produce coherent, accurate, non-contradictory packs that reference specific symbols and tests. If GraphRAG reports are later enabled, compare them against local packs using a small adjudication rubric: factuality, completeness, cited provenance, token/cost, latency.

### Phase 3: Query, Exploration & Visualization Layer
- Expose graph-native queries first (via DuckDB/SQLite/NetworkX or custom traversals). Expose GraphRAG global/local/DRIFT only as an optional backend.
- Build a small query API before a UI. Core commands should return structured JSON as well as human-readable text:
  - `index <repo>`
  - `query-global <question>`
  - `query-symbol <symbol>`
  - `subgraph <symbol-or-module>` – **implemented** as a bounded cycle-safe BFS induced subgraph over stored relationships (`--direction outgoing|incoming|both`, `--max-depth` / `--max-nodes` / `--max-edges`, exact `--edge-type` allow-list). Caps truncate returned material; totals within depth/filter stay exact. Direction does not rewrite stored edge orientation. `--dot` is a deterministic Graphviz DOT interchange over that same producer result (stdout only; Graphviz is not invoked or required; not an image renderer or interactive UI). `--json` and `--dot` are mutually exclusive. This is deterministic structural exploration only: not natural-language search, semantic inference, GraphRAG, community detection, architecture understanding, completeness beyond stored relationships, or a semantic/community visualization. MCP stays exactly 16 read-only tools, does not expose DOT, and registers `subgraph` immediately after `neighbors`.
  - `components` – **implemented** as a deterministic weakly-connected-components summary over persisted entity titles and selected relationship rows (`--edge-type`, `--max-components`, `--max-nodes-per-component`). Weak connectivity ignores direction only for membership. Caps truncate returned lists; totals stay exact. Representative is the smallest UTF-8 title, not a leader. Not semantic community detection, Leiden, clustering, centrality, hierarchy, architecture, GraphRAG, or natural-language meaning. CLI/JSON/human remain; this milestone has no DOT. MCP stays exactly 16 tools, registers `components` immediately after `subgraph`, and does not expose DOT or output-format selection.
  - `strong-components` – **implemented** as exact directed strongly connected components over selected persisted relationship rows (`--edge-type`, `--max-components`, `--max-nodes-per-component`). Direction is preserved. Caps truncate returned lists; totals, internal/cross/self-loop counts, and cyclic-SCC counts stay exact. Representative is the smallest UTF-8 title, not a leader. `is_cyclic` is mutual directed reachability only. Not weak components, semantic communities, Leiden, architecture, hierarchy, importance, dependency/build order, a runtime recursion/deadlock proof, GraphRAG, or natural-language meaning. CLI/JSON/human remain; this milestone has no DOT. MCP stays exactly 16 tools, registers `strong_components` immediately after `components`, and does not expose DOT, a graph path, a symbol, a direction, a rank, or an algorithm.
  - `degree-ranking` – **implemented** as raw directed relationship-row degree ranking (`--rank-by total|incoming|outgoing`, `--edge-type`, `--max-nodes`). Each selected row adds one outgoing count at `source` and one incoming count at `target`. Self-loops contribute 1/1/2. Parallel rows each count. Isolated entities remain zero-degree. Endpoint-only titles are non-entities. Caps truncate returned rows; totals and `sum(in)==sum(out)==n_edges_total`, `sum(total)==2*n_edges_total` stay exact. Canonical ranking then UTF-8 title bytes; no ordinal `rank` field. Not PageRank, betweenness, closeness, eigenvector centrality, normalized score, importance, leadership, architecture, communities, hierarchy, GraphRAG, or semantic meaning. CLI/JSON/human remain; this milestone has no DOT. MCP stays exactly 16 tools, registers `degree_ranking` immediately after `condensation`, and does not expose DOT, a graph path, a symbol, a direction, a metric, a normalized score, or an ordinal rank.
  - `dependency-order` – **implemented** as a deterministic structural containment order over persisted `contains` rows (`source contains target`). Cross-SCC sources appear before their targets. SCC members stay contiguous in UTF-8 title order (presentation inside a cycle, not a topological claim). Isolated entities and contains endpoints remain; non-contains endpoints are excluded unless they are entities. The public result is the full `List[str]`; `--json` emits that list. Unbounded full-list legacy surface: no max-nodes, edge-type, cycle metadata, or DOT. Not a build/import/call/semantic dependency order, hierarchy, architecture, ownership proof, porting plan, GraphRAG, or natural-language analysis. MCP stays exactly 16 tools and does not expose `dependency_order`. Strong-components, dependency-order, and condensation share one iterative SCC engine; dependency-order output remains the containment condensation list.
  - `condensation` – **implemented** as a bounded directed SCC condensation DAG over selected persisted relationship rows (`--edge-type`, `--max-components`, `--max-nodes-per-component`, `--max-edges`). Components are exact mutual-reachability SCCs returned in deterministic Kahn order (heap keyed by representative UTF-8 bytes). Each condensation edge is one distinct ordered SCC pair and stores the exact selected-row count for that pair. Caps truncate returned lists; totals, internal/cross/self-loop counts, cyclic-SCC counts, and condensation-edge totals stay exact. Representative is the smallest UTF-8 title, not a leader. Topological position is not an ordinal rank or semantic layer. `is_cyclic` is mutual directed reachability only. Not weak components, cycle enumeration, transitive closure or reduction, path enumeration, build/import/call/execution/semantic dependency order, architecture, hierarchy, ownership, leadership, importance, Leiden, centrality, GraphRAG, a runtime recursion/deadlock proof, or natural-language meaning. `--dot` is a deterministic Graphviz DOT interchange over that same producer result (stdout only; Graphviz is not invoked or required; not an image renderer or interactive UI). `--json` and `--dot` are mutually exclusive. MCP stays exactly 16 tools, registers `condensation` as the sixteenth read-only tool added immediately after `strong_components` and immediately before `degree_ranking`, and does not expose `condensation_graph`, DOT, a graph path, a symbol, a direction, a rank, or an algorithm. Condensation and dependency-order share one condensation-DAG helper; strong-components keeps size/internal-edge ordering.
  - `context-pack <symbol-or-module> --purpose port-to-rust`
- CLI / simple TUI or Streamlit/Gradio web UI for:
  - "Index this repo".
  - Natural language questions over the code graph.
  - "Show me the subgraph for module X and its direct dependencies".
  - Visualize communities/hierarchy (interactive UI / Memgraph Lab / Neo4j Browser style still future). Bounded `subgraph --dot` and `condensation --dot` are implemented as deterministic Graphviz DOT interchange on stdout; they do not invoke Graphviz, render an image, infer communities, or provide a UI.
- Support "explain this function in context of the broader system".
- **Verification:** Developer can explore a medium codebase faster and more accurately than with grep + ad hoc file reads. Quantitative: fewer tool calls needed for architecture questions (inspired by Codebase-Memory evaluations). Context packs are stable/reproducible and include enough provenance for review.

### Phase 4: Translation / Porting Agent(s)
- Implement a controller/agent loop (LangGraph, CrewAI, or custom state machine; or even simple scripts at first).
- Steps per component or community:
  1. Select target (planner uses dep graph + complexity to order work; prefer leaves / well-contained units).
  2. Write or retrieve a behavior contract for the target: public API, inputs/outputs, state transitions, errors, invariants, side effects, performance-sensitive paths, and known original bugs to preserve or intentionally fix.
  3. Assemble context package: deterministic context pack + local subgraph (entities + relations) + original source snippets + docs/tests/golden traces + extracted claims/porting notes + target language rules (Rust idioms, ownership patterns, `Result`/`Option`, no silent panics in production paths, explicit unsafe boundaries, etc.). Community summaries are optional additions if available.
  4. Generate candidate Rust using manual/local-agent-assisted workflow first (Codex / Claude Code / Grok Build); structure-preserving initially: same modules/files where sensible, more idiomatic only after tests pass.
  5. Verify: parse/compile (rustc/cargo), link if needed, run relevant tests. Capture errors, warnings, and behavioral deltas.
  6. If failures: feed compiler/test output + more targeted graph context (e.g. "the types used here") back to refiner. Limited iterations; escalate to human on persistent issues.
- For Python→Rust: Focus on semantics, performance (avoid unnecessary clones), async if original used it, etc.
- For C→Rust (harder, do later): Explicit handling of pointers (raw → references/Box/Arc where provable), allocators, error codes → Result, undefined behavior risks (document or eliminate), FFI boundaries.
- Dual output: "port" (close to original structure for easy diff/review) and "idiomatic refactor" suggestions.
- **Verification (Phase 4 milestone):** Successful high-fidelity port of the small Python example game (or equivalent). It compiles cleanly, runs, and matches the declared behavior contract on sample inputs and golden traces. For graphical examples, prefer deterministic state/frame/event traces over vague "looks identical" claims. Provide side-by-side diff + test results. Run the same raw-source baseline for comparison and report the observed manual-fix/cost result without assuming a graph advantage.

### Phase 5: Verification Harness, Testing & Iteration at Scale
- Build or integrate a test harness: auto-migrate unit tests where possible, or create differential/black-box tests that exercise the same public surface.
- Add golden-master and trace-based tests before porting when the original project lacks sufficient tests.
- Add property-based testing (proptest/quickcheck) for invariants discovered in the graph, but label whether each invariant is deterministic, inferred, or human-approved.
- For C ports: use sanitizers on the original where possible, then miri/cargo-fuzz/proptest on the Rust side. Undefined behavior in the source must be documented because "equivalent behavior" may be ill-defined.
- Metrics: compile success rate, test pass rate, semantic equivalence (execution traces, output matching), performance delta (optional).
- Incremental re-indexing and re-porting support (change a C module → update graph → re-port affected Rust pieces with context of prior ports).
- Human review workflow: generated ports in PR-like format with graph provenance ("this translation used community summary X and these 12 symbols").
- **Verification:** Apply the full pipeline to a larger component (target 5k-20k LOC well-tested original). Measure engineer-time vs. quality. Document any remaining manual interventions.

### Phase 6: Scale, Cost, C/C++ Specifics, Production Readiness
- Handle million-line codebases: streaming/chunked indexing, parallel extraction, deterministic summary/context-pack caching, sharded or sampled community work. If optional LLM stages are enabled, use cheaper/local models for summarization and stronger models only for synthesis/refinement.
- Cost tracking and optimization for optional LLM-backed stages (GraphRAG indexing can be token-heavy); deterministic stages should report CPU/time/storage instead.
- C/C++ specifics:
  - Require build-system capture (`compile_commands.json`, include paths, defines, generated files) before claiming reliable C/C++ facts.
  - Bootstrap/port status (2026-06-25): vendored MIT `jsmn` indexes via `scripts/extract_c.py` / `scripts/index_c.py` using tree-sitter-c. The resulting graph has 32 entities, 72 relationships, 165 call observations, 43 resolved calls, and `audit_call_edges` pass rate 1.0 with 0 anomalies/dangling/semantic suspicions. The bounded `jsmn_parse` C→Rust port passes `port_eval` with 18 golden cases and `manual_fixes=0`. The second C target, BSD-licensed `inih`, has a clean C graph (19 entities including the golden runner, 54 relationships, 35 observations, 38 calls, pass rate 1.0) after rejecting reserved-word phantom functions caused by preprocessor-fragmented bodies, and its bounded default-config string parser C→Rust port passes `port_eval` with 21 golden cases and `manual_fixes=0`. The third C target, MIT `cJSON`, has a clean ownership-heavy graph (131 entities including the golden runner, 367 relationships, 125 observations, 239 calls, pass rate 1.0; library subgraph 125 entities / 188 calls), including struct typedef entities and recursive ownership self-edges. Its structure-preserving `parse -> inspect -> print -> drop/delete` C→Rust ownership slice passes `port_eval` with 52 golden cases (22 ownership + 30 float-print) and `manual_fixes=0`. This proves the audit/port rails are frontend-agnostic at small-to-medium C scope and now covers a bounded struct/pointer/heap-ownership slice, but not yet clang-accurate C/C++ semantic extraction, full C API migration, or production-scale C/C++ porting.
  - cJSON update (2026-07-26): the same gate covers owned mutation/builder and structural operations – 59 golden cases (22 parse ownership + 30 float print + 7 mutation traces), C ASan over all mutation traces, and a safe Rust `Box`-transfer API for constructors, typed arrays, add/insert/detach/delete/replace, duplicate, compare, value accessors, parse/print options, setters, minify, and object helpers. A generated header audit records 68 covered functions, 6 C-trace-reached compiler rejections for safe-borrow alias candidates, and 4 executable global-state refusals. Each rejection reaches the C-observable later mutation, then records its exact E0502 and the shared-byte/shared-node or handle redesign it would cost; full C API migration is still not claimed.
  - Preprocessor *detection* (2026-07-26): `scripts/c_preprocessor.py` (no clang) stamps `preprocessor_dependent` / `preprocessor_reasons` on entities, call edges, and observations from source text plus `compile_commands.json` – non-guard `#if`/`#ifdef` regions, function-like macros, `-D` defines, and entity bodies containing directives. Labels are provenance only: they do not flip `is_deterministic`, drop edges, or change audit pass rates. Published C graphs now carry the columns (`byog_jsmn` / `byog_inih` reindexed with structure unchanged; `byog_cjson` first stamped in place on the pre-mutation-runner snapshot, then deliberately reindexed the same day to include the mutation golden runner – 495 calls full graph, 188 library-only – without mixing those steps). **Published liveness defaults to `eval_mode=no_compiler`** (host-independent; platform macros stay unknown); local analysis may opt into `--compiler-builtins`. Snapshot manifests record `preprocessor_liveness` (`eval_mode`, toolchain identity when host-specific, `macro_seed_digest`, `host_independent`); re-stamping against a mismatched digest refuses unless `--allow-toolchain-drift`. Context packs surface a top-level `preprocessor_warning` (e.g. `ini:ini_parse_stream`). On the current cJSON graph, 0/495 trusted call edges are flagged (library 0/188); the unconditional-internal-call claim is therefore true for both the full graph and the library subgraph. This is **not** macro expansion, include resolution, typedef-chain resolution, or config-aware dead-branch elimination – those still require clang over `compile_commands.json`.
  - Pre-process with clang tooling or additional static analyzers for aliasing, ownership hints, true macro expansion, and type facts (where possible).
  - Model remaining preprocessor/macros (expansion, multi-config), platform conditionals as first-class build variants, ABI boundaries, generated code, and external dependencies explicitly.
  - Safe subset first; isolate unsafe.
  - Map common patterns (manual memory → RAII/smart pointers, goto/error handling → Result + ? , threads → std or tokio with care).
  - Reference existing public work on C-to-safe-Rust (e.g. formal transpilation research).
- Add support for preserving or improving performance characteristics (mark hot paths from graph).
- Packaging: Docker for the full pipeline, VS Code extension or LSP-adjacent features?, MCP server exposure (following community trends) so agents in Cursor/Claude/etc. can use the code graph as a tool.
- Evaluation suite: multiple ports with before/after metrics.
- **Verification:** Index + useful queries on a large open source C/C++ project (or significant subsystem). Successful pilot port of a non-trivial C component with tests.

### Phase 7: Polish, Documentation, Benchmarking & Community
- Comprehensive docs: how the graph is built, prompt tuning guide for code domain (modeled after GraphRAG's), examples of successful ports.
- Reproduce the spirit of the original demo as a canonical example.
- Benchmarks vs. baselines (raw-code local-agent/manual prompting, vector RAG over code if available, other code-graph tools, optional cloud LLM baselines).
- Ablation: value of hierarchical summaries vs. flat graph vs. AST-only.
  - **Result so far (2026-07-25), recorded honestly:** four pre-registered
    graph-vs-raw ablations (`PHASE7_ABLATION.md`) – v1 `sqlparse.split`,
    `jsonpatch`, v2 `humanize.number`, v3 `isodate.parse_duration`, the last
    across a second model family (GPT-5.6) – **do not demonstrate a capability
    win for the graph.** v1 showed a focus/efficiency advantage on the largest
    target; the rest reached parity because raw source was equally sufficient.
    The structural reason is a property of the experiment: any slice small enough
    to be a clean benchmark is also small enough for the whole raw package to fit
    in context, so the graph's advantage was never actually put under test.
    **The series is closed (2026-07-25) with this negative result as the finding
    – no v4.** A v4 on the reframed premise would be a different experiment
    needing its own protocol: hard material / tool-call / wall budgets and
    isolation strict enough that an arm cannot incrementally pull in the repo.
    Without those, a raw agent just assembles context through tool calls and the
    experiment measures "the model can search", not "the context did not fit".
    The supported claim is that the deterministic graph is an auditability,
    provenance, adequacy-gating and context-assembly discipline, not a measured
    accuracy multiplier.

  - **Interpretive correction (2026-07-28):** the preceding v1 wording is a
    historical record of the early observation. The corrected protocol did not
    establish an efficiency win, so the current closed-series conclusion is no
    measured graph advantage in accuracy **or efficiency** for the tested
    benchmark class. This correction does not alter the archived run record.
- Open issues: legal/attribution for ports, exact behavioral equivalence (incl. original bugs vs. fixes), handling of build systems / platform specifics.
- Contribution model: treat this as a research/engineering project; welcome tree-sitter grammar extensions, new query types, better verifiers.

## 4. Technology Stack Recommendations (Pragmatic, Low Lock-in)

- **Parsing:** tree-sitter (Python `tree-sitter` + `tree-sitter-language-pack` or equivalent; or tree-sitter CLI + custom walker) for syntax, plus language-specific semantic tooling where available (`ast`/Jedi/Pyright/mypy for Python, clang tooling for C/C++, rust-analyzer/`cargo metadata`/`syn` for Rust).
- **GraphRAG core / compatibility:** Keep the official microsoft/graphrag BYOG schema as the interchange target. The package/CLI can remain installed for compatibility tests and optional future community reports, but the first working pipeline should not require an external LLM API.
- **Graph storage/query:** Keep parquet as the canonical interchange. Use DuckDB/SQLite + NetworkX for prototyping and reproducible local queries. Add Memgraph/Neo4j/FalkorDB when Cypher, visualization, or larger interactive traversal becomes necessary.
- **LLMs / agents:** No external API by default. Use deterministic context packs with local interactive agents (Codex / Claude Code / Grok Build) and manual review first. Keep a provider-pluggable interface for optional later backends: local OpenAI-compatible servers (Ollama/vLLM/LM Studio/llama.cpp) or cloud APIs. Track cost/latency only for optional LLM-backed stages.
- **Agents/Orchestration:** Start with explicit state machines + retry logic and durable run logs. Later: expose via MCP so local agents/editors can query the code graph as a tool.
- **Verification:** cargo, pytest equivalents, proptest, miri, etc. Git for diff/review.
- **UI/Exploration:** CLI first (Typer), then Gradio/Streamlit or integrate existing graph viewers. Export DOT/Mermaid for architecture diagrams.
- **Language support priority:** Python (easiest for initial ports), C (high impact), C++ (harder), Rust (for completeness/roundtrip).

Alternatives to evaluate: pure graph DB + LLM-to-Cypher (as in Graph-Code demos), GraphRAG BYOG without semantic overlay, AST-only retrieval, full custom extraction without forking GraphRAG.

## 5. Risks, Limitations & Mitigations

- **Extraction hallucinations:** Pure LLM graphs on code are unreliable for precise calls/edges. **Mitigation:** Hybrid (AST ground truth + LLM semantics). Validate extracted relations against static facts.
- **False confidence from partial static analysis:** Tree-sitter can parse syntax without resolving every reference/type. **Mitigation:** Store confidence/provenance on every edge; distinguish deterministic, heuristic, LLM-inferred, and unknown facts.
- **C/C++ semantic gap:** Perfect automatic translation is extremely difficult (UB, implementation-defined behavior, performance micro-optimizations, platform specifics). **Mitigation:** Scope to "high-fidelity port of semantics + tests" rather than bit-exact + zero-unsafe. Use for acceleration, not replacement of expert review on critical paths. Leverage formal methods where available.
- **Cost & scale:** Official LLM-backed GraphRAG indexing on large corpora can be expensive. **Mitigation:** make the deterministic BYOG/context-pack path useful without LLM calls; add incremental caching, sampling for optional summaries, tiered/local models, and focus on "hot" subsystems first.
- **Verification completeness:** Passing tests ≠ semantic equivalence for all inputs. **Mitigation:** Multi-layered (unit, integration, fuzz, differential, manual for high-risk).
- **IP/Legal:** Porting third-party code may have license implications. **Mitigation:** Start with permissively licensed or your own code; document provenance.
- **"1M LOC / month" is aspirational/internal:** Public version will require significant human guidance and iteration initially. Treat as a powerful assistant, not autonomous magic.
- **Reproducibility of the anecdote:** The exact "no single error" on a complex proprietary codebase likely involved internal tooling, curated prompts, strong test suites, and expert oversight. Our version will aim for excellent results on open examples and document the gap.
- **Source-claim drift:** Talks, issues, docs, and model capabilities change. **Mitigation:** Keep dated source notes in this plan; link to archived transcripts/issues where possible; avoid presenting paraphrased quotes as verified direct quotes until the transcript is captured.

## 6. Immediate Next Actions (Current Frontier)

1. Treat `PHASE5_REPORT.md` + `sqlparse.split` as the frozen Phase 5 evidence baseline, `examples/jsmn/PROVENANCE.md` as the Phase 6 first-C-port baseline, `examples/inih/PROVENANCE.md` as the second-C-port baseline, `examples/cjson/PROVENANCE.md` as the third-C-port ownership baseline, and `examples/charset_normalizer_rust/PORT_STATUS.md` as the advanced data-heavy Python→Rust stress-test baseline.
2. Keep C-specific scope explicit: preprocessor *dependence* is labelled on published C graphs (detection only); optional compiler overlays can add flattened TU `depends_on`, direct `includes`, (via `--clang-signatures`) configuration-derived Clang function-signature *fields* on matched tree-sitter function entities, (via `--clang-calls`) configuration-derived direct-call *evidence fields* on existing tree-sitter `calls` relationships (exact span + byte-offset attachment; no new edges), and (via `--clang-types`) configuration-derived type-declaration *fields* on existing tree-sitter `struct`/`enum`/`typedef` entities (exact title + type + symbol_name + path + canonical span; graph-canonical vs matched-site coordinates both recorded; no alternate-site entities; optional `--clang-type-uses` adds aggregated `uses_type` edges), and (via `--clang-type-shapes`) configuration-derived ordered direct member-*name* evidence in a separate `clang_shape_*` namespace on existing tree-sitter `struct`/`enum` entities (hard equality is the ordered member-name list only; member type spellings, enum values, bit-field widths and locations are diagnostic evidence, explicitly not ABI/layout/FFI/Rust-`repr`/multi-config/C++ claims; no new entities, relationships, `uses_type` edges, alternate-site entities, or ABI facts). When any of the five Clang overlays (signatures/calls/types/type-uses/type-shapes) are enabled they share one in-memory AST capture (exactly N dumps for N compile entries for any non-empty flag subset; no disk cache), and the type-declaration audit is built at most once and reused by the type-use and type-shape builders after validation against the same capture, digest, and toolchain. Tree-sitter C symbol titles are kind-aware under cross-kind collisions (`module_key:entity_kind:name` for colliding function/struct/enum/typedef pairs; legacy `module_key:name` otherwise) so silent title-only collapse of `typedef struct T {…} T;` no longer discards the named struct. Typedef aliases nested in declarators (function-pointer typedefs included) are extracted by walking only declarator structure. The type audit / overlay records every tree-sitter declaration site per semantic entity and matches configured Clang only by exact site coordinates, so mutually exclusive `#if/#else` sites (e.g. inih `ini_handler`) can match without rewriting the graph’s canonical span; unselected owned sites are non-failing alternate diagnostics, not multi-config type semantics. Standalone AST diagnostics remain available: **function-definition audit** (`scripts/c_clang_ast_audit.py`), **call-site audit** (`scripts/c_clang_call_audit.py`), **type-declaration audit** (`scripts/c_clang_type_audit.py`), **type-use audit** (`scripts/c_clang_type_use_audit.py` – declaration-bearing type-use evidence; reuses function/type audits for owners/targets; locations are not claimed as exact type-token spans), and **type-shape audit** (`scripts/c_clang_type_shape_audit.py` – ordered direct member names of configured matched structs/enums vs tree-sitter; not ABI/layout; the CLI itself never mutates a graph). Signature, call, type-declaration, type-use, and type-shape publication (`--clang-type-uses` aggregates `matched_internal` into `uses_type` edges by entity-id pair; `--clang-type-shapes` decorates only `matched_shape` owners) are independent fail-closed opt-ins (default off) with separate manifest blocks; standard type residuals (`tree_sitter_only` / `clang_only` / `ambiguous` / `macro_location_unsupported`) and shape residuals (`tree_sitter_only_members` / `clang_only_members` / `member_order_mismatch` / `duplicate_or_ambiguous_members` / `macro_location_unsupported` / `owner_unmatched`) abort rather than inventing facts, while `unsupported_member_form` and `outside_package_declarations` stay observation-only with counts recorded in the manifest. Local consumers expose direct `types-used-by` / `type-users` queries, a bounded cycle-safe `type-closure` BFS over only `uses_type` (dependencies/users/both; min depths; returned-list caps with exact in-depth totals), and bounded type-dependency context-pack sections (default depth 1 byte-stable; depth > 1 adds `type_*_closure`) without mixing `uses_type` into call-graph traversal. A separate read-only integrity audit (`scripts/c_clang_type_use_graph_audit.py`, also wired into C `published_graph_health`) validates already-persisted configured `uses_type` edges and the `clang_type_uses` manifest block against the producer contract without Clang re-run, reindex, or byog_* rewrite (legacy/off with zero edges pass; missing blocks never legitimize existing edges). Its type-shape counterpart (`scripts/c_clang_type_shape_graph_audit.py`, also wired into C `published_graph_health`) does the same for persisted `clang_shape_*` entity fields and the `clang_type_shapes` manifest block – strict entity payload/canonical-JSON member census, compiler/digest/entry-index provenance, manifest count and contract-text checks, `legacy_absent` / `off` / `enabled` states, and a SHA-256 before/after fingerprint of the manifest, parquet tables, `current` pointer and snapshot listing proving the audit changed nothing; it never runs Clang, reads `compile_commands.json`, captures an AST, reindexes, or repairs data. A third read-only integrity audit (`scripts/c_clang_type_graph_audit.py`, also wired into C `published_graph_health`) does the same for persisted `clang_type_*` entity fields and the `clang_types` manifest block – strict entity payload, graph-canonical vs matched-site coordinates, compiler/digest/entry-index provenance, manifest count and confidence-boundary checks, `legacy_absent` / `off` / `enabled` states, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never runs Clang, reads `compile_commands.json`, captures an AST, reindexes, or repairs data. A fourth read-only integrity audit (`scripts/c_clang_signature_graph_audit.py`, also wired into C `published_graph_health` as `clang_signature_integrity`) does the same for persisted function-signature entity fields and the `clang_signatures` manifest block – exact producer key set including nullable storage/inline/variadic/mangled keys, strict canonical observations JSON, compiler/digest/entry-index provenance, fail-closed `clang_only` / `ambiguous` / `macro_location_unsupported` counts, `legacy_absent` / `off` / `enabled` states, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never runs Clang, reads `compile_commands.json`, captures an AST, reindexes, or repairs data. A fifth read-only integrity audit (`scripts/c_clang_call_graph_audit.py`, also wired into C `published_graph_health` as `clang_call_integrity`) does the same for persisted `clang_call_*` relationship fields and the `clang_calls` manifest block – exact producer key set including nullable resolve-reason/ref-type/singular-compiler keys, canonical compilers/observations JSON, entry-index coverage, tree-sitter call accounting, fail-closed `clang_only_internal` / `ambiguous` / `macro_location_unsupported` / `covered_by_noninternal_clang_observation` counts, `legacy_absent` / `off` / `enabled` states, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never runs Clang, reads `compile_commands.json` or C sources, reconstructs byte offsets, reindexes, or repairs data. A sixth read-only integrity audit (`scripts/c_compiler_dependency_graph_audit.py`, also wired into C `published_graph_health` as `compiler_dependency_integrity`) does the same for persisted flattened `depends_on` / `translation_unit_dependency` relationships and the `compiler_dependencies` manifest block – exact producer key set including nullable `compiler_id`, compiler census, digest, translation-unit titles, flattened edge payload, `legacy_absent` / exact `off` / enabled `mode=compiler_m` states, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never runs a compiler, reads `compile_commands.json` or C sources, reconstructs `-M` output, reindexes, or repairs data. A seventh read-only integrity audit (`scripts/c_compiler_include_graph_audit.py`, also wired into C `published_graph_health` as `compiler_include_integrity`) does the same for persisted direct `includes` / `configured_direct_include` relationships and the `compiler_includes` manifest block – exact producer key set including nullable `compiler_id`, compiler census, digest, translation-unit titles, header-to-header direct edges, `legacy_absent` / exact `off` / enabled `mode=compiler_eh` states, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never runs a compiler, reads `compile_commands.json` or C sources, reconstructs `-E -H` output, reindexes, or repairs data, and it does not treat `depends_on` rows as include carriers. An eighth read-only integrity audit (`scripts/c_preprocessor_liveness_graph_audit.py`, also wired into C `published_graph_health` as `preprocessor_liveness_integrity`) does the same for persisted `preprocessor_*` row stamps and the `preprocessor_liveness` manifest block – exact producer key set, `no_compiler` / `compiler_builtins` contracts, five-field stamps on entities / base relationships / call observations, post-annotation overlay exemptions for complete dependency / include / uses_type identities, `legacy_absent` / `no_compiler` / `compiler_builtins` states, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never reanalyses sources, invokes a compiler, reconstructs macro tables or branch decisions, compares the recorded digest with the current host, reindexes, or repairs data. A ninth read-only integrity audit (`scripts/c_overlay_coherence_graph_audit.py`, also wired into C `published_graph_health` as `c_overlay_coherence_integrity`) compares enabled compiler-backed overlay blocks in one snapshot for shared `compile_commands_digest`, `n_compile_entries`, and normalized compiler census – `legacy_absent` / `off` / `coherent` states, component statuses kept separate from cross-overlay anomalies, preprocessor liveness reported independently and never compared, and a SHA-256 before/after fingerprint proving the audit changed nothing; it never invokes a compiler, reads sources or `compile_commands.json`, reconstructs overlay facts, or repairs data. A tenth read-only integrity audit (`scripts/byog_snapshot_graph_audit.py`, wired into `published_graph_health` as `snapshot_integrity` for every non-frozen published graph) proves the language-independent snapshot envelope – directory identity, required core manifest fields, exact producer `files` list, parquet census, and parquet-only `total_size_bytes` – against `publish_byog_snapshot()` without inventing `corpus_hash` semantics or comparing `source_root` / `git_commit` / `created_at` with the host; it never invokes an extractor, compiler, or overlay reconstruction, and it runs before language-specific overlay failures so a broken base snapshot cannot masquerade as an overlay problem. Concurrent `publish_byog_snapshot()` writers now stage payloads in private `snapshots/.staging-<id>/` directories and take one exclusive graph-root `.publish.lock` only for the atomic staging-to-final rename, the `current` pointer, and keep-last retention; staging names are not published ids, `current` never names a partial or staging snapshot, and a crash may leave a staging directory that retention will not age-reap. Cooperating readers hold a shared lock while materializing a snapshot, so retention waits; MCP fails closed if a managed graph lacks the lock, while immutable pre-lock evidence remains available only through explicitly unleased compatibility reads. A product-level read-only doctor (`scripts/persisted_graph_doctor.py`, also `graphrag-code doctor`) now selects one snapshot and aggregates the envelope plus the nine C overlay contracts in one protected interval – `--indexer python|c|auto`, envelope first, no extractor/compiler/publisher, a shared reader lease when the lock exists, fingerprint-only compatibility for immutable pre-lock evidence, staging listed as a notice, concurrent listing/`current` changes invalidating the report – and `published_graph_health` reuses that aggregator while still doing its separate fresh-extractor comparison after persisted checks pass. There is still no claim for full type resolution, a complete semantic type graph, ABI verification, function-pointer target resolution (points-to), multi-config coverage, full C ABI preservation, or a full upstream cJSON migration beyond its closed exclusive-ownership surface.
3. Decide the next frontier:
   - leave the closed `charset-normalizer` and cJSON scopes closed unless a separately approved target changes their stated boundary;
   - undertake a deliberate cJSON representation/policy redesign (shared mutable bytes/nodes or a handle arena for the compiler-proven safe-borrow alias cases; allocator/error state for the traced global calls) only as a new porting scope, not as incremental closure work;
   - add broader fuzz/Miri coverage of the libc float-print path (currently `cfg(not(miri))`) only with a stated fidelity question and oracle;
   - pursue Phase 7 productization or a new benchmark protocol with hard material/tool/wall budgets; do not reopen the closed small-slice ablation to search for a win.
4. If expanding C/C++ semantics, add clang-backed graph facts only after the AST function-definition audit is clean for the target, and only as an incremental overlay on tree-sitter-c – not a replacement of the proven audit/port rails.
5. In parallel but off the critical path, finish the dated primary-source notes and exact talk transcript/timestamps before making public claims about the Microsoft demo.

## 7. References & Further Reading (Key Sources)

- Microsoft GraphRAG: GitHub repo (https://github.com/microsoft/graphrag), official docs (https://microsoft.github.io/graphrag), BYOG/custom graph docs (https://microsoft.github.io/graphrag/index/byog/), query docs, prompt tuning docs, arXiv:2404.16130, research blog posts.
- Talks: Search YouTube for "Mark Russinovich" + "Rust" + 2025 (Rust Nation / RustConf).
- Galen Hunt comments on the broader AI + algorithms rewrite strategy (LinkedIn if accessible, plus news coverage and clarification reporting around Dec 2025).
- Community code graph projects: Graph-Code / Code-Graph-RAG (Memgraph), Codebase-Memory (arXiv-style papers), various tree-sitter + RAG notebooks.
- Related research: AST-derived vs. LLM-extracted graphs for code RAG reliability; CodexGraph-style repo-level agents; C-to-Rust formal transpilation papers.
- GraphRAG issue requesting the demo: microsoft/graphrag#1779 (https://github.com/microsoft/graphrag/issues/1779; closed as not planned as of 2026-06-15).

This plan is designed to be executed iteratively with strong verification at each phase. It balances fidelity to the demonstrated technique (hierarchical semantic graph via GraphRAG on structured code input) with practical, available open-source components.

Start small, measure everything (quality, cost, human effort), and expand only
where the next claim has a protocol capable of testing it. The combination of
precise static structure + GraphRAG-style global memory is a testable,
reproducible design pattern, not a demonstrated universal advantage.

*Plan created: 2026-06-14. Reviewed through 2026-08-14. Current implementation strategy: productize the proven deterministic BYOG + local context-pack rails without inflating the closed port/ablation claims; semantic overlays, broader C/C++ scope, and new benchmark protocols remain separately gated.*
