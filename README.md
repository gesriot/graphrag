# graphrag-code

Experimenting with Microsoft GraphRAG (official BYOG path) + deterministic code
parsing (tree-sitter + semantic analyzers) to build rich, queryable knowledge
graphs over codebases for understanding and language porting (Python → Rust
first; bounded C → Rust also exercised).

See [Plan.md](Plan.md) for the full plan. See [PHASE7_ABLATION.md](PHASE7_ABLATION.md)
for the honest Phase 7 reading: on bounded library slices the deterministic
graph has **not** shown a cold-port capability or efficiency win over raw
source. What *is* solid is the pipeline rails — extraction, audited edges,
context packs, golden-first gates, and `port_eval`.

## Product CLI (front door)

One entry point wraps the existing scripts (Plan Phase 3). The scripts themselves
are unchanged and remain the stable layer for automation.

```bash
# help
uv run python scripts/graphrag_code.py --help

# index (Python or C)
uv run python scripts/graphrag_code.py index --lang python \
  --package examples/isodate --graph byog_isodate
uv run python scripts/graphrag_code.py index-c \
  --package examples/cjson --graph byog_cjson

# query
uv run python scripts/graphrag_code.py query-symbol parse_duration --graph byog_isodate
uv run python scripts/graphrag_code.py query-symbol parse_duration --graph byog_isodate --json
uv run python scripts/graphrag_code.py callers isoduration:parse_duration --graph byog_isodate
uv run python scripts/graphrag_code.py callees isoduration:parse_duration --graph byog_isodate
uv run python scripts/graphrag_code.py subgraph isoduration:parse_duration --graph byog_isodate
uv run python scripts/graphrag_code.py impact isoduration:parse_duration --graph byog_isodate
uv run python scripts/graphrag_code.py dependency-order --graph byog_isodate

# context pack (default = short summary; --json = full pack, same shape as context_pack.py)
uv run python scripts/graphrag_code.py context-pack isoduration:parse_duration \
  --graph byog_isodate --purpose port-to-rust
uv run python scripts/graphrag_code.py context-pack isoduration:parse_duration \
  --graph byog_isodate --purpose port-to-rust --json

# graph audit + port eval
uv run python scripts/graphrag_code.py audit-graph --graph byog_isodate
uv run python scripts/graphrag_code.py audit-graph --graph byog_isodate --json
uv run python scripts/graphrag_code.py port-eval \
  --graph byog_cjson --source examples/cjson --port examples/cjson_rust
```

`subgraph` is the Phase 3 name for the existing neighbors query (incoming +
outgoing edges). There is no separate multi-hop subgraph stage today.

## Underlying scripts (stable automation layer)

Call these directly when you already have automation, or when you need flags the
product CLI does not re-surface:

| Task | Script |
|---|---|
| Index Python | `uv run python scripts/index_python.py --package … --graph …` |
| Index C | `uv run python scripts/index_c.py --package … --graph …` |
| Graph queries | `uv run python scripts/graph_query.py {callers,callees,neighbors,symbol,impact,dependency-order,observations} …` |
| Context pack | `uv run python scripts/context_pack.py <symbol> --graph … --purpose port-to-rust` |
| Graph audit | `uv run python scripts/audit_call_edges.py --graph …` |
| Port eval | `uv run python scripts/port_eval.py --graph … --source … --port …` |
| Ablation harness | `uv run python scripts/ablation.py {adequacy,prep,audit,verify-fill,eval,report} …` |

Examples still used in day-to-day work:

```bash
# golden / smoke (Phase 0 artifacts)
uv run python -m pytest examples/mini_game/tests/test_sim.py -q
uv run python scripts/make_byog_smoke.py
uv run python scripts/context_pack.py sim:run_simulation --graph byog_mini_game --purpose port-to-rust

# advanced data-heavy Python→Rust stress-test
examples/charset_normalizer_rust/tools/check_port.sh
examples/charset_normalizer_rust/tools/check_port.sh --full
```

## Important (per plan)

- Primary contract: produce `entities.parquet` / `relationships.parquet` /
  `text_units.parquet` from **deterministic** parsing.
- Tree-sitter is syntax. Optional layers (clang / Jedi / Pyright) enrich
  resolution; they are not required for the default path.
- Every edge/node carries provenance + confidence + `is_deterministic`.
- Do not claim a demonstrated graph accuracy advantage for cold porting; see
  Phase 7 ablation docs for the measured negative result and the rails that
  remain useful.
