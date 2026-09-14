"""Deterministic Graphviz DOT export for the bounded strong-components CLI.

Renders the existing ByogGraph.strong_components result. Does not add a
Graphviz runtime, a second SCC algorithm, an MCP format parameter, or an
output file.
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
    PUBLICATION_LOCK_NAME,
    ByogGraph,
    compute_strongly_connected_components,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_strong_components_dot,
    dumps_strong_components_json,
    format_strong_components_human,
)
from graphrag_code.strong_components_dot import (  # type: ignore
    HARD_MAX_STRONG_COMPONENTS_DOT_BYTES,
    STRONG_COMPONENTS_DOT_SCHEMA_VERSION,
    StrongComponentsDotError,
    dumps_strong_components_dot as direct_dumps,
)
from graphrag_code.byog_graph import (  # type: ignore
    HARD_MAX_STRONG_COMPONENT_NODES,
    HARD_MAX_STRONG_COMPONENTS,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

GOLDEN_MULTI = """\
digraph graphrag_strong_components {
  graph [
    schema_version="1",
    edge_types="null",
    max_components="20",
    max_nodes_per_component="20",
    n_components_total="4",
    n_components_returned="4",
    n_nodes_total="6",
    n_edges_total="6",
    n_internal_edges_total="5",
    n_cross_component_edges_total="1",
    n_self_loop_edges_total="1",
    n_cyclic_components_total="2",
    n_entity_nodes_total="4",
    n_endpoint_only_nodes_total="2",
    components_truncated="false",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  subgraph cluster_c0000 {
    label="A";
    component_id="c0000";
    representative="A";
    n_nodes_total="3";
    n_nodes_returned="3";
    n_internal_edges_total="4";
    n_self_loop_edges_total="0";
    n_entity_nodes="3";
    n_endpoint_only_nodes="0";
    is_cyclic="true";
    nodes_truncated="false";
    n0000 [label="A", title="A"];
    n0001 [label="B", title="B"];
    n0002 [label="C", title="C"];
  }
  subgraph cluster_c0001 {
    label="X";
    component_id="c0001";
    representative="X";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="1";
    n_self_loop_edges_total="1";
    n_entity_nodes="0";
    n_endpoint_only_nodes="1";
    is_cyclic="true";
    nodes_truncated="false";
    n0003 [label="X", title="X"];
  }
  subgraph cluster_c0002 {
    label="Isolated";
    component_id="c0002";
    representative="Isolated";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="1";
    n_endpoint_only_nodes="0";
    is_cyclic="false";
    nodes_truncated="false";
    n0004 [label="Isolated", title="Isolated"];
  }
  subgraph cluster_c0003 {
    label="ghost";
    component_id="c0003";
    representative="ghost";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="0";
    n_endpoint_only_nodes="1";
    is_cyclic="false";
    nodes_truncated="false";
    n0005 [label="ghost", title="ghost"];
  }
}
"""

GOLDEN_EMPTY = """\
digraph graphrag_strong_components {
  graph [
    schema_version="1",
    edge_types="null",
    max_components="20",
    max_nodes_per_component="20",
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
    components_truncated="false",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_UNICODE = """\
digraph graphrag_strong_components {
  graph [
    schema_version="1",
    edge_types="null",
    max_components="20",
    max_nodes_per_component="20",
    n_components_total="5",
    n_components_returned="5",
    n_nodes_total="5",
    n_edges_total="3",
    n_internal_edges_total="0",
    n_cross_component_edges_total="3",
    n_self_loop_edges_total="0",
    n_cyclic_components_total="0",
    n_entity_nodes_total="5",
    n_endpoint_only_nodes_total="0",
    components_truncated="false",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  subgraph cluster_c0000 {
    label="A";
    component_id="c0000";
    representative="A";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="1";
    n_endpoint_only_nodes="0";
    is_cyclic="false";
    nodes_truncated="false";
    n0000 [label="A", title="A"];
  }
  subgraph cluster_c0001 {
    label="B";
    component_id="c0001";
    representative="B";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="1";
    n_endpoint_only_nodes="0";
    is_cyclic="false";
    nodes_truncated="false";
    n0001 [label="B", title="B"];
  }
  subgraph cluster_c0002 {
    label="M";
    component_id="c0002";
    representative="M";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="1";
    n_endpoint_only_nodes="0";
    is_cyclic="false";
    nodes_truncated="false";
    n0002 [label="M", title="M"];
  }
  subgraph cluster_c0003 {
    label="Z";
    component_id="c0003";
    representative="Z";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="1";
    n_endpoint_only_nodes="0";
    is_cyclic="false";
    nodes_truncated="false";
    n0003 [label="Z", title="Z"];
  }
  subgraph cluster_c0004 {
    label="é";
    component_id="c0004";
    representative="é";
    n_nodes_total="1";
    n_nodes_returned="1";
    n_internal_edges_total="0";
    n_self_loop_edges_total="0";
    n_entity_nodes="1";
    n_endpoint_only_nodes="0";
    is_cyclic="false";
    nodes_truncated="false";
    n0004 [label="é", title="é"];
  }
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


def _multi_entities() -> list[dict]:
    return [_entity("A"), _entity("B"), _entity("C"), _entity("Isolated")]


def _multi_rels() -> list[dict]:
    return [
        _calls("A", "B", hid=1),
        _calls("B", "C", hid=2),
        _calls("C", "A", hid=3),
        _calls("A", "ghost", hid=4, rid="rel:ghost"),
        _calls("X", "X", hid=5, rid="rel:selfx"),
        _calls("A", "B", hid=6, rid="rel:parallel"),
    ]


def _unicode_entities() -> list[dict]:
    return [_entity(title) for title in ("Z", "A", "M", "B", "é")]


def _unicode_rels() -> list[dict]:
    return [
        _calls("Z", "A", hid=1),
        _calls("A", "M", hid=2),
        _calls("B", "é", hid=3),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_strong_components_dot"
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


def _cluster_ids(dot: str) -> list[str]:
    return re.findall(r"subgraph (cluster_c\d{4}) \{", dot)


def _node_ids(dot: str) -> list[str]:
    return re.findall(r"\b(n\d{4}) \[", dot)


def _base_result(**overrides: object) -> dict:
    result: dict = {
        "edge_types": None,
        "max_components": 20,
        "max_nodes_per_component": 20,
        "components": [
            {
                "representative": "A",
                "nodes": ["A", "B", "C"],
                "n_nodes_total": 3,
                "n_nodes_returned": 3,
                "n_internal_edges_total": 4,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 3,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            {
                "representative": "X",
                "nodes": ["X"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 1,
                "n_self_loop_edges_total": 1,
                "n_entity_nodes": 0,
                "n_endpoint_only_nodes": 1,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            {
                "representative": "Isolated",
                "nodes": ["Isolated"],
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
                "representative": "ghost",
                "nodes": ["ghost"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 0,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 0,
                "n_endpoint_only_nodes": 1,
                "is_cyclic": False,
                "nodes_truncated": False,
            },
        ],
        "n_components_total": 4,
        "n_components_returned": 4,
        "n_nodes_total": 6,
        "n_edges_total": 6,
        "n_internal_edges_total": 5,
        "n_cross_component_edges_total": 1,
        "n_self_loop_edges_total": 1,
        "n_cyclic_components_total": 2,
        "n_entity_nodes_total": 4,
        "n_endpoint_only_nodes_total": 2,
        "components_truncated": False,
        "nodes_truncated": False,
    }
    result.update(copy.deepcopy(overrides))
    return result


def _empty_result(**overrides: object) -> dict:
    result = compute_strongly_connected_components(pd.DataFrame(), pd.DataFrame())
    result.update(copy.deepcopy(overrides))
    return result


def test_golden_cyclic_acyclic_unicode_and_hash_seed_independent():
    payload = dumps_strong_components_dot(_base_result())
    assert payload == GOLDEN_MULTI
    assert payload == dumps_strong_components_dot(_base_result())
    assert payload == direct_dumps(_base_result())
    assert payload.endswith("\n") and not payload.endswith("\n\n")
    assert payload.encode("utf-8").decode("utf-8") == payload
    assert (
        "schema_version="
        + quote_dot_string(str(STRONG_COMPONENTS_DOT_SCHEMA_VERSION))
        in payload
    )
    assert dumps_strong_components_dot(_empty_result()) == GOLDEN_EMPTY
    ents = pd.DataFrame(_unicode_entities())
    rels = pd.DataFrame(_unicode_rels())
    shuffled = dumps_strong_components_dot(
        compute_strongly_connected_components(
            ents.sample(frac=1, random_state=7).reset_index(drop=True),
            rels.sample(frac=1, random_state=11).reset_index(drop=True),
        )
    )
    assert shuffled == GOLDEN_UNICODE
    assert dumps_strong_components_dot(
        compute_strongly_connected_components(ents, rels)
    ) == GOLDEN_UNICODE
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_strongly_connected_components
from graphrag_code.strong_components_dot import dumps_strong_components_dot
ents = pd.DataFrame([
    {"id": "eA", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eB", "title": "B", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eC", "title": "C", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eI", "title": "Isolated", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "r1", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r2", "source": "B", "target": "C", "type": "calls", "extractor": "x"},
    {"id": "r3", "source": "C", "target": "A", "type": "calls", "extractor": "x"},
    {"id": "r4", "source": "A", "target": "ghost", "type": "calls", "extractor": "x"},
    {"id": "r5", "source": "X", "target": "X", "type": "calls", "extractor": "x"},
    {"id": "r6", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
])
print(dumps_strong_components_dot(compute_strongly_connected_components(ents, rels)), end="")
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
    assert payloads[0] == payloads[1] == payloads[2] == GOLDEN_MULTI


def test_live_graph_preserves_clusters_ids_metadata_and_has_no_edges(tmp_path: Path):
    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    result = ByogGraph(graph).strong_components()
    payload = dumps_strong_components_dot(result)
    assert payload == GOLDEN_MULTI
    assert _cluster_ids(payload) == [
        "cluster_c0000",
        "cluster_c0001",
        "cluster_c0002",
        "cluster_c0003",
    ]
    assert _node_ids(payload) == [
        "n0000",
        "n0001",
        "n0002",
        "n0003",
        "n0004",
        "n0005",
    ]
    assert 'is_cyclic="true"' in payload
    assert 'is_cyclic="false"' in payload
    assert 'n_self_loop_edges_total="1"' in payload
    assert 'n_cross_component_edges_total="1"' in payload
    structural = _structural(payload)
    assert " -> " not in structural
    assert " -- " not in structural
    leaky = _base_result()
    leaky["secret"] = "should-not-appear"
    leaky["components"][0] = {
        **leaky["components"][0],
        "secret": "component-secret",
    }
    rendered = dumps_strong_components_dot(leaky)
    assert "should-not-appear" not in rendered
    assert "component-secret" not in rendered
    assert "secret" not in rendered


def test_truncation_empty_and_cyclic_kinds(tmp_path: Path):
    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    g = ByogGraph(graph)
    components = dumps_strong_components_dot(g.strong_components(max_components=1))
    assert 'n_components_total="4"' in components
    assert 'n_components_returned="1"' in components
    assert 'components_truncated="true"' in components
    assert 'nodes_truncated="false"' in components
    assert 'n_cyclic_components_total="2"' in components
    assert _cluster_ids(components) == ["cluster_c0000"]
    assert _node_ids(components) == ["n0000", "n0001", "n0002"]
    assert "cluster_c0001" not in components
    assert "Isolated" not in components
    assert " -> " not in components

    nodes = dumps_strong_components_dot(g.strong_components(max_nodes_per_component=1))
    assert 'nodes_truncated="true"' in nodes
    assert 'components_truncated="false"' in nodes
    assert _cluster_ids(nodes) == [
        "cluster_c0000",
        "cluster_c0001",
        "cluster_c0002",
        "cluster_c0003",
    ]
    assert _node_ids(nodes) == ["n0000", "n0001", "n0002", "n0003"]
    assert 'n_nodes_total="3"' in nodes
    assert 'n_nodes_returned="1"' in nodes
    assert 'is_cyclic="true"' in nodes
    assert "B" not in _structural(nodes)
    empty = dumps_strong_components_dot(
        compute_strongly_connected_components(pd.DataFrame(), pd.DataFrame())
    )
    assert empty == GOLDEN_EMPTY
    assert "subgraph" not in empty
    assert "n0000" not in empty


def test_escaping_injection_controls_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "z /* comment */ // still"
    result = _base_result(
        components=[
            {
                "representative": nasty,
                "nodes": [nasty, comment],
                "n_nodes_total": 2,
                "n_nodes_returned": 2,
                "n_internal_edges_total": 4,
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
                "n_internal_edges_total": 1,
                "n_self_loop_edges_total": 1,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
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
        n_components_total=3,
        n_components_returned=3,
        n_nodes_total=4,
        n_edges_total=6,
        n_internal_edges_total=5,
        n_entity_nodes_total=4,
        n_endpoint_only_nodes_total=0,
    )
    payload = dumps_strong_components_dot(result)
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert "n9999" not in structural
    assert "/*" not in structural
    assert "//" not in structural
    assert nasty not in structural
    assert " -> " not in structural
    assert " -- " not in structural
    assert _cluster_ids(payload) == ["cluster_c0000", "cluster_c0001", "cluster_c0002"]
    assert "\\n" in payload and "\t" not in payload
    assert quote_dot_string(nasty) in payload
    controls = _base_result(
        components=[
            {
                "representative": "A\x01B",
                "nodes": ["A\x01B", "mid\x9f"],
                "n_nodes_total": 2,
                "n_nodes_returned": 2,
                "n_internal_edges_total": 4,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 2,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            {
                "representative": "C\x7fD",
                "nodes": ["C\x7fD"],
                "n_nodes_total": 1,
                "n_nodes_returned": 1,
                "n_internal_edges_total": 1,
                "n_self_loop_edges_total": 1,
                "n_entity_nodes": 1,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            {
                "representative": "Isolated\x85",
                "nodes": ["Isolated\x85"],
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
        n_components_total=3,
        n_components_returned=3,
        n_nodes_total=4,
        n_entity_nodes_total=4,
        n_endpoint_only_nodes_total=0,
        n_cyclic_components_total=2,
    )
    controlled = dumps_strong_components_dot(controls)
    assert "\\x01" in controlled
    assert "\\x9f" in controlled
    assert "\\x7f" in controlled
    assert "\\x85" in controlled
    assert "\x01" not in controlled
    assert "\x7f" not in controlled
    cr = _base_result(
        components=[
            {
                "representative": "A\rB",
                "nodes": ["A\rB", "B", "C"],
                "n_nodes_total": 3,
                "n_nodes_returned": 3,
                "n_internal_edges_total": 4,
                "n_self_loop_edges_total": 0,
                "n_entity_nodes": 3,
                "n_endpoint_only_nodes": 0,
                "is_cyclic": True,
                "nodes_truncated": False,
            },
            *_base_result()["components"][1:],
        ]
    )
    cr_payload = dumps_strong_components_dot(cr)
    assert "\\r" in cr_payload
    assert "\r" not in cr_payload


def test_missing_malformed_is_cyclic_and_lone_surrogate_fail_closed():
    for name in (
        "edge_types",
        "max_components",
        "max_nodes_per_component",
        "components",
        "n_components_total",
        "n_components_returned",
        "n_nodes_total",
        "n_edges_total",
        "n_internal_edges_total",
        "n_cross_component_edges_total",
        "n_self_loop_edges_total",
        "n_cyclic_components_total",
        "n_entity_nodes_total",
        "n_endpoint_only_nodes_total",
        "components_truncated",
        "nodes_truncated",
    ):
        missing = _base_result()
        del missing[name]
        with pytest.raises(StrongComponentsDotError, match="missing"):
            dumps_strong_components_dot(missing)
    with pytest.raises(StrongComponentsDotError):
        dumps_strong_components_dot("not-a-mapping")  # type: ignore[arg-type]
    surrogate = _base_result()
    surrogate["components"][2] = {
        **surrogate["components"][2],
        "representative": "\ud800",
        "nodes": ["\ud800"],
    }
    with pytest.raises(StrongComponentsDotError, match="UTF-8"):
        dumps_strong_components_dot(surrogate)
    duplicate = _base_result()
    duplicate["components"][2] = {
        **duplicate["components"][0],
        "nodes": ["A", "Z"],
        "n_nodes_total": 2,
        "n_nodes_returned": 2,
        "n_internal_edges_total": 0,
        "n_self_loop_edges_total": 0,
        "n_entity_nodes": 2,
        "n_endpoint_only_nodes": 0,
        "is_cyclic": True,
    }
    with pytest.raises(StrongComponentsDotError, match="duplicate"):
        dumps_strong_components_dot(duplicate)
    out_of_order = _base_result(
        components=list(reversed(_base_result()["components"]))
    )
    with pytest.raises(StrongComponentsDotError, match="canonical producer order"):
        dumps_strong_components_dot(out_of_order)
    bad_cyclic = _base_result()
    bad_cyclic["components"][0] = {
        **bad_cyclic["components"][0],
        "is_cyclic": False,
    }
    with pytest.raises(StrongComponentsDotError, match="is_cyclic"):
        dumps_strong_components_dot(bad_cyclic)
    bad_self = _base_result()
    bad_self["components"][1] = {
        **bad_self["components"][1],
        "is_cyclic": False,
    }
    with pytest.raises(StrongComponentsDotError, match="is_cyclic"):
        dumps_strong_components_dot(bad_self)
    fake_cyclic = _base_result()
    fake_cyclic["components"][2] = {
        **fake_cyclic["components"][2],
        "is_cyclic": True,
    }
    with pytest.raises(StrongComponentsDotError, match="is_cyclic"):
        dumps_strong_components_dot(fake_cyclic)
    wrong_trunc_cap = _base_result()
    wrong_trunc_cap["max_nodes_per_component"] = 2
    wrong_trunc_cap["nodes_truncated"] = True
    wrong_trunc_cap["components"][0] = {
        **wrong_trunc_cap["components"][0],
        "nodes": ["A"],
        "n_nodes_returned": 1,
        "nodes_truncated": True,
    }
    with pytest.raises(StrongComponentsDotError, match="exactly max_nodes_per_component"):
        dumps_strong_components_dot(wrong_trunc_cap)
    for invalid in (
        _base_result(n_components_returned=2),
        _base_result(components_truncated=True),
        _base_result(nodes_truncated=True),
        _base_result(max_components=1),
        _base_result(max_components=True),
        _base_result(n_internal_edges_total=1.5),
        _base_result(n_nodes_total=math.nan),
        _base_result(max_components=HARD_MAX_STRONG_COMPONENTS + 1),
        _base_result(max_nodes_per_component=HARD_MAX_STRONG_COMPONENT_NODES + 1),
        _base_result(n_edges_total=-1),
        _base_result(n_internal_edges_total=3, n_cross_component_edges_total=1),
        _base_result(n_self_loop_edges_total=9),
        _base_result(n_cyclic_components_total=9),
        _empty_result(n_nodes_total=1),
        _empty_result(components_truncated=True),
        _base_result(edge_types=[]),
        _base_result(edge_types=["uses_type", "calls"]),
        _base_result(n_internal_edges_total=4),
    ):
        with pytest.raises(StrongComponentsDotError):
            dumps_strong_components_dot(invalid)


def test_truncated_aggregate_invariants_fail_closed():
    truncated = _base_result(
        max_components=1,
        components=_base_result()["components"][:1],
        n_components_returned=1,
        components_truncated=True,
    )
    impossible_nodes = copy.deepcopy(truncated)
    impossible_nodes.update(
        n_nodes_total=4,
        n_entity_nodes_total=3,
        n_endpoint_only_nodes_total=1,
        n_cyclic_components_total=2,
    )
    with pytest.raises(StrongComponentsDotError, match="omitted component"):
        dumps_strong_components_dot(impossible_nodes)
    too_many_internal = copy.deepcopy(truncated)
    too_many_internal.update(n_internal_edges_total=3, n_edges_total=4)
    with pytest.raises(StrongComponentsDotError, match="exceed"):
        dumps_strong_components_dot(too_many_internal)
    uncovered = _base_result(n_cyclic_components_total=3)
    with pytest.raises(StrongComponentsDotError, match="cover n_cyclic"):
        dumps_strong_components_dot(uncovered)


def test_edge_types_none_literal_all_and_commas():
    unfiltered = dumps_strong_components_dot(_base_result(edge_types=None))
    literal_all = dumps_strong_components_dot(_base_result(edge_types=["all"]))
    comma_types = dumps_strong_components_dot(_base_result(edge_types=["a,b", "c"]))
    split_types = dumps_strong_components_dot(_base_result(edge_types=["a", "b,c"]))
    assert 'edge_types="null"' in unfiltered
    assert 'edge_types="[\\"all\\"]"' in literal_all
    assert 'edge_types="[\\"a,b\\",\\"c\\"]"' in comma_types
    assert 'edge_types="[\\"a\\",\\"b,c\\"]"' in split_types
    assert len({unfiltered, literal_all, comma_types, split_types}) == 4


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_strong_components_dot(_base_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.strong_components_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_STRONG_COMPONENTS_DOT_BYTES", size)
    assert dumps_strong_components_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_STRONG_COMPONENTS_DOT_BYTES", size + 1)
    assert dumps_strong_components_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_STRONG_COMPONENTS_DOT_BYTES", size - 1)
    with pytest.raises(StrongComponentsDotError, match="hard limit"):
        dumps_strong_components_dot(_base_result())
    assert HARD_MAX_STRONG_COMPONENTS_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    common = ["strong-components", "--graph", str(graph)]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_strong_components_json(payload) + "\n" == json_out.stdout
    assert format_strong_components_human(payload).strip() == human.stdout.strip()
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
    import graphrag_code.strong_components_dot as dot_mod

    def boom(*_a, **_k):
        raise AssertionError("graph observed before json/dot rejection")

    monkeypatch.setattr(gq, "_scoped_graph", boom)
    rejected = CliRunner().invoke(
        app, ["strong-components", "--graph", str(graph), "--json", "--dot"]
    )
    assert rejected.exit_code == 2
    assert rejected.stdout == ""
    monkeypatch.undo()
    monkeypatch.setattr(dot_mod, "HARD_MAX_STRONG_COMPONENTS_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["strong-components", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "strong-components", "--help"],
        [sys.executable, str(CLI), "strong-components", "--help"],
        [
            sys.executable,
            "-m",
            "graphrag_code.graph_query",
            "strong-components",
            "--help",
        ],
        [sys.executable, "-m", "graphrag_code", "strong-components", "--help"],
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

    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    args = ["strong-components", "--graph", str(graph), "--dot"]
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
    expected = dumps_strong_components_dot(ByogGraph(graph).strong_components())
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed.stdout
        == expected
        == GOLDEN_MULTI
    )
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not (graph / ".publish.lock").is_symlink()


def test_invalid_graph_snapshot_and_unsafe_lease_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "strong-components",
        "--graph",
        str(tmp_path / "missing"),
        "--dot",
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    bad_snap = _run(
        sys.executable,
        str(QUERY),
        "strong-components",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        "--dot",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == ""
    bad_max = _run(
        sys.executable,
        str(QUERY),
        "strong-components",
        "--graph",
        str(graph),
        "--max-components",
        "0",
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
        "strong-components",
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
        "strong-components",
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
        "strong-components",
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
        "strong-components",
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


def _strong_components_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_strong_components_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_strong_components_dot = wrap_dumps

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
            ["strong-components", "--graph", graph, "--dot"],
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
    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_strong_components_dot_hold, args=(str(graph), held, resume, q)
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
    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    g = ByogGraph(graph)
    calls: list[str] = []
    orig = g.strong_components

    def wrapped(*args, **kwargs):
        calls.append("strong_components")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "strong_components", wrapped)
    for name in (
        "condensation",
        "components",
        "shortest_path",
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
    result = g.strong_components()
    assert calls == ["strong_components"]
    dumps_strong_components_dot(result)
    assert calls == ["strong_components"]

    serializer_calls = 0
    orig_dumps = dumps_strong_components_dot

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner

    monkeypatch.setattr(gq, "dumps_strong_components_dot", counted_dumps)
    invoked = CliRunner().invoke(
        gq.app, ["strong-components", "--graph", str(graph), "--dot"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert serializer_calls == 1
    assert invoked.stdout == GOLDEN_MULTI

    dot_src = (ROOT / "src" / "graphrag_code" / "strong_components_dot.py").read_text(
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
    strong_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_strong_components":
            strong_fn = ast.get_source_segment(gq_src, node)
    assert strong_fn is not None
    assert "subprocess" not in strong_fn
    assert "compute_strongly_connected_components" not in strong_fn
    assert ".condensation(" not in strong_fn
    assert "networkx" not in strong_fn
    assert strong_fn.count(".strong_components(") == 1
    assert strong_fn.count("dumps_strong_components_dot(") == 1
    assert "sys.stdout.write(dumps_strong_components_dot(result))" in strong_fn
    assert "sys.stdout.flush()" in strong_fn
    write_at = strong_fn.find("sys.stdout.write(dumps_strong_components_dot(result))")
    flush_at = strong_fn.find("sys.stdout.flush()")
    with_at = strong_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < flush_at
    assert not list(tmp_path.glob("*.dot"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_mcp_remains_seventeen_tools_without_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, _multi_entities(), _multi_rels())
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.strong_components).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == [
        "self",
        "max_components",
        "max_nodes_per_component",
        "edge_types",
        "snapshot",
    ]
    assert list(TOOL_NAMES) == [
        "graph_status",
        "graph_doctor",
        "query_symbol",
        "observations",
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
        "impact_graph",
        "type_closure",
        "context_pack",
        "snapshot_history",
        "snapshot_diff",
    ]
    assert len(TOOL_NAMES) == 19

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 19
            tool = next(item for item in tools if item.name == "strong_components")
            props = tool.input_schema.get("properties") or {}
            assert "dot" not in props
            assert "format" not in props
            assert list(props) == [
                "max_components",
                "max_nodes_per_component",
                "edge_types",
                "snapshot",
            ]
            result = await client.call_tool("strong_components", {})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "strong_components"
            dumped = json.dumps(body)
            assert "digraph" not in dumped
            assert "graphrag_strong_components" not in dumped
            assert "--dot" not in dumped

    anyio_run(_body)
