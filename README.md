# graphrag-code

Experimenting with Microsoft GraphRAG (official BYOG path) + deterministic code parsing (tree-sitter + semantic analyzers) to build rich, queryable, hierarchical knowledge graphs over codebases for understanding and high-fidelity porting (Python → Rust first).

See [Plan.md](Plan.md) for the full detailed plan.
See [PHASE0_STATUS.md](PHASE0_STATUS.md) for execution log of the first phase.

## Quick start (Phase 0 artifacts)

- Golden behavior contract tests (mini side-scroller simulator):
  `uv run python -m pytest examples/mini_game/tests/test_sim.py -q`

- BYOG smoke (hand-authored tables + provenance):
  `uv run python scripts/make_byog_smoke.py`

- First tree-sitter extractor prototype:
  `uv run python scripts/extract_python.py examples/mini_game/sim.py output/extracted_sim.json`

- GraphRAG BYOG workflow (tables are ready; full reports need LLM key):
  `uv run graphrag index --root byog_smoke`

## Important (per plan)
- Primary contract: produce `entities.parquet` / `relationships.parquet` / `text_units.parquet` from **deterministic** parsing.
- Tree-sitter is syntax. Phase 1+ adds clang (`compile_commands.json`) for C/C++, Jedi/Pyright for Python, etc.
- Every edge/node carries provenance + confidence + `is_deterministic`.

