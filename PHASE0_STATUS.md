# Phase 0 Status (Foundations & Reproduction Experiments)

**Date:** 2026-06-15 (executed immediately after user review of Plan.md)

## Completed in this phase

- Workspace initialized with `uv` (modern Python, pyproject.toml).
- Core dependencies installed and verified: graphrag (CLI + library), pyarrow/pandas (BYOG parquets), duckdb/networkx, tree-sitter + tree-sitter-python, pytest (dev).
- `examples/mini_game/` — small (~250 LOC total), pure-Python, fully deterministic side-scroller simulator split across modules:
  - core.py, physics.py, sim.py, main.py, tests/test_sim.py
  - Golden traces for 4 input scenarios committed (`golden_*.json`).
  - 5/5 behavior-contract tests passing (`pytest`).
  - This gives us **golden traces + explicit behavior contract** before any porting (per updated plan).

- Official GraphRAG BYOG path exercised (deterministic part only, per "no external API by default" strategy in Plan.md):
  - `scripts/make_byog_smoke.py` and `scripts/mini_game_to_byog.py` produce `entities.parquet`, `relationships.parquet`, `text_units.parquet` with full provenance.
  - Self-contained schema tests (in `examples/mini_game/tests/test_byog_schema.py`) generate fresh BYOG in tmp dirs and validate no dangling endpoints / text units.
  - `graphrag index --root byog_smoke` is intentionally not required for the core pipeline (it only reaches config parse without a key). LLM-dependent steps (`create_community_reports`, embeddings, Global/Local search) are optional later only.
  - Local `context-pack` (pandas on the parquets) + DuckDB traversals are the active query layer.

- First parser prototype: `scripts/extract_python.py`
  - tree-sitter based.
  - Extracts file + top-level fn/class entities + contains + rough imports + conservative name-based calls.
  - Emits full provenance on every record.
  - Successfully run on `examples/mini_game/sim.py`.

- Schema & provenance model documented in `docs/graph_schema.md` (matches the "decide the initial graph schema..." requirement).

- `.env.example` + `.gitignore` (parquets, output/, ragtest/, .env etc.) in place.
- Default `main.py` from uv init left as-is (harmless).

## Verification performed
- Golden contract tests: ✅ 5 passed.
- BYOG tables have correct schema + our extensions + sample data.
- GraphRAG BYOG workflow ingestion path works up to the LLM requirement.
- Extractor runs and produces usable entity/rel records with spans and confidence.

## Notes / Blockers / Next (aligned to local-first strategy in Plan.md)
- Primary path is **deterministic + no external API**: extractor → BYOG parquets → schema tests (fresh in tmp) → local context-pack / DuckDB traversals → manual or agent work (Grok Build / Claude Code / Codex).
- Official GraphRAG LLM features (`create_community_reports`, Global/Local/DRIFT, embeddings) are explicitly optional later (only if/when a local or cloud endpoint is added). They are not part of the MVP critical path.
- Caveats 1-3 from previous review remain addressed (dangling fixed, collision golden added, self-contained tests).
- New in this session: decorated_definition support (core dataclasses now extracted), source snippets in text_units, improved context-pack lookup, first structure-preserving Rust port experiment that passes the collision golden scenario.
- No external LLM calls were used for any core artifact. All verification is local.
- The mini-game + context-pack + local Rust port is the current working example of the strategy.

## Commands that prove Phase 0 (local deterministic pipeline)
```bash
uv run python -m pytest examples/mini_game/tests -q
uv run python scripts/make_byog_smoke.py
uv run python scripts/mini_game_to_byog.py
uv run python scripts/context_pack.py sim:run_simulation --graph byog_mini_game --purpose port-to-rust
uv run python -m compileall examples scripts
cd examples/mini_game_rust && cargo check && cargo run --quiet
```

Phase 0 success criteria (from Plan.md) met:
> Working GraphRAG quickstart / BYOG tables for code corpus; documented baseline failure modes (LLM config); golden behavior captured.

Ready for Phase 1 (robust parser + semantic analyzers + full BYOG export from extractor).

Update Plan.md with execution date if desired.
