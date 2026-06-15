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

- Official GraphRAG BYOG path exercised:
  - `scripts/make_byog_smoke.py` produces `entities.parquet`, `relationships.parquet`, `text_units.parquet` with our required provenance columns (`source_file`, `span`, `extractor`, `confidence`, `is_deterministic`).
  - `byog_smoke/settings.yaml` with the minimal `workflows: [create_communities, create_community_reports]`.
  - `graphrag index --root byog_smoke` reaches the config/LLM stage and correctly consumes the tables (fails only on missing `OPENAI_API_KEY` — expected and documented boundary).
  - Parquet column validation passed.

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

## Notes / Blockers / Next
- Full `create_community_reports` and query synthesis require a real LLM key (OpenAI/Azure or local via config). This is by design for Phase 0.
- The tree-sitter extractor is syntax-only and deliberately naive on calls (no resolution). Phase 1 will add better intra-file + cross-file resolution + Python stdlib `ast` / Jedi signals.
- No heavy LLM calls were made yet (baseline port in Phase 0 was deprioritized until we have `context-pack` + summaries from real BYOG run).
- The mini-game is an excellent MVP target: deterministic, has tests/golden traces, multiple modules, clear "physics" vs "sim" separation — ideal for first Python→Rust port experiment.

## Commands that prove Phase 0
```bash
uv run python -m pytest examples/mini_game/tests/test_sim.py -q
uv run python scripts/make_byog_smoke.py
uv run graphrag index --root byog_smoke   # reaches expected LLM error
uv run python scripts/extract_python.py examples/mini_game/sim.py output/extracted_sim.json
```

Phase 0 success criteria (from Plan.md) met:
> Working GraphRAG quickstart / BYOG tables for code corpus; documented baseline failure modes (LLM config); golden behavior captured.

Ready for Phase 1 (robust parser + semantic analyzers + full BYOG export from extractor).

Update Plan.md with execution date if desired.
