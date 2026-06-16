"""Schema validation tests for GraphRAG BYOG outputs (self-contained).

The tests generate fresh BYOG artifacts in temporary directories using the
build functions from the generators. This makes them independent of any
pre-generated (and typically gitignored) byog_*/output directories.

Key checks:
- Required columns for BYOG + communities
- No dangling relationship endpoints (source/target resolve to entity titles)
- No dangling text_unit_ids references (entities/rels -> text_units)
- Provenance fields, weight, etc.

Run:
    PYTHONPATH=. uv run python -m pytest examples/mini_game/tests/test_byog_schema.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# Import the pure build functions (no side effects on disk)
import sys
sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))
from make_byog_smoke import build_smoke_byog  # type: ignore
from mini_game_to_byog import build_byog_for_package  # type: ignore
from examples.mini_game.sim import GOLDEN_INPUTS


@pytest.fixture
def smoke_byog_root(tmp_path: Path) -> Iterator[Path]:
    """Generate smoke BYOG into a temp dir and return the project root."""
    data = build_smoke_byog()
    out = tmp_path / "output"
    out.mkdir(parents=True)
    for name, df in [
        ("entities.parquet", data["entities"]),
        ("relationships.parquet", data["relationships"]),
        ("text_units.parquet", data["text_units"]),
    ]:
        pq.write_table(pa.Table.from_pandas(df), out / name)

    # Minimal settings stub (not used by schema tests but good for completeness)
    (tmp_path / "settings.yaml").write_text("workflows: [create_communities, create_community_reports]\n")
    yield tmp_path


@pytest.fixture
def mini_game_byog_root(tmp_path: Path) -> Iterator[Path]:
    """Generate full mini_game bridge BYOG into a temp dir."""
    raw = build_byog_for_package()
    out = tmp_path / "output"
    out.mkdir(parents=True)
    for name, raw_data in [
        ("entities.parquet", raw["entities"]),
        ("relationships.parquet", raw["relationships"]),
        ("text_units.parquet", raw["text_units"]),
    ]:
        df = pd.DataFrame(raw_data) if isinstance(raw_data, list) else raw_data
        pq.write_table(pa.Table.from_pandas(df), out / name)
    (tmp_path / "settings.yaml").write_text("workflows: [create_communities, create_community_reports]\n")
    yield tmp_path


def _load_parquets(root: Path):
    out = root / "output"
    ents = pd.read_parquet(out / "entities.parquet")
    rels = pd.read_parquet(out / "relationships.parquet")
    tus = pd.read_parquet(out / "text_units.parquet") if (out / "text_units.parquet").exists() else pd.DataFrame()
    return ents, rels, tus


@pytest.mark.parametrize("byog_fixture", ["smoke_byog_root", "mini_game_byog_root"])
def test_byog_required_columns_and_no_dangling(byog_fixture, request):
    root: Path = request.getfixturevalue(byog_fixture)
    ents, rels, tus = _load_parquets(root)

    # Required for BYOG + communities (per GraphRAG + our extensions)
    for col in ("id", "title", "description", "text_unit_ids"):
        assert col in ents.columns, f"entities missing {col}"

    for col in ("id", "source", "target", "description", "weight", "text_unit_ids"):
        assert col in rels.columns, f"relationships missing {col}"

    for col in ("id", "human_readable_id", "text", "n_tokens", "document_id", "entity_ids", "relationship_ids", "covariate_ids"):
        assert col in tus.columns, f"text_units missing {col}"

    # Provenance we require
    for col in ("source_file", "extractor", "confidence", "is_deterministic"):
        assert col in ents.columns
        assert col in rels.columns

    # weight present and sensible
    assert (rels["weight"] > 0).all()

    # No dangling relationship endpoints (must resolve to entity titles)
    entity_titles = set(ents["title"].astype(str))
    for _, r in rels.iterrows():
        assert str(r["source"]) in entity_titles, f"dangling source {r['source']} not in titles"
        assert str(r["target"]) in entity_titles, f"dangling target {r['target']} not in titles"

    # No dangling text-unit references from entities or relationships
    if len(tus) > 0:
        text_unit_ids = set(tus["id"].astype(str))
        for frame_name, frame in (("entities", ents), ("relationships", rels)):
            for _, row in frame.iterrows():
                for tuid in row.get("text_unit_ids", []):
                    assert str(tuid) in text_unit_ids, (
                        f"{frame_name} row {row.get('id')} references missing text unit {tuid}"
                    )

    # human_readable_id sanity
    if "human_readable_id" in ents.columns:
        assert ents["human_readable_id"].notna().all()


def test_byog_smoke_specific_alignment(smoke_byog_root: Path):
    """Explicit check on the smoke (generated in tmp) that source/target use titles."""
    ents, rels, _ = _load_parquets(smoke_byog_root)
    titles = set(ents["title"].astype(str))
    for _, r in rels.iterrows():
        assert r["source"] in titles
        assert r["target"] in titles
        assert r["source"] in {"update", "helper"} or "update" in str(r["source"])


def test_core_dataclasses_extracted(mini_game_byog_root: Path):
    """P1 + P2: @dataclass classes in core.py must be extracted and their text_units must contain real source snippets."""
    ents, _, tus = _load_parquets(mini_game_byog_root)
    titles = set(ents["title"].astype(str))
    required = {"core:Config", "core:PlayerState", "core:Event", "core:TraceRecord"}
    missing = required - titles
    assert not missing, f"Missing decorated dataclasses from core.py: {missing}"

    tu_by_id = {str(row["id"]): str(row.get("text", "")) for _, row in tus.iterrows()}

    for t in required:
        ent_row = ents[ents["title"].astype(str) == t].iloc[0]
        tu_ids = ent_row.get("text_unit_ids", []) or []
        snippet_found = False
        for tuid in tu_ids:
            text = tu_by_id.get(str(tuid), "")
            if "@dataclass" in text or "class " + t.split(":")[-1] in text or "def " in text:
                snippet_found = True
                break
        assert snippet_found, f"No real source snippet found for {t} via its text_unit_ids"


def test_main_cross_file_calls_to_sim(mini_game_byog_root: Path):
    """Regression: main.py should produce calls/edges into sim module (run_simulation, events_from_list)."""
    _, rels, _ = _load_parquets(mini_game_byog_root)
    pairs = {
        (str(row["source"]), str(row["target"]), str(row["type"]))
        for _, row in rels.iterrows()
    }

    expected = {
        ("main:run", "sim:events_from_list", "calls"),
        ("main:run", "sim:run_simulation", "calls"),
        ("main:dump_golden", "sim:events_from_list", "calls"),
        ("main:dump_golden", "sim:run_simulation", "calls"),
    }
    missing = expected - pairs
    assert not missing, f"Missing cross-file call edges from main.py into sim: {missing}"


def test_behavior_contract_matches_golden_inputs():
    """Machine-readable contract should stay in sync with committed golden scenarios."""
    contract_path = Path(__file__).parent / "behavior_contract.json"
    contract = json.loads(contract_path.read_text())

    scenarios = contract["scenarios"]
    assert set(scenarios) == set(GOLDEN_INPUTS)
    for name, jumps in GOLDEN_INPUTS.items():
        assert scenarios[name]["jumps"] == jumps
        assert (Path(__file__).parent / f"golden_{name}.json").exists()

    trace_names = set(contract["golden_traces"])
    expected_trace_names = {f"golden_{name}.json" for name in GOLDEN_INPUTS}
    assert trace_names == expected_trace_names


# --- graph_query tests (self-contained via fixture) ---

def test_graph_query_callers_callees(mini_game_byog_root: Path):
    from scripts.graph_query import callers, callees, load_graph
    ents, rels = load_graph(mini_game_byog_root)

    # run_simulation should call into physics (cross-file, thanks to two-pass + better resolution)
    c = callees(ents, rels, "sim:run_simulation")
    assert any("physics:update_player" in x for x in c), f"Expected cross-file callee, got {c}"

    # physics symbol should have callers from sim
    callers_list = callers(ents, rels, "physics:update_player")
    assert any("sim:" in x for x in callers_list), f"Expected caller from sim, got {callers_list}"


def test_graph_query_impact_symbol(mini_game_byog_root: Path):
    from scripts.graph_query import impact, symbol_lookup, load_graph
    ents, rels = load_graph(mini_game_byog_root)

    impacted = impact(ents, rels, "core:Config")
    assert isinstance(impacted, list)

    s = symbol_lookup(ents, "sim:run_simulation")
    assert s is not None and s.get("title") == "sim:run_simulation"
    assert "snippet_preview" in s or "source_file" in s


def test_ast_attribute_resolution_regression(tmp_path: Path):
    """Separate regression fixture for AST Attribute call resolution (module.func).

    We feed a small synthetic Python snippet that uses attribute call style
    (after an import) and assert that the extractor produces a call relationship
    carrying resolved_target_hint with good confidence.
    """
    from scripts.extract_python import extract_from_file

    src = '''
import physics

def before(state, cfg):
    return None

def caller(state, cfg):
    physics.update_player(state, True, cfg)

def after(state, cfg):
    return None
'''

    py_file = tmp_path / "regression_attr.py"
    py_file.write_text(src)

    result = extract_from_file(py_file)
    calls = [r for r in result.get("relationships", []) if r.get("type") == "calls"]

    # We expect at least one call that picked up the Attribute and got a hint
    hinted = [
        r for r in calls
        if r.get("resolved_target_hint") and "update_player" in str(r.get("resolved_target_hint"))
    ]
    assert len(hinted) >= 1, f"No Attribute-resolved call hint found. Calls: {calls}"

    h = hinted[0]
    assert "resolved_target_hint" in h
    assert str(h.get("source", "")).endswith(":caller")
    assert float(h.get("confidence", 0)) >= 0.80
    assert h.get("is_deterministic") is True
    assert "tree-sitter-python+ast" in str(h.get("extractor", ""))
