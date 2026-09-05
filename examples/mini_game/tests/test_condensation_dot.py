"""Deterministic Graphviz DOT export for the bounded condensation CLI.

Renders the existing ByogGraph.condensation result. Does not add a Graphviz
runtime, a second SCC/condensation algorithm, an MCP format parameter, or
an output file.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import math
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
    ByogGraph,
    compute_condensation_graph,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_condensation_dot,
    dumps_condensation_json,
    format_condensation_human,
)
from graphrag_code.condensation_dot import (  # type: ignore
    CONDENSATION_DOT_SCHEMA_VERSION,
    HARD_MAX_CONDENSATION_DOT_BYTES,
    CondensationDotError,
    dumps_condensation_dot as direct_dumps,
)
from graphrag_code.byog_graph import (  # type: ignore
    HARD_MAX_CONDENSATION_COMPONENT_NODES,
    HARD_MAX_CONDENSATION_COMPONENTS,
    HARD_MAX_CONDENSATION_EDGES,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

GOLDEN_DAG = """\
digraph graphrag_condensation {
  graph [
    schema_version="1",
    max_components="20",
    max_nodes_per_component="20",
    max_edges="100",
    edge_types="null",
    n_components_total="4",
    n_components_returned="4",
    n_nodes_total="5",
    n_edges_total="5",
    n_internal_edges_total="2",
    n_cross_component_edges_total="3",
    n_self_loop_edges_total="0",
    n_cyclic_components_total="1",
    n_entity_nodes_total="5",
    n_endpoint_only_nodes_total="0",
    n_condensation_edges_total="2",
    n_condensation_edges_eligible_total="2",
    n_condensation_edges_returned="2",
    components_truncated="false",
    nodes_truncated="false",
    edges_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  c0000 [label="A (2/2, cyclic)", representative="A", nodes="[\\"A\\",\\"B\\"]", n_nodes_total="2", n_nodes_returned="2", n_internal_edges_total="2", n_self_loop_edges_total="0", n_entity_nodes="2", n_endpoint_only_nodes="0", is_cyclic="true", nodes_truncated="false"];
  c0001 [label="C (1/1, acyclic)", representative="C", nodes="[\\"C\\"]", n_nodes_total="1", n_nodes_returned="1", n_internal_edges_total="0", n_self_loop_edges_total="0", n_entity_nodes="1", n_endpoint_only_nodes="0", is_cyclic="false", nodes_truncated="false"];
  c0002 [label="D (1/1, acyclic)", representative="D", nodes="[\\"D\\"]", n_nodes_total="1", n_nodes_returned="1", n_internal_edges_total="0", n_self_loop_edges_total="0", n_entity_nodes="1", n_endpoint_only_nodes="0", is_cyclic="false", nodes_truncated="false"];
  c0003 [label="E (1/1, acyclic)", representative="E", nodes="[\\"E\\"]", n_nodes_total="1", n_nodes_returned="1", n_internal_edges_total="0", n_self_loop_edges_total="0", n_entity_nodes="1", n_endpoint_only_nodes="0", is_cyclic="false", nodes_truncated="false"];
  c0000 -> c0001 [label="rows 2", source="A", target="C", n_relationship_rows_total="2"];
  c0001 -> c0002 [label="rows 1", source="C", target="D", n_relationship_rows_total="1"];
}
"""

GOLDEN_EMPTY = """\
digraph graphrag_condensation {
  graph [
    schema_version="1",
    max_components="20",
    max_nodes_per_component="20",
    max_edges="100",
    edge_types="null",
    n_components_total="0",
    n_components_returned="0",
    n_nodes_total="0",
    n_edges_total="0",
    n_internal_edges_total="0",
    n_cross_component_edges_total="0",
    n_self_loop_edges_total="0",
    n_cyclic_components_total="0",
    n_entity_nodes_total="0",
    n_endpoint_only_nodes_total="0",
    n_condensation_edges_total="0",
    n_condensation_edges_eligible_total="0",
    n_condensation_edges_returned="0",
    components_truncated="false",
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


def _dag_entities() -> list[dict]:
    return [_entity(title) for title in ("A", "B", "C", "D", "E")]


def _dag_rels() -> list[dict]:
    return [
        _calls("A", "B", hid=1),
        _calls("B", "A", hid=2),
        _calls("A", "C", hid=3),
        _calls("A", "C", hid=4, rid="rel:parallel-a-c"),
        _calls("C", "D", hid=5),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_condensation_dot"
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


def _run(*args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
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


def _component_statements(dot: str) -> list[str]:
    return [
        line.strip()
        for line in dot.splitlines()
        if re.match(r"^c\d{4} \[", line.strip())
    ]


def _edge_statements(dot: str) -> list[str]:
    return [
        line.strip()
        for line in dot.splitlines()
        if re.match(r"^c\d{4} -> c\d{4} \[", line.strip())
    ]


def _base_result(**overrides: object) -> dict:
    result: dict = {
        "edge_types": None,
        "max_components": 20,
        "max_nodes_per_component": 20,
        "max_edges": 100,
        "components": [
            {
                "representative": "A",
                "nodes": ["A", "B"],
                "n_nodes_total": 2,
                "n_nodes_returned": 2,
                "n_internal_edges_total": 2,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 2,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            {
                "representative": "C",
                "nodes": ["C"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 0,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": False,
                "nodes_truncated": False,
            },
            {
                "representative": "D",
                "nodes": ["D"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 0,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": False,
                "nodes_truncated": False,
            },
            {
                "representative": "E",
                "nodes": ["E"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 0,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": False,
                "nodes_truncated": False,
            },
        ],
        "edges": [
            {"source": "A", "target": "C", "n_relationship_rows_total": 2},
            {"source": "C", "target": "D", "n_relationship_rows_total": 1},
        ],
        "n_components_total": 4,
        "n_components_returned": 4,
        "n_nodes_total": 5,
        "n_edges_total": 5,
        "n_internal_edges_total": 2,
        "n_cross_component_edges_total": 3,
        "n_self_loop_edges_total": 0,
        "n_cyclic_components_total": 1,
        "n_entity_nodes_total": 5,
        "n_endpoint_only_nodes_total": 0,
        "n_condensation_edges_total": 2,
        "n_condensation_edges_eligible_total": 2,
        "n_condensation_edges_returned": 2,
        "components_truncated": False,
        "nodes_truncated": False,
        "edges_truncated": False,
    }
    result.update(copy.deepcopy(overrides))
    return result


def _empty_result(**overrides: object) -> dict:
    result = compute_condensation_graph(pd.DataFrame(), pd.DataFrame())
    result.update(copy.deepcopy(overrides))
    return result


def test_golden_cyclic_dag_is_deterministic_and_hash_seed_independent():
    payload = dumps_condensation_dot(_base_result())
    assert payload == GOLDEN_DAG
    assert payload == dumps_condensation_dot(_base_result())
    assert payload == direct_dumps(_base_result())
    assert payload.endswith("\n") and not payload.endswith("\n\n")
    assert payload.encode("utf-8").decode("utf-8") == payload
    assert (
        "schema_version=" + quote_dot_string(str(CONDENSATION_DOT_SCHEMA_VERSION))
        in payload
    )
    assert dumps_condensation_dot(_empty_result()) == GOLDEN_EMPTY
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_condensation_graph
from graphrag_code.condensation_dot import dumps_condensation_dot
ents = pd.DataFrame([
    {"id": "eA", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eB", "title": "B", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eC", "title": "C", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eD", "title": "D", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eE", "title": "E", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "r1", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r2", "source": "B", "target": "A", "type": "calls", "extractor": "x"},
    {"id": "r3", "source": "A", "target": "C", "type": "calls", "extractor": "x"},
    {"id": "r4", "source": "A", "target": "C", "type": "calls", "extractor": "x"},
    {"id": "r5", "source": "C", "target": "D", "type": "calls", "extractor": "x"},
])
print(dumps_condensation_dot(compute_condensation_graph(ents, rels)), end="")
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
    assert payloads[0] == payloads[1] == payloads[2] == GOLDEN_DAG


def test_live_graph_preserves_order_ids_and_row_aggregation(tmp_path: Path):
    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    result = ByogGraph(graph).condensation()
    payload = dumps_condensation_dot(result)
    assert payload == GOLDEN_DAG
    comps = _component_statements(payload)
    edges = _edge_statements(payload)
    assert [line.split()[0] for line in comps] == ["c0000", "c0001", "c0002", "c0003"]
    for component, line in zip(result["components"], comps):
        assert f"representative={quote_dot_string(component['representative'])}" in line
        assert component["representative"] not in _structural(line).split()
    for edge, line in zip(result["edges"], edges):
        src, _, tgt, *_ = line.split()
        assert src.startswith("c") and tgt.startswith("c")
        assert edge["source"] not in _structural(line).split()[0:3]
        assert f"n_relationship_rows_total={quote_dot_string(str(edge['n_relationship_rows_total']))}" in line
    assert 'n_relationship_rows_total="2"' in edges[0]
    assert 'label="rows 2"' in edges[0]
    assert payload.count(" -> ") == 2


def test_truncation_and_max_edges_zero(tmp_path: Path):
    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    g = ByogGraph(graph)
    components = dumps_condensation_dot(g.condensation(max_components=1))
    assert 'n_components_total="4"' in components
    assert 'n_components_returned="1"' in components
    assert 'components_truncated="true"' in components
    assert 'n_condensation_edges_total="2"' in components
    assert 'n_condensation_edges_eligible_total="0"' in components
    assert 'n_condensation_edges_returned="0"' in components
    assert 'edges_truncated="true"' in components
    assert _component_statements(components) == [
        line
        for line in _component_statements(GOLDEN_DAG)
        if line.startswith("c0000 ")
    ]
    assert _edge_statements(components) == []

    nodes = dumps_condensation_dot(g.condensation(max_nodes_per_component=1))
    assert 'nodes_truncated="true"' in nodes
    first = _component_statements(nodes)[0]
    assert 'n_nodes_total="2"' in first
    assert 'n_nodes_returned="1"' in first
    assert 'nodes_truncated="true"' in first
    assert 'nodes="[\\"A\\"]"' in first
    assert _edge_statements(nodes) == _edge_statements(GOLDEN_DAG)

    zero = dumps_condensation_dot(g.condensation(max_edges=0))
    assert 'max_edges="0"' in zero
    assert 'n_condensation_edges_total="2"' in zero
    assert 'n_condensation_edges_eligible_total="2"' in zero
    assert 'n_condensation_edges_returned="0"' in zero
    assert 'edges_truncated="true"' in zero
    assert _edge_statements(zero) == []
    assert len(_component_statements(zero)) == 4

    combined = dumps_condensation_dot(
        g.condensation(max_components=1, max_nodes_per_component=1, max_edges=0)
    )
    assert 'components_truncated="true"' in combined
    assert 'nodes_truncated="true"' in combined
    assert 'edges_truncated="true"' in combined
    assert 'n_edges_total="5"' in combined
    assert 'n_cross_component_edges_total="3"' in combined
    assert 'n_condensation_edges_returned="0"' in combined
    assert _edge_statements(combined) == []


def test_escaping_injection_unicode_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "z /* comment */ // still"
    result = _base_result(
        components=[
            {
                "representative": nasty,
                "nodes": [nasty, comment],
                "n_nodes_total": 2,
                "n_nodes_returned": 2,
                "n_internal_edges_total": 2,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 2,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            {
                "representative": "é☃\n\t\\",
                "nodes": ["é☃\n\t\\"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 0,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": False,
                "nodes_truncated": False,
            },
            {
                "representative": "comma,value",
                "nodes": ["comma,value"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 0,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": False,
                "nodes_truncated": False,
            },
        ],
        edges=[
            {
                "source": nasty,
                "target": "é☃\n\t\\",
                "n_relationship_rows_total": 2,
            },
            {
                "source": "é☃\n\t\\",
                "target": "comma,value",
                "n_relationship_rows_total": 1,
            },
        ],
        n_components_total=3,
        n_components_returned=3,
        n_nodes_total=4,
        n_entity_nodes_total=4,
        n_condensation_edges_total=2,
        n_condensation_edges_eligible_total=2,
        n_condensation_edges_returned=2,
    )
    payload = dumps_condensation_dot(result)
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert "n9999" not in structural
    assert "/*" not in structural
    assert "//" not in structural
    assert nasty not in structural
    assert structural.count(" -> ") == 2
    assert len(_component_statements(payload)) == 3
    assert len(_edge_statements(payload)) == 2
    assert "\\n" in payload and "\t" not in payload
    assert quote_dot_string(nasty) in payload
    for line in _component_statements(payload) + _edge_statements(payload):
        assert line.endswith("];")
        assert line.startswith("c")


def test_missing_duplicate_and_malformed_schema_fail_closed():
    missing = _base_result(
        edges=[{"source": "A", "target": "ghost", "n_relationship_rows_total": 2}]
        + _base_result()["edges"][1:],
    )
    with pytest.raises(CondensationDotError, match="endpoint"):
        dumps_condensation_dot(missing)
    duplicate = _base_result()
    duplicate["components"][1] = dict(duplicate["components"][0])
    duplicate["components"][1] = {
        **duplicate["components"][1],
        "nodes": ["A", "Z"],
    }
    with pytest.raises(CondensationDotError, match="duplicate"):
        dumps_condensation_dot(duplicate)
    with pytest.raises(CondensationDotError):
        dumps_condensation_dot("not-a-mapping")  # type: ignore[arg-type]
    surrogate = _base_result()
    surrogate["components"][1] = {
        **surrogate["components"][1],
        "representative": "\ud800",
        "nodes": ["\ud800"],
    }
    with pytest.raises(CondensationDotError, match="UTF-8"):
        dumps_condensation_dot(surrogate)
    backward = _base_result(
        edges=[
            {"source": "C", "target": "A", "n_relationship_rows_total": 2},
            {"source": "C", "target": "D", "n_relationship_rows_total": 1},
        ]
    )
    with pytest.raises(CondensationDotError, match="forward"):
        dumps_condensation_dot(backward)
    out_of_order = _base_result(
        edges=[
            {"source": "C", "target": "D", "n_relationship_rows_total": 1},
            {"source": "A", "target": "C", "n_relationship_rows_total": 2},
        ]
    )
    with pytest.raises(CondensationDotError, match="canonical producer order"):
        dumps_condensation_dot(out_of_order)
    for invalid in (
        _base_result(n_components_returned=3),
        _base_result(n_condensation_edges_returned=1),
        _base_result(components_truncated=True),
        _base_result(edges_truncated=True),
        _base_result(nodes_truncated=True),
        _base_result(max_components=1),
        _base_result(max_edges=0),
        _base_result(max_components=True),
        _base_result(max_edges=1.5),
        _base_result(max_edges=math.nan),
        _base_result(max_components=HARD_MAX_CONDENSATION_COMPONENTS + 1),
        _base_result(
            max_nodes_per_component=HARD_MAX_CONDENSATION_COMPONENT_NODES + 1
        ),
        _base_result(max_edges=HARD_MAX_CONDENSATION_EDGES + 1),
        _base_result(n_nodes_total=1.0),
        _base_result(components_truncated=1),
        _empty_result(n_nodes_total=1),
        _empty_result(components_truncated=True),
        _base_result(edge_types=[]),
        _base_result(edge_types=["uses_type", "calls"]),
    ):
        with pytest.raises(CondensationDotError):
            dumps_condensation_dot(invalid)


def test_truncated_aggregate_invariants_fail_closed():
    truncated = _base_result(
        max_components=2,
        components=_base_result()["components"][:2],
        edges=_base_result()["edges"][:1],
        n_components_returned=2,
        n_condensation_edges_eligible_total=1,
        n_condensation_edges_returned=1,
        components_truncated=True,
        edges_truncated=True,
    )
    impossible_nodes = copy.deepcopy(truncated)
    impossible_nodes.update(n_nodes_total=4, n_entity_nodes_total=4)
    with pytest.raises(CondensationDotError, match="omitted component"):
        dumps_condensation_dot(impossible_nodes)

    impossible_edge_total = copy.deepcopy(truncated)
    impossible_edge_total.update(
        edges=[],
        n_condensation_edges_total=4,
        n_condensation_edges_eligible_total=0,
        n_condensation_edges_returned=0,
    )
    with pytest.raises(CondensationDotError, match="cross-component"):
        dumps_condensation_dot(impossible_edge_total)


def test_edge_types_none_literal_all_and_commas():
    unfiltered = dumps_condensation_dot(_base_result(edge_types=None))
    literal_all = dumps_condensation_dot(_base_result(edge_types=["all"]))
    comma_types = dumps_condensation_dot(_base_result(edge_types=["a,b", "c"]))
    split_types = dumps_condensation_dot(_base_result(edge_types=["a", "b,c"]))
    assert 'edge_types="null"' in unfiltered
    assert 'edge_types="[\\"all\\"]"' in literal_all
    assert 'edge_types="[\\"a,b\\",\\"c\\"]"' in comma_types
    assert 'edge_types="[\\"a\\",\\"b,c\\"]"' in split_types
    assert len({unfiltered, literal_all, comma_types, split_types}) == 4


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_condensation_dot(_base_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.condensation_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_CONDENSATION_DOT_BYTES", size)
    assert dumps_condensation_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_CONDENSATION_DOT_BYTES", size + 1)
    assert dumps_condensation_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_CONDENSATION_DOT_BYTES", size - 1)
    with pytest.raises(CondensationDotError, match="hard limit"):
        dumps_condensation_dot(_base_result())
    assert HARD_MAX_CONDENSATION_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    common = ["condensation", "--graph", str(graph)]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_condensation_json(payload) + "\n" == json_out.stdout
    assert format_condensation_human(payload).strip() == human.stdout.strip()
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
    import graphrag_code.condensation_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_CONDENSATION_DOT_BYTES", 32)
    overflow = CliRunner().invoke(app, ["condensation", "--graph", str(graph), "--dot"])
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "condensation", "--help"],
        [sys.executable, str(CLI), "condensation", "--help"],
        [sys.executable, "-m", "graphrag_code.graph_query", "condensation", "--help"],
        [sys.executable, "-m", "graphrag_code", "condensation", "--help"],
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

    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    args = ["condensation", "--graph", str(graph), "--dot"]
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
    expected = dumps_condensation_dot(ByogGraph(graph).condensation())
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed.stdout
        == expected
        == GOLDEN_DAG
    )
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not (graph / ".publish.lock").is_symlink()


def test_current_and_historical_snapshot_dot_reads_do_not_mutate(tmp_path: Path):
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
    current = (graph / "current").read_text(encoding="utf-8").strip()
    assert current == newer.name
    cur = _run(
        sys.executable,
        str(QUERY),
        "condensation",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--dot",
    )
    assert 'representative="demo:new"' in cur.stdout
    hist = _run(
        sys.executable,
        str(CLI),
        "condensation",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--dot",
    )
    assert 'representative="demo:old"' in hist.stdout
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


def _condensation_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_condensation_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_condensation_dot = wrap_dumps

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
            ["condensation", "--graph", graph, "--dot"],
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
    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_condensation_dot_hold, args=(str(graph), held, resume, q))
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
    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    g = ByogGraph(graph)
    calls: list[str] = []
    orig = g.condensation

    def wrapped(*args, **kwargs):
        calls.append("condensation")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "condensation", wrapped)
    for name in (
        "strong_components",
        "components",
        "dependency_order",
        "subgraph",
        "degree_ranking",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    result = g.condensation()
    assert calls == ["condensation"]
    dumps_condensation_dot(result)
    assert calls == ["condensation"]

    dot_src = (ROOT / "src" / "graphrag_code" / "condensation_dot.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(dot_src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "json", "typing", "subgraph_dot"}
    assert "subprocess" not in imported
    assert "graphviz" not in imported
    assert "networkx" not in imported
    assert "tempfile" not in imported
    assert "byog_graph" not in imported
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    condensation_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_condensation":
            condensation_fn = ast.get_source_segment(gq_src, node)
    assert condensation_fn is not None
    assert "subprocess" not in condensation_fn
    assert "compute_condensation_graph" not in condensation_fn
    assert "strong_components" not in condensation_fn
    assert "dependency_order" not in condensation_fn
    assert condensation_fn.count(".condensation(") == 1
    assert "sys.stdout.write(dumps_condensation_dot(result))" in condensation_fn
    assert "sys.stdout.flush()" in condensation_fn
    write_at = condensation_fn.find("sys.stdout.write(dumps_condensation_dot(result))")
    flush_at = condensation_fn.find("sys.stdout.flush()")
    with_at = condensation_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < flush_at


def test_mcp_remains_sixteen_tools_without_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, _dag_entities(), _dag_rels())
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.condensation).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == [
        "self",
        "max_components",
        "max_nodes_per_component",
        "max_edges",
        "edge_types",
        "snapshot",
    ]
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
        "degree_ranking",
        "impact",
        "type_closure",
        "context_pack",
        "snapshot_history",
        "snapshot_diff",
    ]
    assert len(TOOL_NAMES) == 16
    assert "condensation_graph" not in TOOL_NAMES
    assert "condensation-graph" not in TOOL_NAMES

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 16
            tool = next(item for item in tools if item.name == "condensation")
            props = tool.input_schema.get("properties") or {}
            assert "dot" not in props
            assert "format" not in props
            assert list(props) == [
                "max_components",
                "max_nodes_per_component",
                "max_edges",
                "edge_types",
                "snapshot",
            ]
            result = await client.call_tool("condensation", {})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "condensation"
            assert "digraph" not in json.dumps(body)

    anyio_run(_body)
