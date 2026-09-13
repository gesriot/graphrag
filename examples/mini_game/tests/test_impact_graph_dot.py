"""Deterministic Graphviz DOT for the bounded impact-graph CLI.

Renders the existing ByogGraph.impact_graph result. Does not add a
Graphviz runtime, a second traversal, an MCP format parameter, or an
output file. Legacy unbounded impact remains unchanged.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import multiprocessing
import os
import re
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
    HARD_MAX_IMPACT_GRAPH_DEPTH,
    HARD_MAX_IMPACT_GRAPH_EDGES,
    HARD_MAX_IMPACT_GRAPH_NODES,
    SUBGRAPH_EDGE_FIELDS,
    SUBGRAPH_NODE_FIELDS,
    ByogGraph,
    compute_bounded_call_impact,
    compute_transitive_call_impact,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_impact_graph_dot,
    dumps_impact_graph_json,
    format_impact_graph_human,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.impact_graph_dot import (  # type: ignore
    HARD_MAX_IMPACT_GRAPH_DOT_BYTES,
    IMPACT_GRAPH_DOT_SCHEMA_VERSION,
    ImpactGraphDotError,
    dumps_impact_graph_dot as direct_dumps,
)
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

GOLDEN_RESOLVED = """\
digraph graphrag_impact_graph {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    relationship_type="calls",
    max_depth="1",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="2",
    n_edges_total="1",
    n_nodes_returned="2",
    n_edges_returned="1",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", depth="0", type="function", is_root="true", peripheries="2"];
  n0001 [label="B", title="B", depth="1", type="function", is_root="false"];
  n0001 -> n0000 [label="calls", id="rel:ba", type="calls", depth="0"];
}
"""

GOLDEN_UNRESOLVED = """\
digraph graphrag_impact_graph {
  graph [
    schema_version="1",
    resolved="false",
    relationship_type="calls",
    max_depth="3",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="0",
    n_edges_total="0",
    n_nodes_returned="0",
    n_edges_returned="0",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""


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
    graph = tmp_path / "byog_impact_graph_dot"
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


def _structural(dot: str) -> str:
    return STRING_RE.sub('""', dot)


def _node_statements(dot: str) -> list[str]:
    return [
        line.strip()
        for line in dot.splitlines()
        if re.match(r"^n\d{4} \[", line.strip())
    ]


def _edge_statements(dot: str) -> list[str]:
    return [
        line.strip()
        for line in dot.splitlines()
        if re.match(r"^n\d{4} -> n\d{4} \[", line.strip())
    ]


def _node_rec(title: str, depth: int, **extra) -> dict:
    rec = {field: None for field in SUBGRAPH_NODE_FIELDS}
    rec["title"] = title
    rec["depth"] = depth
    rec.update(extra)
    return rec


def _edge_rec(
    rid: str, source: str, target: str, depth: int, **extra
) -> dict:
    rec = {field: None for field in SUBGRAPH_EDGE_FIELDS}
    rec["id"] = rid
    rec["source"] = source
    rec["target"] = target
    rec["type"] = extra.pop("type", "calls")
    rec["depth"] = depth
    rec.update(extra)
    return rec


def _base_result(**overrides: object) -> dict:
    result: dict = {
        "root": "A",
        "resolved": True,
        "max_depth": 1,
        "max_nodes": 50,
        "max_edges": 100,
        "nodes": [
            _node_rec(
                "A",
                0,
                type="function",
                id="ent:function:A",
                description="secret-description",
                source_file="a.py",
                span="1:0-2:0",
                confidence=1.0,
            ),
            _node_rec(
                "B",
                1,
                type="function",
                id="ent:function:B",
                description="other-secret",
            ),
        ],
        "edges": [
            _edge_rec(
                "rel:ba",
                "B",
                "A",
                0,
                description="secret-edge",
                weight=9.5,
                span="3:0-4:0",
                confidence=0.5,
            )
        ],
        "n_nodes_total": 2,
        "n_edges_total": 1,
        "n_nodes_returned": 2,
        "n_edges_returned": 1,
        "nodes_truncated": False,
        "edges_truncated": False,
    }
    result.update(overrides)
    return result


def _unresolved_result(**overrides: object) -> dict:
    result = {
        "root": None,
        "resolved": False,
        "max_depth": 3,
        "max_nodes": 50,
        "max_edges": 100,
        "nodes": [],
        "edges": [],
        "n_nodes_total": 0,
        "n_edges_total": 0,
        "n_nodes_returned": 0,
        "n_edges_returned": 0,
        "nodes_truncated": False,
        "edges_truncated": False,
    }
    result.update(overrides)
    return result


def test_golden_root_n0000_order_orientation_and_no_secrets():
    payload = dumps_impact_graph_dot(_base_result())
    assert payload == GOLDEN_RESOLVED
    assert payload == dumps_impact_graph_dot(_base_result())
    assert payload == direct_dumps(_base_result())
    assert payload.endswith("\n") and not payload.endswith("\n\n")
    assert payload.encode("utf-8").decode("utf-8") == payload
    assert "schema_version=" + quote_dot_string(
        str(IMPACT_GRAPH_DOT_SCHEMA_VERSION)
    ) in payload
    assert payload.splitlines()[0] == "digraph graphrag_impact_graph {"
    nodes = _node_statements(payload)
    edges = _edge_statements(payload)
    assert nodes[0].startswith("n0000 [")
    assert 'title="A"' in nodes[0]
    assert 'is_root="true"' in nodes[0]
    assert 'peripheries="2"' in nodes[0]
    assert nodes[1].startswith("n0001 [")
    assert edges == [
        'n0001 -> n0000 [label="calls", id="rel:ba", type="calls", depth="0"];'
    ]
    assert "n0000 -> n0001" not in payload
    for leak in (
        "secret-description",
        "other-secret",
        "secret-edge",
        "ent:function:A",
        "weight",
        "confidence",
        "source_file",
        "span",
    ):
        assert leak not in payload
    assert dumps_impact_graph_dot(_unresolved_result()) == GOLDEN_UNRESOLVED


def test_live_graph_self_loop_parallel_caps_and_legacy_impact(tmp_path: Path):
    entities = [
        _entity("A"),
        _entity("B"),
        _entity("C"),
        _entity("Isolated"),
    ]
    rels = [
        _calls("B", "A", rid="rel:ba"),
        _calls("C", "B", rid="rel:cb"),
        _calls("A", "A", rid="rel:self"),
        _calls("B", "A", rid="rel:ba2"),
        _calls("ghost", "A", rid="rel:ghost"),
        _rel("Isolated", "A", "CALLS", rid="rel:CALLS"),
        _calls("X", "Y", rid="rel:xy"),
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    produced = g.impact_graph("A", max_depth=2)
    payload = dumps_impact_graph_dot(produced)
    assert payload == direct_dumps(produced)
    nodes = _node_statements(payload)
    edges = _edge_statements(payload)
    assert nodes[0].startswith("n0000 [")
    assert 'title="A"' in nodes[0]
    titles = [n["title"] for n in produced["nodes"]]
    assert titles[0] == "A"
    for index, title in enumerate(titles):
        assert nodes[index].startswith(f"n{index:04d} [")
        assert f'title="{title}"' in nodes[index]
    assert [e["id"] for e in produced["edges"]] == [
        edge.split('id="', 1)[1].split('"', 1)[0] for edge in edges
    ]
    assert payload.count("n0000 -> n0000") == 1
    assert sum(1 for line in edges if line.startswith("n0001 -> n0000")) == 2
    assert "CALLS" not in payload
    ghost = next(n for n in produced["nodes"] if n["title"] == "ghost")
    assert ghost["type"] is None
    ghost_line = next(line for line in nodes if 'title="ghost"' in line)
    assert "type=" not in ghost_line
    capped = g.impact_graph("A", max_depth=2, max_nodes=1, max_edges=0)
    capped_dot = dumps_impact_graph_dot(capped)
    assert _node_statements(capped_dot) == [
        'n0000 [label="A", title="A", depth="0", type="function", is_root="true", peripheries="2"];'
    ]
    assert _edge_statements(capped_dot) == []
    assert 'nodes_truncated="true"' in capped_dot
    assert 'edges_truncated="true"' in capped_dot
    assert "n0001" not in capped_dot
    assert "rel:cb" not in capped_dot
    assert "rel:ghost" not in capped_dot
    assert g.impact("A") == compute_transitive_call_impact(g.rels, g.resolve("A"))
    assert g.impact("A") == ["B", "C", "ghost"]


def test_escaping_injection_controls_unicode_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "ok /* comment */ // still"
    result = _base_result(
        root=nasty,
        nodes=[
            _node_rec(nasty, 0, type=comment),
            _node_rec("é☃\n\t\\", 1, type="function"),
        ],
        edges=[
            _edge_rec(
                'rel:"id"\n',
                nasty,
                "é☃\n\t\\",
                0,
                type="calls",
            )
        ],
    )
    payload = dumps_impact_graph_dot(result)
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert "n9999" not in structural
    assert "/*" not in structural
    assert "//" not in structural
    assert nasty not in structural
    assert structural.count(" -> ") == 1
    assert len(_node_statements(payload)) == 2
    assert len(_edge_statements(payload)) == 1
    assert "\\n" in payload and "\t" not in payload
    assert "\\x01" in dumps_impact_graph_dot(
        _base_result(
            nodes=[
                _node_rec("A", 0, type="function"),
                _node_rec("\x01\x7f\x85", 1, type="function"),
            ],
            edges=[_edge_rec("rel:ctrl", "\x01\x7f\x85", "A", 0)],
            n_nodes_total=2,
            n_nodes_returned=2,
        )
    )
    assert quote_dot_string(nasty) in payload
    for line in _node_statements(payload) + _edge_statements(payload):
        assert line.endswith("];")
        assert line.startswith("n")


def test_malformed_envelope_order_type_and_utf8_fail_closed():
    with pytest.raises(ImpactGraphDotError):
        dumps_impact_graph_dot("not-a-mapping")  # type: ignore[arg-type]
    missing = _base_result()
    missing.pop("resolved")
    with pytest.raises(ImpactGraphDotError, match="missing"):
        dumps_impact_graph_dot(missing)
    extra = _base_result(direction="incoming")
    with pytest.raises(ImpactGraphDotError, match="unexpected"):
        dumps_impact_graph_dot(extra)
    with pytest.raises(ImpactGraphDotError, match="integer"):
        dumps_impact_graph_dot(_base_result(max_depth=True))
    with pytest.raises(ImpactGraphDotError, match="boolean"):
        dumps_impact_graph_dot(_base_result(resolved=1))
    with pytest.raises(ImpactGraphDotError, match="n_nodes_returned"):
        dumps_impact_graph_dot(_base_result(n_nodes_returned=3))
    with pytest.raises(ImpactGraphDotError, match="nodes_truncated"):
        dumps_impact_graph_dot(_base_result(nodes_truncated=True))
    with pytest.raises(ImpactGraphDotError, match="max_nodes"):
        dumps_impact_graph_dot(_base_result(max_nodes=1))
    with pytest.raises(ImpactGraphDotError, match="max_depth"):
        dumps_impact_graph_dot(_base_result(max_depth=HARD_MAX_IMPACT_GRAPH_DEPTH + 1))
    with pytest.raises(ImpactGraphDotError, match="max_nodes"):
        dumps_impact_graph_dot(
            _base_result(max_nodes=HARD_MAX_IMPACT_GRAPH_NODES + 1)
        )
    with pytest.raises(ImpactGraphDotError, match="max_edges"):
        dumps_impact_graph_dot(
            _base_result(max_edges=HARD_MAX_IMPACT_GRAPH_EDGES + 1)
        )
    with pytest.raises(ImpactGraphDotError, match="duplicate impact-graph node title"):
        dumps_impact_graph_dot(
            _base_result(
                nodes=[_node_rec("A", 0, type="function"), _node_rec("A", 1)],
            )
        )
    with pytest.raises(ImpactGraphDotError, match="duplicate impact-graph edge id"):
        dumps_impact_graph_dot(
            _base_result(
                edges=[_base_result()["edges"][0]] * 2,
                n_edges_total=2,
                n_edges_returned=2,
            )
        )
    with pytest.raises(ImpactGraphDotError, match="root"):
        dumps_impact_graph_dot(
            _base_result(
                nodes=[_node_rec("B", 0, type="function"), _node_rec("A", 1)],
            )
        )
    with pytest.raises(ImpactGraphDotError, match="depth 0"):
        dumps_impact_graph_dot(
            _base_result(
                nodes=[
                    _node_rec("A", 1, type="function"),
                    _node_rec("B", 1, type="function"),
                ],
                edges=[],
                n_edges_total=0,
                n_edges_returned=0,
            )
        )
    with pytest.raises(ImpactGraphDotError, match="canonical producer node order"):
        dumps_impact_graph_dot(
            _base_result(
                max_depth=2,
                nodes=[
                    _node_rec("A", 0, type="function"),
                    _node_rec("C", 2, type="function"),
                    _node_rec("B", 1, type="function"),
                ],
                n_nodes_total=3,
                n_nodes_returned=3,
                edges=[],
                n_edges_total=0,
                n_edges_returned=0,
            )
        )
    with pytest.raises(ImpactGraphDotError, match="canonical producer edge order"):
        dumps_impact_graph_dot(
            _base_result(
                nodes=[
                    _node_rec("A", 0, type="function"),
                    _node_rec("B", 1, type="function"),
                    _node_rec("C", 1, type="function"),
                ],
                n_nodes_total=3,
                n_nodes_returned=3,
                edges=[
                    _edge_rec("rel:ca", "C", "A", 0),
                    _edge_rec("rel:ba", "B", "A", 0),
                ],
                n_edges_total=2,
                n_edges_returned=2,
            )
        )
    with pytest.raises(ImpactGraphDotError, match="exactly 'calls'"):
        dumps_impact_graph_dot(
            _base_result(edges=[_edge_rec("rel:ba", "B", "A", 0, type="CALLS")])
        )
    with pytest.raises(ImpactGraphDotError, match="endpoint"):
        dumps_impact_graph_dot(
            _base_result(edges=[_edge_rec("rel:ghost", "ghost", "A", 0)])
        )
    with pytest.raises(ImpactGraphDotError, match="min\\(endpoint depths\\)"):
        dumps_impact_graph_dot(
            _base_result(edges=[_edge_rec("rel:ba", "B", "A", 1)])
        )
    surrogate = _base_result(
        nodes=[
            _node_rec("A", 0, type="function"),
            _node_rec("\ud800", 1, type="function"),
        ]
    )
    with pytest.raises(ImpactGraphDotError, match="UTF-8"):
        dumps_impact_graph_dot(surrogate)
    with pytest.raises(ImpactGraphDotError):
        dumps_impact_graph_dot(_unresolved_result(nodes=[_node_rec("A", 0)]))
    node_extra = _base_result()
    node_extra["nodes"][0] = dict(node_extra["nodes"][0])
    node_extra["nodes"][0]["extra"] = "nope"
    with pytest.raises(ImpactGraphDotError, match="unexpected node field"):
        dumps_impact_graph_dot(node_extra)
    node_missing = _base_result()
    node_missing["nodes"][0] = dict(node_missing["nodes"][0])
    node_missing["nodes"][0].pop("description")
    with pytest.raises(ImpactGraphDotError, match="missing node field"):
        dumps_impact_graph_dot(node_missing)
    import graphrag_code.impact_graph_dot as dot_mod

    assert dot_mod._REQUIRED_NODE == SUBGRAPH_NODE_FIELDS
    assert dot_mod._REQUIRED_EDGE == SUBGRAPH_EDGE_FIELDS


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_impact_graph_dot(_base_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.impact_graph_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_IMPACT_GRAPH_DOT_BYTES", size)
    assert dumps_impact_graph_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_IMPACT_GRAPH_DOT_BYTES", size + 1)
    assert dumps_impact_graph_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_IMPACT_GRAPH_DOT_BYTES", size - 1)
    with pytest.raises(ImpactGraphDotError, match="hard limit"):
        dumps_impact_graph_dot(_base_result())
    assert HARD_MAX_IMPACT_GRAPH_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="rel:ba")],
    )
    common = ["impact-graph", "A", "--graph", str(graph), "--max-depth", "1"]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_impact_graph_json(payload) + "\n" == json_out.stdout
    assert format_impact_graph_human(payload) + "\n" == human.stdout
    expected = ByogGraph(graph).impact_graph("A", max_depth=1)
    assert dumps_impact_graph_dot(expected) == _run(
        sys.executable, str(QUERY), *common, "--dot"
    ).stdout
    missing = tmp_path / "no-such-graph"
    both = _run(
        sys.executable,
        str(QUERY),
        "impact-graph",
        "A",
        "--graph",
        str(missing),
        "--json",
        "--dot",
        check=False,
    )
    assert both.returncode == 2
    assert both.stdout == ""
    assert "mutually exclusive" in both.stderr
    product_both = _run(
        sys.executable,
        str(CLI),
        "impact-graph",
        "A",
        "--graph",
        str(missing),
        "--json",
        "--dot",
        check=False,
    )
    assert product_both.returncode == 2
    assert product_both.stdout == ""
    from typer.testing import CliRunner

    from graphrag_code.graph_query import app
    import graphrag_code.impact_graph_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_IMPACT_GRAPH_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["impact-graph", "A", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "impact-graph", "--help"],
        [sys.executable, str(CLI), "impact-graph", "--help"],
        [sys.executable, "-m", "graphrag_code.graph_query", "impact-graph", "--help"],
    ):
        help_out = _run(*args)
        assert "--dot" in help_out.stdout
        assert "--json" in help_out.stdout
        assert "Graphviz" in help_out.stdout
        assert "Mutually exclusive" in help_out.stdout


def test_script_module_product_installed_dot_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="rel:ba")],
    )
    args = ["impact-graph", "A", "--graph", str(graph), "--max-depth", "1", "--dot"]
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
    expected = dumps_impact_graph_dot(ByogGraph(graph).impact_graph("A", max_depth=1))
    assert script.stdout == module.stdout == product.stdout == installed.stdout == expected
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_current_and_historical_snapshot_dot_reads_do_not_mutate(tmp_path: Path):
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
        "--dot",
    )
    assert quote_dot_string("demo:fresh") in cur.stdout
    hist = _run(
        sys.executable,
        str(CLI),
        "impact-graph",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--dot",
    )
    assert quote_dot_string("demo:old") in hist.stdout
    assert "demo:fresh" not in hist.stdout
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


def _impact_graph_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_impact_graph_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_impact_graph_dot = wrap_dumps

    class HoldingStdout:
        encoding = "utf-8"
        errors = "strict"
        closed = False

        def __init__(self, inner):
            self._inner = inner
            self._wrote_dot = False

        def write(self, data):
            written = self._inner.write(data)
            if isinstance(data, str) and data.startswith("digraph "):
                self._wrote_dot = True
            return written

        def flush(self):
            if self._wrote_dot:
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
            ["impact-graph", "A", "--graph", graph, "--dot"],
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


def test_publisher_waits_through_dot_render_and_stdout_flush(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="rel:ba")],
    )
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_impact_graph_dot_hold, args=(str(graph), held, resume, q)
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


def test_one_load_resolve_producer_serialize_no_nested_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="rel:ba")],
    )
    g = ByogGraph(graph)
    calls: list[str] = []
    orig = g.impact_graph

    def wrapped(*args, **kwargs):
        calls.append("impact_graph")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "impact_graph", wrapped)
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
    result = g.impact_graph("A", max_depth=1)
    assert calls == ["impact_graph"]
    dumps_impact_graph_dot(result)
    assert calls == ["impact_graph"]

    producer_calls = 0
    orig_producer = compute_bounded_call_impact

    def counted_producer(*args, **kwargs):
        nonlocal producer_calls
        producer_calls += 1
        return orig_producer(*args, **kwargs)

    import graphrag_code.byog_graph as byog
    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner
    import graphrag_code.snapshot_read as snapshot_read

    monkeypatch.setattr(byog, "compute_bounded_call_impact", counted_producer)
    serializer_calls = 0
    orig_dumps = dumps_impact_graph_dot

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    monkeypatch.setattr(gq, "dumps_impact_graph_dot", counted_dumps)
    loads = 0
    orig_load = snapshot_read.RetainedSnapshotScope.load_graph

    def counted_load(self):
        nonlocal loads
        loads += 1
        return orig_load(self)

    monkeypatch.setattr(snapshot_read.RetainedSnapshotScope, "load_graph", counted_load)
    invoked = CliRunner().invoke(
        gq.app, ["impact-graph", "A", "--graph", str(graph), "--max-depth", "1", "--dot"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert serializer_calls == 1
    assert loads == 1
    assert producer_calls == 1
    assert invoked.stdout == GOLDEN_RESOLVED

    dot_src = (ROOT / "src" / "graphrag_code" / "impact_graph_dot.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(dot_src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "typing", "subgraph_dot"}
    assert "subprocess" not in imported
    assert "graphviz" not in imported
    assert "networkx" not in imported
    assert "tempfile" not in imported
    assert "byog_graph" not in imported
    assert "compute_bounded_call_impact(" not in dot_src
    assert "compute_transitive_call_impact(" not in dot_src
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    cli_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_impact_graph":
            cli_fn = ast.get_source_segment(gq_src, node)
    assert cli_fn is not None
    assert "subprocess" not in cli_fn
    assert "compute_bounded_call_impact(" not in cli_fn
    assert ".subgraph(" not in cli_fn
    assert cli_fn.count(".impact_graph(") == 1
    assert cli_fn.count("dumps_impact_graph_dot(") == 1
    assert "sys.stdout.write(dumps_impact_graph_dot(result))" in cli_fn
    assert "sys.stdout.flush()" in cli_fn
    write_at = cli_fn.find("sys.stdout.write(dumps_impact_graph_dot(result))")
    flush_at = cli_fn.find("sys.stdout.flush()")
    with_at = cli_fn.find("with _scoped_graph")
    exclusive_at = cli_fn.find("mutually exclusive")
    assert 0 <= exclusive_at < with_at < write_at < flush_at
    assert not list(tmp_path.glob("*.dot"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert "Popen" not in cli_fn
    assert "import graphviz" not in cli_fn
    assert "pydot" not in cli_fn


def test_mcp_remains_seventeen_tools_without_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="rel:ba")],
    )
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    assert not hasattr(session, "impact_graph")
    params = list(inspect.signature(GraphMcpSession.impact).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == ["self", "symbol", "max_items", "snapshot"]
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

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert "impact_graph" not in names
            tool = next(item for item in tools if item.name == "impact")
            props = tool.input_schema.get("properties") or {}
            assert "dot" not in props
            assert "format" not in props
            result = await client.call_tool("impact", {"symbol": "A"})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "impact"
            assert "digraph" not in json.dumps(body)

    anyio_run(_body)
