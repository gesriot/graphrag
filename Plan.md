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
- Why it helps for code (beyond vanilla RAG): Code understanding and porting require *holistic sensemaking* (architecture, cross-module invariants, data flows, "why this design") + precise local facts. Vector similarity on raw text/chunks fails at connections and global coherence. Hierarchical graphs + summaries solve this.

Many independent projects already combine **Tree-sitter** (precise, multi-language AST parsing) + graph databases (Memgraph, Neo4j, FalkorDB, even SQLite) + LLM/GraphRAG layers for "Code Knowledge Graphs" / CodeRAG. Examples:
- Graph-Code / Code-Graph-RAG (tree-sitter → graph in Memgraph → NL-to-Cypher → visualization + surgical edits via AST).
- Codebase-Memory (tree-sitter for 66 languages, call graphs, Louvain communities, SQLite + MCP for agents).
- Various CodeRAG experiments using dependency graphs from AST for better retrieval than pure vector.

The winning pattern is **hybrid**:
- Deterministic/precise structural graph from AST (functions, types, calls, modules, imports, etc.).
- LLM-powered semantic overlay (intent, summaries, claims, higher-level relationships that static analysis misses) — exactly where the original GraphRAG extraction shines.
- Hierarchical communities + summaries for "subsystem overviews".
- Then rich retrieval (graph traversals + GraphRAG-style global/local queries) to feed translation agents.

Important implementation correction: do **not** rely on generic GraphRAG entity extraction to infer precise code structure from raw source. Use deterministic source tooling for hard facts, then feed the resulting graph into GraphRAG. The official GraphRAG docs support a "bring your own graph" path using `entities.parquet`, `relationships.parquet`, and optional `text_units.parquet`, followed by community creation/report workflows. That should be the primary route for the MVP.

**Current execution strategy (decided 2026-06-15): no external API by default.** The first working system should not require OpenAI/Azure/Anthropic/xAI keys. Build the deterministic pipeline first: extractor → BYOG parquet → schema tests → local graph queries/context packs → manual or local-agent-assisted work in Codex / Claude Code / Grok Build. Official GraphRAG LLM workflows (`create_community_reports`, Global/Local/DRIFT search, embeddings) remain an optional later backend for evaluation or higher-quality summaries, not a blocker for Phase 1-3 progress.

**Means vs. ends — verification boundary (recorded 2026-06-16):** graph-quality auditing is the *means*, Python→Rust fidelity is the *north-star end*. A correct graph is necessary but not sufficient; it must be measured separately from the port outcome. Two repeatable harnesses make this explicit: `scripts/audit_call_edges.py` measures the graph (structural pass rate of CALLS edges, dangling targets, seeded precision sample), and `scripts/port_eval.py` measures the end-to-end port (graph quality → context packs → `cargo fmt/check/test/run` → golden scenarios → manual-fix count) as one comparable report. C/C++ input remains "maybe, later"; Python→Rust is the capability that must work flawlessly and is the primary acceptance metric.

**Porting gate (per-project checklist, recorded 2026-06-16):** before porting any project to Rust, pass these gates in order — do not start the port until all are green:
1. **License captured** — project is permissively licensed; license + provenance recorded (esp. for external/third-party code).
2. **Golden/contract captured first** — `golden_*.json` (or trace contract) exists and the Python reference passes them *before* any Rust is written. The contract scope is stated explicitly (e.g. for mini_lang: `run_source` semantics are pinned; CLI file I/O error text is out of scope, only its pass/fail outcome is kept faithful).
3. **Graph clean** — `audit_call_edges` on the project's graph shows `pass_rate=1.0` with no dangling targets, OR every remaining weak/false edge is demoted to `call_observations` (never a high-confidence deterministic edge).
4. **Then `port_eval`** — only after 1–3, run the end-to-end harness and record the report (graph pass rate, golden cases, manual-fix count, `overall_pass`).

Validated end-to-end on seven ports, all with `overall_pass=True` and 0 recorded manual fixes: `mini_game` (greenhouse), `mini_lang` (interpreter; lexer→parser→eval, 28 golden cases), the external BSD-licensed `semantic_version` 2.10.0 core scope (`Version`, `SimpleSpec`, and `NpmSpec`; 147 golden cases across 13 files), the complete staged algorithmic core of the external Apache-2.0 `diff-match-patch` 20241021 package (Myers diff, line mode and cleanups; arbitrary-length Bitap fuzzy match; patch make/apply/split/serialization with Unicode and percent-codec fidelity; 107 golden cases across 9 files), the external BSD-licensed `sqlparse` 0.5.5 `split` pipeline (lexer with Python-regex lookaround/backreference parity, StatementSplitter, strip-semicolon behavior; 65 golden cases across lex + split), the MIT-licensed C `jsmn` default-mode parser/tokenizer (`jsmn_init` + `jsmn_parse`; byte offsets, count-only mode, NOMEM/PART, non-strict unquoted keys, escapes/unicode/nesting; 18 golden cases), and the BSD-3-Clause C `inih` default-config string parser (`ini_parse_string` / `ini_parse_string_length`; callback sequence, error-line behavior, multiline/inline comments/BOM/CRLF/embedded-NUL C-string behavior; 21 golden cases).

**Readiness snapshot (reviewed 2026-06-25):** the small-project Python→Rust thesis is proven, and the first real multi-package scaled component is now green on vendored `sqlparse` 0.5.5 (~4.1k LOC): 243 entities / 454 relationships / 253 call observations, 229 resolved calls, structural pass rate 1.0, 0 dangling targets, 0 semantic suspicions after the audit heuristic was made import-aware, and a seeded 12/12 manual precision sample including cross-package constructor/module calls. The `sqlparse.split` Rust port passes the staged gates: generated token tree + 811 keyword entries, lexer parity across 40 differential cases / 341 tokens, StatementSplitter + split pipeline, and `port_eval` with 65 golden cases, 3/3 context packs, and 0 manual fixes. Phase 0 is complete. The Python portion of Phase 1 and the deterministic core of Phase 2 are usable (generic package indexing, provenance, audited call edges, local traversals and context packs), while optional LLM overlays remain open. Phase 3 exists as separate CLIs rather than one product surface. The Phase 4 small-project milestone is exceeded. Phase 5 has a successful scaled-component pilot. Phase 6 now has a working C frontend and two bounded C→Rust ports: `jsmn` (43 C calls, 165 observations, `port_eval` 18 golden / 0 manual fixes) and `inih` (38 C calls, 35 observations, `port_eval` 21 golden / 0 manual fixes). `inih` surfaced the expected preprocessor/config-awareness boundary, and the Rust port covers callback/error-line string parsing while file I/O is measured but not ported. Clang-backed macro/type/include semantics and ownership-heavy C remain open. Phases 6–7 are not production-ready. In short: a convincing research prototype with strong small-scope evidence, one real repository-scale Python component port, and two small C→Rust proofs, not yet a production migration product.

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

**Success criteria overall:** Reproduce a high-fidelity port of a non-trivial open-source Python (or small C) project using the system, where the output compiles, passes original (or ported) tests, and behaves equivalently on key scenarios. Demonstrate clear superiority over naive "paste code into LLM" baseline. Document costs, token usage, and failure modes.

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
  - Call graph (conservative static calls; note limitations on dynamic/indirect).
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
  - simple local community/grouping heuristics (module/package grouping, connected components, centrality) before any LLM summarization.
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
  - `subgraph <symbol-or-module>`
  - `context-pack <symbol-or-module> --purpose port-to-rust`
- CLI / simple TUI or Streamlit/Gradio web UI for:
  - "Index this repo".
  - Natural language questions over the code graph.
  - "Show me the subgraph for module X and its direct dependencies".
  - Visualize communities/hierarchy (export to Graphviz, or integrate Memgraph Lab / Neo4j Browser style).
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
- **Verification (Phase 4 milestone):** Successful high-fidelity port of the small Python example game (or equivalent). It compiles cleanly, runs, and matches the declared behavior contract on sample inputs and golden traces. For graphical examples, prefer deterministic state/frame/event traces over vague "looks identical" claims. Provide side-by-side diff + test results. Run the same baseline naive LLM port for comparison — show the graph-augmented version has far fewer manual fixes needed.

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
  - Bootstrap/first-port status (2026-06-25): vendored MIT `jsmn` indexes via `scripts/extract_c.py` / `scripts/index_c.py` using tree-sitter-c. The resulting graph has 32 entities, 72 relationships, 165 call observations, 43 resolved calls, and `audit_call_edges` pass rate 1.0 with 0 anomalies/dangling/semantic suspicions. The bounded `jsmn_parse` C→Rust port passes `port_eval` with 18 golden cases and `manual_fixes=0`. The second C target, BSD-licensed `inih`, has a clean C graph (19 entities including the golden runner, 54 relationships, 35 observations, 38 calls, pass rate 1.0) after rejecting reserved-word phantom functions caused by preprocessor-fragmented bodies, and its bounded default-config string parser C→Rust port passes `port_eval` with 21 golden cases and `manual_fixes=0`. This proves the audit/port rails are frontend-agnostic at small C scope, but not yet clang-accurate C/C++ semantic extraction.
  - Pre-process with clang tooling or additional static analyzers for aliasing, ownership hints (where possible).
  - Model preprocessor/macros, platform conditionals, ABI boundaries, generated code, and external dependencies explicitly.
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

1. Treat `PHASE5_REPORT.md` + `sqlparse.split` as the frozen Phase 5 evidence baseline, `examples/jsmn/PROVENANCE.md` as the Phase 6 first-C-port baseline, and `examples/inih/PROVENANCE.md` as the second-C-port baseline.
2. Move to `cJSON` as the next bounded C target: license/provenance → compile metadata → C graph audit → golden contract before Rust → port a deliberately narrow ownership-bearing surface.
3. Use `cJSON` to measure the next C-specific unknowns separately: struct field modeling, pointer ownership, allocation/free behavior, recursive tree shape, string ownership, and API error/null semantics.
4. Keep C-specific scope explicit: default macro/build configuration from `compile_commands.json`, no claim yet for clang-accurate macro/type facts, include relationships, full function-pointer resolution, multiple build configurations, or full C ABI preservation.
5. Pick the first `cJSON` component small enough to finish with hard evidence (for example parse/print/delete over a bounded JSON corpus) before expanding to mutation APIs.
6. In parallel but off the critical path, finish the dated primary-source notes and exact talk transcript/timestamps before making public claims about the Microsoft demo.

## 7. References & Further Reading (Key Sources)

- Microsoft GraphRAG: GitHub repo (https://github.com/microsoft/graphrag), official docs (https://microsoft.github.io/graphrag), BYOG/custom graph docs (https://microsoft.github.io/graphrag/index/byog/), query docs, prompt tuning docs, arXiv:2404.16130, research blog posts.
- Talks: Search YouTube for "Mark Russinovich" + "Rust" + 2025 (Rust Nation / RustConf).
- Galen Hunt comments on the broader AI + algorithms rewrite strategy (LinkedIn if accessible, plus news coverage and clarification reporting around Dec 2025).
- Community code graph projects: Graph-Code / Code-Graph-RAG (Memgraph), Codebase-Memory (arXiv-style papers), various tree-sitter + RAG notebooks.
- Related research: AST-derived vs. LLM-extracted graphs for code RAG reliability; CodexGraph-style repo-level agents; C-to-Rust formal transpilation papers.
- GraphRAG issue requesting the demo: microsoft/graphrag#1779 (https://github.com/microsoft/graphrag/issues/1779; closed as not planned as of 2026-06-15).

This plan is designed to be executed iteratively with strong verification at each phase. It balances fidelity to the demonstrated technique (hierarchical semantic graph via GraphRAG on structured code input) with practical, available open-source components.

Start small, measure everything (quality, cost, human effort), and expand. The combination of precise static structure + GraphRAG-style global memory is a powerful and replicable pattern.

*Plan created: 2026-06-14. Reviewed/updated: 2026-06-17. Current implementation strategy: prove deterministic BYOG + local context packs on a 10k–20k LOC Python target before expanding to C/C++, semantic overlays, or product packaging.*
