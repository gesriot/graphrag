"""Deterministic Graphviz DOT export for the bounded degree-ranking CLI.

Renders the existing ByogGraph.degree_ranking result. Does not add a
Graphviz runtime, a second ranking algorithm, an MCP format parameter, or
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
    HARD_MAX_DEGREE_RANKING_NODES,
    PUBLICATION_LOCK_NAME,
    ByogGraph,
    compute_structural_degree_ranking,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_degree_ranking_dot,
    dumps_degree_ranking_json,
    format_degree_ranking_human,
)
from graphrag_code.degree_ranking_dot import (  # type: ignore
    DEGREE_RANKING_DOT_SCHEMA_VERSION,
    HARD_MAX_DEGREE_RANKING_DOT_BYTES,
    DegreeRankingDotError,
    dumps_degree_ranking_dot as direct_dumps,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore

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

GOLDEN_TOTAL = """\
digraph graphrag_degree_ranking {
  graph [
    schema_version="1",
    rank_by="total",
    edge_types="null",
    max_nodes="20",
    n_nodes_total="5",
    n_nodes_returned="5",
    n_edges_total="5",
    n_entity_nodes_total="4",
    n_endpoint_only_nodes_total="1",
    sum_in_degree="5",
    sum_out_degree="5",
    sum_total_degree="10",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_degree="1", out_degree="3", total_degree="4", is_entity="true"];
  n0001 [label="B", title="B", in_degree="2", out_degree="1", total_degree="3", is_entity="true"];
  n0002 [label="Isolated", title="Isolated", in_degree="1", out_degree="0", total_degree="1", is_entity="true"];
  n0003 [label="ghost", title="ghost", in_degree="1", out_degree="0", total_degree="1", is_entity="false"];
  n0004 [label="C", title="C", in_degree="0", out_degree="1", total_degree="1", is_entity="true"];
}
"""

GOLDEN_INCOMING = """\
digraph graphrag_degree_ranking {
  graph [
    schema_version="1",
    rank_by="incoming",
    edge_types="null",
    max_nodes="20",
    n_nodes_total="5",
    n_nodes_returned="5",
    n_edges_total="5",
    n_entity_nodes_total="4",
    n_endpoint_only_nodes_total="1",
    sum_in_degree="5",
    sum_out_degree="5",
    sum_total_degree="10",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="B", title="B", in_degree="2", out_degree="1", total_degree="3", is_entity="true"];
  n0001 [label="A", title="A", in_degree="1", out_degree="3", total_degree="4", is_entity="true"];
  n0002 [label="Isolated", title="Isolated", in_degree="1", out_degree="0", total_degree="1", is_entity="true"];
  n0003 [label="ghost", title="ghost", in_degree="1", out_degree="0", total_degree="1", is_entity="false"];
  n0004 [label="C", title="C", in_degree="0", out_degree="1", total_degree="1", is_entity="true"];
}
"""

GOLDEN_OUTGOING = """\
digraph graphrag_degree_ranking {
  graph [
    schema_version="1",
    rank_by="outgoing",
    edge_types="null",
    max_nodes="20",
    n_nodes_total="5",
    n_nodes_returned="5",
    n_edges_total="5",
    n_entity_nodes_total="4",
    n_endpoint_only_nodes_total="1",
    sum_in_degree="5",
    sum_out_degree="5",
    sum_total_degree="10",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", in_degree="1", out_degree="3", total_degree="4", is_entity="true"];
  n0001 [label="B", title="B", in_degree="2", out_degree="1", total_degree="3", is_entity="true"];
  n0002 [label="C", title="C", in_degree="0", out_degree="1", total_degree="1", is_entity="true"];
  n0003 [label="Isolated", title="Isolated", in_degree="1", out_degree="0", total_degree="1", is_entity="true"];
  n0004 [label="ghost", title="ghost", in_degree="1", out_degree="0", total_degree="1", is_entity="false"];
}
"""

GOLDEN_EMPTY = """\
digraph graphrag_degree_ranking {
  graph [
    schema_version="1",
    rank_by="total",
    edge_types="null",
    max_nodes="20",
    n_nodes_total="0",
    n_nodes_returned="0",
    n_edges_total="0",
    n_entity_nodes_total="0",
    n_endpoint_only_nodes_total="0",
    sum_in_degree="0",
    sum_out_degree="0",
    sum_total_degree="0",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_UNICODE = """\
digraph graphrag_degree_ranking {
  graph [
    schema_version="1",
    rank_by="total",
    edge_types="null",
    max_nodes="20",
    n_nodes_total="4",
    n_nodes_returned="4",
    n_edges_total="3",
    n_entity_nodes_total="4",
    n_endpoint_only_nodes_total="0",
    sum_in_degree="3",
    sum_out_degree="3",
    sum_total_degree="6",
    nodes_truncated="false"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="Z", title="Z", in_degree="2", out_degree="0", total_degree="2", is_entity="true"];
  n0001 [label="A", title="A", in_degree="0", out_degree="2", total_degree="2", is_entity="true"];
  n0002 [label="M", title="M", in_degree="1", out_degree="0", total_degree="1", is_entity="true"];
  n0003 [label="é", title="é", in_degree="0", out_degree="1", total_degree="1", is_entity="true"];
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


def _fixture_entities() -> list[dict]:
    return [_entity("A"), _entity("B"), _entity("C"), _entity("Isolated")]


def _fixture_rels() -> list[dict]:
    return [
        _calls("A", "B", hid=1),
        _calls("A", "B", hid=2, rid="rel:parallel"),
        _calls("A", "A", hid=3, rid="rel:self"),
        _calls("B", "ghost", hid=4, rid="rel:endpoint"),
        _rel("C", "Isolated", "contains", hid=5),
    ]


def _unicode_entities() -> list[dict]:
    return [_entity(title) for title in ("Z", "A", "é", "M")]


def _unicode_rels() -> list[dict]:
    return [
        _calls("A", "Z", hid=1),
        _calls("A", "Z", hid=2, rid="rel:az-2"),
        _calls("é", "M", hid=3),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_degree_ranking_dot"
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
    return re.findall(r"\b(n\d{4}) \[", dot)


def _graph_attr_names(dot: str) -> list[str]:
    block = GRAPH_ATTR_RE.search(dot)
    assert block is not None
    return re.findall(r"^\s+([A-Za-z_][A-Za-z0-9_]*)=", block.group(1), re.MULTILINE)


def _node_attr_names(dot: str) -> list[list[str]]:
    return [re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", attrs) for attrs in NODE_ATTR_RE.findall(dot)]


def _base_result(**overrides: object) -> dict:
    ents = pd.DataFrame(_fixture_entities())
    rels = pd.DataFrame(_fixture_rels())
    result = compute_structural_degree_ranking(ents, rels)
    result.update(copy.deepcopy(overrides))
    return result


def _empty_result(**overrides: object) -> dict:
    result = compute_structural_degree_ranking(pd.DataFrame(), pd.DataFrame())
    result.update(copy.deepcopy(overrides))
    return result


def test_golden_modes_ties_unicode_and_hash_seed_independent():
    ents = pd.DataFrame(_fixture_entities())
    rels = pd.DataFrame(_fixture_rels())
    total = dumps_degree_ranking_dot(_base_result())
    incoming = dumps_degree_ranking_dot(
        compute_structural_degree_ranking(ents, rels, rank_by="incoming")
    )
    outgoing = dumps_degree_ranking_dot(
        compute_structural_degree_ranking(ents, rels, rank_by="outgoing")
    )
    assert total == GOLDEN_TOTAL
    assert incoming == GOLDEN_INCOMING
    assert outgoing == GOLDEN_OUTGOING
    assert total == dumps_degree_ranking_dot(_base_result())
    assert total == direct_dumps(_base_result())
    assert total.endswith("\n") and not total.endswith("\n\n")
    assert total.encode("utf-8").decode("utf-8") == total
    assert (
        "schema_version="
        + quote_dot_string(str(DEGREE_RANKING_DOT_SCHEMA_VERSION))
        in total
    )
    assert dumps_degree_ranking_dot(_empty_result()) == GOLDEN_EMPTY
    uents = pd.DataFrame(_unicode_entities())
    urels = pd.DataFrame(_unicode_rels())
    shuffled = dumps_degree_ranking_dot(
        compute_structural_degree_ranking(
            uents.sample(frac=1, random_state=7).reset_index(drop=True),
            urels.sample(frac=1, random_state=11).reset_index(drop=True),
        )
    )
    assert shuffled == GOLDEN_UNICODE
    assert dumps_degree_ranking_dot(
        compute_structural_degree_ranking(uents, urels)
    ) == GOLDEN_UNICODE
    assert [
        node["title"]
        for node in compute_structural_degree_ranking(
            uents, urels, rank_by="incoming"
        )["nodes"]
    ] == ["Z", "M", "A", "é"]
    assert [
        node["title"]
        for node in compute_structural_degree_ranking(
            uents, urels, rank_by="outgoing"
        )["nodes"]
    ] == ["A", "é", "Z", "M"]
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_structural_degree_ranking
from graphrag_code.degree_ranking_dot import dumps_degree_ranking_dot
ents = pd.DataFrame([
    {"id": "eA", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eB", "title": "B", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eC", "title": "C", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eI", "title": "Isolated", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "r1", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r2", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r3", "source": "A", "target": "A", "type": "calls", "extractor": "x"},
    {"id": "r4", "source": "B", "target": "ghost", "type": "calls", "extractor": "x"},
    {"id": "r5", "source": "C", "target": "Isolated", "type": "contains", "extractor": "x"},
])
print(dumps_degree_ranking_dot(compute_structural_degree_ranking(ents, rels)), end="")
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
    assert payloads[0] == payloads[1] == payloads[2] == GOLDEN_TOTAL


def test_live_graph_metadata_order_no_edges_and_ids_are_not_ranks(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    result = ByogGraph(graph).degree_ranking()
    payload = dumps_degree_ranking_dot(result)
    assert payload == GOLDEN_TOTAL
    assert _node_ids(payload) == ["n0000", "n0001", "n0002", "n0003", "n0004"]
    assert _graph_attr_names(payload) == [
        "schema_version",
        "rank_by",
        "edge_types",
        "max_nodes",
        "n_nodes_total",
        "n_nodes_returned",
        "n_edges_total",
        "n_entity_nodes_total",
        "n_endpoint_only_nodes_total",
        "sum_in_degree",
        "sum_out_degree",
        "sum_total_degree",
        "nodes_truncated",
    ]
    for attrs in _node_attr_names(payload):
        assert attrs == [
            "label",
            "title",
            "in_degree",
            "out_degree",
            "total_degree",
            "is_entity",
        ]
        assert "rank" not in attrs
    structural = _structural(payload)
    assert " -> " not in structural
    assert " -- " not in structural
    assert "invis" not in structural
    assert "constraint=" not in structural
    assert "{ rank" not in structural
    assert "rank=same" not in payload
    assert 'in_degree="1"' in payload
    assert 'out_degree="3"' in payload
    assert 'total_degree="4"' in payload
    assert 'is_entity="false"' in payload
    leaky = _base_result()
    leaky["secret"] = "should-not-appear"
    leaky["nodes"][0] = {**leaky["nodes"][0], "secret": "node-secret", "rank": 1}
    rendered = dumps_degree_ranking_dot(leaky)
    assert "should-not-appear" not in rendered
    assert "node-secret" not in rendered
    assert 'rank="1"' not in rendered
    src = (ROOT / "src" / "graphrag_code" / "degree_ranking_dot.py").read_text(
        encoding="utf-8"
    )
    assert "not a claim that a rendered Graphviz layout will" in src
    assert "serialization identifiers only" in src


def test_truncation_empty_filter_self_loop_and_endpoint(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    truncated = dumps_degree_ranking_dot(g.degree_ranking(max_nodes=2))
    assert 'n_nodes_total="5"' in truncated
    assert 'n_nodes_returned="2"' in truncated
    assert 'nodes_truncated="true"' in truncated
    assert 'max_nodes="2"' in truncated
    assert _node_ids(truncated) == ["n0000", "n0001"]
    assert 'title="A"' in truncated
    assert 'title="B"' in truncated
    assert "Isolated" not in truncated
    assert "ghost" not in truncated
    assert " -> " not in truncated
    assert 'sum_in_degree="5"' in truncated
    assert 'n_edges_total="5"' in truncated
    incoming = dumps_degree_ranking_dot(
        g.degree_ranking(rank_by="incoming", max_nodes=2)
    )
    assert _node_ids(incoming) == ["n0000", "n0001"]
    assert 'title="B"' in incoming.split("n0000", 1)[1].split("n0001", 1)[0]
    assert 'title="A"' in incoming.split("n0001", 1)[1]
    calls = dumps_degree_ranking_dot(g.degree_ranking(edge_types=["calls"]))
    assert 'edge_types="[\\"calls\\"]"' in calls
    assert 'n_edges_total="4"' in calls
    assert 'title="ghost"' in calls
    assert 'title="Isolated"' in calls
    contains = dumps_degree_ranking_dot(g.degree_ranking(edge_types=["contains"]))
    assert "ghost" not in contains
    assert 'n_endpoint_only_nodes_total="0"' in contains
    empty = dumps_degree_ranking_dot(
        compute_structural_degree_ranking(pd.DataFrame(), pd.DataFrame())
    )
    assert empty == GOLDEN_EMPTY
    assert "n0000" not in empty
    self_loop = {node["title"]: node for node in g.degree_ranking()["nodes"]}["A"]
    assert self_loop["in_degree"] == 1
    assert self_loop["out_degree"] == 3
    assert self_loop["total_degree"] == 4


def test_escaping_injection_controls_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "z /* comment */ // still"
    result = _base_result()
    result["nodes"] = [
        {
            "title": nasty,
            "in_degree": 3,
            "out_degree": 3,
            "total_degree": 6,
            "is_entity": True,
        },
        {
            "title": comment,
            "in_degree": 1,
            "out_degree": 1,
            "total_degree": 2,
            "is_entity": True,
        },
        {
            "title": "é☃\n\t\\",
            "in_degree": 1,
            "out_degree": 0,
            "total_degree": 1,
            "is_entity": True,
        },
        {
            "title": "comma,value",
            "in_degree": 0,
            "out_degree": 1,
            "total_degree": 1,
            "is_entity": False,
        },
    ]
    result.update(
        n_nodes_total=4,
        n_nodes_returned=4,
        n_edges_total=5,
        n_entity_nodes_total=3,
        n_endpoint_only_nodes_total=1,
        sum_in_degree=5,
        sum_out_degree=5,
        sum_total_degree=10,
        nodes_truncated=False,
    )
    payload = dumps_degree_ranking_dot(result)
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert "n9999" not in structural
    assert "/*" not in structural
    assert "//" not in structural
    assert nasty not in structural
    assert " -> " not in structural
    assert " -- " not in structural
    assert _node_ids(payload) == ["n0000", "n0001", "n0002", "n0003"]
    assert "\\n" in payload and "\t" not in payload
    assert quote_dot_string(nasty) in payload
    controls = _base_result()
    controls["nodes"] = [
        {
            "title": "A\x01B",
            "in_degree": 2,
            "out_degree": 2,
            "total_degree": 4,
            "is_entity": True,
        },
        {
            "title": "mid\x9f",
            "in_degree": 2,
            "out_degree": 1,
            "total_degree": 3,
            "is_entity": True,
        },
        {
            "title": "C\x7fD",
            "in_degree": 1,
            "out_degree": 1,
            "total_degree": 2,
            "is_entity": True,
        },
        {
            "title": "Isolated\x85",
            "in_degree": 0,
            "out_degree": 1,
            "total_degree": 1,
            "is_entity": False,
        },
    ]
    controls.update(
        n_nodes_total=4,
        n_nodes_returned=4,
        n_edges_total=5,
        n_entity_nodes_total=3,
        n_endpoint_only_nodes_total=1,
        sum_in_degree=5,
        sum_out_degree=5,
        sum_total_degree=10,
    )
    controlled = dumps_degree_ranking_dot(controls)
    assert "\\x01" in controlled
    assert "\\x9f" in controlled
    assert "\\x7f" in controlled
    assert "\\x85" in controlled
    assert "\x01" not in controlled
    assert "\x7f" not in controlled
    cr = _base_result()
    cr["nodes"][0] = {**cr["nodes"][0], "title": "A\rB"}
    cr_payload = dumps_degree_ranking_dot(cr)
    assert "\\r" in cr_payload
    assert "\r" not in cr_payload


def test_missing_malformed_rank_by_order_sums_and_lone_surrogate_fail_closed():
    for name in (
        "rank_by",
        "edge_types",
        "max_nodes",
        "nodes",
        "n_nodes_total",
        "n_nodes_returned",
        "n_edges_total",
        "n_entity_nodes_total",
        "n_endpoint_only_nodes_total",
        "sum_in_degree",
        "sum_out_degree",
        "sum_total_degree",
        "nodes_truncated",
    ):
        missing = _base_result()
        del missing[name]
        with pytest.raises(DegreeRankingDotError, match="missing"):
            dumps_degree_ranking_dot(missing)
    with pytest.raises(DegreeRankingDotError):
        dumps_degree_ranking_dot("not-a-mapping")  # type: ignore[arg-type]
    for rank_by in ("Total", "pagerank", " total", "incoming ", "", None, True, 1):
        with pytest.raises(DegreeRankingDotError, match="rank_by"):
            dumps_degree_ranking_dot(_base_result(rank_by=rank_by))
    surrogate = _base_result()
    surrogate["nodes"][2] = {**surrogate["nodes"][2], "title": "\ud800"}
    with pytest.raises(DegreeRankingDotError, match="UTF-8"):
        dumps_degree_ranking_dot(surrogate)
    duplicate = _base_result()
    duplicate["nodes"][2] = {**duplicate["nodes"][2], "title": "A"}
    with pytest.raises(DegreeRankingDotError, match="duplicate"):
        dumps_degree_ranking_dot(duplicate)
    out_of_order = _base_result(
        nodes=list(reversed(_base_result()["nodes"]))
    )
    with pytest.raises(DegreeRankingDotError, match="canonical producer order"):
        dumps_degree_ranking_dot(out_of_order)
    incoming_wrong = compute_structural_degree_ranking(
        pd.DataFrame(_fixture_entities()),
        pd.DataFrame(_fixture_rels()),
        rank_by="incoming",
    )
    incoming_wrong["nodes"] = list(reversed(incoming_wrong["nodes"]))
    with pytest.raises(DegreeRankingDotError, match="canonical producer order"):
        dumps_degree_ranking_dot(incoming_wrong)
    outgoing_wrong = compute_structural_degree_ranking(
        pd.DataFrame(_fixture_entities()),
        pd.DataFrame(_fixture_rels()),
        rank_by="outgoing",
    )
    outgoing_wrong["nodes"] = list(reversed(outgoing_wrong["nodes"]))
    with pytest.raises(DegreeRankingDotError, match="canonical producer order"):
        dumps_degree_ranking_dot(outgoing_wrong)
    bad_total = _base_result()
    bad_total["nodes"][0] = {**bad_total["nodes"][0], "total_degree": 99}
    with pytest.raises(DegreeRankingDotError, match="total_degree"):
        dumps_degree_ranking_dot(bad_total)
    missing_node_field = _base_result()
    missing_node_field["nodes"][0] = {
        key: value
        for key, value in missing_node_field["nodes"][0].items()
        if key != "is_entity"
    }
    with pytest.raises(DegreeRankingDotError, match="missing node field"):
        dumps_degree_ranking_dot(missing_node_field)
    for invalid in (
        _base_result(n_nodes_returned=2),
        _base_result(nodes_truncated=True),
        _base_result(max_nodes=1),
        _base_result(max_nodes=True),
        _base_result(n_edges_total=1.5),
        _base_result(n_nodes_total=math.nan),
        _base_result(max_nodes=HARD_MAX_DEGREE_RANKING_NODES + 1),
        _base_result(n_edges_total=-1),
        _base_result(sum_in_degree=4),
        _base_result(sum_out_degree=4),
        _base_result(sum_total_degree=9),
        _base_result(n_entity_nodes_total=3),
        _empty_result(n_nodes_total=1, n_entity_nodes_total=1),
        _empty_result(nodes_truncated=True),
        _base_result(edge_types=[]),
        _base_result(edge_types=["uses_type", "calls"]),
        _base_result(n_endpoint_only_nodes_total=0),
    ):
        with pytest.raises(DegreeRankingDotError):
            dumps_degree_ranking_dot(invalid)


def test_truncated_aggregate_invariants_fail_closed():
    truncated = _base_result(
        max_nodes=2,
        nodes=_base_result()["nodes"][:2],
        n_nodes_returned=2,
        nodes_truncated=True,
    )
    assert dumps_degree_ranking_dot(truncated).count("n000") == 2
    too_many_in = copy.deepcopy(truncated)
    too_many_in.update(
        n_edges_total=2,
        sum_in_degree=2,
        sum_out_degree=2,
        sum_total_degree=4,
    )
    with pytest.raises(DegreeRankingDotError, match="exceeds sum_in_degree"):
        dumps_degree_ranking_dot(too_many_in)
    uncovered = _base_result(n_entity_nodes_total=5, n_endpoint_only_nodes_total=0)
    with pytest.raises(DegreeRankingDotError, match="exceed n_endpoint"):
        dumps_degree_ranking_dot(uncovered)
    truncated_entity = copy.deepcopy(truncated)
    truncated_entity.update(n_entity_nodes_total=1, n_endpoint_only_nodes_total=4)
    with pytest.raises(DegreeRankingDotError, match="exceed n_entity"):
        dumps_degree_ranking_dot(truncated_entity)
    wrong_trunc_cap = _base_result(
        max_nodes=3,
        nodes=_base_result()["nodes"][:2],
        n_nodes_returned=2,
        nodes_truncated=True,
    )
    with pytest.raises(DegreeRankingDotError, match="exactly max_nodes"):
        dumps_degree_ranking_dot(wrong_trunc_cap)


def test_edge_types_none_literal_all_and_commas():
    unfiltered = dumps_degree_ranking_dot(_base_result(edge_types=None))
    literal_all = dumps_degree_ranking_dot(_base_result(edge_types=["all"]))
    comma_types = dumps_degree_ranking_dot(_base_result(edge_types=["a,b", "c"]))
    split_types = dumps_degree_ranking_dot(_base_result(edge_types=["a", "b,c"]))
    assert 'edge_types="null"' in unfiltered
    assert 'edge_types="[\\"all\\"]"' in literal_all
    assert 'edge_types="[\\"a,b\\",\\"c\\"]"' in comma_types
    assert 'edge_types="[\\"a\\",\\"b,c\\"]"' in split_types
    assert len({unfiltered, literal_all, comma_types, split_types}) == 4


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_degree_ranking_dot(_base_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.degree_ranking_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_DEGREE_RANKING_DOT_BYTES", size)
    assert dumps_degree_ranking_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_DEGREE_RANKING_DOT_BYTES", size + 1)
    assert dumps_degree_ranking_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_DEGREE_RANKING_DOT_BYTES", size - 1)
    with pytest.raises(DegreeRankingDotError, match="hard limit"):
        dumps_degree_ranking_dot(_base_result())
    assert HARD_MAX_DEGREE_RANKING_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    common = ["degree-ranking", "--graph", str(graph)]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_degree_ranking_json(payload) + "\n" == json_out.stdout
    assert format_degree_ranking_human(payload).strip() == human.stdout.strip()
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
    import graphrag_code.degree_ranking_dot as dot_mod

    def boom(*_a, **_k):
        raise AssertionError("graph observed before json/dot rejection")

    monkeypatch.setattr(gq, "_scoped_graph", boom)
    rejected = CliRunner().invoke(
        app, ["degree-ranking", "--graph", str(graph), "--json", "--dot"]
    )
    assert rejected.exit_code == 2
    assert rejected.stdout == ""
    monkeypatch.undo()
    monkeypatch.setattr(dot_mod, "HARD_MAX_DEGREE_RANKING_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["degree-ranking", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "degree-ranking", "--help"],
        [sys.executable, str(CLI), "degree-ranking", "--help"],
        [
            sys.executable,
            "-m",
            "graphrag_code.graph_query",
            "degree-ranking",
            "--help",
        ],
        [sys.executable, "-m", "graphrag_code", "degree-ranking", "--help"],
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
    args = ["degree-ranking", "--graph", str(graph), "--dot"]
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
    expected = dumps_degree_ranking_dot(ByogGraph(graph).degree_ranking())
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed.stdout
        == expected
        == GOLDEN_TOTAL
    )
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not (graph / ".publish.lock").is_symlink()


def test_invalid_graph_snapshot_and_unsafe_lease_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "degree-ranking",
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
        "degree-ranking",
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
        "degree-ranking",
        "--graph",
        str(graph),
        "--max-nodes",
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
        "degree-ranking",
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
        "degree-ranking",
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
        "degree-ranking",
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
        "degree-ranking",
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


def _degree_ranking_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_degree_ranking_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_degree_ranking_dot = wrap_dumps

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
            ["degree-ranking", "--graph", graph, "--dot"],
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
        target=_degree_ranking_dot_hold, args=(str(graph), held, resume, q)
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
    orig = g.degree_ranking

    def wrapped(*args, **kwargs):
        calls.append("degree_ranking")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "degree_ranking", wrapped)
    for name in (
        "condensation",
        "components",
        "strong_components",
        "shortest_path",
        "dependency_order",
        "subgraph",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    result = g.degree_ranking()
    assert calls == ["degree_ranking"]
    dumps_degree_ranking_dot(result)
    assert calls == ["degree_ranking"]

    serializer_calls = 0
    orig_dumps = dumps_degree_ranking_dot

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner

    monkeypatch.setattr(gq, "dumps_degree_ranking_dot", counted_dumps)
    invoked = CliRunner().invoke(
        gq.app, ["degree-ranking", "--graph", str(graph), "--dot"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert serializer_calls == 1
    assert invoked.stdout == GOLDEN_TOTAL

    dot_src = (ROOT / "src" / "graphrag_code" / "degree_ranking_dot.py").read_text(
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
    ranking_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_degree_ranking":
            ranking_fn = ast.get_source_segment(gq_src, node)
    assert ranking_fn is not None
    assert "subprocess" not in ranking_fn
    assert "compute_structural_degree_ranking" not in ranking_fn
    assert "networkx" not in ranking_fn
    assert ranking_fn.count(".degree_ranking(") == 1
    assert ranking_fn.count("dumps_degree_ranking_dot(") == 1
    assert "sys.stdout.write(dumps_degree_ranking_dot(result))" in ranking_fn
    assert "sys.stdout.flush()" in ranking_fn
    write_at = ranking_fn.find("sys.stdout.write(dumps_degree_ranking_dot(result))")
    flush_at = ranking_fn.find("sys.stdout.flush()")
    with_at = ranking_fn.find("with _scoped_graph")
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
    params = list(inspect.signature(GraphMcpSession.degree_ranking).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == ["self", "rank_by", "max_nodes", "edge_types", "snapshot"]
    expected = ByogGraph(graph).degree_ranking()
    payload = session.degree_ranking()
    assert payload["tool"] == "degree_ranking"
    assert payload["data"] == json.loads(
        json.dumps(expected, allow_nan=False, default=str)
    )
    assert "dot" not in payload
    assert "format" not in payload
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
            tool = next(item for item in tools if item.name == "degree_ranking")
            props = tool.input_schema.get("properties") or {}
            assert "dot" not in props
            assert "format" not in props
            assert list(props) == ["rank_by", "max_nodes", "edge_types", "snapshot"]
            result = await client.call_tool("degree_ranking", {})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "degree_ranking"
            dumped = json.dumps(body)
            assert "digraph" not in dumped
            assert "graphrag_degree_ranking" not in dumped
            assert "--dot" not in dumped

    anyio_run(_body)
