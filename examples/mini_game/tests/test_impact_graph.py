"""Bounded reverse-call impact graph (CLI/Python only).

Canonical producer: compute_bounded_call_impact. Legacy unbounded
impact List[str] and MCP impact remain unchanged. No DOT, no MCP tool.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
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
    DEFAULT_IMPACT_GRAPH_MAX_DEPTH,
    DEFAULT_IMPACT_GRAPH_MAX_EDGES,
    DEFAULT_IMPACT_GRAPH_MAX_NODES,
    HARD_MAX_IMPACT_GRAPH_DEPTH,
    HARD_MAX_IMPACT_GRAPH_EDGES,
    HARD_MAX_IMPACT_GRAPH_NODES,
    PUBLICATION_LOCK_NAME,
    SUBGRAPH_EDGE_FIELDS,
    SUBGRAPH_NODE_FIELDS,
    ByogGraph,
    compute_bounded_call_impact,
    compute_transitive_call_impact,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_impact_graph_json,
    format_impact_graph_human,
    impact as free_impact,
    impact_graph as free_impact_graph,
)
from graphrag_code.mcp_server import (  # type: ignore
    DEFAULT_MAX_ITEMS,
    TOOL_NAMES,
    GraphMcpSession,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
SCHEMA_KEYS = (
    "root",
    "resolved",
    "max_depth",
    "max_nodes",
    "max_edges",
    "nodes",
    "edges",
    "n_nodes_total",
    "n_edges_total",
    "n_nodes_returned",
    "n_edges_returned",
    "nodes_truncated",
    "edges_truncated",
)


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


def _fixture_entities() -> list[dict]:
    return [
        _entity("A"),
        _entity("B"),
        _entity("C"),
        _entity("Isolated"),
        _entity("pkg:pkg", "module"),
        _entity("D"),
    ]


def _fixture_rels() -> list[dict]:
    return [
        _calls("B", "A", hid=1, rid="rel:ba"),
        _calls("C", "B", hid=2, rid="rel:cb"),
        _calls("C", "A", hid=3, rid="rel:ca"),
        _calls("A", "A", hid=4, rid="rel:self"),
        _calls("B", "A", hid=5, rid="rel:ba2"),
        _calls("ghost", "A", hid=6, rid="rel:ghost"),
        _rel("A", "T", "uses_type", hid=7, rid="rel:uses"),
        _rel("pkg:pkg", "A", "contains", hid=8, rid="rel:contains"),
        _rel("A", "B", "depends_on", hid=9, rid="rel:dep"),
        _rel("Isolated", "A", "CALLS", hid=10, rid="rel:CALLS"),
        _calls("X", "Y", hid=11, rid="rel:xy"),
        _calls("Y", "X", hid=12, rid="rel:yx"),
        _calls("B", "C", hid=13, rid="rel:bc"),
        _calls("D", "D", hid=14, rid="rel:dself"),
        _calls("A", "B", hid=15, rid="rel:ab"),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_impact_graph"
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


def _run(
    *args: str, check: bool = True, cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        list(args),
        capture_output=True,
        env=_child_env(),
        cwd=cwd,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr.decode() + proc.stdout.decode())
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


def _node_map(result: dict) -> dict[str, int]:
    return {n["title"]: n["depth"] for n in result["nodes"]}


def _assert_schema(result: dict) -> None:
    assert tuple(result) == SCHEMA_KEYS
    for node in result["nodes"]:
        assert tuple(node) == SUBGRAPH_NODE_FIELDS
    for edge in result["edges"]:
        assert tuple(edge) == SUBGRAPH_EDGE_FIELDS
        assert edge["type"] == "calls"


def test_public_surfaces_root_depths_and_legacy_impact_untouched(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    produced = compute_bounded_call_impact(
        g.ents, g.rels, g.resolve("A"), max_depth=3
    )
    via_graph = g.impact_graph("A", max_depth=3)
    via_free = free_impact_graph(g.ents, g.rels, "A", max_depth=3)
    assert produced == via_graph == via_free
    _assert_schema(produced)
    assert produced["root"] == "A"
    assert produced["resolved"] is True
    assert produced["nodes"][0]["title"] == "A"
    assert produced["nodes"][0]["depth"] == 0
    assert _node_map(produced) == {"A": 0, "B": 1, "C": 1, "ghost": 1}
    assert [n["title"] for n in produced["nodes"]] == ["A", "B", "C", "ghost"]
    ghost = next(n for n in produced["nodes"] if n["title"] == "ghost")
    assert ghost["id"] is None and ghost["type"] is None
    assert "Isolated" not in _node_map(produced)
    assert "X" not in _node_map(produced)
    assert "Y" not in _node_map(produced)
    assert "D" not in _node_map(produced)
    assert "pkg:pkg" not in _node_map(produced)
    edge_ids = [e["id"] for e in produced["edges"]]
    assert edge_ids == [
        "rel:self",
        "rel:ab",
        "rel:ba",
        "rel:ba2",
        "rel:ca",
        "rel:ghost",
        "rel:bc",
        "rel:cb",
    ]
    assert "rel:uses" not in edge_ids
    assert "rel:contains" not in edge_ids
    assert "rel:dep" not in edge_ids
    assert "rel:CALLS" not in edge_ids
    assert "rel:xy" not in edge_ids
    assert "rel:dself" not in edge_ids
    ba = [e for e in produced["edges"] if e["id"] in {"rel:ba", "rel:ba2"}]
    assert len(ba) == 2
    assert all(e["source"] == "B" and e["target"] == "A" for e in ba)
    ab = next(e for e in produced["edges"] if e["id"] == "rel:ab")
    assert ab["source"] == "A" and ab["target"] == "B"
    assert ab["depth"] == 0
    bc = next(e for e in produced["edges"] if e["id"] == "rel:bc")
    assert bc["source"] == "B" and bc["target"] == "C"
    assert bc["depth"] == 1
    legacy = g.impact("A")
    assert legacy == free_impact(g.ents, g.rels, "A")
    assert legacy == compute_transitive_call_impact(g.rels, g.resolve("A"))
    assert legacy == ["B", "C", "ghost"]
    assert "A" not in legacy


def test_max_depth_zero_self_loop_cycles_and_endpoint_only(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    d0 = g.impact_graph("A", max_depth=0)
    assert _node_map(d0) == {"A": 0}
    assert [e["id"] for e in d0["edges"]] == ["rel:self"]
    assert d0["n_nodes_total"] == 1
    assert d0["n_edges_total"] == 1
    cycle = compute_bounded_call_impact(
        pd.DataFrame([_entity("P"), _entity("Q")]),
        pd.DataFrame(
            [_calls("P", "Q", rid="pq"), _calls("Q", "P", rid="qp")]
        ),
        "P",
        max_depth=3,
    )
    assert _node_map(cycle) == {"P": 0, "Q": 1}
    assert [e["id"] for e in cycle["edges"]] == ["pq", "qp"]
    off = compute_bounded_call_impact(
        pd.DataFrame([_entity("A")]),
        pd.DataFrame(
            [
                _calls("X", "Y", rid="xy"),
                _calls("Y", "X", rid="yx"),
            ]
        ),
        "A",
        max_depth=3,
    )
    assert _node_map(off) == {"A": 0}
    assert off["edges"] == []
    only = compute_bounded_call_impact(
        pd.DataFrame([_entity("A")]),
        pd.DataFrame([_calls("ghost", "A", rid="g")]),
        "A",
        max_depth=1,
    )
    ghost = next(n for n in only["nodes"] if n["title"] == "ghost")
    assert ghost["depth"] == 1
    for field in SUBGRAPH_NODE_FIELDS:
        if field in {"title", "depth"}:
            continue
        assert ghost[field] is None


def test_caps_referential_closure_and_totals(tmp_path: Path):
    entities = [_entity("A")] + [_entity(f"N{i}") for i in range(5)]
    rels = [_calls(f"N{i}", "A" if i == 0 else f"N{i - 1}", rid=f"rel:{i}") for i in range(5)]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).impact_graph(
        "A", max_depth=3, max_nodes=2, max_edges=1
    )
    assert [n["title"] for n in r["nodes"]] == ["A", "N0"]
    assert r["n_nodes_total"] == 4
    assert r["n_edges_total"] == 3
    assert r["n_nodes_returned"] == 2
    assert r["n_edges_returned"] == 1
    assert r["nodes_truncated"] is True
    assert r["edges_truncated"] is True
    returned = {n["title"] for n in r["nodes"]}
    assert all(
        e["source"] in returned and e["target"] in returned for e in r["edges"]
    )
    assert r["edges"][0]["id"] == "rel:0"

    node_only = ByogGraph(graph).impact_graph(
        "A", max_depth=3, max_nodes=1, max_edges=500
    )
    assert [n["title"] for n in node_only["nodes"]] == ["A"]
    assert node_only["edges"] == []
    assert node_only["n_nodes_total"] == 4
    assert node_only["n_edges_total"] == 3
    assert node_only["edges_truncated"] is True

    edge_only = ByogGraph(graph).impact_graph(
        "A", max_depth=3, max_nodes=50, max_edges=1
    )
    assert edge_only["n_nodes_returned"] == 4
    assert edge_only["n_edges_returned"] == 1
    assert edge_only["nodes_truncated"] is False
    assert edge_only["edges_truncated"] is True

    ents_df = pd.DataFrame(entities)
    rels_df = pd.DataFrame(rels)
    with pytest.raises(ValueError, match="max_depth"):
        compute_bounded_call_impact(ents_df, rels_df, "A", max_depth=-1)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_bounded_call_impact(ents_df, rels_df, "A", max_nodes=0)
    with pytest.raises(ValueError, match="max_edges"):
        compute_bounded_call_impact(ents_df, rels_df, "A", max_edges=-1)
    with pytest.raises(ValueError, match="max_depth"):
        compute_bounded_call_impact(
            ents_df, rels_df, "A", max_depth=HARD_MAX_IMPACT_GRAPH_DEPTH + 1
        )
    with pytest.raises(ValueError, match="max_nodes"):
        compute_bounded_call_impact(
            ents_df, rels_df, "A", max_nodes=HARD_MAX_IMPACT_GRAPH_NODES + 1
        )
    with pytest.raises(ValueError, match="max_edges"):
        compute_bounded_call_impact(
            ents_df, rels_df, "A", max_edges=HARD_MAX_IMPACT_GRAPH_EDGES + 1
        )
    with pytest.raises(ValueError, match="max_depth"):
        compute_bounded_call_impact(ents_df, rels_df, "A", max_depth=True)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_bounded_call_impact(ents_df, rels_df, "A", max_nodes=True)
    with pytest.raises(ValueError, match="max_edges"):
        compute_bounded_call_impact(ents_df, rels_df, "A", max_edges=True)


def test_utf8_ordering_shuffled_rows_and_hash_seed():
    rels = pd.DataFrame(
        [
            _calls("é", "Z", rid="e1"),
            _calls("A", "Z", rid="e2"),
            _calls("Ā", "Z", rid="e3"),
        ]
    )
    ents = pd.DataFrame([_entity("Z"), _entity("A"), _entity("é"), _entity("Ā")])
    produced = compute_bounded_call_impact(ents, rels, "Z", max_depth=1)
    assert [n["title"] for n in produced["nodes"]] == ["Z", "A", "é", "Ā"]
    assert [e["id"] for e in produced["edges"]] == ["e2", "e1", "e3"]
    shuffled = compute_bounded_call_impact(
        ents.sample(frac=1, random_state=7).reset_index(drop=True),
        rels.sample(frac=1, random_state=11).reset_index(drop=True),
        "Z",
        max_depth=1,
    )
    assert shuffled == produced
    empty = compute_bounded_call_impact(None, None, None)
    assert empty["resolved"] is False
    assert empty["root"] is None
    assert empty["nodes"] == [] and empty["edges"] == []
    assert empty["n_nodes_total"] == 0 and empty["n_edges_total"] == 0
    assert empty["nodes_truncated"] is False and empty["edges_truncated"] is False
    assert empty["max_depth"] == DEFAULT_IMPACT_GRAPH_MAX_DEPTH
    lone = compute_bounded_call_impact(None, pd.DataFrame(), "Z")
    assert lone["resolved"] is True
    assert _node_map(lone) == {"Z": 0}
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_bounded_call_impact
rels = pd.DataFrame([
    {"id": "e1", "source": "é", "target": "Z", "type": "calls"},
    {"id": "e2", "source": "A", "target": "Z", "type": "calls"},
    {"id": "e3", "source": "Ā", "target": "Z", "type": "calls"},
])
ents = pd.DataFrame([
    {"id": "z", "title": "Z", "type": "function"},
    {"id": "a", "title": "A", "type": "function"},
    {"id": "e", "title": "é", "type": "function"},
    {"id": "m", "title": "Ā", "type": "function"},
])
print(repr([n["title"] for n in compute_bounded_call_impact(ents, rels, "Z", max_depth=1)["nodes"]]))
"""
    payloads = []
    for seed in ("0", "1", "random"):
        env = _child_env()
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        payloads.append(proc.stdout)
    assert payloads[0] == payloads[1] == payloads[2] == "['Z', 'A', 'é', 'Ā']\n"


def test_unresolved_and_strict_malformed_table_fails_closed(tmp_path: Path):
    entities = _fixture_entities() + [_entity("other:f"), _entity("m:f")]
    graph = _publish(tmp_path, entities, _fixture_rels())
    g = ByogGraph(graph)
    missing = g.impact_graph("does-not-exist")
    assert missing["resolved"] is False
    assert missing["root"] is None
    assert missing["nodes"] == [] and missing["edges"] == []
    assert missing["n_nodes_total"] == 0
    assert missing["max_nodes"] == DEFAULT_IMPACT_GRAPH_MAX_NODES
    amb = g.impact_graph("f")
    assert amb["resolved"] is False
    alias = g.impact_graph("pkg", max_depth=0)
    assert alias["root"] == "pkg:pkg"
    assert alias["resolved"] is True
    partial = g.impact_graph("Isolat", max_depth=0)
    assert partial["root"] == "Isolated"
    good = [_calls("B", "A", rid="ok")]
    malformed = _calls("B", "A", rid="bad")
    malformed["target"] = None
    with pytest.raises(ValueError, match="invalid target"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]), pd.DataFrame([malformed]), "A"
        )
    missing_cols = pd.DataFrame([{"id": "r1", "source": "B"}])
    with pytest.raises(ValueError, match="missing required columns"):
        compute_bounded_call_impact(pd.DataFrame([_entity("A")]), missing_cols, "A")
    for field, value in (
        ("id", None),
        ("id", ""),
        ("id", 1),
        ("source", None),
        ("source", ""),
        ("source", True),
        ("target", None),
        ("target", ""),
        ("type", None),
        ("type", ""),
        ("type", 1),
    ):
        row = _calls("B", "A", rid="row")
        row[field] = value
        with pytest.raises(ValueError):
            compute_bounded_call_impact(
                pd.DataFrame([_entity("A")]), pd.DataFrame([row]), "A"
            )
    dup = [_calls("B", "A", rid="dup"), _rel("A", "T", "uses_type", rid="dup")]
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]), pd.DataFrame(dup), "A"
        )
    non_call = _rel("A", "T", "uses_type", rid="nc")
    non_call["source"] = None
    with pytest.raises(ValueError, match="invalid source"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]),
            pd.DataFrame(good + [non_call]),
            "A",
        )
    with pytest.raises(ValueError, match="invalid source"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]),
            pd.DataFrame(good + [non_call]),
            None,
        )
    lone = _calls("B", "A", rid="surr")
    lone["target"] = "\ud800"
    with pytest.raises(ValueError, match="target"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]), pd.DataFrame([lone]), "A"
        )
    with pytest.raises(ValueError, match="root_title"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]), pd.DataFrame(good), ""
        )
    with pytest.raises(ValueError, match="root_title"):
        compute_bounded_call_impact(
            pd.DataFrame([_entity("A")]), pd.DataFrame(good), True  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="relationships must be a dataframe"):
        compute_bounded_call_impact(pd.DataFrame([_entity("A")]), [], "A")  # type: ignore[arg-type]


def test_one_load_resolve_producer_no_nested_query_and_no_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="ba")],
    )
    byog_src = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(
        encoding="utf-8"
    )
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    byog_tree = ast.parse(byog_src)
    gq_tree = ast.parse(gq_src)
    method_fn = None
    producer_fn = None
    for node in byog_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ByogGraph":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "impact_graph":
                    method_fn = ast.get_source_segment(byog_src, item)
        if isinstance(node, ast.FunctionDef) and node.name == "compute_bounded_call_impact":
            producer_fn = ast.get_source_segment(byog_src, node)
    free_fn = None
    cli_fn = None
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "impact_graph":
            free_fn = ast.get_source_segment(gq_src, node)
        if isinstance(node, ast.FunctionDef) and node.name == "cli_impact_graph":
            cli_fn = ast.get_source_segment(gq_src, node)
    assert method_fn and producer_fn and free_fn and cli_fn
    assert method_fn.count("compute_bounded_call_impact(") == 1
    assert method_fn.count("self.resolve(") == 1
    assert "compute_bounded_subgraph(" not in method_fn
    assert "compute_transitive_call_impact(" not in method_fn
    assert free_fn.count("compute_bounded_call_impact(") == 1
    assert "_resolve_symbol(" in free_fn
    assert "compute_bounded_subgraph(" not in free_fn
    assert "networkx" not in producer_fn
    assert "subprocess" not in producer_fn
    assert "tempfile" not in producer_fn
    assert "while " in producer_fn
    assert "def " not in producer_fn.split("while ", 1)[1]
    assert "_strict_selected_relationship_rows(" in producer_fn
    assert cli_fn.count(".impact_graph(") == 1
    assert cli_fn.count("dumps_impact_graph_json(") == 1
    assert cli_fn.count("format_impact_graph_human(") == 1
    assert "flush=True" in cli_fn
    assert "with _scoped_graph" in cli_fn
    assert "--dot" not in cli_fn.split("typer.Option", 1)[0] or "no ``--dot``" in cli_fn
    assert inspect.signature(compute_bounded_call_impact).parameters["max_depth"].default == (
        DEFAULT_IMPACT_GRAPH_MAX_DEPTH
    )

    g = ByogGraph(graph)
    for name in (
        "subgraph",
        "impact",
        "callers",
        "callees",
        "type_closure",
        "components",
        "strong_components",
        "condensation",
        "dependency_order",
        "degree_ranking",
        "shortest_path",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    resolves: list[str] = []
    orig_resolve = g.resolve

    def counted_resolve(query):
        resolves.append(query)
        return orig_resolve(query)

    monkeypatch.setattr(g, "resolve", counted_resolve)
    producer_calls = 0
    orig = compute_bounded_call_impact

    def counted_producer(*args, **kwargs):
        nonlocal producer_calls
        producer_calls += 1
        return orig(*args, **kwargs)

    import graphrag_code.byog_graph as byog
    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner
    import graphrag_code.snapshot_read as snapshot_read

    monkeypatch.setattr(byog, "compute_bounded_call_impact", counted_producer)
    result = g.impact_graph("A")
    assert result["root"] == "A"
    assert resolves == ["A"]
    assert producer_calls == 1
    dumps_impact_graph_json(result)
    format_impact_graph_human(result)
    assert producer_calls == 1

    loads = 0
    orig_load = snapshot_read.RetainedSnapshotScope.load_graph

    def counted_load(self):
        nonlocal loads
        loads += 1
        return orig_load(self)

    monkeypatch.setattr(snapshot_read.RetainedSnapshotScope, "load_graph", counted_load)
    serializer_calls = 0
    orig_dumps = dumps_impact_graph_json

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    monkeypatch.setattr(gq, "dumps_impact_graph_json", counted_dumps)
    producer_calls = 0
    invoked = CliRunner().invoke(
        gq.app, ["impact-graph", "A", "--graph", str(graph), "--json"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert loads == 1
    assert serializer_calls == 1
    assert producer_calls == 1
    assert invoked.stdout.endswith("\n")
    assert not list(tmp_path.glob("*.dot"))


def test_cli_human_json_byte_parity_and_wheel(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="ba")],
    )
    args = [
        "impact-graph",
        "A",
        "--graph",
        str(graph),
        "--max-depth",
        "2",
        "--max-nodes",
        "10",
        "--max-edges",
        "10",
    ]
    script = _run(sys.executable, str(QUERY), *args)
    module = _run(sys.executable, "-m", "graphrag_code.graph_query", *args)
    product = _run(sys.executable, str(CLI), *args)
    package = _run(sys.executable, "-m", "graphrag_code", *args)
    json_args = [*args, "--json"]
    script_j = _run(sys.executable, str(QUERY), *json_args)
    module_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *json_args)
    product_j = _run(sys.executable, str(CLI), *json_args)
    package_j = _run(sys.executable, "-m", "graphrag_code", *json_args)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed_h = subprocess.run(
        ["graphrag-code", *args], cwd=outside, capture_output=True, env=env
    )
    installed_j = subprocess.run(
        ["graphrag-code", *json_args], cwd=outside, capture_output=True, env=env
    )
    assert installed_h.returncode == 0, installed_h.stderr
    assert installed_j.returncode == 0, installed_j.stderr
    expected = ByogGraph(graph).impact_graph("A", max_depth=2, max_nodes=10, max_edges=10)
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed_h.stdout
        == (format_impact_graph_human(expected) + "\n").encode()
    )
    assert (
        script_j.stdout
        == module_j.stdout
        == product_j.stdout
        == package_j.stdout
        == installed_j.stdout
        == (dumps_impact_graph_json(expected) + "\n").encode()
    )
    payload = json.loads(script_j.stdout)
    assert payload["root"] == "A"
    assert dumps_impact_graph_json(payload) + "\n" == script_j.stdout.decode()
    assert b"--dot" not in script.stdout
    assert not list(outside.glob("*.dot"))
    assert not (graph / ".publish.lock").is_symlink()
    help_proc = _run(sys.executable, str(QUERY), "impact-graph", "--help")
    options = help_proc.stdout.split(b"Options")[-1]
    assert b"--dot" not in options
    assert b"--json" in options


def test_invalid_graph_snapshot_and_data_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "impact-graph",
        "A",
        "--graph",
        str(tmp_path / "missing"),
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == b""
    graph = _publish(tmp_path, [_entity("A")], [_calls("B", "A", rid="ba")])
    bad_snap = _run(
        sys.executable,
        str(QUERY),
        "impact-graph",
        "A",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == b""
    bad_nodes = _run(
        sys.executable,
        str(CLI),
        "impact-graph",
        "A",
        "--graph",
        str(graph),
        "--max-nodes",
        "0",
        check=False,
    )
    assert bad_nodes.returncode == 2
    assert bad_nodes.stdout == b""
    assert b"max_nodes" in bad_nodes.stderr
    dotted = _run(
        sys.executable,
        str(QUERY),
        "impact-graph",
        "A",
        "--graph",
        str(graph),
        "--dot",
        check=False,
    )
    assert dotted.returncode == 2
    assert dotted.stdout == b""
    unresolved = _run(
        sys.executable,
        str(QUERY),
        "impact-graph",
        "missing",
        "--graph",
        str(graph),
        "--json",
    )
    body = json.loads(unresolved.stdout)
    assert body["resolved"] is False
    assert unresolved.returncode == 0


def test_current_and_historical_snapshot_reads_do_not_mutate(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old")]),
        pd.DataFrame([_calls("demo:caller", "demo:old", rid="rel:old")]),
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
        pd.DataFrame([_entity("demo:fresh")]),
        pd.DataFrame([_calls("demo:fresh_caller", "demo:fresh", rid="rel:new")]),
        pd.DataFrame(
            [
                {
                    "id": "tu:new",
                    "text": "new",
                    "n_tokens": 1,
                    "document_ids": [],
                    "entity_ids": ["ent:function:demo:fresh"],
                    "relationship_ids": [],
                }
            ]
        ),
        graph,
        keep_last=5,
    )
    before = _payload_hashes(graph)
    stats = _payload_stats(graph)
    current = (graph / "current").read_text(encoding="utf-8").strip()
    assert current == newer.name
    cur = _run(
        sys.executable,
        str(QUERY),
        "impact-graph",
        "demo:fresh",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--json",
    )
    assert json.loads(cur.stdout)["root"] == "demo:fresh"
    hist = _run(
        sys.executable,
        str(CLI),
        "impact-graph",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body["root"] == "demo:old"
    assert b"demo:fresh" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
    assert not (graph / ".publish.lock").is_symlink()


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


def _impact_graph_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_impact_graph_json

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_impact_graph_json = wrap_dumps

    class HoldingStdout:
        encoding = "utf-8"
        errors = "strict"
        closed = False

        def __init__(self, inner):
            self._inner = inner
            self._wrote = False

        def write(self, data):
            written = self._inner.write(data)
            if isinstance(data, str) and data.startswith("{"):
                self._wrote = True
            return written

        def flush(self):
            if self._wrote:
                held.set()
                if not resume.wait(timeout=TIMEOUT):
                    q.put("timeout")
            return self._inner.flush()

        def isatty(self):
            return False

        def __getattr__(self, name):
            return getattr(self._inner, name)

    sys_mod.stdout = HoldingStdout(sys_mod.stdout)
    try:
        graph_query.app(
            ["impact-graph", "A", "--graph", graph, "--json"],
            standalone_mode=False,
        )
        q.put("exit:0")
    except SystemExit as exc:
        q.put(f"exit:{exc.code}")
    except Exception as exc:
        code = getattr(exc, "exit_code", None)
        if code is None:
            q.put(f"error:{type(exc).__name__}:{exc}")
        else:
            q.put(f"exit:{code}")


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


def test_publisher_waits_through_producer_stdout_write_and_flush(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="ba")],
    )
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_impact_graph_json_hold, args=(str(graph), held, resume, q)
    )
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


def test_mcp_remains_seventeen_legacy_impact_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session
    import graphrag_code.byog_graph as byog

    entities = [_entity("A")] + [_entity(f"C{i}") for i in range(3)]
    rels = [_calls(f"C{i}", "A", rid=f"r{i}") for i in range(3)]
    graph = _publish(tmp_path, entities, rels)
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.impact).parameters)
    assert params == ["self", "symbol", "max_items", "snapshot"]
    assert not hasattr(session, "impact_graph")
    expected = ByogGraph(graph).impact("A")
    assert expected == ["C0", "C1", "C2"]
    payload = session.impact("A")
    assert payload["tool"] == "impact"
    assert payload["data"] == expected
    assert payload["truncated"] is False
    truncated = session.impact("A", max_items=1)
    assert truncated["data"] == ["C0"]
    assert truncated["truncated"] is True
    assert truncated["limits"]["max_items"] == 1
    assert payload["limits"]["max_items"] == DEFAULT_MAX_ITEMS
    producer_calls = 0
    orig = byog.compute_transitive_call_impact

    def counted(rels, root_title):
        nonlocal producer_calls
        producer_calls += 1
        return orig(rels, root_title)

    monkeypatch.setattr(byog, "compute_transitive_call_impact", counted)
    graph_calls = 0
    orig_graph = byog.compute_bounded_call_impact

    def counted_graph(*args, **kwargs):
        nonlocal graph_calls
        graph_calls += 1
        return orig_graph(*args, **kwargs)

    monkeypatch.setattr(byog, "compute_bounded_call_impact", counted_graph)
    session.impact("A")
    assert producer_calls == 1
    assert graph_calls == 0
    assert list(TOOL_NAMES) == [
        "graph_status",
        "graph_doctor",
        "query_symbol",
        "callers",
        "callees",
        "neighbors",
        "subgraph",
        "components",
        "strong_components",
        "condensation",
        "shortest_path",
        "degree_ranking",
        "impact",
        "type_closure",
        "context_pack",
        "snapshot_history",
        "snapshot_diff",
    ]
    assert len(TOOL_NAMES) == 17
    assert "impact_graph" not in TOOL_NAMES
    assert not (graph / ".publish.lock").is_symlink()

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert "impact_graph" not in names
            assert "impact-graph" not in names
            tool = next(item for item in tools if item.name == "impact")
            props = tool.input_schema.get("properties") or {}
            assert list(props) == ["symbol", "max_items", "snapshot"]
            result = await client.call_tool("impact", {"symbol": "A"})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "impact"
            assert body["data"] == expected

    anyio_run(_body)
