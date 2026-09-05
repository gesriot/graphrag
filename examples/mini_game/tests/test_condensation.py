"""Directed SCC condensation DAG over persisted relationship rows.

CLI/Python topology summary plus deterministic Graphviz DOT interchange.
MCP exposes the structured JSON contract as the sixteenth read-only tool
and does not expose DOT. No NetworkX or Graphviz runtime.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import multiprocessing
import os
import random
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
    DEFAULT_CONDENSATION_MAX_COMPONENTS,
    DEFAULT_CONDENSATION_MAX_EDGES,
    DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
    HARD_MAX_CONDENSATION_COMPONENT_NODES,
    HARD_MAX_CONDENSATION_COMPONENTS,
    HARD_MAX_CONDENSATION_EDGES,
    ByogGraph,
    compute_bounded_subgraph,
    compute_condensation_graph,
    compute_containment_dependency_order,
    compute_structural_degree_ranking,
    compute_strongly_connected_components,
    compute_weakly_connected_components,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    condensation as free_condensation,
    dumps_components_json,
    dumps_condensation_json,
    dumps_degree_ranking_json,
    dumps_subgraph_json,
    format_condensation_human,
    format_subgraph_human,
)
from graphrag_code.mcp_server import TOOL_NAMES  # type: ignore
from graphrag_code.subgraph_dot import dumps_subgraph_dot  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
SRC_BYOG = ROOT / "src" / "graphrag_code" / "byog_graph.py"

TOP_KEYS = {
    "edge_types",
    "max_components",
    "max_nodes_per_component",
    "max_edges",
    "components",
    "edges",
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
    "n_condensation_edges_total",
    "n_condensation_edges_eligible_total",
    "n_condensation_edges_returned",
    "components_truncated",
    "nodes_truncated",
    "edges_truncated",
}
COMP_KEYS = {
    "representative",
    "nodes",
    "n_nodes_total",
    "n_nodes_returned",
    "n_internal_edges_total",
    "n_self_loop_edges_total",
    "n_entity_nodes",
    "n_endpoint_only_nodes",
    "is_cyclic",
    "nodes_truncated",
}
EDGE_KEYS = {"source", "target", "n_relationship_rows_total"}


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


def _contains(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "contains", hid=hid, **extra)


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_cond"
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


def _assert_schema(result: dict) -> None:
    assert set(result) == TOP_KEYS
    assert "rank" not in result
    assert "ordinal" not in result
    for comp in result["components"]:
        assert set(comp) == COMP_KEYS
        assert "rank" not in comp
        assert "id" not in comp
    for edge in result["edges"]:
        assert set(edge) == EDGE_KEYS
        assert "weight" not in edge
        assert "id" not in edge


def _assert_full_invariants(result: dict) -> None:
    _assert_schema(result)
    comps = result["components"]
    edges = result["edges"]
    if result["components_truncated"] is False:
        assert result["n_components_returned"] == result["n_components_total"]
        titles = [title for comp in comps for title in comp["nodes"]]
        if result["nodes_truncated"] is False:
            assert len(titles) == result["n_nodes_total"]
            assert len(set(titles)) == result["n_nodes_total"]
        assert sum(int(c["n_nodes_total"]) for c in comps) == result["n_nodes_total"]
        assert (
            sum(int(c["n_internal_edges_total"]) for c in comps)
            == result["n_internal_edges_total"]
        )
        assert (
            sum(int(c["n_self_loop_edges_total"]) for c in comps)
            == result["n_self_loop_edges_total"]
        )
        reps = [c["representative"] for c in comps]
        pos = {rep: i for i, rep in enumerate(reps)}
        for edge in edges:
            assert pos[edge["source"]] < pos[edge["target"]]
    assert (
        result["n_entity_nodes_total"] + result["n_endpoint_only_nodes_total"]
        == result["n_nodes_total"]
    )
    assert (
        result["n_internal_edges_total"] + result["n_cross_component_edges_total"]
        == result["n_edges_total"]
    )
    if result["edges_truncated"] is False:
        assert (
            result["n_condensation_edges_returned"]
            == result["n_condensation_edges_total"]
        )
        assert (
            sum(int(e["n_relationship_rows_total"]) for e in edges)
            == result["n_cross_component_edges_total"]
        )
    assert (
        result["n_condensation_edges_returned"]
        == len(edges)
        <= result["n_condensation_edges_eligible_total"]
        <= result["n_condensation_edges_total"]
    )
    assert result["n_components_returned"] == len(comps)
    assert result["components_truncated"] is (
        result["n_components_returned"] < result["n_components_total"]
    )
    assert result["edges_truncated"] is (
        result["n_condensation_edges_returned"] < result["n_condensation_edges_total"]
    )
    if comps:
        assert result["nodes_truncated"] is any(c["nodes_truncated"] for c in comps)
    else:
        assert result["nodes_truncated"] is False


def _reachable(adj: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, ()))
    return seen


def _oracle_condensation(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    *,
    edge_types=None,
    max_components: int = DEFAULT_CONDENSATION_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
    max_edges: int = DEFAULT_CONDENSATION_MAX_EDGES,
) -> dict:
    """Independent BFS mutual-reachability + heap-Kahn condensation oracle."""
    if edge_types is None or len(list(edge_types)) == 0:
        allow = None
    else:
        seen: set[str] = set()
        allow_list: list[str] = []
        for item in edge_types:
            if item not in seen:
                seen.add(item)
                allow_list.append(item)
        allow_list.sort(key=lambda item: item.encode("utf-8"))
        allow = allow_list
    entity_titles: set[str] = set()
    if len(ents):
        for title in ents["title"].astype(str).tolist():
            if title in entity_titles:
                raise ValueError(f"duplicate entity title {title!r}")
            entity_titles.add(title)
    selected: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    if len(rels):
        allow_set = None if allow is None else set(allow)
        for _, row in rels.iterrows():
            rid = str(row["id"])
            if rid in seen_ids:
                raise ValueError(f"duplicate relationship id {rid!r}")
            seen_ids.add(rid)
            if allow_set is not None and str(row["type"]) not in allow_set:
                continue
            selected.append((str(row["source"]), str(row["target"])))
    nodes = set(entity_titles)
    for src, tgt in selected:
        nodes.add(src)
        nodes.add(tgt)
    if not nodes:
        return compute_condensation_graph(
            ents,
            rels,
            edge_types=edge_types,
            max_components=max_components,
            max_nodes_per_component=max_nodes_per_component,
            max_edges=max_edges,
        )
    adj: dict[str, list[str]] = {title: [] for title in nodes}
    pairs = set(selected)
    for src, tgt in pairs:
        adj[src].append(tgt)
    assigned: set[str] = set()
    sccs: list[list[str]] = []
    for title in sorted(nodes, key=lambda item: item.encode("utf-8")):
        if title in assigned:
            continue
        reach = _reachable(adj, title)
        members = [
            other
            for other in reach
            if other not in assigned and title in _reachable(adj, other)
        ]
        members.sort(key=lambda item: item.encode("utf-8"))
        assigned.update(members)
        sccs.append(members)
    title_to_scc = {
        title: index for index, members in enumerate(sccs) for title in members
    }
    cond_adj: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    indeg = [0] * len(sccs)
    cond_pairs: set[tuple[int, int]] = set()
    for src, tgt in pairs:
        a = title_to_scc[src]
        b = title_to_scc[tgt]
        if a == b or b in cond_adj[a]:
            continue
        cond_adj[a].add(b)
        indeg[b] += 1
        cond_pairs.add((a, b))
    import heapq

    heap = [
        (sccs[i][0].encode("utf-8"), i) for i, deg in enumerate(indeg) if deg == 0
    ]
    heapq.heapify(heap)
    order: list[int] = []
    while heap:
        _key, i = heapq.heappop(heap)
        order.append(i)
        for nxt in cond_adj[i]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(heap, (sccs[nxt][0].encode("utf-8"), nxt))
    internal = [0] * len(sccs)
    self_loops = [0] * len(sccs)
    cross_counts: dict[tuple[int, int], int] = {}
    n_cross = 0
    n_self = 0
    for src, tgt in selected:
        a = title_to_scc[src]
        b = title_to_scc[tgt]
        if a == b:
            internal[a] += 1
            if src == tgt:
                self_loops[a] += 1
                n_self += 1
        else:
            n_cross += 1
            cross_counts[(a, b)] = cross_counts.get((a, b), 0) + 1
    records = []
    n_cyclic = 0
    for index in order:
        titles = list(sccs[index])
        n_nodes = len(titles)
        n_entity = sum(1 for title in titles if title in entity_titles)
        is_cyclic = n_nodes > 1 or self_loops[index] > 0
        if is_cyclic:
            n_cyclic += 1
        records.append(
            {
                "representative": titles[0],
                "nodes": titles,
                "n_nodes_total": n_nodes,
                "n_internal_edges_total": internal[index],
                "n_self_loop_edges_total": self_loops[index],
                "n_entity_nodes": n_entity,
                "n_endpoint_only_nodes": n_nodes - n_entity,
                "is_cyclic": is_cyclic,
            }
        )
    returned = []
    for rec in records[:max_components]:
        shown = rec["nodes"][:max_nodes_per_component]
        returned.append(
            {
                "representative": rec["representative"],
                "nodes": shown,
                "n_nodes_total": rec["n_nodes_total"],
                "n_nodes_returned": len(shown),
                "n_internal_edges_total": rec["n_internal_edges_total"],
                "n_self_loop_edges_total": rec["n_self_loop_edges_total"],
                "n_entity_nodes": rec["n_entity_nodes"],
                "n_endpoint_only_nodes": rec["n_endpoint_only_nodes"],
                "is_cyclic": rec["is_cyclic"],
                "nodes_truncated": rec["n_nodes_total"] > len(shown),
            }
        )
    returned_set = set(order[:max_components])
    topo_pos = {index: pos for pos, index in enumerate(order)}
    eligible = []
    for src_scc, tgt_scc in cond_pairs:
        if src_scc in returned_set and tgt_scc in returned_set:
            eligible.append(
                (
                    topo_pos[src_scc],
                    topo_pos[tgt_scc],
                    src_scc,
                    tgt_scc,
                    cross_counts[(src_scc, tgt_scc)],
                )
            )
    eligible.sort()
    shown_edges = eligible[:max_edges]
    edges = [
        {
            "source": sccs[src_scc][0],
            "target": sccs[tgt_scc][0],
            "n_relationship_rows_total": count,
        }
        for _sp, _tp, src_scc, tgt_scc, count in shown_edges
    ]
    n_nodes = len(nodes)
    n_entity = len(entity_titles)
    return {
        "edge_types": allow,
        "max_components": max_components,
        "max_nodes_per_component": max_nodes_per_component,
        "max_edges": max_edges,
        "components": returned,
        "edges": edges,
        "n_components_total": len(records),
        "n_components_returned": len(returned),
        "n_nodes_total": n_nodes,
        "n_edges_total": len(selected),
        "n_internal_edges_total": sum(internal),
        "n_cross_component_edges_total": n_cross,
        "n_self_loop_edges_total": n_self,
        "n_cyclic_components_total": n_cyclic,
        "n_entity_nodes_total": n_entity,
        "n_endpoint_only_nodes_total": n_nodes - n_entity,
        "n_condensation_edges_total": len(cond_pairs),
        "n_condensation_edges_eligible_total": len(eligible),
        "n_condensation_edges_returned": len(edges),
        "components_truncated": len(records) > len(returned),
        "nodes_truncated": any(c["nodes_truncated"] for c in returned),
        "edges_truncated": len(edges) < len(cond_pairs),
    }


def _fn_source(tree: ast.AST, src: str, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            text = ast.get_source_segment(src, node)
            assert text is not None
            return text
    raise AssertionError(f"missing function {name}")


def test_shared_helper_schema_empty_and_public_parity():
    src = SRC_BYOG.read_text(encoding="utf-8")
    tree = ast.parse(src)
    engine = _fn_source(tree, src, "_iterative_sccs")
    helper = _fn_source(tree, src, "_scc_condensation_dag")
    cond_order = _fn_source(tree, src, "_containment_scc_order")
    producer = _fn_source(tree, src, "compute_condensation_graph")
    method = _fn_source(tree, src, "condensation")
    dep = _fn_source(tree, src, "compute_containment_dependency_order")
    strong = _fn_source(tree, src, "compute_strongly_connected_components")
    assert "_iterative_sccs" in helper
    assert helper.count("_iterative_sccs(") == 1
    assert "_scc_condensation_dag" in producer
    assert "_scc_condensation_dag" in cond_order
    assert "_containment_scc_order" in dep
    assert "compute_condensation_graph" in method
    assert "_scc_condensation_dag" not in strong
    assert src.count("def _iterative_sccs") == 1
    assert src.count("def _scc_condensation_dag") == 1
    assert "DEFAULT_CONDENSATION_MAX_COMPONENTS = 20" in src
    assert "DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT = 20" in src
    assert "DEFAULT_CONDENSATION_MAX_EDGES = 100" in src
    assert "HARD_MAX_CONDENSATION_COMPONENTS = 100" in src
    assert "HARD_MAX_CONDENSATION_COMPONENT_NODES = 100" in src
    assert "HARD_MAX_CONDENSATION_EDGES = 500" in src
    producer_src = producer
    assert "DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS" not in producer_src
    assert "DEFAULT_COMPONENTS_MAX_COMPONENTS" not in producer_src
    assert "DEFAULT_SUBGRAPH_MAX_EDGES" not in producer_src
    assert "HARD_MAX_STRONG_COMPONENTS" not in producer_src
    assert "HARD_MAX_COMPONENTS" not in producer_src
    assert "HARD_MAX_SUBGRAPH_EDGES" not in producer_src
    assert "stack_rev" in engine
    assert "def strongconnect" not in engine
    assert "def strongconnect" not in helper
    assert "tarjan" not in helper.lower()
    for banned in (
        "compute_weakly_connected_components(",
        "compute_strongly_connected_components(",
        "compute_containment_dependency_order(",
        ".components(",
        ".strong_components(",
        ".dependency_order(",
        ".subgraph(",
        "networkx",
        "subprocess",
        "graphviz",
        "tempfile",
    ):
        assert banned not in helper
        assert banned not in producer
        assert banned not in engine

    ents = pd.DataFrame(
        [
            _entity("B", description="LEAK_DESC"),
            _entity("A", description="LEAK_DESC"),
            _entity("Z", description="LEAK_DESC"),
        ]
    )
    rels = pd.DataFrame(
        [
            _calls("A", "B", rid="rel:ab", description="LEAK_REL"),
            _calls("B", "A", rid="rel:ba", description="LEAK_REL"),
        ]
    )
    produced = compute_condensation_graph(ents, rels)
    _assert_full_invariants(produced)
    g = ByogGraph.__new__(ByogGraph)
    g.ents, g.rels = ents, rels
    assert list(inspect.signature(ByogGraph.condensation).parameters) == [
        "self",
        "edge_types",
        "max_components",
        "max_nodes_per_component",
        "max_edges",
    ]
    assert g.condensation() == produced
    assert free_condensation(ents, rels) == produced
    dumped = dumps_condensation_json(produced)
    for leak in (
        '"description"',
        '"snippet"',
        '"span"',
        '"weight"',
        '"confidence"',
        '"id"',
        "LEAK_DESC",
        "LEAK_REL",
    ):
        assert leak not in dumped
        assert leak not in format_condensation_human(produced)

    empty = compute_condensation_graph(pd.DataFrame(), pd.DataFrame())
    _assert_full_invariants(empty)
    assert empty["components"] == []
    assert empty["edges"] == []
    assert empty["edge_types"] is None
    assert empty["n_components_total"] == 0
    assert empty["n_nodes_total"] == 0
    assert empty["n_edges_total"] == 0
    assert empty["n_internal_edges_total"] == 0
    assert empty["n_cross_component_edges_total"] == 0
    assert empty["n_self_loop_edges_total"] == 0
    assert empty["n_cyclic_components_total"] == 0
    assert empty["n_condensation_edges_total"] == 0
    assert empty["n_condensation_edges_eligible_total"] == 0
    assert empty["n_condensation_edges_returned"] == 0
    assert empty["components_truncated"] is False
    assert empty["nodes_truncated"] is False
    assert empty["edges_truncated"] is False
    assert compute_condensation_graph(None, None)["n_nodes_total"] == 0
    human_empty = format_condensation_human(empty)
    assert "n_nodes_total: 0" in human_empty
    assert "components (0/0):" in human_empty
    assert "edges (0/0):" in human_empty
    assert "n_condensation_edges_total: 0" in human_empty


def test_topologies_aggregation_and_distinct_from_other_queries():
    chain = compute_condensation_graph(
        pd.DataFrame([_entity("A"), _entity("B"), _entity("C")]),
        pd.DataFrame([_calls("A", "B"), _calls("B", "C")]),
    )
    _assert_full_invariants(chain)
    assert [c["representative"] for c in chain["components"]] == ["A", "B", "C"]
    assert [(e["source"], e["target"], e["n_relationship_rows_total"]) for e in chain["edges"]] == [
        ("A", "B", 1),
        ("B", "C", 1),
    ]
    assert chain["n_cyclic_components_total"] == 0
    assert all(c["is_cyclic"] is False for c in chain["components"])

    diamond = compute_condensation_graph(
        pd.DataFrame([_entity("A"), _entity("B"), _entity("C"), _entity("D")]),
        pd.DataFrame(
            [
                _calls("A", "B", hid=1),
                _calls("A", "C", hid=2),
                _calls("B", "D", hid=3),
                _calls("C", "D", hid=4),
            ]
        ),
    )
    _assert_full_invariants(diamond)
    assert [c["representative"] for c in diamond["components"]] == ["A", "B", "C", "D"]
    assert [(e["source"], e["target"]) for e in diamond["edges"]] == [
        ("A", "B"),
        ("A", "C"),
        ("B", "D"),
        ("C", "D"),
    ]

    disconnected = compute_condensation_graph(
        pd.DataFrame([_entity("P"), _entity("X"), _entity("Isolated")]),
        pd.DataFrame([_calls("P", "Q", hid=1), _calls("X", "Y", hid=2)]),
    )
    _assert_full_invariants(disconnected)
    assert [c["representative"] for c in disconnected["components"]] == [
        "Isolated",
        "P",
        "Q",
        "X",
        "Y",
    ]
    assert disconnected["n_entity_nodes_total"] == 3
    assert disconnected["n_endpoint_only_nodes_total"] == 2

    cyclic = compute_condensation_graph(
        pd.DataFrame([_entity("A"), _entity("B"), _entity("C"), _entity("X")]),
        pd.DataFrame(
            [
                _calls("A", "B", hid=1),
                _calls("B", "C", hid=2),
                _calls("C", "A", hid=3),
                _calls("X", "A", hid=4),
                _calls("A", "B", hid=5, rid="rel:parallel"),
            ]
        ),
    )
    _assert_full_invariants(cyclic)
    assert [c["representative"] for c in cyclic["components"]] == ["X", "A"]
    cycle = cyclic["components"][1]
    assert cycle["nodes"] == ["A", "B", "C"]
    assert cycle["is_cyclic"] is True
    assert cycle["n_internal_edges_total"] == 4
    assert cyclic["edges"] == [
        {"source": "X", "target": "A", "n_relationship_rows_total": 1}
    ]
    assert cyclic["n_cross_component_edges_total"] == 1
    assert cyclic["n_condensation_edges_total"] == 1

    self_loop = compute_condensation_graph(
        pd.DataFrame([_entity("X")]),
        pd.DataFrame([_calls("X", "X")]),
    )
    _assert_full_invariants(self_loop)
    assert self_loop["components"][0]["is_cyclic"] is True
    assert self_loop["n_self_loop_edges_total"] == 1
    assert self_loop["n_internal_edges_total"] == 1
    assert self_loop["n_condensation_edges_total"] == 0
    assert self_loop["edges"] == []

    isolates = compute_condensation_graph(
        pd.DataFrame([_entity("Z"), _entity("A")]),
        pd.DataFrame(),
    )
    _assert_full_invariants(isolates)
    assert [c["representative"] for c in isolates["components"]] == ["A", "Z"]
    assert all(c["is_cyclic"] is False for c in isolates["components"])

    reverse_chain = compute_condensation_graph(
        pd.DataFrame([_entity("Z"), _entity("Y"), _entity("X")]),
        pd.DataFrame([_calls("Z", "Y"), _calls("Y", "X")]),
    )
    strong = compute_strongly_connected_components(
        pd.DataFrame([_entity("Z"), _entity("Y"), _entity("X")]),
        pd.DataFrame([_calls("Z", "Y"), _calls("Y", "X")]),
    )
    _assert_full_invariants(reverse_chain)
    assert [c["representative"] for c in reverse_chain["components"]] == ["Z", "Y", "X"]
    assert [c["representative"] for c in strong["components"]] == ["X", "Y", "Z"]
    assert reverse_chain["n_condensation_edges_total"] == 2

    size_vs_topo = compute_condensation_graph(
        pd.DataFrame([_entity("A"), _entity("Y"), _entity("Z")]),
        pd.DataFrame([_calls("Y", "Z"), _calls("Z", "Y")]),
    )
    strong_size = compute_strongly_connected_components(
        pd.DataFrame([_entity("A"), _entity("Y"), _entity("Z")]),
        pd.DataFrame([_calls("Y", "Z"), _calls("Z", "Y")]),
    )
    assert [c["representative"] for c in size_vs_topo["components"]] == ["A", "Y"]
    assert [c["representative"] for c in strong_size["components"]] == ["Y", "A"]
    assert size_vs_topo["components"][1]["is_cyclic"] is True
    assert size_vs_topo["n_condensation_edges_total"] == 0

    weak = compute_weakly_connected_components(
        pd.DataFrame([_entity("A"), _entity("B")]),
        pd.DataFrame([_calls("A", "B")]),
    )
    directed = compute_condensation_graph(
        pd.DataFrame([_entity("A"), _entity("B")]),
        pd.DataFrame([_calls("A", "B")]),
    )
    assert weak["n_components_total"] == 1
    assert directed["n_components_total"] == 2

    dep = compute_containment_dependency_order(
        pd.DataFrame([_entity("B"), _entity("A"), _entity("Z")]),
        pd.DataFrame([_contains("B", "A")]),
    )
    assert dep == ["B", "A", "Z"]
    cond_contains = compute_condensation_graph(
        pd.DataFrame([_entity("B"), _entity("A"), _entity("Z")]),
        pd.DataFrame([_contains("B", "A")]),
        edge_types=["contains"],
    )
    assert [c["representative"] for c in cond_contains["components"]] == ["B", "A", "Z"]
    flattened = [title for c in cond_contains["components"] for title in c["nodes"]]
    assert flattened == dep


def test_filters_duplicates_malformed_and_limits():
    ents = pd.DataFrame([_entity("A"), _entity("B"), _entity("C")])
    rels = pd.DataFrame(
        [
            _calls("A", "B", hid=1),
            _rel("A", "ghost", "contains", hid=2),
            _rel("C", "C", "uses_type", hid=3, rid="rel:self-c"),
        ]
    )
    all_types = compute_condensation_graph(ents, rels, edge_types=None)
    empty_filter = compute_condensation_graph(ents, rels, edge_types=[])
    assert all_types == empty_filter
    assert all_types["edge_types"] is None
    calls_only = compute_condensation_graph(ents, rels, edge_types=["calls", "calls"])
    assert calls_only["edge_types"] == ["calls"]
    assert "ghost" not in {
        title for comp in calls_only["components"] for title in comp["nodes"]
    }
    mixed = compute_condensation_graph(ents, rels, edge_types=["uses_type", "calls"])
    assert mixed["edge_types"] == ["calls", "uses_type"]
    none = compute_condensation_graph(ents, rels, edge_types=["imports"])
    assert none["n_nodes_total"] == 3
    assert none["n_edges_total"] == 0
    assert none["n_condensation_edges_total"] == 0
    with pytest.raises(ValueError, match="not a single string"):
        compute_condensation_graph(ents, rels, edge_types="calls")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_condensation_graph(ents, rels, edge_types=[""])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_condensation_graph(ents, rels, edge_types=[" calls"])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_condensation_graph(ents, rels, edge_types=["ca\x00lls"])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_condensation_graph(ents, rels, edge_types=["calls", 1])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="duplicate entity title"):
        compute_condensation_graph(
            pd.DataFrame([_entity("A"), _entity("A", id="ent:other")]),
            pd.DataFrame(),
        )
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_condensation_graph(
            pd.DataFrame([_entity("A")]),
            pd.DataFrame(
                [_calls("A", "A", rid="rel:dup"), _calls("A", "A", rid="rel:dup")]
            ),
        )
    hidden = pd.DataFrame(
        [
            _calls("A", "B", rid="rel:ok"),
            {**_rel("A", "B", "imports"), "target": ""},
        ]
    )
    with pytest.raises(ValueError, match="invalid target"):
        compute_condensation_graph(
            pd.DataFrame([_entity("A")]), hidden, edge_types=["calls"]
        )
    with pytest.raises(ValueError, match="missing required columns"):
        compute_condensation_graph(pd.DataFrame([{"id": "x"}]), pd.DataFrame())
    with pytest.raises(ValueError, match="max_components"):
        compute_condensation_graph(ents, rels, max_components=True)
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_condensation_graph(ents, rels, max_nodes_per_component=1.5)
    with pytest.raises(ValueError, match="max_edges"):
        compute_condensation_graph(ents, rels, max_edges="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_components"):
        compute_condensation_graph(ents, rels, max_components=0)
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_condensation_graph(ents, rels, max_nodes_per_component=-1)
    with pytest.raises(ValueError, match="max_edges"):
        compute_condensation_graph(ents, rels, max_edges=-1)
    with pytest.raises(ValueError, match="max_components"):
        compute_condensation_graph(ents, rels, max_components=float("nan"))
    with pytest.raises(ValueError, match="max_edges"):
        compute_condensation_graph(ents, rels, max_edges=math.inf)
    with pytest.raises(ValueError, match="max_components"):
        compute_condensation_graph(
            ents, rels, max_components=HARD_MAX_CONDENSATION_COMPONENTS + 1
        )
    with pytest.raises(ValueError, match="max_nodes_per_component"):
        compute_condensation_graph(
            ents, rels, max_nodes_per_component=HARD_MAX_CONDENSATION_COMPONENT_NODES + 1
        )
    with pytest.raises(ValueError, match="max_edges"):
        compute_condensation_graph(
            ents, rels, max_edges=HARD_MAX_CONDENSATION_EDGES + 1
        )


def test_canonical_order_truncation_shuffle_and_eligible_edges():
    ents = pd.DataFrame(
        [_entity("Z"), _entity("A"), _entity("M"), _entity("B"), _entity("é")]
    )
    rels = pd.DataFrame(
        [
            _calls("Z", "A", hid=1),
            _calls("A", "Z", hid=2),
            _calls("B", "é", hid=3),
        ]
    )
    r1 = compute_condensation_graph(ents, rels)
    r2 = compute_condensation_graph(
        ents.sample(frac=1, random_state=7).reset_index(drop=True),
        rels.sample(frac=1, random_state=11),
    )
    r2_idx = rels.sample(frac=1, random_state=3)
    r2_idx.index = list(range(50, 50 + len(r2_idx)))
    r3 = compute_condensation_graph(ents, r2_idx)
    assert r1 == r2 == r3
    _assert_full_invariants(r1)
    assert [c["representative"] for c in r1["components"]] == ["A", "B", "M", "é"]
    cycle = next(c for c in r1["components"] if c["representative"] == "A")
    assert cycle["nodes"] == ["A", "Z"]
    assert cycle["is_cyclic"] is True
    assert r1["max_components"] == DEFAULT_CONDENSATION_MAX_COMPONENTS
    assert (
        r1["max_nodes_per_component"] == DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT
    )
    assert r1["max_edges"] == DEFAULT_CONDENSATION_MAX_EDGES
    assert r1["edges"] == [
        {"source": "B", "target": "é", "n_relationship_rows_total": 1}
    ]

    utf = compute_condensation_graph(
        pd.DataFrame([_entity("é"), _entity("Ω"), _entity("A")]),
        pd.DataFrame(),
    )
    assert [c["representative"] for c in utf["components"]] == ["A", "é", "Ω"]
    assert "é".encode("utf-8") < "Ω".encode("utf-8")

    # Adversarial discovery vs topological order: dataframe reverse of UTF-8.
    adv_ents = pd.DataFrame([_entity("Z"), _entity("Y"), _entity("X")])
    adv_rels = pd.DataFrame(
        [_calls("Z", "Y", hid=1), _calls("Y", "X", hid=2), _calls("Z", "X", hid=3)]
    )
    adv = compute_condensation_graph(
        adv_ents.sample(frac=1, random_state=99).reset_index(drop=True),
        adv_rels.sample(frac=1, random_state=123).reset_index(drop=True),
    )
    assert [c["representative"] for c in adv["components"]] == ["Z", "Y", "X"]
    assert [(e["source"], e["target"]) for e in adv["edges"]] == [
        ("Z", "Y"),
        ("Z", "X"),
        ("Y", "X"),
    ]

    big_ents = pd.DataFrame([_entity(t) for t in ("A", "B", "C", "D")])
    big_rels = pd.DataFrame(
        [
            _calls("A", "B", hid=1),
            _calls("B", "C", hid=2),
            _calls("C", "D", hid=3),
            _calls("A", "B", hid=4, rid="rel:parallel"),
        ]
    )
    full = compute_condensation_graph(big_ents, big_rels)
    _assert_full_invariants(full)
    assert full["n_components_total"] == 4
    assert full["n_condensation_edges_total"] == 3
    assert full["edges"][0]["n_relationship_rows_total"] == 2

    cap_comp = compute_condensation_graph(
        big_ents, big_rels, max_components=2, max_nodes_per_component=20, max_edges=100
    )
    assert cap_comp["n_components_total"] == 4
    assert cap_comp["n_components_returned"] == 2
    assert cap_comp["components_truncated"] is True
    assert [c["representative"] for c in cap_comp["components"]] == ["A", "B"]
    assert cap_comp["n_condensation_edges_total"] == 3
    assert cap_comp["n_condensation_edges_eligible_total"] == 1
    assert cap_comp["n_condensation_edges_returned"] == 1
    assert cap_comp["edges"] == [
        {"source": "A", "target": "B", "n_relationship_rows_total": 2}
    ]
    assert cap_comp["edges_truncated"] is True
    assert cap_comp["nodes_truncated"] is False

    cap_nodes = compute_condensation_graph(
        pd.DataFrame([_entity("A"), _entity("B"), _entity("C")]),
        pd.DataFrame([_calls("A", "B"), _calls("B", "A"), _calls("B", "C")]),
        max_components=20,
        max_nodes_per_component=1,
        max_edges=100,
    )
    assert cap_nodes["components_truncated"] is False
    assert cap_nodes["nodes_truncated"] is True
    cycle = next(c for c in cap_nodes["components"] if c["representative"] == "A")
    assert cycle["nodes"] == ["A"]
    assert cycle["n_nodes_total"] == 2
    assert cycle["n_nodes_returned"] == 1
    assert cap_nodes["n_condensation_edges_returned"] == 1

    cap_edges = compute_condensation_graph(
        big_ents, big_rels, max_components=20, max_nodes_per_component=20, max_edges=1
    )
    assert cap_edges["n_condensation_edges_eligible_total"] == 3
    assert cap_edges["n_condensation_edges_returned"] == 1
    assert cap_edges["edges_truncated"] is True
    assert cap_edges["edges"][0]["source"] == "A"
    assert cap_edges["edges"][0]["target"] == "B"

    zero_edges = compute_condensation_graph(
        big_ents, big_rels, max_components=20, max_nodes_per_component=20, max_edges=0
    )
    assert zero_edges["edges"] == []
    assert zero_edges["n_condensation_edges_eligible_total"] == 3
    assert zero_edges["n_condensation_edges_returned"] == 0
    assert zero_edges["edges_truncated"] is True
    assert zero_edges["max_edges"] == 0

    combined = compute_condensation_graph(
        big_ents, big_rels, max_components=1, max_nodes_per_component=1, max_edges=0
    )
    assert combined["n_components_returned"] == 1
    assert combined["components"][0]["nodes"] == ["A"]
    assert combined["n_condensation_edges_eligible_total"] == 0
    assert combined["n_condensation_edges_returned"] == 0
    assert combined["components_truncated"] is True
    assert combined["nodes_truncated"] is False
    assert combined["edges_truncated"] is True


def test_randomized_oracle_and_existing_producers_unchanged():
    rng = random.Random(20260831)
    titles = ["A", "B", "C", "M", "Z", "é", "Ω"]
    for _ in range(12):
        n_ent = rng.randint(1, 6)
        chosen = rng.sample(titles, n_ent)
        ents = pd.DataFrame([_entity(title) for title in chosen])
        rels_rows = []
        for hid in range(rng.randint(0, 8)):
            src = rng.choice(chosen + ["ghost", "x-only"])
            tgt = rng.choice(chosen + ["ghost", "y-only"])
            rel_type = rng.choice(["contains", "calls", "calls"])
            rels_rows.append(_rel(src, tgt, rel_type, hid=hid + 1, rid=f"rel:{hid+1}"))
        rels = pd.DataFrame(rels_rows)
        shuffled_ents = (
            ents.sample(frac=1, random_state=rng.randint(1, 10_000)).reset_index(
                drop=True
            )
            if len(ents)
            else ents
        )
        shuffled_rels = (
            rels.sample(frac=1, random_state=rng.randint(1, 10_000)).reset_index(
                drop=True
            )
            if len(rels)
            else rels
        )
        produced = compute_condensation_graph(shuffled_ents, shuffled_rels)
        assert produced == _oracle_condensation(ents, rels)
        assert compute_containment_dependency_order(
            shuffled_ents, shuffled_rels
        ) == compute_containment_dependency_order(ents, rels)
        assert compute_strongly_connected_components(
            shuffled_ents, shuffled_rels
        ) == compute_strongly_connected_components(ents, rels)

    assert compute_containment_dependency_order(
        pd.DataFrame([_entity("B"), _entity("A"), _entity("Z")]),
        pd.DataFrame([_contains("B", "A")]),
    ) == ["B", "A", "Z"]
    assert compute_containment_dependency_order(
        pd.DataFrame([_entity("A"), _entity("B"), _entity("C"), _entity("X")]),
        pd.DataFrame(
            [
                _contains("A", "A", rid="rel:self"),
                _contains("A", "B", rid="rel:ab1"),
                _contains("A", "B", rid="rel:ab2"),
                _contains("B", "C", rid="rel:bc"),
                _contains("C", "A", rid="rel:ca"),
                _contains("X", "A", rid="rel:xa"),
                _contains("B", "Y", rid="rel:by"),
            ]
        ),
    ) == ["X", "A", "B", "C", "Y"]
    strong_order = compute_strongly_connected_components(
        pd.DataFrame([_entity("A"), _entity("Y"), _entity("Z")]),
        pd.DataFrame([_calls("Y", "Z"), _calls("Z", "Y")]),
    )
    assert [c["representative"] for c in strong_order["components"]] == ["Y", "A"]


def test_long_chain_and_large_scc_without_recursion():
    n = 80
    chain_ents = pd.DataFrame([_entity(f"n{i:03d}") for i in range(n)])
    chain_rels = pd.DataFrame(
        [_calls(f"n{i:03d}", f"n{i+1:03d}", hid=i + 1) for i in range(n - 1)]
    )
    cycle_ents = pd.DataFrame([_entity(f"c{i:03d}") for i in range(n)])
    cycle_rels = pd.DataFrame(
        [
            _calls(f"c{i:03d}", f"c{(i + 1) % n:03d}", hid=i + 1)
            for i in range(n)
        ]
    )
    dep_ents = pd.DataFrame([_entity(f"d{i:03d}") for i in range(n)])
    dep_rels = pd.DataFrame(
        [_contains(f"d{i:03d}", f"d{i+1:03d}", hid=i + 1) for i in range(n - 1)]
    )
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(50)
    try:
        chain = compute_condensation_graph(
            chain_ents,
            chain_rels,
            max_components=100,
            max_nodes_per_component=100,
            max_edges=500,
        )
        cycle = compute_condensation_graph(
            cycle_ents,
            cycle_rels,
            max_components=100,
            max_nodes_per_component=100,
            max_edges=500,
        )
        order = compute_containment_dependency_order(dep_ents, dep_rels)
        strong = compute_strongly_connected_components(cycle_ents, cycle_rels)
    finally:
        sys.setrecursionlimit(old)
    assert chain["n_components_total"] == n
    assert chain["n_condensation_edges_total"] == n - 1
    assert chain["n_cyclic_components_total"] == 0
    assert chain["components"][0]["representative"] == "n000"
    assert chain["components"][-1]["representative"] == f"n{n-1:03d}"
    assert cycle["n_components_total"] == 1
    assert cycle["components"][0]["n_nodes_total"] == n
    assert cycle["components"][0]["is_cyclic"] is True
    assert cycle["n_condensation_edges_total"] == 0
    assert strong["n_components_total"] == 1
    assert len(order) == n
    assert order[0] == "d000"
    assert order[-1] == f"d{n-1:03d}"


def test_human_json_cli_parity_and_malformed_exit(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B"), _entity("C")],
        [_calls("A", "B"), _calls("B", "A"), _rel("C", "ghost", "contains")],
    )
    g = ByogGraph(graph)
    payload = g.condensation(edge_types=["calls", "contains"], max_components=10)
    dumped = dumps_condensation_json(payload)
    assert dumped == dumps_condensation_json(json.loads(dumped))
    human = format_condensation_human(payload)
    assert "edge_types: calls,contains" in human
    assert "cyclic" in human
    assert "ghost" in human
    assert "rows " in human
    common = [
        "condensation",
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
        "--max-edges",
        "100",
    ]
    gq_h = _run(sys.executable, str(QUERY), *common)
    gc_h = _run(sys.executable, str(CLI), *common)
    mod_h = _run(sys.executable, "-m", "graphrag_code.graph_query", *common)
    assert gq_h.stdout == gc_h.stdout == mod_h.stdout
    assert format_condensation_human(payload).strip() == gq_h.stdout.strip()
    gq_j = _run(sys.executable, str(QUERY), *common, "--json")
    gc_j = _run(sys.executable, str(CLI), *common, "--json")
    mod_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *common, "--json")
    assert gq_j.stdout == gc_j.stdout == mod_j.stdout
    assert dumps_condensation_json(json.loads(gq_j.stdout)) + "\n" == gq_j.stdout
    empty_graph = tmp_path / "empty"
    publish_byog_snapshot(
        pd.DataFrame(columns=["id", "title", "type", "source_file", "extractor"]),
        pd.DataFrame(columns=["id", "source", "target", "type", "extractor"]),
        pd.DataFrame(columns=["id", "title", "source_file"]),
        empty_graph,
        keep_last=1,
    )
    empty_h = _run(
        sys.executable, str(QUERY), "condensation", "--graph", str(empty_graph)
    )
    empty_j = _run(
        sys.executable,
        str(QUERY),
        "condensation",
        "--graph",
        str(empty_graph),
        "--json",
    )
    assert "n_nodes_total: 0" in empty_h.stdout
    assert "n_condensation_edges_total: 0" in empty_h.stdout
    assert empty_h.stdout.endswith("\n")
    assert json.loads(empty_j.stdout)["n_nodes_total"] == 0
    assert dumps_condensation_json(json.loads(empty_j.stdout)) + "\n" == empty_j.stdout
    bad = _run(
        sys.executable,
        str(QUERY),
        "condensation",
        "--graph",
        str(graph),
        "--max-components",
        "0",
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert "max_components" in bad.stderr
    snap_bad = _run(
        sys.executable,
        str(QUERY),
        "condensation",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        check=False,
    )
    assert snap_bad.returncode == 2
    assert snap_bad.stdout == ""
    help_out = _run(sys.executable, str(QUERY), "condensation", "--help")
    assert "--json" in help_out.stdout
    assert "--dot" in help_out.stdout
    assert "--edge-type" in help_out.stdout
    assert "--max-edges" in help_out.stdout
    assert "community" in help_out.stdout.lower() or "condensation" in help_out.stdout.lower()


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
    args = ["condensation", "--graph", str(graph), "--json"]
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
        "condensation",
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
        "condensation",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body["n_nodes_total"] == 1
    assert body["components"][0]["representative"] == "demo:old"
    assert body["components"][0]["is_cyclic"] is True
    assert "demo:new" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not list(outside.glob("*.dot"))
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


def _cond_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from graphrag_code import graph_query
    from typer.testing import CliRunner

    orig = graph_query.dumps_condensation_json

    def wrap_dumps(result):
        payload = orig(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_condensation_json = wrap_dumps
    runner = CliRunner()
    result = runner.invoke(
        graph_query.app,
        ["condensation", "--graph", graph, "--json"],
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


def test_publisher_waits_through_condensation_serialization(tmp_path: Path):
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_cond_json_hold, args=(str(graph), held, resume, q))
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


def test_no_nested_query_mcp_unchanged_and_existing_surfaces(
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

    monkeypatch.setattr(g, "components", track("components"))
    monkeypatch.setattr(g, "strong_components", track("strong_components"))
    monkeypatch.setattr(g, "subgraph", track("subgraph"))
    monkeypatch.setattr(g, "degree_ranking", track("degree_ranking"))
    monkeypatch.setattr(g, "dependency_order", track("dependency_order"))
    monkeypatch.setattr(g, "impact", track("impact"))
    producer_calls = 0
    scc_calls = 0
    orig = compute_condensation_graph
    orig_sccs = getattr(
        __import__("graphrag_code.byog_graph", fromlist=["_iterative_sccs"]),
        "_iterative_sccs",
    )

    def counted(ents, rels, **kwargs):
        nonlocal producer_calls
        producer_calls += 1
        return orig(ents, rels, **kwargs)

    def counted_sccs(*args, **kwargs):
        nonlocal scc_calls
        scc_calls += 1
        return orig_sccs(*args, **kwargs)

    monkeypatch.setattr(
        "graphrag_code.byog_graph.compute_condensation_graph", counted
    )
    monkeypatch.setattr("graphrag_code.byog_graph._iterative_sccs", counted_sccs)
    result = g.condensation()
    assert result["n_nodes_total"] == 2
    assert producer_calls == 1
    assert scc_calls == 1
    assert called == []

    other = ByogGraph(graph)
    sub = other.subgraph("A", direction="outgoing", max_depth=1)
    assert dumps_subgraph_json(sub)
    assert format_subgraph_human(sub)
    assert dumps_subgraph_dot(sub).startswith("digraph graphrag_subgraph")
    assert compute_bounded_subgraph(
        other.ents, other.rels, "A", direction="outgoing", max_depth=1
    ) == sub
    comps = compute_weakly_connected_components(other.ents, other.rels)
    assert dumps_components_json(comps)
    assert other.components() == comps
    ranked = compute_structural_degree_ranking(other.ents, other.rels)
    assert dumps_degree_ranking_json(ranked)
    assert other.degree_ranking() == ranked
    assert other.strong_components() == compute_strongly_connected_components(
        other.ents, other.rels
    )
    assert other.dependency_order() == compute_containment_dependency_order(
        other.ents, other.rels
    )
    assert isinstance(other.impact("B"), list)


def test_mcp_exposes_condensation_as_sixteenth_tool(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import GraphMcpSession, build_mcp_server, build_session

    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.condensation).parameters)
    assert params == [
        "self",
        "max_components",
        "max_nodes_per_component",
        "max_edges",
        "edge_types",
        "snapshot",
    ]
    assert "graph" not in params
    assert "format" not in params
    assert "dot" not in params
    assert "symbol" not in params
    assert "condensation-graph" not in TOOL_NAMES
    assert "condensation_graph" not in TOOL_NAMES
    assert not hasattr(session, "condensation_graph")
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
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 17

    expected = ByogGraph(graph).condensation()
    payload = session.condensation()
    assert payload["tool"] == "condensation"
    assert payload["ok"] is True
    assert payload["data"] == json.loads(json.dumps(expected, allow_nan=False, default=str))
    returned_nodes = sum(int(c["n_nodes_returned"]) for c in payload["data"]["components"])
    assert payload["total"] == (
        payload["data"]["n_components_total"]
        + payload["data"]["n_nodes_total"]
        + payload["data"]["n_condensation_edges_total"]
    )
    assert payload["returned"] == (
        payload["data"]["n_components_returned"]
        + returned_nodes
        + payload["data"]["n_condensation_edges_returned"]
    )
    assert payload["truncated"] is bool(
        payload["data"]["components_truncated"]
        or payload["data"]["nodes_truncated"]
        or payload["data"]["edges_truncated"]
    )
    strong = session.strong_components()
    assert strong["tool"] == "strong_components"
    ranked = session.degree_ranking()
    assert ranked["tool"] == "degree_ranking"
    comps = session.components()
    assert comps["tool"] == "components"
    assert compute_condensation_graph(ByogGraph(graph).ents, ByogGraph(graph).rels) == expected

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert "condensation-graph" not in names
            assert "condensation_graph" not in names
            assert names[names.index("strong_components") + 1] == "condensation"
            assert names[names.index("condensation") + 1] == "shortest_path"
            assert names[names.index("shortest_path") + 1] == "degree_ranking"
            tool = next(item for item in tools if item.name == "condensation")
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
            assert tool.input_schema["additionalProperties"] is False
            props = tool.input_schema.get("properties") or {}
            assert list(props) == [
                "max_components",
                "max_nodes_per_component",
                "max_edges",
                "edge_types",
                "snapshot",
            ]
            body = await client.call_tool("condensation", {})
            data = body.structured_content
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
            assert data["tool"] == "condensation"
            assert data["data"] == payload["data"]
            ranked = await client.call_tool("degree_ranking", {})
            assert ranked.is_error is False
            comps = await client.call_tool("components", {})
            assert comps.is_error is False
            strong_tool = await client.call_tool("strong_components", {})
            assert strong_tool.is_error is False

    anyio_run(_body)


def test_hash_seed_invariance(tmp_path: Path):
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_condensation_graph
ents = pd.DataFrame([
    {"id": "e1", "title": "Z", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "e2", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "e3", "title": "M", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "r1", "source": "Z", "target": "A", "type": "calls", "extractor": "x"},
    {"id": "r2", "source": "A", "target": "Z", "type": "calls", "extractor": "x"},
    {"id": "r3", "source": "M", "target": "ghost", "type": "contains", "extractor": "x"},
])
import json
print(json.dumps(compute_condensation_graph(ents, rels), sort_keys=True))
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
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        payloads.append(proc.stdout)
    assert payloads[0] == payloads[1] == payloads[2]
