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
    # Persist observations (if any) so ByogGraph flat load and context_pack see them
    obs = raw.get("call_observations", [])
    if obs:
        obs_df = pd.DataFrame(obs) if isinstance(obs, list) else obs
        pq.write_table(pa.Table.from_pandas(obs_df), out / "call_observations.parquet")
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


def test_byog_graph_observations_symbol_and_module(mini_game_byog_root: Path):
    from scripts.byog_graph import ByogGraph

    g = ByogGraph(mini_game_byog_root)
    symbol_obs = g.observations("sim:run_simulation")
    module_obs = g.observations("sim")

    assert any(o.get("display_target") == "trace.append" for o in symbol_obs)
    assert any(o.get("display_target") == "trace.append" for o in module_obs)
    assert any(o.get("reason") == "builtin/container call observation" for o in module_obs)


def test_generic_python_bridge_uses_relative_module_keys(tmp_path: Path):
    """Generic indexing must not collide on repeated basenames like a/models.py and b/models.py."""
    from scripts.mini_game_to_byog import build_byog_for_package

    pkg = tmp_path / "pkg"
    (pkg / "a").mkdir(parents=True)
    (pkg / "b").mkdir(parents=True)
    (pkg / "a" / "models.py").write_text("class Alpha:\n    pass\n")
    (pkg / "b" / "models.py").write_text("class Beta:\n    pass\n")

    data = build_byog_for_package(package_dir=pkg)
    titles = [e["title"] for e in data["entities"]]

    assert "a.models:Alpha" in titles
    assert "b.models:Beta" in titles
    assert len(titles) == len(set(titles))


def test_generic_python_bridge_keeps_same_named_callers_separate(tmp_path: Path):
    """Calls from repeated function names (main, run, etc.) must stay module-qualified."""
    from scripts.mini_game_to_byog import build_byog_for_package

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "alpha.py").write_text(
        "def target_alpha():\n"
        "    pass\n\n"
        "def main():\n"
        "    target_alpha()\n"
    )
    (pkg / "beta.py").write_text(
        "def target_beta():\n"
        "    pass\n\n"
        "def main():\n"
        "    target_beta()\n"
    )

    data = build_byog_for_package(package_dir=pkg)
    calls = [
        r for r in data["relationships"]
        if r.get("type") == "calls"
    ]
    pairs = {(r.get("source"), r.get("target")) for r in calls}

    assert ("alpha:main", "alpha:target_alpha") in pairs
    assert ("beta:main", "beta:target_beta") in pairs
    assert ("alpha:main", "beta:target_beta") not in pairs
    assert ("beta:main", "alpha:target_alpha") not in pairs


def test_same_named_callers_do_not_absorb_cross_module_targets(tmp_path: Path):
    """Regression for the P1 audit symptom on byog_tool_eval.

    The companion test above uses module-local targets. The real bug was
    subtler: the winning ``*:main`` also absorbed calls whose *targets* lived
    in OTHER modules (e.g. ``extract_python:main`` owned
    ``byog_graph:publish_byog_snapshot`` and
    ``mini_game_to_byog:build_byog_for_package``). This reproduces that exact
    topology: several same-named callers, each invoking a *different*
    cross-module imported callee.
    """
    from scripts.mini_game_to_byog import build_byog_for_package

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "shared.py").write_text(
        "def shared_a():\n    pass\n\n"
        "def shared_b():\n    pass\n"
    )
    (pkg / "alpha.py").write_text(
        "from .shared import shared_a\n\n"
        "def main():\n"
        "    shared_a()\n"
    )
    (pkg / "beta.py").write_text(
        "from .shared import shared_b\n\n"
        "def main():\n"
        "    shared_b()\n"
    )

    data = build_byog_for_package(use_advanced=True, package_dir=pkg)
    pairs = {
        (r.get("source"), r.get("target"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    }

    # Each main keeps its own cross-module call ...
    assert ("alpha:main", "shared:shared_a") in pairs
    assert ("beta:main", "shared:shared_b") in pairs
    # ... and neither main absorbs the other's cross-module target.
    assert ("alpha:main", "shared:shared_b") not in pairs
    assert ("beta:main", "shared:shared_a") not in pairs
    # No single main ends up owning calls that belong to a sibling (the precise
    # byog_tool_eval symptom where one module's main hoarded every main's calls).
    main_sources = {s for (s, _t) in pairs if str(s).endswith(":main")}
    assert main_sources == {"alpha:main", "beta:main"}, main_sources


def test_bare_calls_inside_method_use_method_caller(tmp_path: Path):
    """Bare calls inside Class.run must not be attributed to module-level run()."""
    from scripts.mini_game_to_byog import build_byog_for_package

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "eval.py").write_text(
        "class MiniLangError(Exception):\n"
        "    pass\n\n"
        "def format_number(value):\n"
        "    return str(value)\n\n"
        "class Interpreter:\n"
        "    def run(self, stmts):\n"
        "        format_number(1)\n"
        "        MiniLangError('x')\n\n"
        "def run(stmts):\n"
        "    return Interpreter().run(stmts)\n"
    )

    data = build_byog_for_package(package_dir=pkg)
    pairs = {
        (r.get("source"), r.get("target"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    }

    assert ("eval:Interpreter.run", "eval:format_number") in pairs
    assert ("eval:Interpreter.run", "eval:MiniLangError") in pairs
    assert ("eval:run", "eval:format_number") not in pairs
    assert ("eval:run", "eval:MiniLangError") not in pairs


def test_module_entity_title_does_not_collide_with_main_function(tmp_path: Path):
    """main.py with def main() needs distinct module and function entity titles."""
    from scripts.mini_game_to_byog import build_byog_for_package

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.py").write_text(
        "def run_source(source):\n"
        "    return source\n\n"
        "def main():\n"
        "    return run_source('x')\n"
    )

    data = build_byog_for_package(package_dir=pkg)
    titles = [e["title"] for e in data["entities"]]
    module_titles = [
        e["title"] for e in data["entities"] if e.get("type") == "module"
    ]

    assert "main:main" in titles
    assert "main:__module__" in module_titles
    assert len(titles) == len(set(titles))


def test_audit_call_edges_clean_on_mini_game(mini_game_byog_root: Path):
    """The reproducible audit tool must report zero structural anomalies and no
    dangling targets on the (correct) mini_game bridge graph.

    Guards both the resolver (no span-outside-caller / dangling regressions) and
    the audit tool itself against drift.
    """
    from scripts.audit_call_edges import build_report

    report = build_report(mini_game_byog_root, sample=5, seed=42)
    assert report["total_calls"] > 0
    assert report["structural"]["anomaly_count"] == 0, report["structural"]["anomalies"]
    assert report["dangling_count"] == 0, report["dangling_targets"]
    assert report["structural"]["pass_rate"] == 1.0
    assert all(e["structural_ok"] for e in report["sample"]["edges"])


def test_port_eval_graph_stage_and_golden(mini_game_byog_root: Path):
    """port_eval graph stage must be clean and golden scenarios discoverable.

    Keeps the suite fast/toolchain-independent by exercising the pure stages only
    (no cargo); the cargo end-to-end path is covered by running the harness.
    """
    from scripts.port_eval import eval_graph, count_golden

    # source is unused when reindex=False; pass the graph root as a placeholder.
    g = eval_graph(mini_game_byog_root, source=mini_game_byog_root, reindex=False, use_advanced=False)
    assert g["total_calls"] > 0
    assert g["structural_anomalies"] == 0
    assert g["dangling_targets"] == 0
    assert g["clean"] is True

    golden = count_golden(Path(__file__).parent.parent)
    assert golden["count"] == 5
    assert golden["file_count"] == 5
    assert all(n.startswith("golden_") and n.endswith(".json") for n in golden["names"])

    mini_lang_golden = count_golden(Path(__file__).parents[2] / "mini_lang")
    assert mini_lang_golden["count"] == 28
    assert mini_lang_golden["file_count"] == 3


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

    # Root-level snapshot layout is the generator's current production format:
    # <graph>/current + <graph>/snapshots/<id> should win over stale <graph>/output.
    root_layout = tmp_path / "root_layout"
    root_snap_id = "20240105-000000-root"
    root_snap = root_layout / "snapshots" / root_snap_id
    root_snap.mkdir(parents=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame({"id": ["e1"], "title": ["root_current"]})), root_snap / "entities.parquet")
    pq.write_table(pa.Table.from_pandas(dummy_r), root_snap / "relationships.parquet")
    pq.write_table(pa.Table.from_pandas(dummy_t), root_snap / "text_units.parquet")
    (root_layout / "current").write_text(root_snap_id)
    stale_out = root_layout / "output"
    stale_out.mkdir()
    pq.write_table(pa.Table.from_pandas(pd.DataFrame({"id": ["e1"], "title": ["stale_output"]})), stale_out / "entities.parquet")
    pq.write_table(pa.Table.from_pandas(dummy_r), stale_out / "relationships.parquet")
    pq.write_table(pa.Table.from_pandas(dummy_t), stale_out / "text_units.parquet")
    g_root = ByogGraph(root_layout)
    assert "20240105" in str(g_root._snap_base)
    assert "root_current" in list(g_root.ents["title"].astype(str))
    assert "stale_output" not in list(g_root.ents["title"].astype(str))

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


def test_bridge_level_synthetic_package_resolution(tmp_path: Path):
    """Bridge-level test: extractor hints must survive the full pipeline
    extractor → two-pass bridge → parquet → ByogGraph.

    We create a minimal synthetic package with interesting import/call patterns,
    run build_byog_for_package on it (with use_advanced), publish the resulting
    data through the real snapshot writer, load via ByogGraph, and assert that
    call relationships carry the correct resolved_target_hint, high confidence,
    is_deterministic=True, and proper FQN titles.
    """
    from scripts.mini_game_to_byog import build_byog_for_package
    from scripts.byog_graph import ByogGraph, publish_byog_snapshot
    import pandas as pd

    # Create synthetic package
    pkg = tmp_path / "synth_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    # Target module with functions and class
    (pkg / "physics.py").write_text("""
def update_player(state, did_jump, cfg):
    pass

class Engine:
    def tick(self):
        pass
""")

    # Submodule
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "mod.py").write_text("def deep_call(): pass")

    # Main exercising patterns
    (pkg / "main.py").write_text("""
from .physics import update_player
import physics as phys
from .physics import Engine
import pkg.sub.mod as submod
from . import physics as phys_direct   # from . import module

class Demo:
    def run(self):
        self.helper()   # self. method call
    @classmethod
    def cm(cls):
        cls.helper()    # cls. method call
    def helper(self):
        pass

class Other:
    def helper(self):
        pass

def outer():
    dd = Demo()
    def inner():
        dd = "bad in inner"
        dd.helper()                       # inner scope: guarded weak edge
    dd.helper()                           # outer scope: high-conf
    inner()

def another_outer():
    dd = Demo()
    def inner():
        dd = "bad in another"
        dd.helper()                       # another_inner scope: guarded independently
    dd.helper()                           # another_outer scope: high-conf, must not be polluted by any inner
    inner()

def runner():
    update_player(None, False, None)      # from-import
    phys.update_player(None, True, None)  # aliased module.attr
    submod.deep_call()                    # submodule.func
    eng = Engine()
    eng.tick()                            # constructor-tracked method
    phys_direct.update_player(None, False, None)  # from . import module
    d = Demo()
    d.run()                               # exercises self via instance
    d.helper()                            # high-conf before reassignment
    d = "reassigned to str"
    d.helper()                            # guard: no high-conf Demo.helper after this
    outer()
    another_outer()

    # Ambiguity / confidence tiers cases (two classes with same method name,
    # if branches with different assignments to same var, ctor rebind).
    # These must downgrade instead of emitting a strong resolved_target_hint.
    o = Other()
    o.helper()                            # clear single-ctor for Other → high "main:Other.helper"
    if 0:
        v = Demo()
    else:
        v = Other()
    v.helper()                            # ambiguous (multiple ctors seen) → 0.50, no hint
    w = Demo()
    w = Other()
    w.helper()                            # ctor rebind across classes with overlapping .helper → ambiguous

    # Builtin container mutations (separate from plain reassignment guards)
    trace = []
    trace.append(42)                      # should be "builtin/container call observation", not "guarded by reassignment"
    cfg = {}
    _ = cfg.get("missing", None)          # container .get
    mixed = []
    mixed = Demo()
    mixed.helper()                        # prior container marker must not pollute ctor resolution

    # Typed annotations: provide hints from annot even without (or in addition to) ctor expr.
    x: Demo = Demo()
    x.helper()                            # high conf "main:Demo.helper" via annotation
    y: Demo
    y.helper()                            # bare annotation only, no ctor expression
    z: Demo
    z = Demo()
    z.helper()
    guarded_annot: Demo
    guarded_annot = "bad"
    guarded_annot.helper()                # later reassignment must override annotation hint
    wrong_annot: Demo = "bad"
    wrong_annot.helper()                  # explicit scalar initializer must not become high-conf
    events: list = []
    events.append(99)                     # container via annot + literal

    # typing aliases + PEP 604 unions (the next requested support)
    items: List[Demo] = []
    items.append(Demo())                  # container via typing.List alias
    typing_items: typing.List[Demo] = []
    typing_items.append(Demo())
    seq_items: collections.abc.Sequence[Demo] = []
    seq_items.append(Demo())
    opt: Optional[Demo] = Demo()
    opt.helper()                          # Optional unwrapped to Demo → high conf
    uni: Demo | None
    uni = Demo()
    uni.helper()                          # PEP604 union → high Demo
    amb_union: Demo | Other
    amb_union.helper()                    # multiple real union types → ambiguous, no hint
    amb_typing_union: Union[Demo, Other]
    amb_typing_union.helper()
""")

    # Run the bridge on the synthetic package (this exercises title normalization,
    # hint preservation in relationships, etc.)
    data = build_byog_for_package(use_advanced=True, package_dir=pkg)

    # Build dfs and publish them through the real snapshot writer. This exercises
    # parquet roundtrip, atomic snapshot layout, current pointer resolution, and
    # ByogGraph loading in one path.
    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    tus_df = pd.DataFrame(data["text_units"])
    obs_df = pd.DataFrame(data.get("call_observations", []))

    graph_root = tmp_path / "synth_graph"
    snap_dir = publish_byog_snapshot(
        ents_df,
        rels_df,
        tus_df,
        graph_root / "output",
        keep_last=2,
        source_root=pkg,
        call_observations_df=obs_df if len(obs_df) > 0 else None,
    )

    # Load via ByogGraph (simulates what context_pack / graph_query / agent do)
    g = ByogGraph(graph_root)
    assert g._snap_base == snap_dir

    # Inspect relationships for preserved hints
    call_rels = g.rels[g.rels["type"].astype(str) == "calls"]

    # We expect hints from the AST pass to be present on the call edges
    # after bridge normalization (titles are FQN like "main:runner", "physics:update_player")
    hints = call_rels["resolved_target_hint"].dropna().astype(str).tolist()
    assert any("physics:update_player" in h for h in hints), f"Missing physics hint in {hints}"
    assert any("sub.mod:deep_call" in h or "mod:deep_call" in h for h in hints), f"Missing submodule hint in {hints}"
    assert "physics:Engine.tick" in hints, f"Missing method hint in {hints}"
    assert "main:Demo.helper" in hints, f"Missing self/cls method hint in {hints}"
    assert "main:Other.helper" in hints, f"Missing clear Other helper hint (single-ctor) in {hints}"
    # Ambiguous v/w (branches/rebinds) must not survive bridge as spurious
    # high-conf specific calls.
    ambiguous_bridge_calls = call_rels[
        call_rels["description"].astype(str).str.contains("v.helper|w.helper", regex=True, na=False)
    ]
    assert ambiguous_bridge_calls.empty

    # First-class observations must capture the weak/ambiguous calls (they are
    # intentionally not in core rels, per the negative above). This makes
    # uncertain call sites available to context_pack and the port agent.
    obs = getattr(g, "call_observations", pd.DataFrame())
    assert len(obs) > 0, "call_observations should have been published for the synthetic"
    # Look for our guarded and ambiguous cases by display_target or description
    obs_descs = obs.get("description", pd.Series(dtype=str)).astype(str).tolist() if len(obs) else []
    obs_reasons = obs.get("reason", pd.Series(dtype=str)).astype(str).tolist() if len(obs) else []
    obs_targets = obs.get("display_target", pd.Series(dtype=str)).astype(str).tolist() if len(obs) else []
    assert any("v.helper" in d or "w.helper" in d for d in obs_descs), f"Missing ambiguous v/w in observations: {obs_descs[:5]}"
    assert any("v.helper" in t or "w.helper" in t for t in obs_targets), f"Missing display target for ambiguous v/w: {obs_targets[:5]}"
    assert any("ambiguous" in str(r) for r in obs_reasons), f"Missing 'ambiguous' reason in observations: {obs_reasons[:5]}"
    # Also the post-reassign guard on d.helper should be observable
    assert any("guarded" in str(r) for r in obs_reasons) or any("d.helper" in d and "reassigned" in d for d in obs_descs)
    # Builtin container mutations (trace.append etc.) must get their own reason, not be
    # misclassified as generic "guarded by reassignment".
    assert any(
        any(target in t for target in ("trace.append", "cfg.get", "items.append", "typing_items.append", "seq_items.append"))
        for t in obs_targets
    ), f"Missing container call display_target: {obs_targets[:5]}"
    assert any("builtin/container" in str(r) for r in obs_reasons), f"Missing builtin/container reason: {obs_reasons[:5]}"
    assert any("amb_union.helper" in t or "amb_typing_union.helper" in t for t in obs_targets), \
        f"Missing ambiguous union observation: {obs_targets[:5]}"
    assert any("ambiguous annotation" in str(r) for r in obs_reasons), \
        f"Missing ambiguous annotation reason: {obs_reasons[:5]}"
    assert not any("mixed.helper" in t for t in obs_targets), f"Resolved mixed.helper should not be an observation: {obs_targets[:5]}"
    # Annotated high-conf cases (x, y, z) must resolve properly and not appear as weak obs
    assert not any(any(bad in t for bad in ("x.helper", "y.helper", "z.helper", "opt.helper", "uni.helper")) for t in obs_targets), \
        f"High-conf annotated calls leaked to weak observations: {obs_targets[:5]}"
    assert any("guarded_annot.helper" in t for t in obs_targets), f"Missing guarded annotation observation: {obs_targets[:5]}"
    assert any("wrong_annot.helper" in t for t in obs_targets), f"Missing contradictory annotation observation: {obs_targets[:5]}"
    # Their confidence must be the downgraded tier
    if len(obs):
        low_conf_mask = obs["confidence"].astype(float) < 0.7
        assert low_conf_mask.any(), "Expected at least one low-confidence observation"

    mixed_calls = call_rels[
        call_rels["description"].astype(str).str.contains("mixed.helper", na=False)
    ]
    assert len(mixed_calls) == 1
    assert mixed_calls.iloc[0].get("resolved_target_hint") == "main:Demo.helper"

    annot_y_calls = call_rels[
        call_rels["description"].astype(str).str.contains("ast Attribute: y.helper ", regex=False, na=False)
    ]
    assert len(annot_y_calls) == 1
    assert annot_y_calls.iloc[0].get("resolved_target_hint") == "main:Demo.helper"

    phys_direct_calls = call_rels[
        call_rels["description"].astype(str).str.contains("phys_direct.update_player", na=False)
    ]
    assert not phys_direct_calls.empty
    assert set(phys_direct_calls["target"].astype(str)) == {"physics:update_player"}

    # Check that at least some have the high-confidence AST metadata
    ast_calls = call_rels[call_rels["extractor"].astype(str).str.contains("ast", na=False)]
    assert len(ast_calls) > 0
    for _, row in ast_calls.iterrows():
        assert float(row.get("confidence", 0)) >= 0.80
        assert row.get("is_deterministic") is True or str(row.get("is_deterministic")).lower() == "true"

    # Bonus: ByogGraph queries should work on the resulting graph
    callees = g.callees("main:runner")
    assert "physics:update_player" in callees
    assert "sub.mod:deep_call" in callees
    assert "physics:Engine.tick" in callees
    assert "main:Demo.run" in callees
    assert "main:Demo.helper" in callees
    assert g.callees("main:Demo.run") == ["main:Demo.helper"]
    assert g.callees("main:Demo.cm") == ["main:Demo.helper"]

    runner_helper_calls = call_rels[
        (call_rels["source"].astype(str) == "main:runner")
        & (call_rels["description"].astype(str).str.contains("ast Attribute: d.helper ", regex=False, na=False))
    ]
    assert len(runner_helper_calls) == 1
    assert runner_helper_calls.iloc[0]["target"] == "main:Demo.helper"

    # Nested/inner function scope + reassignment guard (qualified keys).
    # If assignment from inner() polluted outer's scope (or cross-polluted between two inners),
    # the high-conf edge from that outer would be missing (or wrong target).
    # Using qualified scope keys ("outer.inner", "another_outer.inner") isolates the buckets.
    assert "main:Demo.helper" in g.callees("main:outer")
    assert "main:Demo.helper" in g.callees("main:another_outer")
    assert not (
        call_rels["source"].astype(str).str.contains("inner", na=False)
        & (call_rels["target"].astype(str) == "main:Demo.helper")
    ).any()


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
from . import physics as phys_direct

import pkg.sub.mod
import pkg.sub.mod as submod   # for submodule test

class Demo:
    def run(self):
        self.helper()

    @classmethod
    def cm(cls):
        cls.helper()

    def helper(self):
        pass

class Other:
    def helper(self):
        pass

def outer():
    dd = Demo()
    def inner():
        dd = "bad in inner"
        dd.helper()
    dd.helper()
    inner()

def another_outer():
    dd = Demo()
    def inner():
        dd = "bad in another"
        dd.helper()
    dd.helper()
    inner()

def runner():
    update_player(None, False, None)           # from-import bare
    phys.update_player(None, True, None)       # import-as + bare attr
    pkg.sub.mod.deep_call()                    # module.submodule.func style
    submod.deep_call()                         # alias to dotted module
    eng = Engine()
    eng.tick()                                 # method call
    phys_direct.update_player(None, False, None)
    d = Demo()
    d.run()
    d.helper()
    d = "reassigned to str"
    d.helper()
    outer()
    another_outer()

    # Ambiguity / confidence tiers cases (two classes same method, if branches
    # different assignments, alias/ctor rebind). Must produce weak edges without
    # strong resolved_target_hint (honest downgrade, not resolve-or-nothing).
    o = Other()
    o.helper()                            # clear, single ctor → high conf + "main:Other.helper"
    if 0:
        v = Demo()
    else:
        v = Other()
    v.helper()                            # branch: two ctors → ambiguous 0.50 no hint
    w = Demo()
    w = Other()
    w.helper()                            # rebind different class → ambiguous

    # Builtin container mutations (separate from plain reassignment guards)
    trace = []
    trace.append(42)                      # should produce distinct "builtin/container call observation"
    cfg = {}
    _ = cfg.get("missing", None)
    mixed = []
    mixed = Demo()
    mixed.helper()                        # prior container marker must not pollute ctor resolution

    # Typed annotations
    x: Demo = Demo()
    x.helper()
    y: Demo
    y.helper()
    z: Demo
    z = Demo()
    z.helper()
    guarded_annot: Demo
    guarded_annot = "bad"
    guarded_annot.helper()
    wrong_annot: Demo = "bad"
    wrong_annot.helper()
    events: list = []
    events.append(99)

    # typing aliases + PEP 604 unions
    items: List[Demo] = []
    items.append(Demo())
    typing_items: typing.List[Demo] = []
    typing_items.append(Demo())
    seq_items: collections.abc.Sequence[Demo] = []
    seq_items.append(Demo())
    opt: Optional[Demo] = Demo()
    opt.helper()
    uni: Demo | None
    uni = Demo()
    uni.helper()
    amb_union: Demo | Other
    amb_union.helper()
    amb_typing_union: Union[Demo, Other]
    amb_typing_union.helper()
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

    direct_module_calls = [
        c for c in calls
        if c.get("resolved_target_hint") == "physics:update_player"
        and "phys_direct.update_player" in str(c.get("description", ""))
    ]
    assert direct_module_calls, "from . import module alias call missing hint"

    self_calls = [
        c for c in calls
        if c.get("source") == "main:Demo.run"
        and c.get("resolved_target_hint") == "main:Demo.helper"
        and "self.helper" in str(c.get("description", ""))
    ]
    assert self_calls, "self.method call missing bridge-resolvable hint"

    cls_calls = [
        c for c in calls
        if c.get("source") == "main:Demo.cm"
        and c.get("resolved_target_hint") == "main:Demo.helper"
        and "cls.helper" in str(c.get("description", ""))
    ]
    assert cls_calls, "cls.method call missing bridge-resolvable hint"

    d_helper_calls = [
        c for c in calls
        if "ast Attribute: d.helper " in str(c.get("description", ""))
    ]
    assert len(d_helper_calls) == 2
    high_conf_d_helper = [
        c for c in d_helper_calls
        if c.get("resolved_target_hint") == "main:Demo.helper"
    ]
    assert len(high_conf_d_helper) == 1
    guarded_d_helper = [
        c for c in d_helper_calls
        if not c.get("resolved_target_hint")
    ]
    assert len(guarded_d_helper) == 1
    assert float(guarded_d_helper[0].get("confidence", 1.0)) < 0.80
    assert guarded_d_helper[0].get("is_deterministic") is False

    outer_dd_calls = [
        c for c in calls
        if c.get("source") == "ent:fn:main:outer"
        and "ast Attribute: dd.helper " in str(c.get("description", ""))
    ]
    assert len(outer_dd_calls) == 1
    assert outer_dd_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(outer_dd_calls[0].get("confidence", 0)) >= 0.80

    another_outer_dd_calls = [
        c for c in calls
        if c.get("source") == "ent:fn:main:another_outer"
        and "ast Attribute: dd.helper " in str(c.get("description", ""))
    ]
    assert len(another_outer_dd_calls) == 1
    assert another_outer_dd_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(another_outer_dd_calls[0].get("confidence", 0)) >= 0.80

    # Qualified scope keys: two different inners (outer.inner and another_outer.inner)
    # must have independent guard buckets; neither gets high-conf "main:Demo.helper".
    # The produced caller source for nested uses qualified -> make_id safe name (dots -> _).
    inner_dd_calls = [
        c for c in calls
        if c.get("source") in ("ent:fn:main:outer_inner", "ent:fn:main:another_outer_inner")
        and "ast Attribute: dd.helper " in str(c.get("description", ""))
    ]
    assert len(inner_dd_calls) == 2
    for c in inner_dd_calls:
        assert not c.get("resolved_target_hint")
        assert float(c.get("confidence", 1.0)) < 0.80
        assert c.get("is_deterministic") is False

    # Ambiguity / confidence tiers (if branches with different assignments,
    # two classes sharing method name, ctor rebind/alias shadowing on same var).
    # Must downgrade honestly (0.50, no resolved_target_hint) rather than pick one.
    other_helper_calls = [
        c for c in calls
        if "ast Attribute: o.helper " in str(c.get("description", ""))
    ]
    assert len(other_helper_calls) == 1
    assert other_helper_calls[0].get("resolved_target_hint") == "main:Other.helper"
    assert float(other_helper_calls[0].get("confidence", 0)) >= 0.80

    ambiguous_calls = [
        c for c in calls
        if any(b in str(c.get("description", "")) for b in ("v.helper", "w.helper"))
        and "ast Attribute:" in str(c.get("description", ""))
    ]
    assert len(ambiguous_calls) >= 2
    for c in ambiguous_calls:
        assert not c.get("resolved_target_hint"), f"Ambiguous must not have resolved_target_hint: {c}"
        assert float(c.get("confidence", 1.0)) <= 0.55
        assert c.get("is_deterministic") is False

    mixed_calls = [
        c for c in calls
        if "ast Attribute: mixed.helper " in str(c.get("description", ""))
    ]
    assert len(mixed_calls) == 1
    assert mixed_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(mixed_calls[0].get("confidence", 0)) >= 0.80
    assert mixed_calls[0].get("is_deterministic") is True

    # Typed annotation cases
    annot_x_calls = [
        c for c in calls
        if "ast Attribute: x.helper " in str(c.get("description", ""))
    ]
    assert len(annot_x_calls) == 1
    assert annot_x_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(annot_x_calls[0].get("confidence", 0)) >= 0.80

    annot_y_calls = [
        c for c in calls
        if "ast Attribute: y.helper " in str(c.get("description", ""))
    ]
    assert len(annot_y_calls) == 1
    assert annot_y_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(annot_y_calls[0].get("confidence", 0)) >= 0.80

    annot_z_calls = [
        c for c in calls
        if "ast Attribute: z.helper " in str(c.get("description", ""))
    ]
    assert len(annot_z_calls) == 1
    assert annot_z_calls[0].get("resolved_target_hint") == "main:Demo.helper"

    guarded_annot_calls = [
        c for c in calls
        if "ast Attribute: guarded_annot.helper " in str(c.get("description", ""))
    ]
    assert len(guarded_annot_calls) == 1
    assert not guarded_annot_calls[0].get("resolved_target_hint")
    assert float(guarded_annot_calls[0].get("confidence", 1.0)) <= 0.45
    assert guarded_annot_calls[0].get("is_deterministic") is False

    wrong_annot_calls = [
        c for c in calls
        if "ast Attribute: wrong_annot.helper " in str(c.get("description", ""))
    ]
    assert len(wrong_annot_calls) == 1
    assert not wrong_annot_calls[0].get("resolved_target_hint")
    assert float(wrong_annot_calls[0].get("confidence", 1.0)) <= 0.45
    assert wrong_annot_calls[0].get("is_deterministic") is False

    # typing.List / Optional / | union high-conf cases (new parser support)
    items_append_calls = [
        c for c in calls
        if any(name in str(c.get("description", "")) for name in (
            "ast Attribute: items.append ",
            "ast Attribute: typing_items.append ",
            "ast Attribute: seq_items.append ",
        ))
    ]
    assert len(items_append_calls) >= 3
    for c in items_append_calls:
        assert "builtin container" in str(c.get("description", ""))
        assert not c.get("resolved_target_hint")
        assert float(c.get("confidence", 1)) <= 0.45

    opt_calls = [
        c for c in calls
        if "ast Attribute: opt.helper " in str(c.get("description", ""))
    ]
    assert len(opt_calls) == 1
    assert opt_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(opt_calls[0].get("confidence", 0)) >= 0.80

    uni_calls = [
        c for c in calls
        if "ast Attribute: uni.helper " in str(c.get("description", ""))
    ]
    assert len(uni_calls) == 1
    assert uni_calls[0].get("resolved_target_hint") == "main:Demo.helper"
    assert float(uni_calls[0].get("confidence", 0)) >= 0.80

    ambiguous_union_calls = [
        c for c in calls
        if any(name in str(c.get("description", "")) for name in (
            "ast Attribute: amb_union.helper ",
            "ast Attribute: amb_typing_union.helper ",
        ))
    ]
    assert len(ambiguous_union_calls) == 2
    for c in ambiguous_union_calls:
        assert not c.get("resolved_target_hint")
        assert "ambiguous annotation" in str(c.get("description", ""))
        assert float(c.get("confidence", 1.0)) <= 0.55
        assert c.get("is_deterministic") is False

    # Builtin container calls (trace.append, cfg.get etc.) must be classified separately
    # from generic "guarded by reassignment". They get their own reason in observations.
    container_calls = [
        c for c in calls
        if any(
            b in str(c.get("description", ""))
            for b in ("trace.append", "cfg.get", "events.append", "items.append", "typing_items.append", "seq_items.append")
        )
        and "ast Attribute:" in str(c.get("description", ""))
    ]
    assert len(container_calls) >= 1
    for c in container_calls:
        desc = str(c.get("description", ""))
        assert "builtin container" in desc
        assert not c.get("resolved_target_hint")
        assert float(c.get("confidence", 1.0)) <= 0.45
        assert c.get("is_deterministic") is False

    # All created call relationships from AST should have good metadata
    ast_calls = [c for c in calls if "tree-sitter-python+ast" in str(c.get("extractor", ""))]
    guarded_weak_calls = (
        guarded_d_helper
        + inner_dd_calls
        + ambiguous_calls
        + container_calls
        + guarded_annot_calls
        + wrong_annot_calls
        + items_append_calls
        + ambiguous_union_calls
    )
    for c in ast_calls:
        assert "resolved_target_hint" in c or "description" in c
        if c in guarded_weak_calls:
            continue
        assert float(c.get("confidence", 0)) >= 0.80
        assert c.get("is_deterministic") is True

    # Also sanity: imports were parsed
    assert any("physics" in str(imp) for imp in result.get("imports", []))
