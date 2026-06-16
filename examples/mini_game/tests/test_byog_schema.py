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


def test_snapshot_keep_last_n_and_fallback(tmp_path: Path):
    """Test keep-last-N cleanup + current/fallback resolution.

    - Creates multiple timestamped snapshot dirs.
    - Sets 'current' to one of them.
    - Runs cleanup(keep_last=2).
    - Verifies only the 2 most recent (including current) remain.
    - Verifies ByogGraph can resolve and load from current.
    - Verifies flat fallback still works when no snapshots/current.
    """
    from scripts.byog_graph import ByogGraph, cleanup_old_snapshots, _resolve_output_base

    out = tmp_path / "output"
    snapshots = out / "snapshots"
    snapshots.mkdir(parents=True)

    # Create 4 fake snapshots (names sort chronologically)
    snap_ids = [
        "20240101-000000-aaaa",
        "20240102-000000-bbbb",
        "20240103-000000-cccc",  # will be set as current
        "20240104-000000-dddd",
    ]
    # Minimal valid dataframes for the test snapshots
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    dummy_e = pd.DataFrame({"id": ["e1"], "title": ["dummy"]})
    dummy_r = pd.DataFrame({"id": ["r1"], "source": ["s"], "target": ["t"]})
    dummy_t = pd.DataFrame({"id": ["t1"], "text": ["sample"]})
    for sid in snap_ids:
        sd = snapshots / sid
        sd.mkdir()
        pq.write_table(pa.Table.from_pandas(dummy_e), sd / "entities.parquet")
        pq.write_table(pa.Table.from_pandas(dummy_r), sd / "relationships.parquet")
        pq.write_table(pa.Table.from_pandas(dummy_t), sd / "text_units.parquet")
        (sd / "manifest.json").write_text(json.dumps({"id": sid}))

    # Set current to the third one
    (out / "current").write_text("20240103-000000-cccc")

    # Run cleanup keep_last=2
    deleted = cleanup_old_snapshots(out, keep_last=2)
    assert deleted == 2

    remaining = sorted([d.name for d in snapshots.iterdir()])
    assert remaining == ["20240103-000000-cccc", "20240104-000000-dddd"]

    # ByogGraph should load the current snapshot
    g = ByogGraph(tmp_path)  # pass the parent root, not the output subdir
    assert len(g.ents) >= 0  # at least doesn't crash
    # For robustness in test, just check resolve works and _snap_base points inside snapshots
    assert "20240103" in str(g._snap_base)

    # Test flat fallback (structure: <parent>/output/<parquets flat>)
    flat_parent = tmp_path / "flat_parent"
    flat_out = flat_parent / "output"
    flat_out.mkdir(parents=True)
    # Write minimal valid parquets for all three directly in the output dir (flat)
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    dummy_e = pd.DataFrame({"id": ["e1"], "title": ["test"]})
    pq.write_table(pa.Table.from_pandas(dummy_e), flat_out / "entities.parquet")
    dummy_r = pd.DataFrame({"id": ["r1"], "source": ["s"], "target": ["t"]})
    pq.write_table(pa.Table.from_pandas(dummy_r), flat_out / "relationships.parquet")
    dummy_t = pd.DataFrame({"id": ["t1"], "text": ["sample"]})
    pq.write_table(pa.Table.from_pandas(dummy_t), flat_out / "text_units.parquet")

    g2 = ByogGraph(flat_parent)  # root is parent; no current/snapshots -> fallback to flat_out
    assert "test" in list(g2.ents["title"].astype(str))
    # The resolved base should be the flat output dir
    assert _resolve_output_base(flat_out) == flat_out

    # keep_last=0 is clamped to 1, and current is still protected.
    out_zero = tmp_path / "zero_keep" / "output"
    snaps_zero = out_zero / "snapshots"
    snaps_zero.mkdir(parents=True)
    for sid in ["20240101-000000-aaaa", "20240102-000000-bbbb"]:
        sd = snaps_zero / sid
        sd.mkdir()
    (out_zero / "current").write_text("20240101-000000-aaaa")
    assert cleanup_old_snapshots(out_zero, keep_last=0) == 1
    assert sorted(d.name for d in snaps_zero.iterdir()) == ["20240101-000000-aaaa"]


def test_python_name_resolution_regression(tmp_path: Path):
    """Real regression fixtures for Python import/call patterns using pure AST + tree-sitter.

    Patterns covered (as requested):
    - from module import name  → bare call gets resolved_target_hint
    - import module as alias   → alias.func gets hint
    - module.submodule.func    → qualified Attribute
    - method calls (obj.method)
    - relative imports in package context

    We assert on the data returned by extract_from_file (before bridge normalization).
    This acts as a living fixture for the deterministic resolution.
    """
    from scripts.extract_python import extract_from_file

    # Create a tiny package structure for relative imports
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    # physics.py (target module)
    (pkg / "physics.py").write_text("""
def update_player(state, did_jump, cfg):
    pass

class Engine:
    def tick(self):
        pass
""")

    # main.py exercising patterns
    main_py = pkg / "main.py"
    main_py.write_text("""
from .physics import update_player
import physics as phys
from .physics import Engine

import pkg.sub.mod
import pkg.sub.mod as submod   # for submodule test

def runner():
    update_player(None, False, None)           # from-import bare
    phys.update_player(None, True, None)       # import-as + bare attr
    pkg.sub.mod.deep_call()                    # module.submodule.func style
    submod.deep_call()                         # alias to dotted module
    eng = Engine()
    eng.tick()                                 # method call
""")

    # Also a submodule for qualified test
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "mod.py").write_text("def deep_call(): pass")

    result = extract_from_file(main_py)

    calls = [r for r in result.get("relationships", []) if r.get("type") == "calls"]

    # 1. from .physics import update_player → bare call should have hint
    bare_calls = [
        c for c in calls
        if c.get("target") == "update_player"
        and c.get("resolved_target_hint") == "physics:update_player"
    ]
    assert bare_calls, "from-import bare call did not get resolved_target_hint"

    # 2. import physics as phys → alias.attr call
    alias_calls = [
        c for c in calls
        if c.get("resolved_target_hint") == "physics:update_player"
        and "phys.update_player" in str(c.get("description", ""))
    ]
    assert alias_calls, "aliased import + Attribute call missing hint"

    # 3. module.submodule.func style (we created pkg.sub.mod.deep_call)
    dotted = [
        c for c in calls
        if c.get("resolved_target_hint") == "mod:deep_call"
        and "pkg.sub.mod.deep_call" in str(c.get("description", ""))
    ]
    assert dotted, "module.submodule.func style call not detected"

    alias_dotted = [
        c for c in calls
        if c.get("resolved_target_hint") == "mod:deep_call"
        and "submod.deep_call" in str(c.get("description", ""))
    ]
    assert alias_dotted, "dotted module alias call not detected"

    # 4. Method call (Engine.tick)
    method_calls = [
        c for c in calls
        if c.get("resolved_target_hint") == "physics:Engine.tick"
        and "eng.tick" in str(c.get("description", ""))
    ]
    assert method_calls, "method call (obj.method) not recorded"

    # All created call relationships from AST should have good metadata
    ast_calls = [c for c in calls if "tree-sitter-python+ast" in str(c.get("extractor", ""))]
    for c in ast_calls:
        assert "resolved_target_hint" in c or "description" in c
        assert float(c.get("confidence", 0)) >= 0.80
        assert c.get("is_deterministic") is True

    # Also sanity: imports were parsed
    assert any("physics" in str(imp) for imp in result.get("imports", []))
