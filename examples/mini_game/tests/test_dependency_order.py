"""Deterministic structural containment order over persisted contains rows.

Full-list compatibility surface. MCP does not expose this producer. No DOT,
NetworkX, or Graphviz.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
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
    ByogGraph,
    compute_bounded_subgraph,
    compute_containment_dependency_order,
    compute_structural_degree_ranking,
    compute_weakly_connected_components,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dependency_order as free_dependency_order,
    dumps_components_json,
    dumps_degree_ranking_json,
    dumps_dependency_order_json,
    dumps_subgraph_json,
    format_dependency_order_human,
    format_subgraph_human,
)
from graphrag_code.mcp_server import TOOL_NAMES  # type: ignore
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


def _contains(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "contains", hid=hid, **extra)


def _calls(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "calls", hid=hid, **extra)


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_dep"
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


def _oracle(ents: pd.DataFrame, rels: pd.DataFrame) -> list[str]:
    """Independent Tarjan SCC + heap-Kahn condensation oracle."""
    entity_titles = []
    seen_titles: set[str] = set()
    for title in ents["title"].astype(str).tolist() if len(ents) else []:
        if title in seen_titles:
            raise ValueError(f"duplicate entity title {title!r}")
        seen_titles.add(title)
        entity_titles.append(title)
    pairs: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    nodes = set(entity_titles)
    if len(rels):
        for _, row in rels.iterrows():
            rid = row["id"]
            if rid in seen_ids:
                raise ValueError(f"duplicate relationship id {rid!r}")
            seen_ids.add(rid)
            if str(row["type"]) != "contains":
                continue
            src = str(row["source"])
            tgt = str(row["target"])
            pairs.add((src, tgt))
            nodes.add(src)
            nodes.add(tgt)
    if not nodes:
        return []
    adj = {title: [] for title in nodes}
    for src, tgt in pairs:
        adj[src].append(tgt)
    for title in adj:
        adj[title] = sorted(set(adj[title]), key=lambda item: item.encode("utf-8"))
    index = 0
    stack: list[str] = []
    onstack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(vertex: str) -> None:
        nonlocal index
        indices[vertex] = index
        low[vertex] = index
        index += 1
        stack.append(vertex)
        onstack.add(vertex)
        for nxt in adj[vertex]:
            if nxt not in indices:
                strongconnect(nxt)
                low[vertex] = min(low[vertex], low[nxt])
            elif nxt in onstack:
                low[vertex] = min(low[vertex], indices[nxt])
        if low[vertex] == indices[vertex]:
            comp = []
            while True:
                item = stack.pop()
                onstack.remove(item)
                comp.append(item)
                if item == vertex:
                    break
            sccs.append(sorted(comp, key=lambda item: item.encode("utf-8")))

    for title in sorted(nodes, key=lambda item: item.encode("utf-8")):
        if title not in indices:
            strongconnect(title)
    title_to_scc = {
        title: i for i, members in enumerate(sccs) for title in members
    }
    cond_adj: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    indeg = [0] * len(sccs)
    for src, tgt in pairs:
        a = title_to_scc[src]
        b = title_to_scc[tgt]
        if a == b or b in cond_adj[a]:
            continue
        cond_adj[a].add(b)
        indeg[b] += 1
    import heapq

    heap = [
        (sccs[i][0].encode("utf-8"), i) for i, deg in enumerate(indeg) if deg == 0
    ]
    heapq.heapify(heap)
    ordered: list[str] = []
    while heap:
        _key, i = heapq.heappop(heap)
        ordered.extend(sccs[i])
        for nxt in cond_adj[i]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(heap, (sccs[nxt][0].encode("utf-8"), nxt))
    return ordered


def test_single_producer_and_public_parity():
    ents = pd.DataFrame([_entity("B"), _entity("A"), _entity("Z")])
    rels = pd.DataFrame([_contains("B", "A")])
    produced = compute_containment_dependency_order(ents, rels)
    assert produced == ["B", "A", "Z"]
    g = ByogGraph.__new__(ByogGraph)
    g.ents, g.rels = ents, rels
    assert list(inspect.signature(ByogGraph.dependency_order).parameters) == ["self"]
    assert g.dependency_order() == produced
    assert free_dependency_order(ents, rels) == produced
    src = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    methods = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dependency_order"
    ]
    assert len(methods) == 1
    body = ast.get_source_segment(src, methods[0])
    assert "compute_containment_dependency_order" in body
    assert "remaining" not in body
    gq = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(encoding="utf-8")
    fn = None
    for node in ast.parse(gq).body:
        if isinstance(node, ast.FunctionDef) and node.name == "dependency_order":
            fn = ast.get_source_segment(gq, node)
    assert fn is not None
    assert "compute_containment_dependency_order" in fn
    assert "indeg" not in fn


def test_priority_queue_ready_nodes_and_reentry():
    # Initial ready B and Z; B contains A; A is smaller than remaining Z.
    ents = pd.DataFrame([_entity("B"), _entity("A"), _entity("Z")])
    rels = pd.DataFrame([_contains("B", "A")])
    assert compute_containment_dependency_order(ents, rels) == ["B", "A", "Z"]
    # Simultaneously ready isolates plus one parent.
    ents = pd.DataFrame([_entity("M"), _entity("C"), _entity("A")])
    rels = pd.DataFrame([_contains("C", "A")])
    assert compute_containment_dependency_order(ents, rels) == ["C", "A", "M"]


def test_disconnected_dags_isolates_and_endpoint_only():
    ents = pd.DataFrame(
        [_entity("P"), _entity("X"), _entity("Isolated")]
    )
    rels = pd.DataFrame(
        [
            _contains("P", "Q", rid="rel:pq"),
            _contains("X", "Y", rid="rel:xy"),
        ]
    )
    order = compute_containment_dependency_order(ents, rels)
    assert order == ["Isolated", "P", "Q", "X", "Y"]
    assert order.index("P") < order.index("Q")
    assert order.index("X") < order.index("Y")
    ghost = compute_containment_dependency_order(
        pd.DataFrame([_entity("A")]),
        pd.DataFrame([_contains("A", "ghost")]),
    )
    assert ghost == ["A", "ghost"]


def test_non_contains_endpoints_excluded_unless_entities():
    ents = pd.DataFrame([_entity("A"), _entity("Keep")])
    rels = pd.DataFrame(
        [
            _calls("A", "ghost", rid="rel:call"),
            _contains("A", "Keep", rid="rel:keep"),
        ]
    )
    assert compute_containment_dependency_order(ents, rels) == ["A", "Keep"]
    assert "ghost" not in compute_containment_dependency_order(ents, rels)


def test_self_loops_parallel_rows_and_cycles():
    ents = pd.DataFrame([_entity("A"), _entity("B"), _entity("C"), _entity("X")])
    rels = pd.DataFrame(
        [
            _contains("A", "A", rid="rel:self"),
            _contains("A", "B", rid="rel:ab1"),
            _contains("A", "B", rid="rel:ab2"),
            _contains("B", "C", rid="rel:bc"),
            _contains("C", "A", rid="rel:ca"),
            _contains("X", "A", rid="rel:xa"),
            _contains("B", "Y", rid="rel:by"),
        ]
    )
    order = compute_containment_dependency_order(ents, rels)
    assert order == ["X", "A", "B", "C", "Y"]
    assert len(order) == len(set(order))
    cycle = order[order.index("A") : order.index("C") + 1]
    assert cycle == ["A", "B", "C"]
    assert order.index("X") < order.index("A")
    assert order.index("C") < order.index("Y")
    self_only = compute_containment_dependency_order(
        pd.DataFrame([_entity("A")]),
        pd.DataFrame([_contains("A", "A")]),
    )
    assert self_only == ["A"]


def test_cross_scc_edges_source_before_target():
    ents = pd.DataFrame(
        [_entity("A"), _entity("B"), _entity("C"), _entity("D"), _entity("E")]
    )
    rels = pd.DataFrame(
        [
            _contains("A", "B", rid="rel:ab"),
            _contains("B", "A", rid="rel:ba"),
            _contains("C", "A", rid="rel:ca"),
            _contains("B", "D", rid="rel:bd"),
            _contains("D", "E", rid="rel:de"),
            _contains("E", "D", rid="rel:ed"),
        ]
    )
    order = compute_containment_dependency_order(ents, rels)
    pos = {title: i for i, title in enumerate(order)}
    expected = _oracle(ents, rels)
    assert order == expected
    for src, tgt in {
        ("A", "B"),
        ("B", "A"),
        ("C", "A"),
        ("B", "D"),
        ("D", "E"),
        ("E", "D"),
    }:
        same_cycle = (
            {src, tgt} <= {"A", "B"} or {src, tgt} <= {"D", "E"}
        )
        if not same_cycle:
            assert pos[src] < pos[tgt]
    assert order.index("C") < min(order.index("A"), order.index("B"))
    assert max(order.index("A"), order.index("B")) < min(
        order.index("D"), order.index("E")
    )


def test_empty_graph_and_none_tables():
    assert compute_containment_dependency_order(pd.DataFrame(), pd.DataFrame()) == []
    assert compute_containment_dependency_order(None, None) == []
    dumped = dumps_dependency_order_json([])
    assert dumped == "[]"
    assert format_dependency_order_human([]) == ""


def test_shuffle_index_and_utf8_invariance():
    ents = pd.DataFrame([_entity("é"), _entity("Z"), _entity("A"), _entity("Ω")])
    rels = pd.DataFrame(
        [
            _contains("Z", "é", rid="rel:ze"),
            _contains("A", "Ω", rid="rel:ao"),
        ]
    )
    first = compute_containment_dependency_order(ents, rels)
    assert first == ["A", "Z", "é", "Ω"]
    assert "é".encode("utf-8") < "Ω".encode("utf-8")
    for seed in (1, 7, 11):
        shuffled_ents = ents.sample(frac=1, random_state=seed).reset_index(drop=True)
        shuffled_rels = rels.sample(frac=1, random_state=seed + 3)
        shuffled_rels.index = list(range(100, 100 + len(shuffled_rels)))
        assert compute_containment_dependency_order(shuffled_ents, shuffled_rels) == first


def test_duplicates_and_malformed_rows_fail_closed():
    ents = pd.DataFrame([_entity("A")])
    with pytest.raises(ValueError, match="duplicate entity title"):
        compute_containment_dependency_order(
            pd.DataFrame([_entity("A"), _entity("A", id="ent:other")]),
            pd.DataFrame(),
        )
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_containment_dependency_order(
            ents,
            pd.DataFrame(
                [_contains("A", "A", rid="rel:dup"), _contains("A", "A", rid="rel:dup")]
            ),
        )
    hidden = pd.DataFrame(
        [
            _contains("A", "A", rid="rel:ok"),
            {**_rel("A", "B", "calls"), "target": ""},
        ]
    )
    with pytest.raises(ValueError, match="invalid target"):
        compute_containment_dependency_order(ents, hidden)
    with pytest.raises(ValueError, match="invalid target"):
        compute_containment_dependency_order(
            ents,
            pd.DataFrame(
                [
                    {
                        "id": "rel:bad",
                        "source": "A",
                        "target": None,
                        "type": "contains",
                        "extractor": "tree-sitter-python",
                    }
                ]
            ),
        )
    with pytest.raises(ValueError, match="missing required columns"):
        compute_containment_dependency_order(pd.DataFrame([{"id": "x"}]), pd.DataFrame())
    with pytest.raises(ValueError, match="missing required columns"):
        compute_containment_dependency_order(
            ents, pd.DataFrame([{"source": "A", "target": "A"}])
        )


def test_randomized_small_graphs_match_independent_oracle():
    rng = random.Random(20260827)
    titles = ["A", "B", "C", "M", "Z", "é", "Ω"]
    for _ in range(12):
        n_ent = rng.randint(1, 6)
        chosen = rng.sample(titles, n_ent)
        ents = pd.DataFrame([_entity(title) for title in chosen])
        rels_rows = []
        for hid in range(rng.randint(0, 8)):
            src = rng.choice(chosen + ["ghost", "x-only"])
            tgt = rng.choice(chosen + ["ghost", "y-only"])
            rel_type = rng.choice(["contains", "contains", "calls"])
            rels_rows.append(
                _rel(src, tgt, rel_type, hid=hid + 1, rid=f"rel:{hid+1}")
            )
        rels = pd.DataFrame(rels_rows)
        produced = compute_containment_dependency_order(
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
        )
        assert produced == _oracle(ents, rels)
        assert len(produced) == len(set(produced))


def test_human_json_cli_parity_and_malformed_exit(tmp_path: Path):
    entities = [_entity("B"), _entity("A"), _entity("Z")]
    relationships = [_contains("B", "A")]
    graph = _publish(tmp_path, entities, relationships)
    g = ByogGraph(graph)
    payload = g.dependency_order()
    dumped = dumps_dependency_order_json(payload)
    assert dumped == dumps_dependency_order_json(json.loads(dumped))
    human = format_dependency_order_human(payload)
    common = ["dependency-order", "--graph", str(graph)]
    gq_h = _run(sys.executable, str(QUERY), *common)
    gc_h = _run(sys.executable, str(CLI), *common)
    mod_h = _run(sys.executable, "-m", "graphrag_code.graph_query", *common)
    assert gq_h.stdout == gc_h.stdout == mod_h.stdout
    assert human == gq_h.stdout[:-1]
    assert gq_h.stdout.endswith("\n")
    gq_j = _run(sys.executable, str(QUERY), *common, "--json")
    gc_j = _run(sys.executable, str(CLI), *common, "--json")
    mod_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *common, "--json")
    assert gq_j.stdout == gc_j.stdout == mod_j.stdout
    assert dumps_dependency_order_json(json.loads(gq_j.stdout)) + "\n" == gq_j.stdout
    empty_graph = tmp_path / "empty"
    publish_byog_snapshot(
        pd.DataFrame(columns=["id", "title", "type", "source_file", "extractor"]),
        pd.DataFrame(columns=["id", "source", "target", "type", "extractor"]),
        pd.DataFrame(columns=["id", "title", "source_file"]),
        empty_graph,
        keep_last=1,
    )
    empty_h = _run(
        sys.executable, str(QUERY), "dependency-order", "--graph", str(empty_graph)
    )
    empty_j = _run(
        sys.executable,
        str(QUERY),
        "dependency-order",
        "--graph",
        str(empty_graph),
        "--json",
    )
    assert empty_h.stdout == ""
    assert empty_j.stdout == "[]\n"
    bad = _run(
        sys.executable,
        str(QUERY),
        "dependency-order",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert bad.stderr != ""
    help_out = _run(sys.executable, str(QUERY), "dependency-order", "--help")
    assert "--json" in help_out.stdout
    assert "--dot" not in help_out.stdout
    assert "--max-nodes" not in help_out.stdout


def test_installed_wheel_parity_and_snapshots_do_not_mutate(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old")]),
        pd.DataFrame([_contains("demo:old", "demo:old", rid="rel:old")]),
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
        pd.DataFrame([_contains("demo:new", "demo:other", rid="rel:new")]),
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
    args = ["dependency-order", "--graph", str(graph), "--json"]
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
        "dependency-order",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--json",
    )
    assert json.loads(cur.stdout) == ["demo:new", "demo:other"]
    hist = _run(
        sys.executable,
        str(CLI),
        "dependency-order",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body == ["demo:old"]
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


def _dep_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from graphrag_code import graph_query
    from typer.testing import CliRunner

    orig = graph_query.dumps_dependency_order_json

    def wrap_dumps(result):
        payload = orig(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_dependency_order_json = wrap_dumps
    runner = CliRunner()
    result = runner.invoke(
        graph_query.app,
        ["dependency-order", "--graph", graph, "--json"],
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


def test_publisher_waits_through_dependency_order_serialization(tmp_path: Path):
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_contains("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_dep_json_hold, args=(str(graph), held, resume, q))
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
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_contains("A", "B")])
    g = ByogGraph(graph)
    called: list[str] = []

    def track(name):
        def _inner(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"nested public query {name}")

        return _inner

    monkeypatch.setattr(g, "components", track("components"))
    monkeypatch.setattr(g, "subgraph", track("subgraph"))
    monkeypatch.setattr(g, "degree_ranking", track("degree_ranking"))
    monkeypatch.setattr(g, "impact", track("impact"))
    producer_calls = 0
    orig = compute_containment_dependency_order

    def counted(ents, rels):
        nonlocal producer_calls
        producer_calls += 1
        return orig(ents, rels)

    monkeypatch.setattr(
        "graphrag_code.byog_graph.compute_containment_dependency_order", counted
    )
    result = g.dependency_order()
    assert result == ["A", "B"]
    assert producer_calls == 1
    assert called == []

    src = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(encoding="utf-8")
    helper = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute_containment_dependency_order":
            helper = ast.get_source_segment(src, node)
        if isinstance(node, ast.FunctionDef) and node.name == "_containment_scc_order":
            helper = (helper or "") + ast.get_source_segment(src, node)
        if isinstance(node, ast.FunctionDef) and node.name == "_scc_condensation_dag":
            helper = (helper or "") + ast.get_source_segment(src, node)
    assert helper is not None
    assert "_iterative_sccs" in helper
    assert "networkx" not in helper
    assert "graphviz" not in helper
    assert "subprocess" not in helper
    assert "tempfile" not in helper
    assert "socket" not in helper
    assert "urllib" not in helper
    assert "components(" not in helper
    assert "subgraph(" not in helper
    assert "degree_ranking(" not in helper

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
    assert isinstance(other.impact("B"), list)


def test_mcp_remains_fifteen_tools_without_dependency_order(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_contains("A", "B")])
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    assert not hasattr(session, "dependency_order")
    assert "dependency_order" not in TOOL_NAMES
    assert "dependency-order" not in TOOL_NAMES
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
        "degree_ranking",
        "impact",
        "type_closure",
        "context_pack",
        "snapshot_history",
        "snapshot_diff",
    ]
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == 15

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert "dependency_order" not in names
            assert "dependency-order" not in names
            for tool in tools:
                props = tool.input_schema.get("properties") or {}
                assert "dependency_order" not in props
            ranked = await client.call_tool("degree_ranking", {})
            assert ranked.is_error is False
            comps = await client.call_tool("components", {})
            assert comps.is_error is False

    anyio_run(_body)
