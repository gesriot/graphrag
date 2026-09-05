"""Weakly connected components over the persisted structural graph.

Topology summary only. MCP exposes the existing producer. No DOT,
NetworkX, or Graphviz.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import multiprocessing
import os
import stat
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.byog_graph import (  # type: ignore
    DEFAULT_COMPONENTS_MAX_COMPONENTS,
    DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
    HARD_MAX_COMPONENT_NODES,
    HARD_MAX_COMPONENTS,
    ByogGraph,
    compute_bounded_subgraph,
    compute_weakly_connected_components,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    components as free_components,
    dumps_components_json,
    dumps_subgraph_json,
    format_components_human,
    format_subgraph_human,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import dumps_subgraph_dot  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"


def _entity(title: str, etype: str = "function", **extra) -> dict:
    e = {
        "id": extra.pop("id", f"ent:{etype}:{title}"),
        "title": title,
        "type": etype,
        "description": extra.pop("description", f"{etype} {title}"),
        "source_file": extra.pop("source_file", "a.py"),
        "span": extra.pop("span", "1:0-2:0"),
        "extractor": extra.pop("extractor", "tree-sitter-python"),
        "confidence": extra.pop("confidence", 1.0),
        "is_deterministic": extra.pop("is_deterministic", True),
        "text_unit_ids": [f"tu:{title}"],
        "document_ids": ["doc:a"],
    }
    e.update(extra)
    return e


def _rel(
    source: str,
    target: str,
    rel_type: str,
    *,
    rid: str | None = None,
    hid: int = 1,
    **extra,
) -> dict:
    row = {
        "id": rid or f"rel:{rel_type}:{source}->{target}:{hid}",
        "source": source,
        "target": target,
        "type": rel_type,
        "description": extra.pop("description", f"{source} {rel_type} {target}"),
        "weight": extra.pop("weight", 1.0),
        "text_unit_ids": [],
        "human_readable_id": hid,
        "source_file": extra.pop("source_file", "a.py"),
        "span": extra.pop("span", ""),
        "extractor": extra.pop("extractor", "tree-sitter-python"),
        "confidence": extra.pop("confidence", 1.0),
        "is_deterministic": extra.pop("is_deterministic", True),
        "document_ids": [],
        "covariate_ids": [],
        "fact_kind": extra.pop("fact_kind", None),
    }
    row.update(extra)
    return row


def _calls(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "calls", hid=hid, **extra)


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_components"
    texts = [
        {
            "id": f"tu:{e['title']}",
            "text": f"// body of {e['title']}\n",
            "n_tokens": 3,
            "document_ids": ["doc:a"],
            "entity_ids": [e["id"]],
            "relationship_ids": [],
        }
        for e in entities
    ]
    publish_byog_snapshot(
        pd.DataFrame(entities),
        pd.DataFrame(relationships),
        pd.DataFrame(texts),
        graph,
        keep_last=2,
    )
    return graph


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr + proc.stdout)
    return proc


def _payload_hashes(graph: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in [graph, *(graph.rglob("*") if graph.exists() else [])]:
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(graph).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _payload_stats(graph: Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for path in [graph, *(graph.rglob("*") if graph.exists() else [])]:
        if path.is_file() and not path.is_symlink():
            info = path.lstat()
            out[path.relative_to(graph).as_posix()] = (
                info.st_size,
                info.st_mtime_ns,
                stat.S_IMODE(info.st_mode),
            )
    return out


def test_disconnected_isolated_endpoint_self_parallel_and_totals():
    ents = pd.DataFrame(
        [
            _entity("A"),
            _entity("B"),
            _entity("C"),
            _entity("Isolated"),
            _entity("Zsmall"),
        ]
    )
    rels = pd.DataFrame(
        [
            _calls("A", "B", hid=1),
            _calls("B", "ghost", hid=2, rid="rel:endpoint"),
            _calls("A", "A", hid=3, rid="rel:self"),
            _calls("A", "B", hid=4, rid="rel:parallel"),
            _rel("C", "Zsmall", "contains", hid=5),
        ]
    )
    result = compute_weakly_connected_components(ents, rels)
    assert result["edge_types"] is None
    assert result["max_components"] == DEFAULT_COMPONENTS_MAX_COMPONENTS
    assert result["max_nodes_per_component"] == DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT
    assert result["n_components_total"] == 3
    assert result["n_components_returned"] == 3
    assert result["components_truncated"] is False
    assert result["n_nodes_total"] == 6
    assert result["n_edges_total"] == 5
    assert result["n_entity_nodes_total"] == 5
    assert result["n_endpoint_only_nodes_total"] == 1
    assert result["n_entity_nodes_total"] + result["n_endpoint_only_nodes_total"] == 6
    assert sum(c["n_nodes_total"] for c in result["components"]) == 6
    assert sum(c["n_edges_total"] for c in result["components"]) == 5
    reps = [c["representative"] for c in result["components"]]
    assert reps == ["A", "C", "Isolated"]
    big = result["components"][0]
    assert big["nodes"] == ["A", "B", "ghost"]
    assert big["n_nodes_total"] == 3
    assert big["n_edges_total"] == 4
    assert big["n_entity_nodes"] == 2
    assert big["n_endpoint_only_nodes"] == 1
    assert big["nodes_truncated"] is False
    pair = result["components"][1]
    assert pair["nodes"] == ["C", "Zsmall"]
    assert pair["n_edges_total"] == 1
    iso = result["components"][2]
    assert iso["nodes"] == ["Isolated"]
    assert iso["n_edges_total"] == 0
    assert iso["n_entity_nodes"] == 1
    g = ByogGraph.__new__(ByogGraph)
    g.ents, g.rels = ents, rels
    assert g.components() == result
    assert free_components(ents, rels) == result
    dumped = dumps_components_json(result)
    for leak in ('"description"', '"snippet"', '"span"', '"weight"', '"confidence"'):
        assert leak not in dumped


def test_edge_type_filter_and_invalid_filters():
    ents = pd.DataFrame([_entity("A"), _entity("B"), _entity("C")])
    rels = pd.DataFrame(
        [
            _calls("A", "B", hid=1),
            _rel("A", "ghost", "contains", hid=2),
            _rel("C", "C", "uses_type", hid=3, rid="rel:self-c"),
        ]
    )
    all_types = compute_weakly_connected_components(ents, rels, edge_types=None)
    empty_filter = compute_weakly_connected_components(ents, rels, edge_types=[])
    assert all_types == empty_filter
    assert all_types["n_nodes_total"] == 4
    assert all_types["n_endpoint_only_nodes_total"] == 1
    calls_only = compute_weakly_connected_components(
        ents, rels, edge_types=["calls", "calls"]
    )
    assert calls_only["edge_types"] == ["calls"]
    assert calls_only["n_nodes_total"] == 3
    assert calls_only["n_edges_total"] == 1
    assert calls_only["n_endpoint_only_nodes_total"] == 0
    assert [c["representative"] for c in calls_only["components"]] == ["A", "C"]
    mixed = compute_weakly_connected_components(
        ents, rels, edge_types=["uses_type", "calls"]
    )
    assert mixed["edge_types"] == ["calls", "uses_type"]
    assert mixed["n_edges_total"] == 2
    none = compute_weakly_connected_components(ents, rels, edge_types=["imports"])
    assert none["n_nodes_total"] == 3
    assert none["n_edges_total"] == 0
    assert none["n_endpoint_only_nodes_total"] == 0
    with pytest.raises(ValueError, match="not a single string"):
        compute_weakly_connected_components(ents, rels, edge_types="calls")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_weakly_connected_components(ents, rels, edge_types=[""])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_weakly_connected_components(ents, rels, edge_types=[" calls"])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_weakly_connected_components(ents, rels, edge_types=["ca\x00lls"])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_weakly_connected_components(ents, rels, edge_types=["calls", 1])  # type: ignore[list-item]


def test_canonical_order_determinism_and_shuffled_rows():
    ents = pd.DataFrame(
        [_entity("Z"), _entity("A"), _entity("M"), _entity("B"), _entity("é")]
    )
    rels = pd.DataFrame(
        [
            _calls("Z", "A", hid=1),
            _calls("A", "M", hid=2),
            _calls("B", "é", hid=3),
        ]
    )
    r1 = compute_weakly_connected_components(ents, rels)
    r2 = compute_weakly_connected_components(
        ents.sample(frac=1, random_state=7).reset_index(drop=True),
        rels.sample(frac=1, random_state=11).reset_index(drop=True),
    )
    assert r1 == r2
    assert r1 == compute_weakly_connected_components(ents, rels)
    reps = [c["representative"] for c in r1["components"]]
    assert reps[0] == "A"
    assert r1["components"][0]["n_nodes_total"] == 3
    assert r1["components"][0]["nodes"] == ["A", "M", "Z"]
    two = [c for c in r1["components"] if c["n_nodes_total"] == 2]
    assert [c["representative"] for c in two] == sorted(
        [c["representative"] for c in two], key=lambda s: s.encode("utf-8")
    )


def test_truncation_flags_are_independent():
    ents = pd.DataFrame([_entity(t) for t in ("A", "B", "C", "D", "E", "F")])
    rels = pd.DataFrame(
        [
            _calls("A", "B", hid=1),
            _calls("B", "C", hid=2),
            _calls("D", "E", hid=3),
        ]
    )
    capped_comp = compute_weakly_connected_components(
        ents, rels, max_components=1, max_nodes_per_component=20
    )
    assert capped_comp["n_components_total"] == 3
    assert capped_comp["n_components_returned"] == 1
    assert capped_comp["components_truncated"] is True
    assert capped_comp["nodes_truncated"] is False
    assert capped_comp["components"][0]["representative"] == "A"
    assert sum(c["n_nodes_total"] for c in capped_comp["components"]) != capped_comp["n_nodes_total"]
    capped_nodes = compute_weakly_connected_components(
        ents, rels, max_components=20, max_nodes_per_component=1
    )
    assert capped_nodes["components_truncated"] is False
    assert capped_nodes["nodes_truncated"] is True
    assert capped_nodes["n_components_returned"] == 3
    big = next(c for c in capped_nodes["components"] if c["representative"] == "A")
    assert big["nodes"] == ["A"]
    assert big["n_nodes_total"] == 3
    assert big["n_nodes_returned"] == 1
    assert big["nodes_truncated"] is True
    iso = next(c for c in capped_nodes["components"] if c["representative"] == "F")
    assert iso["nodes_truncated"] is False
    both = compute_weakly_connected_components(
        ents, rels, max_components=1, max_nodes_per_component=1
    )
    assert both["components_truncated"] is True
    assert both["nodes_truncated"] is True
    omitted = compute_weakly_connected_components(
        ents, rels, max_components=1, max_nodes_per_component=20
    )
    assert omitted["nodes_truncated"] is False


def test_empty_graph_and_malformed_input():
    empty = compute_weakly_connected_components(pd.DataFrame(), pd.DataFrame())
    assert empty["n_components_total"] == 0
    assert empty["components"] == []
    assert empty["n_nodes_total"] == 0
    assert empty["n_edges_total"] == 0
    assert empty["components_truncated"] is False
    assert empty["nodes_truncated"] is False
    assert compute_weakly_connected_components(None, None)["n_components_total"] == 0

    ents = pd.DataFrame([_entity("A")])
    with pytest.raises(ValueError, match="missing required columns"):
        compute_weakly_connected_components(
            pd.DataFrame([{"id": "x"}]), pd.DataFrame()
        )
    with pytest.raises(ValueError, match="missing required columns"):
        compute_weakly_connected_components(
            ents, pd.DataFrame([{"source": "A", "target": "A"}])
        )
    with pytest.raises(ValueError, match="duplicate entity title"):
        compute_weakly_connected_components(
            pd.DataFrame([_entity("A"), _entity("A", id="ent:other")]),
            pd.DataFrame(),
        )
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_weakly_connected_components(
            ents,
            pd.DataFrame(
                [_calls("A", "A", rid="rel:dup"), _calls("A", "A", rid="rel:dup")]
            ),
        )
    bad = _calls("A", "B")
    bad["target"] = None
    with pytest.raises(ValueError, match="invalid target"):
        compute_weakly_connected_components(ents, pd.DataFrame([bad]))
    hidden = pd.DataFrame(
        [_calls("A", "A", rid="rel:ok"), {**_rel("A", "B", "contains"), "target": ""}]
    )
    with pytest.raises(ValueError, match="invalid target"):
        compute_weakly_connected_components(ents, hidden, edge_types=["calls"])
    with pytest.raises(ValueError, match="invalid title"):
        compute_weakly_connected_components(
            pd.DataFrame([{"title": ""}]), pd.DataFrame()
        )


def test_limit_validation():
    ents = pd.DataFrame([_entity("A")])
    rels = pd.DataFrame()
    with pytest.raises(ValueError, match="max_components"):
        compute_weakly_connected_components(ents, rels, max_components=True)
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_weakly_connected_components(ents, rels, max_nodes_per_component=1.5)
    with pytest.raises(ValueError, match="max_components"):
        compute_weakly_connected_components(ents, rels, max_components=0)
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_weakly_connected_components(ents, rels, max_nodes_per_component=-1)
    with pytest.raises(ValueError, match="max_components"):
        compute_weakly_connected_components(ents, rels, max_components=float("nan"))
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_weakly_connected_components(ents, rels, max_nodes_per_component=math.inf)
    with pytest.raises(ValueError, match="max_components"):
        compute_weakly_connected_components(
            ents, rels, max_components=HARD_MAX_COMPONENTS + 1
        )
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_weakly_connected_components(
            ents, rels, max_nodes_per_component=HARD_MAX_COMPONENT_NODES + 1
        )


def test_human_json_cli_parity_and_malformed_exit(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B"), _entity("C")],
        [_calls("A", "B"), _rel("C", "ghost", "contains")],
    )
    g = ByogGraph(graph)
    payload = g.components(edge_types=["calls", "contains"], max_components=10)
    dumped = dumps_components_json(payload)
    assert dumped == dumps_components_json(json.loads(dumped))
    human = format_components_human(payload)
    assert "edge_types: calls,contains" in human
    assert "ghost" in human
    assert "endpoint-only" in human
    common = [
        "components",
        "--graph",
        str(graph),
        "--edge-type",
        "calls",
        "--edge-type",
        "contains",
        "--max-components",
        "10",
        "--max-nodes-per-component",
        "20",
    ]
    gq_h = _run(sys.executable, str(QUERY), *common)
    gc_h = _run(sys.executable, str(CLI), *common)
    mod_h = _run(sys.executable, "-m", "graphrag_code.graph_query", *common)
    assert gq_h.stdout == gc_h.stdout == mod_h.stdout
    assert format_components_human(payload).strip() == gq_h.stdout.strip()
    gq_j = _run(sys.executable, str(QUERY), *common, "--json")
    gc_j = _run(sys.executable, str(CLI), *common, "--json")
    mod_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *common, "--json")
    assert gq_j.stdout == gc_j.stdout == mod_j.stdout
    assert dumps_components_json(json.loads(gq_j.stdout)) + "\n" == gq_j.stdout
    bad = _run(
        sys.executable,
        str(QUERY),
        "components",
        "--graph",
        str(graph),
        "--max-components",
        "0",
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert "max_components" in bad.stderr
    help_out = _run(sys.executable, str(QUERY), "components", "--help")
    assert "--json" in help_out.stdout
    assert "--dot" not in help_out.stdout
    assert "community" in help_out.stdout.lower() or "topology" in help_out.stdout.lower()


def test_installed_wheel_parity_and_snapshots_do_not_mutate(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old")]),
        pd.DataFrame([_calls("demo:old", "demo:old", rid="rel:old")]),
        pd.DataFrame(
            [
                {
                    "id": "tu:old",
                    "text": "old",
                    "n_tokens": 1,
                    "document_ids": [],
                    "entity_ids": ["ent:function:demo:old"],
                    "relationship_ids": [],
                }
            ]
        ),
        graph,
        keep_last=5,
    )
    newer = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:new"), _entity("demo:other")]),
        pd.DataFrame([_calls("demo:new", "demo:other", rid="rel:new")]),
        pd.DataFrame(
            [
                {
                    "id": "tu:new",
                    "text": "new",
                    "n_tokens": 1,
                    "document_ids": [],
                    "entity_ids": ["ent:function:demo:new"],
                    "relationship_ids": [],
                }
            ]
        ),
        graph,
        keep_last=5,
    )
    before = _payload_hashes(graph)
    stats = _payload_stats(graph)
    args = ["components", "--graph", str(graph), "--json"]
    script = _run(sys.executable, str(QUERY), *args)
    module = _run(sys.executable, "-m", "graphrag_code.graph_query", *args)
    product = _run(sys.executable, str(CLI), *args)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        ["graphrag-code", *args],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert (
        json.loads(script.stdout)
        == json.loads(module.stdout)
        == json.loads(product.stdout)
        == json.loads(installed.stdout)
    )
    cur = _run(
        sys.executable,
        str(QUERY),
        "components",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--json",
    )
    assert json.loads(cur.stdout)["n_nodes_total"] == 2
    hist = _run(
        sys.executable,
        str(CLI),
        "components",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body["n_nodes_total"] == 1
    assert body["components"][0]["representative"] == "demo:old"
    assert "demo:new" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not list(outside.glob("*.dot"))


def _cleanup_processes(*processes, release=None) -> None:
    if release is not None:
        release.set()
    for process in processes:
        if process.pid is None:
            continue
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def _components_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from graphrag_code import graph_query
    from typer.testing import CliRunner

    orig = graph_query.dumps_components_json

    def wrap_dumps(result):
        payload = orig(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_components_json = wrap_dumps
    runner = CliRunner()
    result = runner.invoke(
        graph_query.app,
        ["components", "--graph", graph, "--json"],
    )
    q.put(f"exit:{result.exit_code}")


def _publisher(graph: str, marker: str, keep_last: int, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    ents = pd.DataFrame(
        [{"id": f"ent:{marker}", "title": marker, "type": "function", "source_file": "x.py"}]
    )
    rels = pd.DataFrame(
        [{"id": f"rel:{marker}", "source": "x.py", "target": marker, "type": "contains"}]
    )
    tus = pd.DataFrame(
        [{"id": f"tu:{marker}", "title": "x.py", "source_file": "x.py", "entity_id": f"ent:{marker}"}]
    )
    snap = byog.publish_byog_snapshot(
        ents, rels, tus, ChildPath(graph), keep_last=keep_last
    )
    q.put(snap.name)


def test_publisher_waits_through_components_serialization(tmp_path: Path):
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_components_json_hold, args=(str(graph), held, resume, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 1, about, got, q))
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        resume.set()
        pub.join(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not reader.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(reader, pub, release=resume)


def test_no_nested_query_networkx_graphviz_and_subgraph_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    g = ByogGraph(graph)
    called: list[str] = []

    def track(name):
        def _inner(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"nested public query {name}")

        return _inner

    monkeypatch.setattr(g, "subgraph", track("subgraph"))
    monkeypatch.setattr(g, "neighbors", track("neighbors"))
    monkeypatch.setattr(g, "type_closure", track("type_closure"))
    monkeypatch.setattr(g, "impact", track("impact"))
    result = g.components()
    assert result["n_nodes_total"] == 2
    assert called == []

    src = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    helper = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute_weakly_connected_components":
            helper = ast.get_source_segment(src, node)
    assert helper is not None
    assert "networkx" not in helper
    assert "graphviz" not in helper
    assert "subprocess" not in helper
    assert "tempfile" not in helper
    assert "socket" not in helper
    assert "urllib" not in helper

    other = ByogGraph(graph)
    sub = other.subgraph("A", direction="outgoing", max_depth=1)
    assert dumps_subgraph_json(sub)
    assert format_subgraph_human(sub)
    assert dumps_subgraph_dot(sub).startswith("digraph graphrag_subgraph")
    assert compute_bounded_subgraph(
        other.ents, other.rels, "A", direction="outgoing", max_depth=1
    ) == sub


def test_mcp_exposes_components_as_thirteenth_tool(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.components).parameters)
    assert params == [
        "self",
        "max_components",
        "max_nodes_per_component",
        "edge_types",
        "snapshot",
    ]
    assert "graph" not in params
    assert "format" not in params
    assert "dot" not in params
    assert "symbol" not in params

    expected = ByogGraph(graph).components()
    payload = session.components()
    assert payload["tool"] == "components"
    assert payload["ok"] is True
    assert payload["data"] == json.loads(json.dumps(expected, allow_nan=False, default=str))
    sub = session.subgraph("A", direction="outgoing", max_depth=1)
    assert sub["tool"] == "subgraph"
    assert sub["data"]["root"] == "A"

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == len(set(names)) == 17
            assert names[names.index("neighbors") + 1] == "subgraph"
            assert names[names.index("subgraph") + 1] == "components"
            assert names[names.index("components") + 1] == "strong_components"
            assert names[names.index("strong_components") + 1] == "condensation"
            assert names[names.index("condensation") + 1] == "shortest_path"
            assert names[names.index("shortest_path") + 1] == "degree_ranking"
            assert names[names.index("degree_ranking") + 1] == "impact"
            tool = next(item for item in tools if item.name == "components")
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
            assert tool.input_schema["additionalProperties"] is False
            props = tool.input_schema.get("properties") or {}
            assert list(props) == [
                "max_components",
                "max_nodes_per_component",
                "edge_types",
                "snapshot",
            ]
            assert "dot" not in props
            assert "format" not in props
            assert "graph" not in props
            body = await client.call_tool("components", {})
            data = body.structured_content
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
            assert data["tool"] == "components"
            assert data["data"] == payload["data"]

    anyio_run(_body)
