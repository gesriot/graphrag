"""Raw directed relationship-row degree ranking.

Structural accounting only. Does not add MCP, DOT, NetworkX, or Graphviz.
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
    DEFAULT_DEGREE_RANKING_MAX_NODES,
    DEGREE_RANKING_MODES,
    HARD_MAX_DEGREE_RANKING_NODES,
    ByogGraph,
    compute_bounded_subgraph,
    compute_structural_degree_ranking,
    compute_weakly_connected_components,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    degree_ranking as free_degree_ranking,
    dumps_components_json,
    dumps_degree_ranking_json,
    dumps_subgraph_json,
    format_degree_ranking_human,
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
    graph = tmp_path / "byog_degree"
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


def _fixture_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    ents = pd.DataFrame(
        [
            _entity("A"),
            _entity("B"),
            _entity("C"),
            _entity("Isolated"),
        ]
    )
    rels = pd.DataFrame(
        [
            _calls("A", "B", hid=1),
            _calls("A", "B", hid=2, rid="rel:parallel"),
            _calls("A", "A", hid=3, rid="rel:self"),
            _calls("B", "ghost", hid=4, rid="rel:endpoint"),
            _rel("C", "Isolated", "contains", hid=5),
        ]
    )
    return ents, rels


def _oracle(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    *,
    rank_by: str = "total",
    edge_types: list[str] | None = None,
    max_nodes: int = DEFAULT_DEGREE_RANKING_MAX_NODES,
) -> dict:
    """Independent brute-force degree ranking over well-formed tables."""
    entity_titles = {str(title) for title in ents["title"].tolist()}
    if edge_types:
        allow = sorted(set(edge_types), key=lambda item: item.encode("utf-8"))
        allow_set = set(allow)
    else:
        allow = None
        allow_set = None
    selected: list[tuple[str, str]] = []
    for _, row in rels.iterrows():
        rel_type = str(row["type"])
        if allow_set is not None and rel_type not in allow_set:
            continue
        selected.append((str(row["source"]), str(row["target"])))
    nodes = set(entity_titles)
    for src, tgt in selected:
        nodes.add(src)
        nodes.add(tgt)
    incoming = {title: 0 for title in nodes}
    outgoing = {title: 0 for title in nodes}
    for src, tgt in selected:
        outgoing[src] += 1
        incoming[tgt] += 1
    records = []
    for title in nodes:
        inn = incoming[title]
        out = outgoing[title]
        records.append(
            {
                "title": title,
                "in_degree": inn,
                "out_degree": out,
                "total_degree": inn + out,
                "is_entity": title in entity_titles,
            }
        )

    def key(rec: dict) -> tuple:
        title_key = rec["title"].encode("utf-8")
        inn = -rec["in_degree"]
        out = -rec["out_degree"]
        tot = -rec["total_degree"]
        if rank_by == "incoming":
            return (inn, tot, out, title_key)
        if rank_by == "outgoing":
            return (out, tot, inn, title_key)
        return (tot, inn, out, title_key)

    records.sort(key=key)
    n_edges = len(selected)
    sum_in = sum(rec["in_degree"] for rec in records)
    sum_out = sum(rec["out_degree"] for rec in records)
    returned = records[:max_nodes]
    return {
        "rank_by": rank_by,
        "edge_types": allow if allow else None,
        "max_nodes": max_nodes,
        "nodes": returned,
        "n_nodes_total": len(records),
        "n_nodes_returned": len(returned),
        "n_edges_total": n_edges,
        "n_entity_nodes_total": len(entity_titles),
        "n_endpoint_only_nodes_total": len(records) - len(entity_titles),
        "sum_in_degree": sum_in,
        "sum_out_degree": sum_out,
        "sum_total_degree": sum_in + sum_out,
        "nodes_truncated": len(records) > len(returned),
    }


def test_directed_self_loop_parallel_isolate_and_endpoint_degrees():
    ents, rels = _fixture_tables()
    result = compute_structural_degree_ranking(ents, rels)
    assert result["rank_by"] == "total"
    assert result["edge_types"] is None
    assert result["max_nodes"] == DEFAULT_DEGREE_RANKING_MAX_NODES
    assert result["n_nodes_total"] == 5
    assert result["n_nodes_returned"] == 5
    assert result["n_edges_total"] == 5
    assert result["n_entity_nodes_total"] == 4
    assert result["n_endpoint_only_nodes_total"] == 1
    assert result["sum_in_degree"] == 5
    assert result["sum_out_degree"] == 5
    assert result["sum_total_degree"] == 10
    assert result["nodes_truncated"] is False
    by_title = {node["title"]: node for node in result["nodes"]}
    assert by_title["A"] == {
        "title": "A",
        "in_degree": 1,
        "out_degree": 3,
        "total_degree": 4,
        "is_entity": True,
    }
    assert by_title["B"]["in_degree"] == 2
    assert by_title["B"]["out_degree"] == 1
    assert by_title["B"]["total_degree"] == 3
    assert by_title["ghost"] == {
        "title": "ghost",
        "in_degree": 1,
        "out_degree": 0,
        "total_degree": 1,
        "is_entity": False,
    }
    assert by_title["C"] == {
        "title": "C",
        "in_degree": 0,
        "out_degree": 1,
        "total_degree": 1,
        "is_entity": True,
    }
    assert by_title["Isolated"] == {
        "title": "Isolated",
        "in_degree": 1,
        "out_degree": 0,
        "total_degree": 1,
        "is_entity": True,
    }
    assert [node["title"] for node in result["nodes"]] == [
        "A",
        "B",
        "Isolated",
        "ghost",
        "C",
    ]
    g = ByogGraph.__new__(ByogGraph)
    g.ents, g.rels = ents, rels
    assert g.degree_ranking() == result
    assert free_degree_ranking(ents, rels) == result
    dumped = dumps_degree_ranking_json(result)
    for leak in ('"description"', '"snippet"', '"span"', '"weight"', '"confidence"', '"id"'):
        assert leak not in dumped
    assert '"rank"' not in dumped


def test_edge_type_filter_excludes_filtered_only_endpoints():
    ents, rels = _fixture_tables()
    all_types = compute_structural_degree_ranking(ents, rels, edge_types=None)
    empty_filter = compute_structural_degree_ranking(ents, rels, edge_types=[])
    assert all_types == empty_filter
    calls_only = compute_structural_degree_ranking(
        ents, rels, edge_types=["calls", "calls"]
    )
    assert calls_only["edge_types"] == ["calls"]
    titles = {node["title"] for node in calls_only["nodes"]}
    assert titles == {"A", "B", "C", "Isolated", "ghost"}
    by_title = {node["title"]: node for node in calls_only["nodes"]}
    assert by_title["C"]["total_degree"] == 0
    assert by_title["Isolated"]["total_degree"] == 0
    assert by_title["ghost"]["is_entity"] is False
    contains_only = compute_structural_degree_ranking(
        ents, rels, edge_types=["contains"]
    )
    titles = {node["title"] for node in contains_only["nodes"]}
    assert titles == {"A", "B", "C", "Isolated"}
    assert "ghost" not in titles
    assert contains_only["n_endpoint_only_nodes_total"] == 0
    mixed = compute_structural_degree_ranking(
        ents, rels, edge_types=["contains", "calls"]
    )
    assert mixed["edge_types"] == ["calls", "contains"]
    with pytest.raises(ValueError, match="not a single string"):
        compute_structural_degree_ranking(ents, rels, edge_types="calls")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_structural_degree_ranking(ents, rels, edge_types=[""])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_structural_degree_ranking(ents, rels, edge_types=[" calls"])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_structural_degree_ranking(ents, rels, edge_types=["ca\x00lls"])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_structural_degree_ranking(ents, rels, edge_types=["calls", 1])  # type: ignore[list-item]


def test_malformed_rows_cannot_hide_behind_filter():
    ents = pd.DataFrame([_entity("A")])
    hidden = pd.DataFrame(
        [_calls("A", "A", rid="rel:ok"), {**_rel("A", "B", "contains"), "target": ""}]
    )
    with pytest.raises(ValueError, match="invalid target"):
        compute_structural_degree_ranking(ents, hidden, edge_types=["calls"])
    with pytest.raises(ValueError, match="duplicate entity title"):
        compute_structural_degree_ranking(
            pd.DataFrame([_entity("A"), _entity("A", id="ent:other")]),
            pd.DataFrame(),
        )
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_structural_degree_ranking(
            ents,
            pd.DataFrame(
                [_calls("A", "A", rid="rel:dup"), _calls("A", "A", rid="rel:dup")]
            ),
        )
    with pytest.raises(ValueError, match="missing required columns"):
        compute_structural_degree_ranking(pd.DataFrame([{"id": "x"}]), pd.DataFrame())
    with pytest.raises(ValueError, match="missing required columns"):
        compute_structural_degree_ranking(
            ents, pd.DataFrame([{"source": "A", "target": "A"}])
        )
    bad = _calls("A", "B")
    bad["target"] = None
    with pytest.raises(ValueError, match="invalid target"):
        compute_structural_degree_ranking(ents, pd.DataFrame([bad]))
    with pytest.raises(ValueError, match="entities must be a dataframe"):
        compute_structural_degree_ranking(["A"], pd.DataFrame())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="relationships must be a dataframe"):
        compute_structural_degree_ranking(ents, {"source": "A"})  # type: ignore[arg-type]


def test_limit_and_rank_by_validation():
    ents = pd.DataFrame([_entity("A")])
    rels = pd.DataFrame()
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(ents, rels, max_nodes=True)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(ents, rels, max_nodes=1.5)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(ents, rels, max_nodes=float("nan"))
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(ents, rels, max_nodes=math.inf)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(ents, rels, max_nodes=0)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(ents, rels, max_nodes=-1)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_structural_degree_ranking(
            ents, rels, max_nodes=HARD_MAX_DEGREE_RANKING_NODES + 1
        )
    with pytest.raises(ValueError, match="rank_by"):
        compute_structural_degree_ranking(ents, rels, rank_by="pagerank")
    with pytest.raises(ValueError, match="rank_by"):
        compute_structural_degree_ranking(ents, rels, rank_by=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rank_by"):
        compute_structural_degree_ranking(ents, rels, rank_by=1)  # type: ignore[arg-type]
    assert DEGREE_RANKING_MODES == ("total", "incoming", "outgoing")


def test_canonical_ranking_orders_ties_and_utf8():
    ents = pd.DataFrame(
        [_entity("Z"), _entity("A"), _entity("é"), _entity("M")]
    )
    rels = pd.DataFrame(
        [
            _calls("A", "Z", hid=1),
            _calls("A", "Z", hid=2, rid="rel:az-2"),
            _calls("é", "M", hid=3),
        ]
    )
    total = compute_structural_degree_ranking(ents, rels, rank_by="total")
    incoming = compute_structural_degree_ranking(ents, rels, rank_by="incoming")
    outgoing = compute_structural_degree_ranking(ents, rels, rank_by="outgoing")
    assert [node["title"] for node in total["nodes"]] == ["Z", "A", "M", "é"]
    assert [node["title"] for node in incoming["nodes"]] == ["Z", "M", "A", "é"]
    assert [node["title"] for node in outgoing["nodes"]] == ["A", "é", "Z", "M"]
    zero_ents = pd.DataFrame([_entity("é"), _entity("Z"), _entity("A")])
    zeros = compute_structural_degree_ranking(zero_ents, pd.DataFrame())
    assert [node["title"] for node in zeros["nodes"]] == ["A", "Z", "é"]
    assert "é".encode("utf-8") > "Z".encode("utf-8")


def test_determinism_truncation_invariants_and_empty_graph():
    ents, rels = _fixture_tables()
    first = compute_structural_degree_ranking(ents, rels, rank_by="incoming", max_nodes=2)
    second = compute_structural_degree_ranking(
        ents.sample(frac=1, random_state=7).reset_index(drop=True),
        rels.sample(frac=1, random_state=11).reset_index(drop=True),
        rank_by="incoming",
        max_nodes=2,
    )
    assert first == second == compute_structural_degree_ranking(
        ents, rels, rank_by="incoming", max_nodes=2
    )
    assert first["n_nodes_total"] == 5
    assert first["n_nodes_returned"] == 2
    assert first["nodes_truncated"] is True
    assert first["n_edges_total"] == 5
    assert first["sum_in_degree"] == 5
    assert first["sum_out_degree"] == 5
    assert first["sum_total_degree"] == 10
    assert sum(node["in_degree"] for node in first["nodes"]) != first["sum_in_degree"]
    empty = compute_structural_degree_ranking(pd.DataFrame(), pd.DataFrame())
    assert empty["nodes"] == []
    assert empty["n_nodes_total"] == 0
    assert empty["n_edges_total"] == 0
    assert empty["sum_in_degree"] == 0
    assert empty["sum_out_degree"] == 0
    assert empty["sum_total_degree"] == 0
    assert empty["nodes_truncated"] is False
    assert compute_structural_degree_ranking(None, None)["n_nodes_total"] == 0


def test_human_json_cli_parity_and_malformed_exit(tmp_path: Path):
    ents, rels = _fixture_tables()
    graph = _publish(tmp_path, ents.to_dict("records"), rels.to_dict("records"))
    g = ByogGraph(graph)
    payload = g.degree_ranking(rank_by="outgoing", edge_types=["calls"], max_nodes=10)
    dumped = dumps_degree_ranking_json(payload)
    assert dumped == dumps_degree_ranking_json(json.loads(dumped))
    human = format_degree_ranking_human(payload)
    assert "rank_by: outgoing" in human
    assert "edge_types: calls" in human
    assert "endpoint-only" in human
    assert "incoming" in human
    common = [
        "degree-ranking",
        "--graph",
        str(graph),
        "--rank-by",
        "outgoing",
        "--edge-type",
        "calls",
        "--max-nodes",
        "10",
    ]
    gq_h = _run(sys.executable, str(QUERY), *common)
    gc_h = _run(sys.executable, str(CLI), *common)
    mod_h = _run(sys.executable, "-m", "graphrag_code.graph_query", *common)
    assert gq_h.stdout == gc_h.stdout == mod_h.stdout
    assert format_degree_ranking_human(payload).strip() == gq_h.stdout.strip()
    assert gq_h.stdout.endswith("\n")
    assert gq_h.stdout[:-1] == format_degree_ranking_human(payload)
    gq_j = _run(sys.executable, str(QUERY), *common, "--json")
    gc_j = _run(sys.executable, str(CLI), *common, "--json")
    mod_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *common, "--json")
    assert gq_j.stdout == gc_j.stdout == mod_j.stdout
    assert dumps_degree_ranking_json(json.loads(gq_j.stdout)) + "\n" == gq_j.stdout
    bad = _run(
        sys.executable,
        str(QUERY),
        "degree-ranking",
        "--graph",
        str(graph),
        "--max-nodes",
        "0",
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert "max_nodes" in bad.stderr
    help_out = _run(sys.executable, str(QUERY), "degree-ranking", "--help")
    assert "--json" in help_out.stdout
    assert "--dot" not in help_out.stdout
    assert "pagerank" in help_out.stdout.lower() or "structural" in help_out.stdout.lower()


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
    args = ["degree-ranking", "--graph", str(graph), "--json"]
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
        "degree-ranking",
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
        "degree-ranking",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body["n_nodes_total"] == 1
    assert body["nodes"][0]["title"] == "demo:old"
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


def _degree_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from graphrag_code import graph_query
    from typer.testing import CliRunner

    orig = graph_query.dumps_degree_ranking_json

    def wrap_dumps(result):
        payload = orig(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_degree_ranking_json = wrap_dumps
    runner = CliRunner()
    result = runner.invoke(
        graph_query.app,
        ["degree-ranking", "--graph", graph, "--json"],
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


def test_publisher_waits_through_degree_ranking_serialization(tmp_path: Path):
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_degree_json_hold, args=(str(graph), held, resume, q))
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


def test_no_nested_query_networkx_and_existing_surfaces_unchanged(
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
    monkeypatch.setattr(g, "subgraph", track("subgraph"))
    monkeypatch.setattr(g, "neighbors", track("neighbors"))
    monkeypatch.setattr(g, "impact", track("impact"))
    result = g.degree_ranking()
    assert result["n_nodes_total"] == 2
    assert called == []

    src = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    helper = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute_structural_degree_ranking":
            helper = ast.get_source_segment(src, node)
    assert helper is not None
    assert "networkx" not in helper
    assert "graphviz" not in helper
    assert "subprocess" not in helper
    assert "tempfile" not in helper
    assert "socket" not in helper
    assert "urllib" not in helper
    assert "components(" not in helper
    assert "subgraph(" not in helper

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


def test_mcp_remains_thirteen_tools_without_degree_ranking(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    assert not hasattr(session, "degree_ranking")
    assert "degree_ranking" not in TOOL_NAMES
    assert "degree-ranking" not in TOOL_NAMES
    params = list(inspect.signature(GraphMcpSession.components).parameters)
    assert "degree_ranking" not in params
    assert "rank_by" not in params

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 13
            assert "degree_ranking" not in names
            assert "degree-ranking" not in names
            assert names[names.index("subgraph") + 1] == "components"
            for tool in tools:
                props = tool.input_schema.get("properties") or {}
                assert "degree_ranking" not in props
                assert "rank_by" not in props or tool.name != "components"

    anyio_run(_body)


def test_randomized_small_multigraph_matches_independent_oracle():
    rng = random.Random(20260827)
    titles = ["A", "B", "C", "M", "Z", "é", "Ω"]
    types = ["calls", "contains", "uses_type"]
    for _ in range(12):
        n_ent = rng.randint(1, 6)
        chosen = rng.sample(titles, n_ent)
        ents = pd.DataFrame([_entity(title) for title in chosen])
        rels_rows = []
        for hid in range(rng.randint(0, 8)):
            src = rng.choice(chosen + ["ghost", "x-only"])
            tgt = rng.choice(chosen + ["ghost", "y-only"])
            rels_rows.append(
                _rel(
                    src,
                    tgt,
                    rng.choice(types),
                    hid=hid + 1,
                    rid=f"rel:{hid+1}",
                )
            )
        rels = pd.DataFrame(rels_rows)
        edge_types = rng.choice([None, [], ["calls"], ["contains", "calls"]])
        rank_by = rng.choice(list(DEGREE_RANKING_MODES))
        max_nodes = rng.randint(1, 7)
        produced = compute_structural_degree_ranking(
            ents.sample(frac=1, random_state=rng.randint(1, 10_000)).reset_index(
                drop=True
            )
            if len(ents)
            else ents,
            rels.sample(frac=1, random_state=rng.randint(1, 10_000)).reset_index(
                drop=True
            )
            if len(rels)
            else rels,
            rank_by=rank_by,
            edge_types=edge_types,
            max_nodes=max_nodes,
        )
        expected = _oracle(
            ents,
            rels,
            rank_by=rank_by,
            edge_types=edge_types,
            max_nodes=max_nodes,
        )
        assert produced == expected
        assert produced["sum_in_degree"] == produced["n_edges_total"]
        assert produced["sum_out_degree"] == produced["n_edges_total"]
        assert produced["sum_total_degree"] == 2 * produced["n_edges_total"]
