"""Regression for the cJSON C frontend bootstrap (Phase 6, third C target).

cJSON is the first struct/pointer/ownership-heavy C target (~3.2k LOC). This
locks the bootstrap facts that matter for the planned ownership slice
(parse -> inspect -> print -> delete):
- the struct graph is captured (the `cJSON` node struct and the parse/print
  buffer structs are entities);
- the ownership-slice API functions are extracted;
- the parse entry chain and the recursive ownership functions (cJSON_Delete and
  friends) are deterministic CALLS edges;
- allocation primitives (malloc/free/realloc/memcpy) stay weak observations,
  never core edges -- so heap ownership is visible but not silently promoted.

Run: uv run python -m pytest examples/cjson/tests/test_cjson_extract.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_c import build_c_byog  # type: ignore


def _graph():
    return build_c_byog(ROOT / "examples" / "cjson")


def test_struct_graph_and_slice_functions():
    data = _graph()
    titles = {e["title"] for e in data["entities"]}
    # struct graph: the node struct and the internal parse/print buffers.
    for struct in ("cJSON:cJSON", "cJSON:parse_buffer", "cJSON:printbuffer"):
        assert struct in titles, f"missing struct entity {struct}"
    # ownership-slice API surface.
    for fn in (
        "cJSON:cJSON_Parse",
        "cJSON:cJSON_ParseWithOpts",
        "cJSON:cJSON_Print",
        "cJSON:cJSON_PrintUnformatted",
        "cJSON:cJSON_Delete",
        "cJSON:cJSON_GetObjectItem",
        "cJSON:cJSON_GetArrayItem",
        "cJSON:cJSON_GetArraySize",
    ):
        assert fn in titles, f"missing function entity {fn}"


def test_parse_chain_and_recursive_ownership_edges():
    data = _graph()
    calls = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    }
    assert ("cJSON:cJSON_Parse", "cJSON:cJSON_ParseWithOpts") in calls
    # Recursive free/compare/duplicate are captured as self-edges.
    assert ("cJSON:cJSON_Delete", "cJSON:cJSON_Delete") in calls
    assert ("cJSON:cJSON_Compare", "cJSON:cJSON_Compare") in calls


def test_allocation_primitives_stay_observations():
    data = _graph()
    targets = {r["target"] for r in data["relationships"] if r["type"] == "calls"}
    assert all(t.startswith("cJSON:") for t in targets), (
        "non-library call leaked into cJSON core edges"
    )
    obs_targets = {o["display_target"] for o in data["call_observations"]}
    for prim in ("malloc", "free", "realloc"):
        assert prim in obs_targets, f"{prim} should be a weak observation"
