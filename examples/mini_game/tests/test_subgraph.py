"""Bounded cycle-safe multi-hop induced subgraph (structural exploration).

Does not modify extraction, overlays, MCP, or published byog_* roots.
"""
from __future__ import annotations

import hashlib
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
    HARD_MAX_SUBGRAPH_DEPTH,
    HARD_MAX_SUBGRAPH_EDGES,
    HARD_MAX_SUBGRAPH_NODES,
    ByogGraph,
    compute_bounded_subgraph,
    compute_uses_type_closure,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_subgraph_json,
    format_subgraph_human,
    neighbors as free_neighbors,
    subgraph as free_subgraph,
)
from graphrag_code.mcp_server import TOOL_NAMES  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"


def _entity(title: str, etype: str, **extra) -> dict:
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


def _uses(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "uses_type", hid=hid, fact_kind="configured_type_use", **extra)


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_subgraph"
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


def _edge_pairs(result: dict) -> list[tuple[str, str, str]]:
    return [(e["source"], e["target"], e["type"]) for e in result["edges"]]


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


# ---------------------------------------------------------------------------
# Pure BFS / topology
# ---------------------------------------------------------------------------


def test_resolution_directions_depth_and_induced_edges(tmp_path: Path):
    entities = [
        _entity("pkg:pkg", "module"),
        _entity("pkg:root", "function"),
        _entity("pkg:mid", "function"),
        _entity("pkg:leaf", "function"),
        _entity("pkg:user", "function"),
        _entity("pkg:other", "function"),
        _entity("pkg:run_unique", "function"),
    ]
    rels = [
        _calls("pkg:root", "pkg:mid", hid=1),
        _calls("pkg:mid", "pkg:leaf", hid=2),
        _calls("pkg:user", "pkg:root", hid=3),
        _uses("pkg:root", "pkg:mid", hid=4),
        _contains("pkg:pkg", "pkg:root", hid=5),
        _calls("pkg:other", "pkg:run_unique", hid=6),
        _rel("pkg:root", "pkg:root", "calls", rid="rel:self", hid=7),
        _calls("pkg:root", "pkg:mid", hid=8, rid="rel:parallel-root-mid"),
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)

    exact = g.subgraph("pkg:root", direction="outgoing", max_depth=2)
    assert exact["resolved"] is True
    assert exact["root"] == "pkg:root"
    assert exact["nodes"][0]["title"] == "pkg:root"
    assert _node_map(exact) == {"pkg:root": 0, "pkg:mid": 1, "pkg:leaf": 2}
    # Induced: self-edge + parallel A->mid + mid->leaf + root uses_type leaf.
    assert exact["n_nodes_total"] == 3
    assert {e["id"] for e in exact["edges"]} >= {
        "rel:self",
        "rel:parallel-root-mid",
    }
    self_edge = next(e for e in exact["edges"] if e["id"] == "rel:self")
    assert self_edge["source"] == "pkg:root" and self_edge["target"] == "pkg:root"
    assert [n["title"] for n in exact["nodes"]].count("pkg:root") == 1
    assert "pkg:other" not in _node_map(exact)
    assert "pkg:user" not in _node_map(exact)

    incoming = g.subgraph("pkg:root", direction="incoming", max_depth=1)
    assert set(_node_map(incoming)) == {"pkg:root", "pkg:user", "pkg:pkg"}
    user_edge = next(e for e in incoming["edges"] if e["source"] == "pkg:user")
    assert user_edge["source"] == "pkg:user" and user_edge["target"] == "pkg:root"

    both = g.subgraph("pkg:root", direction="both", max_depth=1)
    assert set(_node_map(both)) == {"pkg:root", "pkg:mid", "pkg:user", "pkg:pkg"}
    # Direction did not flip stored orientation.
    assert all(
        (e["source"], e["target"])
        in {
            ("pkg:root", "pkg:mid"),
            ("pkg:user", "pkg:root"),
            ("pkg:pkg", "pkg:root"),
            ("pkg:root", "pkg:root"),
            ("pkg:root", "pkg:leaf"),
        }
        for e in both["edges"]
    )

    d0 = g.subgraph("pkg:root", direction="outgoing", max_depth=0)
    assert _node_map(d0) == {"pkg:root": 0}
    assert any(e["id"] == "rel:self" for e in d0["edges"])
    assert d0["n_edges_total"] == 1

    d1 = g.subgraph("pkg:root", direction="outgoing", max_depth=1)
    assert _node_map(d1) == {"pkg:root": 0, "pkg:mid": 1}
    # leaf is not reachable at depth 1, so mid->leaf is not induced.
    assert ("pkg:mid", "pkg:leaf", "calls") not in _edge_pairs(d1)
    d2 = g.subgraph("pkg:root", direction="outgoing", max_depth=2)
    assert ("pkg:mid", "pkg:leaf", "calls") in _edge_pairs(d2)

    alias = g.subgraph("pkg", direction="outgoing", max_depth=1)
    assert alias["root"] == "pkg:pkg"
    partial = g.subgraph("run_unique", max_depth=1)
    assert partial["root"] == "pkg:run_unique"
    missing = g.subgraph("does-not-exist")
    assert missing["resolved"] is False
    assert missing["root"] is None
    assert missing["nodes"] == [] and missing["edges"] == []
    assert missing["n_nodes_total"] == 0 and missing["n_edges_total"] == 0
    amb = g.subgraph("pkg:")
    assert amb["resolved"] is False
    assert free_subgraph(g.ents, g.rels, "pkg:root", max_depth=1) == g.subgraph(
        "pkg:root", max_depth=1
    )
    # neighbors / type_closure contracts stay independent.
    assert g.neighbors("pkg:root") == free_neighbors(g.ents, g.rels, "pkg:root")
    assert compute_uses_type_closure(g.rels, "pkg:root", max_depth=1)[
        "n_edges_total"
    ] == 1


def test_cycles_self_parallel_disconnected_and_min_depth(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "function"),
        _entity("C", "function"),
        _entity("D", "function"),
        _entity("Z", "function"),
    ]
    rels = [
        _calls("A", "B", hid=1),
        _calls("B", "C", hid=2),
        _calls("C", "A", hid=3),
        _calls("A", "C", hid=4, rid="rel:shortcut"),  # shorter path to C
        _calls("A", "A", hid=5, rid="rel:self-a"),
        _calls("A", "B", hid=6, rid="rel:parallel-ab"),
        _calls("Z", "Z", hid=7, rid="rel:disconnected"),
    ]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).subgraph("A", direction="outgoing", max_depth=10)
    assert set(_node_map(r)) == {"A", "B", "C"}
    assert _node_map(r)["A"] == 0
    assert _node_map(r)["C"] == 1  # min depth via shortcut, not A->B->C
    assert "D" not in _node_map(r)
    assert "Z" not in _node_map(r)
    ids = [e["id"] for e in r["edges"]]
    assert "rel:self-a" in ids
    assert "rel:parallel-ab" in ids
    assert "rel:disconnected" not in ids
    assert r["n_nodes_returned"] == 3


def test_edge_type_filter_and_mixed_relationships(tmp_path: Path):
    entities = [_entity(t, "function") for t in ("A", "B", "C")]
    rels = [
        _calls("A", "B", hid=1),
        _contains("A", "B", hid=2),
        _uses("A", "C", hid=3),
        _calls("B", "C", hid=4),
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    only_calls = g.subgraph(
        "A",
        direction="outgoing",
        max_depth=2,
        edge_types=["calls", "calls"],
    )
    assert only_calls["edge_types"] == ["calls"]
    assert set(_node_map(only_calls)) == {"A", "B", "C"}
    assert {e["type"] for e in only_calls["edges"]} == {"calls"}
    mixed = g.subgraph(
        "A",
        direction="outgoing",
        max_depth=1,
        edge_types=["uses_type", "calls"],
    )
    assert mixed["edge_types"] == ["calls", "uses_type"]
    assert set(_node_map(mixed)) == {"A", "B", "C"}
    assert {e["type"] for e in mixed["edges"]} == {"calls", "uses_type"}
    none = g.subgraph(
        "A", direction="outgoing", max_depth=3, edge_types=["imports"]
    )
    assert none["n_nodes_total"] == 1
    assert none["n_edges_total"] == 0


def test_caps_root_preservation_and_limit_validation(tmp_path: Path):
    entities = [_entity("A", "function")] + [
        _entity(f"N{i}", "function") for i in range(5)
    ]
    rels = [_calls("A", f"N{i}", hid=i + 1, rid=f"rel:{i}") for i in range(5)]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).subgraph(
        "A", direction="outgoing", max_depth=1, max_nodes=2, max_edges=2
    )
    assert r["n_nodes_total"] == 6
    assert r["n_edges_total"] == 5
    assert r["n_nodes_returned"] == 2
    assert r["n_edges_returned"] == 1
    assert r["nodes_truncated"] is True
    assert r["edges_truncated"] is True
    assert r["nodes"][0]["title"] == "A"
    assert r["nodes"][0]["depth"] == 0
    returned_titles = {node["title"] for node in r["nodes"]}
    assert all(
        edge["source"] in returned_titles and edge["target"] in returned_titles
        for edge in r["edges"]
    )

    rels_df = pd.DataFrame(rels)
    ents_df = pd.DataFrame(entities)
    with pytest.raises(ValueError, match="max_depth"):
        compute_bounded_subgraph(ents_df, rels_df, "A", max_depth=-1)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_bounded_subgraph(ents_df, rels_df, "A", max_nodes=0)
    with pytest.raises(ValueError, match="max_edges"):
        compute_bounded_subgraph(ents_df, rels_df, "A", max_edges=-1)
    with pytest.raises(ValueError, match="direction"):
        compute_bounded_subgraph(ents_df, rels_df, "A", direction="sideways")
    with pytest.raises(ValueError, match="direction"):
        compute_bounded_subgraph(  # type: ignore[arg-type]
            ents_df, rels_df, "A", direction=[]
        )
    with pytest.raises(ValueError, match="root_title"):
        compute_bounded_subgraph(  # type: ignore[arg-type]
            ents_df, rels_df, 7
        )
    with pytest.raises(ValueError, match="max_depth"):
        compute_bounded_subgraph(
            ents_df, rels_df, "A", max_depth=HARD_MAX_SUBGRAPH_DEPTH + 1
        )
    with pytest.raises(ValueError, match="max_nodes"):
        compute_bounded_subgraph(
            ents_df, rels_df, "A", max_nodes=HARD_MAX_SUBGRAPH_NODES + 1
        )
    with pytest.raises(ValueError, match="max_edges"):
        compute_bounded_subgraph(
            ents_df, rels_df, "A", max_edges=HARD_MAX_SUBGRAPH_EDGES + 1
        )
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_bounded_subgraph(ents_df, rels_df, "A", edge_types=[""])
    with pytest.raises(ValueError, match="invalid edge-type filter"):
        compute_bounded_subgraph(ents_df, rels_df, "A", edge_types=[" calls"])
    with pytest.raises(ValueError, match="max_depth"):
        compute_bounded_subgraph(ents_df, rels_df, "A", max_depth=True)

    malformed = _calls("A", "B")
    malformed["target"] = None
    with pytest.raises(ValueError, match="invalid target"):
        compute_bounded_subgraph(
            pd.DataFrame([_entity("A", "function")]),
            pd.DataFrame([malformed]),
            "A",
        )
    dup = [_calls("A", "B", rid="rel:dup"), _calls("B", "C", rid="rel:dup")]
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_bounded_subgraph(
            pd.DataFrame([_entity("A", "function")]),
            pd.DataFrame(dup),
            "A",
        )


def test_utf8_ordering_json_nulls_and_determinism(tmp_path: Path):
    titles = ["A", "Z", "é", "Ā"]
    entities = [_entity(t, "function") for t in titles]
    entities[0]["description"] = None
    entities[1]["confidence"] = float("nan")
    entities[2]["span"] = pd.NA
    rels = [
        _calls("A", "Z", hid=1, rid="rel:z"),
        _calls("A", "é", hid=2, rid="rel:e"),
        _calls("A", "Ā", hid=3, rid="rel:a"),
    ]
    rels = [rels[2], rels[0], rels[1]]
    entities = [entities[3], entities[1], entities[2], entities[0]]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    r1 = g.subgraph("A", direction="outgoing", max_depth=1)
    r2 = g.subgraph("A", direction="outgoing", max_depth=1)
    assert r1 == r2
    shuffled = compute_bounded_subgraph(
        g.ents.sample(frac=1, random_state=7).reset_index(drop=True),
        g.rels.sample(frac=1, random_state=11).reset_index(drop=True),
        "A",
        direction="outgoing",
        max_depth=1,
    )
    assert shuffled == r1
    expected = ["A"] + sorted(titles[1:], key=lambda s: s.encode("utf-8"))
    assert [n["title"] for n in r1["nodes"]] == expected
    # UTF-8 target bytes: Z (0x5A), é (0xC3 0xA9), Ā (0xC4 0x80).
    assert [e["id"] for e in r1["edges"]] == ["rel:z", "rel:e", "rel:a"]
    payload = json.loads(dumps_subgraph_json(r1))
    assert payload["nodes"][0]["description"] is None
    z_node = next(n for n in payload["nodes"] if n["title"] == "Z")
    assert z_node["confidence"] is None
    e_node = next(n for n in payload["nodes"] if n["title"] == "é")
    assert e_node["span"] is None
    dumped = dumps_subgraph_json(r1)
    assert dumped == dumps_subgraph_json(json.loads(dumped))
    assert "nan" not in dumped.lower()

    inf_ent = pd.DataFrame([_entity("A", "function", confidence=math.inf)])
    inf_rel = pd.DataFrame([_calls("A", "A")])
    with pytest.raises(ValueError, match="non-finite"):
        compute_bounded_subgraph(inf_ent, inf_rel, "A", max_depth=0)


def test_cli_surfaces_malformed_exit_and_parity(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "function"),
        _entity("C", "function"),
    ]
    rels = [_calls("A", "B", hid=1), _uses("B", "C", hid=2)]
    graph = _publish(tmp_path, entities, rels)
    common = [
        "subgraph",
        "A",
        "--graph",
        str(graph),
        "--direction",
        "outgoing",
        "--max-depth",
        "2",
        "--max-nodes",
        "10",
        "--max-edges",
        "10",
        "--edge-type",
        "calls",
        "--edge-type",
        "uses_type",
    ]
    gq_h = _run(sys.executable, str(QUERY), *common)
    gc_h = _run(sys.executable, str(CLI), *common)
    mod_h = _run(sys.executable, "-m", "graphrag_code.graph_query", *common)
    assert gq_h.stdout == gc_h.stdout == mod_h.stdout
    gq_j = _run(sys.executable, str(QUERY), *common, "--json")
    gc_j = _run(sys.executable, str(CLI), *common, "--json")
    mod_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *common, "--json")
    assert gq_j.stdout == gc_j.stdout == mod_j.stdout
    payload = json.loads(gq_j.stdout)
    assert payload["n_nodes_total"] == 3
    assert payload["edge_types"] == ["calls", "uses_type"]
    assert dumps_subgraph_json(payload) + "\n" == gq_j.stdout
    assert format_subgraph_human(payload).strip() == gq_h.stdout.strip()

    bad = _run(
        sys.executable,
        str(QUERY),
        "subgraph",
        "A",
        "--graph",
        str(graph),
        "--max-nodes",
        "0",
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert "max_nodes" in bad.stderr

    empty_filter = _run(
        sys.executable,
        str(CLI),
        "subgraph",
        "A",
        "--graph",
        str(graph),
        "--edge-type",
        "",
        check=False,
    )
    assert empty_filter.returncode == 2
    assert empty_filter.stdout == ""

    missing = _run(
        sys.executable,
        str(QUERY),
        "subgraph",
        "missing",
        "--graph",
        str(graph),
        "--json",
    )
    miss = json.loads(missing.stdout)
    assert miss["resolved"] is False
    assert miss["n_nodes_total"] == 0
    assert missing.returncode == 0


def test_current_and_historical_snapshot_reads_do_not_mutate(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old", "function")]),
        pd.DataFrame([_calls("demo:old", "demo:old", rid="rel:old")]),
        pd.DataFrame(
            [{"id": "tu:old", "text": "old", "n_tokens": 1, "document_ids": [], "entity_ids": ["ent:function:demo:old"], "relationship_ids": []}]
        ),
        graph,
        keep_last=5,
    )
    newer = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:new", "function")]),
        pd.DataFrame([_calls("demo:new", "demo:new", rid="rel:new")]),
        pd.DataFrame(
            [{"id": "tu:new", "text": "new", "n_tokens": 1, "document_ids": [], "entity_ids": ["ent:function:demo:new"], "relationship_ids": []}]
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
        "subgraph",
        "demo:new",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--json",
    )
    assert json.loads(cur.stdout)["root"] == "demo:new"
    hist = _run(
        sys.executable,
        str(CLI),
        "subgraph",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body["root"] == "demo:old"
    assert "demo:new" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


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


def _subgraph_json_hold(graph: str, symbol: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import json as json_mod

    orig = json_mod.dumps

    def wrapped(*args, **kwargs):
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return orig(*args, **kwargs)

    json_mod.dumps = wrapped
    from typer.testing import CliRunner

    from graphrag_code.graph_query import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["subgraph", symbol, "--graph", graph, "--json"],
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


def test_publisher_waits_through_serialization_scope(tmp_path: Path):
    entities = [_entity("A", "function"), _entity("B", "function")]
    graph = _publish(tmp_path, entities, [_calls("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_subgraph_json_hold, args=(str(graph), "A", held, resume, q)
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


def test_no_nested_public_queries_or_mcp_subgraph_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    entities = [_entity("A", "function"), _entity("B", "function")]
    graph = _publish(tmp_path, entities, [_calls("A", "B")])
    g = ByogGraph(graph)
    called: list[str] = []

    def track(name):
        def _inner(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"nested public query {name}")

        return _inner

    monkeypatch.setattr(g, "neighbors", track("neighbors"))
    monkeypatch.setattr(g, "type_closure", track("type_closure"))
    monkeypatch.setattr(g, "callers", track("callers"))
    monkeypatch.setattr(g, "callees", track("callees"))
    monkeypatch.setattr(g, "impact", track("impact"))
    result = g.subgraph("A", max_depth=1)
    assert result["resolved"] is True
    assert called == []

    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 11
            assert "subgraph" not in names

    anyio_run(_body)


def test_installed_console_module_script_json_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    entities = [_entity("A", "function"), _entity("B", "function")]
    graph = _publish(tmp_path, entities, [_calls("A", "B")])
    args = [
        "subgraph",
        "A",
        "--graph",
        str(graph),
        "--direction",
        "outgoing",
        "--max-depth",
        "1",
        "--json",
    ]
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
    assert json.loads(script.stdout)["root"] == "A"
