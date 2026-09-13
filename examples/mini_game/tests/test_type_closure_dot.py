"""Deterministic Graphviz DOT export for the bounded type-closure CLI.

Renders the existing ByogGraph.type_closure result. Does not add a Graphviz
runtime, a second BFS, an MCP format parameter, or an output file.
"""
from __future__ import annotations

import ast
import copy
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
    PUBLICATION_LOCK_NAME,
    ByogGraph,
    compute_uses_type_closure,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_type_closure_dot,
    format_type_closure_human,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore
from graphrag_code.type_closure_dot import (  # type: ignore
    HARD_MAX_TYPE_CLOSURE_DOT_BYTES,
    TYPE_CLOSURE_DOT_SCHEMA_VERSION,
    TypeClosureDotError,
    dumps_type_closure_dot as direct_dumps,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
GRAPH_ATTR_RE = re.compile(
    r"graph \[\n(.*?)\n  \];",
    re.DOTALL,
)
NODE_ATTR_RE = re.compile(r"n\d{4} \[([^\]]+)\];")

GOLDEN_DEPENDENCIES = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="dependencies",
    max_depth="3",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="4",
    n_edges_total="6",
    n_nodes_returned="4",
    n_edges_returned="6",
    n_rendered_nodes="4",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="true", depth="0", is_root="true", peripheries="2"];
  n0001 [label="B", title="B", in_nodes="true", depth="1", is_root="false"];
  n0002 [label="C", title="C", in_nodes="true", depth="2", is_root="false"];
  n0003 [label="Y", title="Y", in_nodes="true", depth="2", is_root="false"];
  n0000 -> n0000 [label="uses_type", id="rel:self", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab2", type="uses_type", depth="0"];
  n0001 -> n0002 [label="uses_type", id="rel:bc", type="uses_type", depth="1"];
  n0001 -> n0003 [label="uses_type", id="rel:by", type="uses_type", depth="1"];
  n0002 -> n0000 [label="uses_type", id="rel:ca", type="uses_type", depth="2"];
}
"""

GOLDEN_USERS = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="users",
    max_depth="3",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="3",
    n_edges_total="5",
    n_nodes_returned="3",
    n_edges_returned="5",
    n_rendered_nodes="3",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="true", depth="0", is_root="true", peripheries="2"];
  n0001 [label="C", title="C", in_nodes="true", depth="1", is_root="false"];
  n0002 [label="B", title="B", in_nodes="true", depth="2", is_root="false"];
  n0000 -> n0000 [label="uses_type", id="rel:self", type="uses_type", depth="0"];
  n0001 -> n0000 [label="uses_type", id="rel:ca", type="uses_type", depth="0"];
  n0002 -> n0001 [label="uses_type", id="rel:bc", type="uses_type", depth="1"];
  n0000 -> n0002 [label="uses_type", id="rel:ab", type="uses_type", depth="2"];
  n0000 -> n0002 [label="uses_type", id="rel:ab2", type="uses_type", depth="2"];
}
"""

GOLDEN_BOTH = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="both",
    max_depth="3",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="4",
    n_edges_total="6",
    n_nodes_returned="4",
    n_edges_returned="6",
    n_rendered_nodes="4",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="true", depth="0", is_root="true", peripheries="2"];
  n0001 [label="B", title="B", in_nodes="true", depth="1", is_root="false"];
  n0002 [label="C", title="C", in_nodes="true", depth="1", is_root="false"];
  n0003 [label="Y", title="Y", in_nodes="true", depth="2", is_root="false"];
  n0000 -> n0000 [label="uses_type", id="rel:self", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab2", type="uses_type", depth="0"];
  n0002 -> n0000 [label="uses_type", id="rel:ca", type="uses_type", depth="0"];
  n0001 -> n0002 [label="uses_type", id="rel:bc", type="uses_type", depth="1"];
  n0001 -> n0003 [label="uses_type", id="rel:by", type="uses_type", depth="1"];
}
"""

GOLDEN_UNRESOLVED = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="false",
    direction="dependencies",
    max_depth="3",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="0",
    n_edges_total="0",
    n_nodes_returned="0",
    n_edges_returned="0",
    n_rendered_nodes="0",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_DEPTH0 = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="dependencies",
    max_depth="0",
    max_nodes="50",
    max_edges="100",
    n_nodes_total="1",
    n_edges_total="0",
    n_nodes_returned="1",
    n_edges_returned="0",
    n_rendered_nodes="1",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="true", depth="0", is_root="true", peripheries="2"];
}
"""

GOLDEN_EDGE_ONLY = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="dependencies",
    max_depth="3",
    max_nodes="1",
    max_edges="3",
    n_nodes_total="4",
    n_edges_total="6",
    n_nodes_returned="1",
    n_edges_returned="3",
    n_rendered_nodes="2",
    nodes_truncated="true",
    edges_truncated="true"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="true", depth="0", is_root="true", peripheries="2"];
  n0001 [label="B", title="B", in_nodes="false", is_root="false"];
  n0000 -> n0000 [label="uses_type", id="rel:self", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab2", type="uses_type", depth="0"];
}
"""

GOLDEN_MAX_NODES_0 = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="dependencies",
    max_depth="3",
    max_nodes="0",
    max_edges="2",
    n_nodes_total="4",
    n_edges_total="6",
    n_nodes_returned="0",
    n_edges_returned="2",
    n_rendered_nodes="2",
    nodes_truncated="true",
    edges_truncated="true"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="false", is_root="true", peripheries="2"];
  n0001 [label="B", title="B", in_nodes="false", is_root="false"];
  n0000 -> n0000 [label="uses_type", id="rel:self", type="uses_type", depth="0"];
  n0000 -> n0001 [label="uses_type", id="rel:ab", type="uses_type", depth="0"];
}
"""

GOLDEN_MAX_EDGES_0 = """\
digraph graphrag_type_closure {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="dependencies",
    max_depth="3",
    max_nodes="50",
    max_edges="0",
    n_nodes_total="4",
    n_edges_total="6",
    n_nodes_returned="4",
    n_edges_returned="0",
    n_rendered_nodes="4",
    nodes_truncated="false",
    edges_truncated="true"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_nodes="true", depth="0", is_root="true", peripheries="2"];
  n0001 [label="B", title="B", in_nodes="true", depth="1", is_root="false"];
  n0002 [label="C", title="C", in_nodes="true", depth="2", is_root="false"];
  n0003 [label="Y", title="Y", in_nodes="true", depth="2", is_root="false"];
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


def _uses(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "uses_type", hid=hid, **extra)


def _calls(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "calls", hid=hid, **extra)


def _fixture_entities() -> list[dict]:
    return [
        _entity("A"),
        _entity("B", "typedef"),
        _entity("C", "typedef"),
        _entity("Isolated"),
    ]


def _fixture_rels() -> list[dict]:
    return [
        _uses("A", "B", hid=1, rid="rel:ab"),
        _uses("B", "C", hid=2, rid="rel:bc"),
        _uses("C", "A", hid=3, rid="rel:ca"),
        _uses("A", "A", hid=4, rid="rel:self"),
        _uses("A", "B", hid=5, rid="rel:ab2"),
        _uses("B", "Y", hid=6, rid="rel:by"),
        _calls("A", "ghost", hid=7, rid="rel:calls"),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_type_closure_dot"
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
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=cwd,
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


def _node_ids(dot: str) -> list[str]:
    return re.findall(r"^\s+(n\d{4}) \[", dot, re.MULTILINE)


def _node_statements(dot: str) -> list[str]:
    return re.findall(r"^\s+(n\d{4} \[.*\];)$", dot, re.MULTILINE)


def _edge_statements(dot: str) -> list[str]:
    return re.findall(r"^\s+(n\d{4} -> n\d{4} \[.*\];)$", dot, re.MULTILINE)


def _graph_attr_names(dot: str) -> list[str]:
    block = GRAPH_ATTR_RE.search(dot)
    assert block is not None
    return re.findall(r"^\s+([A-Za-z_][A-Za-z0-9_]*)=", block.group(1), re.MULTILINE)


def _node_attr_names(dot: str) -> list[list[str]]:
    return [
        re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", attrs)
        for attrs in NODE_ATTR_RE.findall(dot)
    ]


def _rels_df(rows: list | None = None) -> pd.DataFrame:
    return pd.DataFrame(rows if rows is not None else _fixture_rels())


def _base_result(**overrides: object) -> dict:
    result = compute_uses_type_closure(_rels_df(), "A", max_depth=3)
    result.update(copy.deepcopy(overrides))
    return result


def _empty_result(**overrides: object) -> dict:
    result = compute_uses_type_closure(_rels_df(), None)
    result.update(copy.deepcopy(overrides))
    return result


def test_golden_directions_cycle_self_parallel_and_hash_seed():
    rels = _rels_df()
    deps = dumps_type_closure_dot(compute_uses_type_closure(rels, "A", max_depth=3))
    users = dumps_type_closure_dot(
        compute_uses_type_closure(rels, "A", direction="users", max_depth=3)
    )
    both = dumps_type_closure_dot(
        compute_uses_type_closure(rels, "A", direction="both", max_depth=3)
    )
    assert deps == GOLDEN_DEPENDENCIES
    assert users == GOLDEN_USERS
    assert both == GOLDEN_BOTH
    assert deps == dumps_type_closure_dot(_base_result())
    assert deps == direct_dumps(_base_result())
    assert deps.endswith("\n") and not deps.endswith("\n\n")
    assert deps.encode("utf-8").decode("utf-8") == deps
    assert (
        "schema_version="
        + quote_dot_string(str(TYPE_CLOSURE_DOT_SCHEMA_VERSION))
        in deps
    )
    assert dumps_type_closure_dot(_empty_result()) == GOLDEN_UNRESOLVED
    shuffled = dumps_type_closure_dot(
        compute_uses_type_closure(
            rels.sample(frac=1, random_state=11).reset_index(drop=True),
            "A",
            max_depth=3,
        )
    )
    assert shuffled == GOLDEN_DEPENDENCIES
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_uses_type_closure
from graphrag_code.type_closure_dot import dumps_type_closure_dot
rels = pd.DataFrame([
    {"id": "rel:ab", "source": "A", "target": "B", "type": "uses_type"},
    {"id": "rel:bc", "source": "B", "target": "C", "type": "uses_type"},
    {"id": "rel:ca", "source": "C", "target": "A", "type": "uses_type"},
    {"id": "rel:self", "source": "A", "target": "A", "type": "uses_type"},
    {"id": "rel:ab2", "source": "A", "target": "B", "type": "uses_type"},
    {"id": "rel:by", "source": "B", "target": "Y", "type": "uses_type"},
    {"id": "rel:calls", "source": "A", "target": "ghost", "type": "calls"},
])
print(dumps_type_closure_dot(compute_uses_type_closure(rels, "A", max_depth=3)), end="")
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
    assert payloads[0] == payloads[1] == payloads[2] == GOLDEN_DEPENDENCIES


def test_live_graph_preserves_producer_order_orientation_and_exclusions(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    result = g.type_closure("A", direction="dependencies", max_depth=3)
    payload = dumps_type_closure_dot(result)
    assert payload == GOLDEN_DEPENDENCIES
    assert [n["title"] for n in result["nodes"]] == ["A", "B", "C", "Y"]
    assert [line.split()[0] for line in _node_statements(payload)] == [
        "n0000",
        "n0001",
        "n0002",
        "n0003",
    ]
    title_by_ident = {}
    for node, line in zip(result["nodes"], _node_statements(payload)):
        assert f"title={quote_dot_string(node['title'])}" in line
        title_by_ident[line.split()[0]] = node["title"]
    for edge, line in zip(result["edges"], _edge_statements(payload)):
        src, _, tgt, *_ = line.split()
        assert title_by_ident[src] == edge["source"]
        assert title_by_ident[tgt] == edge["target"]
        assert f"id={quote_dot_string(edge['id'])}" in line
        assert 'label="uses_type"' in line
        assert 'type="uses_type"' in line
    users = g.type_closure("A", direction="users", max_depth=3)
    users_dot = dumps_type_closure_dot(users)
    assert users_dot == GOLDEN_USERS
    ca = next(edge for edge in users["edges"] if edge["id"] == "rel:ca")
    assert ca["source"] == "C" and ca["target"] == "A"
    assert "n0000 -> n0001" not in users_dot.split("n0001 -> n0000", 1)[0]
    assert any(
        line.startswith("n0001 -> n0000 ") for line in _edge_statements(users_dot)
    )
    assert "Isolated" not in payload
    assert "ghost" not in payload
    assert _graph_attr_names(payload) == [
        "schema_version",
        "resolved",
        "root",
        "direction",
        "max_depth",
        "max_nodes",
        "max_edges",
        "n_nodes_total",
        "n_edges_total",
        "n_nodes_returned",
        "n_edges_returned",
        "n_rendered_nodes",
        "nodes_truncated",
        "edges_truncated",
    ]
    src = (ROOT / "src" / "graphrag_code" / "type_closure_dot.py").read_text(
        encoding="utf-8"
    )
    assert "not a claim that a rendered Graphviz layout will" in src
    assert "serialization identifiers only" in src


def test_unresolved_ambiguous_and_max_depth_zero(tmp_path: Path):
    entities = _fixture_entities() + [_entity("m:f"), _entity("n:f")]
    graph = _publish(tmp_path, entities, _fixture_rels())
    g = ByogGraph(graph)
    missing = dumps_type_closure_dot(g.type_closure("does-not-exist"))
    amb = dumps_type_closure_dot(g.type_closure("f"))
    assert missing == GOLDEN_UNRESOLVED
    assert amb == GOLDEN_UNRESOLVED
    assert "root=" not in missing
    assert _node_statements(missing) == []
    assert _edge_statements(missing) == []
    depth0 = dumps_type_closure_dot(g.type_closure("A", max_depth=0))
    assert depth0 == GOLDEN_DEPTH0
    assert _edge_statements(depth0) == []
    assert len(_node_statements(depth0)) == 1


def test_independent_caps_edge_only_nodes_and_no_implicit_titles():
    rels = _rels_df()
    one_node = dumps_type_closure_dot(
        compute_uses_type_closure(rels, "A", max_depth=3, max_nodes=1, max_edges=3)
    )
    nodes_only = dumps_type_closure_dot(
        compute_uses_type_closure(rels, "A", max_depth=3, max_edges=0)
    )
    edges_only = dumps_type_closure_dot(
        compute_uses_type_closure(rels, "A", max_depth=3, max_nodes=0, max_edges=2)
    )
    assert one_node == GOLDEN_EDGE_ONLY
    assert nodes_only == GOLDEN_MAX_EDGES_0
    assert edges_only == GOLDEN_MAX_NODES_0
    assert 'n_rendered_nodes="2"' in one_node
    assert 'n_nodes_returned="1"' in one_node
    assert 'in_nodes="false"' in one_node
    assert "depth=" not in one_node.split('in_nodes="false"', 1)[1].split("];", 1)[0]
    assert _node_ids(one_node) == ["n0000", "n0001"]
    assert " Y " not in _structural(one_node)
    assert re.search(r"\bY\b", _structural(one_node)) is None
    assert 'title="Y"' not in one_node
    bang = _base_result(
        nodes=[{"title": "A", "depth": 0}],
        edges=[
            {"id": "rel:bang", "source": "A", "target": "!", "depth": 0},
        ],
        n_nodes_total=2,
        n_edges_total=1,
        n_nodes_returned=1,
        n_edges_returned=1,
        max_nodes=1,
        max_edges=1,
        nodes_truncated=True,
        edges_truncated=False,
        max_depth=1,
    )
    bang_dot = dumps_type_closure_dot(bang)
    assert _node_ids(bang_dot) == ["n0000", "n0001"]
    assert 'title="A"' in _node_statements(bang_dot)[0]
    assert 'title="!"' in _node_statements(bang_dot)[1]
    assert 'in_nodes="false"' in _node_statements(bang_dot)[1]
    assert "depth=" not in _node_statements(bang_dot)[1]
    both_missing = _base_result(
        nodes=[],
        edges=[{"id": "rel:bc", "source": "B", "target": "C", "depth": 0}],
        n_nodes_total=4,
        n_edges_total=1,
        n_nodes_returned=0,
        n_edges_returned=1,
        max_nodes=0,
        max_edges=1,
        nodes_truncated=True,
        edges_truncated=False,
        max_depth=1,
    )
    missing_dot = dumps_type_closure_dot(both_missing)
    assert _node_ids(missing_dot) == ["n0000", "n0001"]
    assert 'title="B"' in _node_statements(missing_dot)[0]
    assert 'title="C"' in _node_statements(missing_dot)[1]
    assert 'in_nodes="false"' in _node_statements(missing_dot)[0]
    assert 'in_nodes="false"' in _node_statements(missing_dot)[1]
    assert 'is_root="false"' in _node_statements(missing_dot)[0]
    assert "depth=" not in _node_statements(missing_dot)[0]
    assert 'root="A"' in missing_dot
    assert 'n_rendered_nodes="2"' in missing_dot


def test_escaping_injection_controls_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "z /* comment */ // still"
    braces = "a[--]; {b} -> c;"
    result = _base_result(
        root=nasty,
        nodes=[
            {"title": nasty, "depth": 0},
            {"title": braces, "depth": 1},
            {"title": comment, "depth": 1},
            {"title": "é☃\n\t\\", "depth": 1},
        ],
        edges=[
            {
                "id": 'rel:"id"\n',
                "source": nasty,
                "target": "é☃\n\t\\",
                "depth": 0,
            },
            {
                "id": "rel:ctrl\x01\x7f",
                "source": comment,
                "target": braces,
                "depth": 0,
            },
        ],
        n_nodes_total=4,
        n_edges_total=2,
        n_nodes_returned=4,
        n_edges_returned=2,
        max_depth=1,
        nodes_truncated=False,
        edges_truncated=False,
    )
    payload = dumps_type_closure_dot(result)
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert " -> " in payload
    assert structural.count(" -> ") == 2
    assert "graph [" in payload
    assert "/*" not in structural
    assert "// still" not in structural
    assert "{b}" not in structural
    assert "[--]" not in structural
    assert "\\n" in quote_dot_string("é☃\n\t\\")
    assert "\\n" in payload
    assert "\\t" in payload
    assert "\\\\" in payload
    assert "\\x01" in payload
    assert "\\x7f" in payload
    assert "\x01" not in payload
    quoted_nasty = quote_dot_string(nasty)
    assert quoted_nasty in payload
    assert payload.count(quoted_nasty) >= 3


def test_reject_malformed_mappings():
    with pytest.raises(TypeClosureDotError, match="must be a mapping"):
        dumps_type_closure_dot(["A"])  # type: ignore[arg-type]
    with pytest.raises(TypeClosureDotError, match="must be a mapping"):
        dumps_type_closure_dot("A")  # type: ignore[arg-type]
    missing = _base_result()
    del missing["direction"]
    with pytest.raises(TypeClosureDotError, match="missing type-closure field"):
        dumps_type_closure_dot(missing)
    with pytest.raises(TypeClosureDotError, match="must be a list"):
        dumps_type_closure_dot(_base_result(nodes=("A",)))  # type: ignore[arg-type]
    with pytest.raises(TypeClosureDotError, match="must be a string"):
        dumps_type_closure_dot(
            _base_result(
                nodes=[{"title": 1, "depth": 0}],
                n_nodes_returned=1,
                n_nodes_total=1,
                edges=[],
                n_edges_returned=0,
                n_edges_total=0,
                nodes_truncated=False,
                edges_truncated=False,
            )
        )
    empty_title = _base_result(
        nodes=[{"title": "", "depth": 0}],
        n_nodes_returned=1,
        n_nodes_total=1,
        edges=[],
        n_edges_returned=0,
        n_edges_total=0,
        nodes_truncated=False,
        edges_truncated=False,
    )
    with pytest.raises(TypeClosureDotError, match="non-empty"):
        dumps_type_closure_dot(empty_title)
    with pytest.raises(TypeClosureDotError, match="must be an integer"):
        dumps_type_closure_dot(_base_result(max_depth=True))
    with pytest.raises(TypeClosureDotError, match="must be an integer"):
        dumps_type_closure_dot(_base_result(max_nodes=1.5))
    with pytest.raises(TypeClosureDotError, match="must be >= 0"):
        dumps_type_closure_dot(_base_result(max_edges=-1))
    with pytest.raises(TypeClosureDotError, match="must be a boolean"):
        dumps_type_closure_dot(_base_result(resolved=1))
    with pytest.raises(TypeClosureDotError, match="does not match nodes"):
        dumps_type_closure_dot(_base_result(n_nodes_returned=1))
    with pytest.raises(TypeClosureDotError, match="disagrees with node counts"):
        dumps_type_closure_dot(_base_result(nodes_truncated=True))
    dup_title = _base_result(
        nodes=[{"title": "A", "depth": 0}, {"title": "A", "depth": 1}],
        n_nodes_returned=2,
        n_nodes_total=2,
        edges=[],
        n_edges_returned=0,
        n_edges_total=0,
        nodes_truncated=False,
        edges_truncated=False,
        max_depth=1,
    )
    with pytest.raises(TypeClosureDotError, match="duplicate type-closure node title"):
        dumps_type_closure_dot(dup_title)
    dup_edge = _base_result(
        edges=[
            {"id": "rel:x", "source": "A", "target": "B", "depth": 0},
            {"id": "rel:x", "source": "A", "target": "C", "depth": 0},
        ],
        n_edges_returned=2,
        n_edges_total=2,
    )
    with pytest.raises(TypeClosureDotError, match="duplicate type-closure edge id"):
        dumps_type_closure_dot(dup_edge)
    out_of_order = _base_result()
    out_of_order["nodes"] = list(reversed(out_of_order["nodes"]))
    with pytest.raises(TypeClosureDotError, match="canonical producer node order"):
        dumps_type_closure_dot(out_of_order)
    edge_order = _base_result()
    edge_order["edges"] = list(reversed(edge_order["edges"]))
    with pytest.raises(TypeClosureDotError, match="canonical producer edge order"):
        dumps_type_closure_dot(edge_order)
    with pytest.raises(TypeClosureDotError, match="invalid type-closure direction"):
        dumps_type_closure_dot(_base_result(direction="outgoing"))
    unresolved_root = _empty_result(root="A", resolved=False)
    with pytest.raises(TypeClosureDotError, match="null root"):
        dumps_type_closure_dot(unresolved_root)
    resolved_null = _base_result(root=None)
    with pytest.raises(TypeClosureDotError, match="root"):
        dumps_type_closure_dot(resolved_null)
    lone = _base_result(
        nodes=[{"title": "A", "depth": 0}, {"title": "\ud800", "depth": 1}],
        n_nodes_returned=2,
        n_nodes_total=2,
        edges=[],
        n_edges_returned=0,
        n_edges_total=0,
        max_depth=1,
        nodes_truncated=False,
        edges_truncated=False,
    )
    with pytest.raises(TypeClosureDotError, match="strict UTF-8"):
        dumps_type_closure_dot(lone)
    too_deep_edge = _base_result(
        edges=[{"id": "rel:x", "source": "A", "target": "B", "depth": 3}],
        n_edges_returned=1,
        n_edges_total=1,
        max_depth=3,
    )
    with pytest.raises(TypeClosureDotError, match="strictly below max_depth"):
        dumps_type_closure_dot(too_deep_edge)
    rendered_overflow = _base_result(
        nodes=[],
        edges=[{"id": "rel:x", "source": "B", "target": "C", "depth": 0}],
        n_nodes_returned=0,
        n_nodes_total=1,
        n_edges_returned=1,
        n_edges_total=1,
        max_nodes=0,
        max_edges=1,
        max_depth=1,
        nodes_truncated=True,
        edges_truncated=False,
    )
    with pytest.raises(TypeClosureDotError, match="n_rendered_nodes exceeds"):
        dumps_type_closure_dot(rendered_overflow)


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_type_closure_dot(_base_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.type_closure_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_TYPE_CLOSURE_DOT_BYTES", size)
    assert dumps_type_closure_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_TYPE_CLOSURE_DOT_BYTES", size + 1)
    assert dumps_type_closure_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_TYPE_CLOSURE_DOT_BYTES", size - 1)
    with pytest.raises(TypeClosureDotError, match="hard limit"):
        dumps_type_closure_dot(_base_result())
    assert HARD_MAX_TYPE_CLOSURE_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    common = ["type-closure", "A", "--graph", str(graph), "--max-depth", "3"]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert json.dumps(payload, indent=2, ensure_ascii=False) + "\n" == json_out.stdout
    assert format_type_closure_human(payload).strip() == human.stdout.strip()
    both = _run(sys.executable, str(QUERY), *common, "--json", "--dot", check=False)
    assert both.returncode == 2
    assert both.stdout == ""
    assert "mutually exclusive" in both.stderr
    product_both = _run(
        sys.executable, str(CLI), *common, "--json", "--dot", check=False
    )
    assert product_both.returncode == 2
    assert product_both.stdout == ""
    from typer.testing import CliRunner

    from graphrag_code.graph_query import app
    import graphrag_code.graph_query as gq
    import graphrag_code.type_closure_dot as dot_mod

    def boom(*_a, **_k):
        raise AssertionError("graph observed before json/dot rejection")

    monkeypatch.setattr(gq, "_scoped_graph", boom)
    rejected = CliRunner().invoke(
        app, ["type-closure", "A", "--graph", str(graph), "--json", "--dot"]
    )
    assert rejected.exit_code == 2
    assert rejected.stdout == ""
    monkeypatch.undo()
    monkeypatch.setattr(dot_mod, "HARD_MAX_TYPE_CLOSURE_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["type-closure", "A", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "type-closure", "--help"],
        [sys.executable, str(CLI), "type-closure", "--help"],
        [
            sys.executable,
            "-m",
            "graphrag_code.graph_query",
            "type-closure",
            "--help",
        ],
        [sys.executable, "-m", "graphrag_code", "type-closure", "--help"],
    ):
        help_out = _run(*args)
        assert "--dot" in help_out.stdout
        assert "--json" in help_out.stdout
        assert "Graphviz" in help_out.stdout
        assert "Mutually" in help_out.stdout
        assert "exclusive" in help_out.stdout


def test_script_module_product_installed_dot_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    args = ["type-closure", "A", "--graph", str(graph), "--max-depth", "3", "--dot"]
    script = _run(sys.executable, str(QUERY), *args)
    module = _run(sys.executable, "-m", "graphrag_code.graph_query", *args)
    product = _run(sys.executable, str(CLI), *args)
    package = _run(sys.executable, "-m", "graphrag_code", *args)
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
    expected = dumps_type_closure_dot(
        ByogGraph(graph).type_closure("A", max_depth=3)
    )
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed.stdout
        == expected
        == GOLDEN_DEPENDENCIES
    )
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not (graph / ".publish.lock").is_symlink()


def test_invalid_graph_snapshot_and_unsafe_lease_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "A",
        "--graph",
        str(tmp_path / "missing"),
        "--dot",
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    bad_snap = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "A",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        "--dot",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == ""
    bad_dir = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "A",
        "--graph",
        str(graph),
        "--direction",
        "sideways",
        "--dot",
        check=False,
    )
    assert bad_dir.returncode == 2
    assert bad_dir.stdout == ""
    bad_max = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "A",
        "--graph",
        str(graph),
        "--max-depth",
        "-1",
        "--dot",
        check=False,
    )
    assert bad_max.returncode == 2
    assert bad_max.stdout == ""
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    unlocked_explicit = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "A",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--dot",
        check=False,
    )
    assert unlocked_explicit.returncode == 2
    assert unlocked_explicit.stdout == ""
    assert "publication lock is missing" in unlocked_explicit.stderr
    lock.symlink_to(tmp_path / "elsewhere")
    unsafe = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "A",
        "--graph",
        str(graph),
        "--dot",
        check=False,
    )
    assert unsafe.returncode == 2
    assert unsafe.stdout == ""


def test_current_and_historical_snapshot_dot_reads_do_not_mutate(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old")]),
        pd.DataFrame([_uses("demo:old", "demo:old", rid="rel:old")]),
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
        pd.DataFrame([_uses("demo:new", "demo:other", rid="rel:new")]),
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
    current = (graph / "current").read_text(encoding="utf-8").strip()
    assert current == newer.name
    cur = _run(
        sys.executable,
        str(QUERY),
        "type-closure",
        "demo:new",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--dot",
    )
    assert 'title="demo:new"' in cur.stdout
    hist = _run(
        sys.executable,
        str(CLI),
        "type-closure",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--dot",
    )
    assert 'title="demo:old"' in hist.stdout
    assert "demo:new" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
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


def _type_closure_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_type_closure_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_type_closure_dot = wrap_dumps

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
            ["type-closure", "A", "--graph", graph, "--dot"],
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
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_type_closure_dot_hold, args=(str(graph), held, resume, q)
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


def test_no_nested_query_graphviz_or_tempfiles_and_single_producer_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    calls: list[str] = []
    orig = g.type_closure

    def wrapped(*args, **kwargs):
        calls.append("type_closure")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "type_closure", wrapped)
    for name in (
        "subgraph",
        "condensation",
        "components",
        "strong_components",
        "shortest_path",
        "dependency_order",
        "degree_ranking",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    result = g.type_closure("A", max_depth=3)
    assert calls == ["type_closure"]
    dumps_type_closure_dot(result)
    assert calls == ["type_closure"]

    bfs_calls = 0
    orig_bfs = compute_uses_type_closure

    def counted_bfs(*args, **kwargs):
        nonlocal bfs_calls
        bfs_calls += 1
        return orig_bfs(*args, **kwargs)

    import graphrag_code.byog_graph as byog
    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner

    monkeypatch.setattr(byog, "compute_uses_type_closure", counted_bfs)
    serializer_calls = 0
    orig_dumps = dumps_type_closure_dot
    loads = 0

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    monkeypatch.setattr(gq, "dumps_type_closure_dot", counted_dumps)
    import graphrag_code.snapshot_read as snapshot_read

    orig_load = snapshot_read.RetainedSnapshotScope.load_graph

    def counted_load(self):
        nonlocal loads
        loads += 1
        return orig_load(self)

    monkeypatch.setattr(snapshot_read.RetainedSnapshotScope, "load_graph", counted_load)
    invoked = CliRunner().invoke(
        gq.app, ["type-closure", "A", "--graph", str(graph), "--max-depth", "3", "--dot"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert serializer_calls == 1
    assert loads == 1
    assert bfs_calls == 1
    assert invoked.stdout == GOLDEN_DEPENDENCIES

    dot_src = (ROOT / "src" / "graphrag_code" / "type_closure_dot.py").read_text(
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
    assert "compute_uses_type_closure(" not in dot_src
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    closure_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_type_closure":
            closure_fn = ast.get_source_segment(gq_src, node)
    assert closure_fn is not None
    assert "subprocess" not in closure_fn
    assert "compute_uses_type_closure(" not in closure_fn
    assert ".subgraph(" not in closure_fn
    assert closure_fn.count(".type_closure(") == 1
    assert closure_fn.count("dumps_type_closure_dot(") == 1
    assert "sys.stdout.write(dumps_type_closure_dot(result))" in closure_fn
    assert "sys.stdout.flush()" in closure_fn
    write_at = closure_fn.find("sys.stdout.write(dumps_type_closure_dot(result))")
    flush_at = closure_fn.find("sys.stdout.flush()")
    with_at = closure_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < flush_at
    assert not list(tmp_path.glob("*.dot"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_mcp_remains_seventeen_tools_without_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.type_closure).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == [
        "self",
        "symbol",
        "direction",
        "max_depth",
        "max_nodes",
        "max_edges",
        "snapshot",
    ]
    expected = ByogGraph(graph).type_closure("A")
    payload = session.type_closure("A")
    assert payload["tool"] == "type_closure"
    assert payload["data"] == json.loads(
        json.dumps(expected, allow_nan=False, default=str)
    )
    assert "dot" not in payload
    assert "format" not in payload
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

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 17
            tool = next(item for item in tools if item.name == "type_closure")
            props = tool.input_schema.get("properties") or {}
            assert "dot" not in props
            assert "format" not in props
            assert list(props) == [
                "symbol",
                "direction",
                "max_depth",
                "max_nodes",
                "max_edges",
                "snapshot",
            ]
            result = await client.call_tool("type_closure", {"symbol": "A"})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "type_closure"
            dumped = json.dumps(body)
            assert "digraph" not in dumped
            assert "graphrag_type_closure" not in dumped
            assert "--dot" not in dumped

    anyio_run(_body)
