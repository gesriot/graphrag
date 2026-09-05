"""Directed structural shortest-path query over persisted relationship rows.

CLI/Python only. MCP does not expose this producer. No DOT, NetworkX, or
Graphviz.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import multiprocessing
import os
import random
import stat
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.byog_graph import (  # type: ignore
    DEFAULT_SHORTEST_PATH_MAX_DEPTH,
    HARD_MAX_SHORTEST_PATH_DEPTH,
    ByogGraph,
    compute_shortest_path,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_shortest_path_json,
    format_shortest_path_human,
    shortest_path as free_shortest_path,
)
from graphrag_code.mcp_server import TOOL_NAMES  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"

TOP_KEYS = {
    "source",
    "target",
    "source_resolved",
    "target_resolved",
    "status",
    "found",
    "edge_types",
    "max_depth",
    "distance",
    "nodes",
    "steps",
    "n_nodes_returned",
    "n_steps_returned",
    "n_relationship_rows_on_path_total",
}
STEP_KEYS = {"source", "target", "n_relationship_rows_total"}


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


def _frames(entities: list, relationships: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    return pd.DataFrame(entities), pd.DataFrame(relationships)


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_shortest"
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
    assert result["status"] in {
        "found",
        "unresolved_source",
        "unresolved_target",
        "unresolved_both",
        "not_found_within_max_depth",
    }
    assert result["found"] is (result["status"] == "found")
    assert result["n_nodes_returned"] == len(result["nodes"])
    assert result["n_steps_returned"] == len(result["steps"])
    for step in result["steps"]:
        assert set(step) == STEP_KEYS
    if result["found"]:
        assert result["source_resolved"] is True
        assert result["target_resolved"] is True
        assert result["distance"] == len(result["steps"])
        assert len(result["nodes"]) == result["distance"] + 1
        assert result["nodes"][0] == result["source"]
        assert result["nodes"][-1] == result["target"]
        total = 0
        for index, step in enumerate(result["steps"]):
            assert step["source"] == result["nodes"][index]
            assert step["target"] == result["nodes"][index + 1]
            assert int(step["n_relationship_rows_total"]) >= 1
            total += int(step["n_relationship_rows_total"])
        assert result["n_relationship_rows_on_path_total"] == total
    else:
        assert result["distance"] is None
        assert result["nodes"] == []
        assert result["steps"] == []
        assert result["n_nodes_returned"] == 0
        assert result["n_steps_returned"] == 0
        assert result["n_relationship_rows_on_path_total"] == 0


def _oracle_shortest_path(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    source: str,
    target: str,
    *,
    edge_types=None,
    max_depth: int = DEFAULT_SHORTEST_PATH_MAX_DEPTH,
) -> dict:
    """Independent all-shortest-paths oracle; then UTF-8-min complete path."""
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
    pair_counts: dict[tuple[str, str], int] = {}
    adj: dict[str, set[str]] = defaultdict(set)
    allow_set = None if allow is None else set(allow)
    if len(rels):
        for _, row in rels.iterrows():
            if allow_set is not None and str(row["type"]) not in allow_set:
                continue
            src = str(row["source"])
            tgt = str(row["target"])
            pair_counts[(src, tgt)] = pair_counts.get((src, tgt), 0) + 1
            if src != tgt:
                adj[src].add(tgt)
    if source == target:
        return {
            "status": "found",
            "found": True,
            "distance": 0,
            "nodes": [source],
            "steps": [],
        }
    dist = {source: 0}
    parents: dict[str, list[str]] = {source: []}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        if dist[node] >= max_depth:
            continue
        for nxt in sorted(adj.get(node, ()), key=lambda title: title.encode("utf-8")):
            nxt_dist = dist[node] + 1
            if nxt not in dist:
                dist[nxt] = nxt_dist
                parents[nxt] = [node]
                queue.append(nxt)
            elif dist[nxt] == nxt_dist:
                parents[nxt].append(node)
    if target not in dist:
        return {"status": "not_found_within_max_depth", "found": False}
    paths: list[list[str]] = []

    def walk(node: str, acc: list[str]) -> None:
        if node == source:
            paths.append(list(reversed(acc + [node])))
            return
        for parent in parents[node]:
            walk(parent, acc + [node])

    walk(target, [])
    best = min(paths, key=lambda titles: [title.encode("utf-8") for title in titles])
    steps = [
        {
            "source": src,
            "target": tgt,
            "n_relationship_rows_total": pair_counts[(src, tgt)],
        }
        for src, tgt in zip(best, best[1:])
    ]
    return {
        "status": "found",
        "found": True,
        "distance": len(steps),
        "nodes": best,
        "steps": steps,
    }


def test_one_hop_and_multi_hop_and_oracle(tmp_path: Path):
    ents, rels = _frames(
        [_entity("A"), _entity("B"), _entity("C"), _entity("D")],
        [_calls("A", "B"), _calls("B", "C"), _calls("C", "D")],
    )
    one = compute_shortest_path(ents, rels, "A", "B")
    _assert_schema(one)
    assert one["status"] == "found"
    assert one["distance"] == 1
    assert one["nodes"] == ["A", "B"]
    assert one["steps"] == [
        {"source": "A", "target": "B", "n_relationship_rows_total": 1}
    ]
    multi = compute_shortest_path(ents, rels, "A", "D")
    _assert_schema(multi)
    assert multi["nodes"] == ["A", "B", "C", "D"]
    assert multi["distance"] == 3
    oracle = _oracle_shortest_path(ents, rels, "A", "D")
    assert multi["nodes"] == oracle["nodes"]
    assert multi["steps"] == oracle["steps"]
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B"), _entity("C"), _entity("D")],
        [_calls("A", "B"), _calls("B", "C"), _calls("C", "D")],
    )
    live = ByogGraph(graph).shortest_path("A", "D")
    assert live == multi
    assert free_shortest_path(ents, rels, "A", "D") == multi


def test_diamond_utf8_tie_break_row_order_and_hash_seed():
    entities = [_entity("S"), _entity("A"), _entity("B"), _entity("T")]
    rels_fwd = [
        _calls("S", "B", hid=1),
        _calls("S", "A", hid=2),
        _calls("B", "T", hid=3),
        _calls("A", "T", hid=4),
    ]
    ents, rels = _frames(entities, rels_fwd)
    result = compute_shortest_path(ents, rels, "S", "T")
    _assert_schema(result)
    assert result["nodes"] == ["S", "A", "T"]
    reversed_rels = pd.DataFrame(list(reversed(rels_fwd)))
    assert compute_shortest_path(ents, reversed_rels, "S", "T")["nodes"] == ["S", "A", "T"]
    oracle = _oracle_shortest_path(ents, rels, "S", "T")
    assert oracle["nodes"] == ["S", "A", "T"]
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_shortest_path
ents = pd.DataFrame([
    {"id": "eS", "title": "S", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eA", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eB", "title": "B", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eT", "title": "T", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "r1", "source": "S", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r2", "source": "S", "target": "A", "type": "calls", "extractor": "x"},
    {"id": "r3", "source": "B", "target": "T", "type": "calls", "extractor": "x"},
    {"id": "r4", "source": "A", "target": "T", "type": "calls", "extractor": "x"},
])
import json
print(json.dumps(compute_shortest_path(ents, rels, "S", "T"), sort_keys=True))
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
    assert payloads[0] == payloads[1] == payloads[2]
    assert json.loads(payloads[0])["nodes"] == ["S", "A", "T"]


def test_seeded_small_graphs_match_independent_oracle():
    rng = random.Random(20260905)
    relationship_types = ("calls", "contains", "uses_type")
    for case in range(80):
        n_nodes = rng.randint(2, 7)
        titles = [f"n{index}" for index in range(n_nodes)]
        entities = [_entity(title) for title in titles]
        relationships: list[dict] = []
        row_index = 0
        for source in titles:
            for target in titles:
                if rng.random() >= 0.24:
                    continue
                rel_type = rng.choice(relationship_types)
                relationships.append(
                    _rel(
                        source,
                        target,
                        rel_type,
                        rid=f"rel:{case}:{row_index}",
                        hid=row_index + 1,
                    )
                )
                row_index += 1
                if rng.random() < 0.18:
                    relationships.append(
                        _rel(
                            source,
                            target,
                            rel_type,
                            rid=f"rel:{case}:{row_index}",
                            hid=row_index + 1,
                        )
                    )
                    row_index += 1
        ents, rels = _frames(entities, relationships)
        source = rng.choice(titles)
        target = rng.choice(titles)
        max_depth = rng.randint(0, 5)
        edge_types = None
        if case % 3 == 1:
            edge_types = [rng.choice(relationship_types)]
        elif case % 3 == 2:
            edge_types = ["calls", "contains"]
        actual = compute_shortest_path(
            ents,
            rels,
            source,
            target,
            edge_types=edge_types,
            max_depth=max_depth,
        )
        expected = _oracle_shortest_path(
            ents,
            rels,
            source,
            target,
            edge_types=edge_types,
            max_depth=max_depth,
        )
        _assert_schema(actual)
        assert actual["status"] == expected["status"], case
        assert actual["found"] == expected["found"], case
        if actual["found"]:
            assert actual["distance"] == expected["distance"], case
            assert actual["nodes"] == expected["nodes"], case
            assert actual["steps"] == expected["steps"], case


def test_cycles_self_loops_parallel_rows_and_zero_hop():
    ents, rels = _frames(
        [_entity("A"), _entity("B"), _entity("C")],
        [
            _calls("A", "B", hid=1),
            _calls("A", "B", hid=2, rid="rel:parallel"),
            _calls("B", "A", hid=3),
            _calls("A", "A", hid=4, rid="rel:self"),
            _calls("B", "C", hid=5),
        ],
    )
    looped = compute_shortest_path(ents, rels, "A", "C")
    _assert_schema(looped)
    assert looped["nodes"] == ["A", "B", "C"]
    assert looped["steps"][0]["n_relationship_rows_total"] == 2
    assert looped["n_relationship_rows_on_path_total"] == 3
    same = compute_shortest_path(ents, rels, "A", "A")
    _assert_schema(same)
    assert same["distance"] == 0
    assert same["nodes"] == ["A"]
    assert same["steps"] == []
    assert same["n_relationship_rows_on_path_total"] == 0
    reverse = compute_shortest_path(ents, rels, "C", "A")
    assert reverse["status"] == "not_found_within_max_depth"
    human = format_shortest_path_human(reverse)
    assert "within max_depth" in human
    assert "unreachable" not in human.lower()
    assert "no path exists" not in human.lower()


def test_max_depth_bounds_and_not_found_wording():
    ents, rels = _frames(
        [_entity("A"), _entity("B"), _entity("C"), _entity("D")],
        [_calls("A", "B"), _calls("B", "C"), _calls("C", "D")],
    )
    zero = compute_shortest_path(ents, rels, "A", "B", max_depth=0)
    _assert_schema(zero)
    assert zero["status"] == "not_found_within_max_depth"
    assert "unreachable" not in format_shortest_path_human(zero).lower()
    at_bound = compute_shortest_path(ents, rels, "A", "D", max_depth=3)
    assert at_bound["found"] is True
    assert at_bound["distance"] == 3
    too_short = compute_shortest_path(ents, rels, "A", "D", max_depth=2)
    assert too_short["status"] == "not_found_within_max_depth"
    assert too_short["distance"] is None
    missing = compute_shortest_path(
        ents,
        rels,
        "A",
        "Z",
        max_depth=HARD_MAX_SHORTEST_PATH_DEPTH,
    )
    assert missing["status"] == "not_found_within_max_depth"
    assert missing["target"] == "Z"
    assert "unreachable" not in dumps_shortest_path_json(missing).lower()


def test_edge_type_filter_literal_all_commas_unicode_and_endpoint_only():
    ents, rels = _frames(
        [_entity("A"), _entity("B"), _entity("é")],
        [
            _calls("A", "ghost", hid=1),
            _rel("ghost", "B", "contains", hid=2),
            _rel("A", "é", "uses_type", hid=3),
            _rel("A", "B", "all", hid=4, rid="rel:literal-all"),
            _rel("A", "B", "a,b", hid=5, rid="rel:comma"),
        ],
    )
    via_endpoint = compute_shortest_path(ents, rels, "A", "B")
    assert via_endpoint["nodes"] == ["A", "B"]
    filtered = compute_shortest_path(ents, rels, "A", "B", edge_types=["contains"])
    assert filtered["status"] == "not_found_within_max_depth"
    contains_path = compute_shortest_path(
        ents, rels, "A", "B", edge_types=["calls", "contains", "contains"]
    )
    assert contains_path["nodes"] == ["A", "ghost", "B"]
    assert contains_path["edge_types"] == ["calls", "contains"]
    omitted = compute_shortest_path(ents, rels, "A", "B", edge_types=None)
    empty = compute_shortest_path(ents, rels, "A", "B", edge_types=[])
    assert omitted["nodes"] == empty["nodes"] == ["A", "B"]
    literal = compute_shortest_path(ents, rels, "A", "B", edge_types=["all"])
    assert literal["found"] is True
    assert literal["distance"] == 1
    assert literal["edge_types"] == ["all"]
    comma = compute_shortest_path(ents, rels, "A", "B", edge_types=["a,b"])
    assert comma["found"] is True
    unicode_path = compute_shortest_path(ents, rels, "A", "é", edge_types=["uses_type"])
    assert unicode_path["nodes"] == ["A", "é"]


def test_endpoint_resolution_unresolved_and_empty_graph(tmp_path: Path):
    entities = [
        _entity("sim:run", "function"),
        _entity("sim:sim", "module"),
        _entity("pkg:alpha", "function"),
        _entity("pkg:beta", "function"),
    ]
    rels = [_calls("sim:run", "pkg:alpha"), _calls("pkg:alpha", "pkg:beta")]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    exact = g.shortest_path("sim:run", "pkg:beta")
    assert exact["found"] is True
    alias = g.shortest_path("sim", "pkg:alpha")
    assert alias["source"] == "sim:sim"
    assert alias["status"] == "not_found_within_max_depth"
    partial = g.shortest_path("run", "beta")
    assert partial["source"] == "sim:run"
    assert partial["target"] == "pkg:beta"
    assert partial["found"] is True
    amb = g.shortest_path("pkg:", "sim:run")
    assert amb["status"] == "unresolved_source"
    assert amb["source"] is None
    assert amb["target"] == "sim:run"
    missing_t = g.shortest_path("sim:run", "nope")
    assert missing_t["status"] == "unresolved_target"
    both = g.shortest_path("nope", "pkg:")
    assert both["status"] == "unresolved_both"
    empty = compute_shortest_path(pd.DataFrame(), pd.DataFrame(), None, None)
    _assert_schema(empty)
    assert empty["status"] == "unresolved_both"


def test_malformed_graph_and_limits_fail_closed():
    good_ents, good_rels = _frames([_entity("A"), _entity("B")], [_calls("A", "B")])
    dup_title = pd.DataFrame([_entity("A"), _entity("A", id="ent:other")])
    with pytest.raises(ValueError, match="duplicate entity title"):
        compute_shortest_path(dup_title, good_rels, "A", "B")
    dup_id = pd.DataFrame([_calls("A", "B", hid=1), _calls("A", "B", hid=1, rid="rel:calls:A->B:1")])
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_shortest_path(good_ents, dup_id, "A", "B")
    hidden = pd.DataFrame(
        [
            _calls("A", "B"),
            _rel("", "B", "uses_type", hid=9, rid="rel:bad"),
        ]
    )
    with pytest.raises(ValueError, match="invalid source"):
        compute_shortest_path(good_ents, hidden, "A", "B", edge_types=["calls"])
    surrogate = pd.DataFrame([_entity("A"), _entity("\ud800")])
    with pytest.raises(ValueError):
        compute_shortest_path(surrogate, good_rels, "A", "B")
    control_ents, control_rels = _frames(
        [_entity("A"), _entity("B\x01C")],
        [_calls("A", "B\x01C")],
    )
    controlled = compute_shortest_path(control_ents, control_rels, "A", "B\x01C")
    assert controlled["found"] is True
    assert controlled["nodes"] == ["A", "B\x01C"]
    for bad in (True, 1.5, math.nan, -1, HARD_MAX_SHORTEST_PATH_DEPTH + 1):
        with pytest.raises(ValueError, match="max_depth"):
            compute_shortest_path(good_ents, good_rels, "A", "B", max_depth=bad)
    orig_ents = good_ents.copy(deep=True)
    orig_rels = good_rels.copy(deep=True)
    compute_shortest_path(good_ents, good_rels, "A", "B")
    assert good_ents.equals(orig_ents)
    assert good_rels.equals(orig_rels)


def test_large_cycle_is_iterative():
    n = 80
    entities = [_entity(f"n{i:03d}") for i in range(n)]
    rels = [_calls(f"n{i:03d}", f"n{(i + 1) % n:03d}", hid=i + 1) for i in range(n)]
    ents, rels_df = _frames(entities, rels)
    result = compute_shortest_path(
        ents, rels_df, "n000", "n030", max_depth=HARD_MAX_SHORTEST_PATH_DEPTH
    )
    assert result["found"] is True
    assert result["distance"] == 30
    assert result["nodes"][0] == "n000"
    assert result["nodes"][-1] == "n030"


def test_cli_json_human_parity_malformed_and_help(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B"), _entity("C")],
        [_calls("A", "B"), _calls("B", "C")],
    )
    common = ["shortest-path", "A", "C", "--graph", str(graph)]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_shortest_path_json(payload) + "\n" == json_out.stdout
    assert format_shortest_path_human(payload).strip() == human.stdout.strip()
    assert payload["found"] is True
    bad = _run(
        sys.executable,
        str(QUERY),
        "shortest-path",
        "A",
        "C",
        "--graph",
        str(graph),
        "--max-depth",
        "-1",
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert "max_depth" in bad.stderr
    help_out = _run(sys.executable, str(QUERY), "shortest-path", "--help")
    assert "--json" in help_out.stdout
    assert "--max-depth" in help_out.stdout
    assert "--edge-type" in help_out.stdout
    assert "--dot" not in help_out.stdout
    assert "--direction" not in help_out.stdout


def test_script_module_product_installed_parity(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    args = ["shortest-path", "A", "B", "--graph", str(graph), "--json"]
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
    bodies = [
        json.loads(script.stdout),
        json.loads(module.stdout),
        json.loads(product.stdout),
        json.loads(package.stdout),
        json.loads(installed.stdout),
    ]
    assert bodies[0] == bodies[1] == bodies[2] == bodies[3] == bodies[4]
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_current_and_historical_snapshot_reads_do_not_mutate(tmp_path: Path):
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
    cur = _run(
        sys.executable,
        str(QUERY),
        "shortest-path",
        "demo:new",
        "demo:other",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--json",
    )
    assert json.loads(cur.stdout)["found"] is True
    hist = _run(
        sys.executable,
        str(CLI),
        "shortest-path",
        "demo:old",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    body = json.loads(hist.stdout)
    assert body["source"] == "demo:old"
    assert body["found"] is True
    assert body["distance"] == 0
    assert "demo:new" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
    assert not (graph / ".publish.lock").is_symlink()


def test_current_resolved_once_explicit_id_does_not_read_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from typer.testing import CliRunner

    from graphrag_code.graph_query import app
    import graphrag_code.snapshot_read as snapshot_read

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
    publish_byog_snapshot(
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
    calls: list[object] = []
    orig = snapshot_read.resolve_snapshot

    def wrapped(root, snapshot=None):
        calls.append(snapshot)
        return orig(root, snapshot)

    monkeypatch.setattr(snapshot_read, "resolve_snapshot", wrapped)
    runner = CliRunner()
    current = runner.invoke(
        app,
        [
            "shortest-path",
            "demo:new",
            "demo:other",
            "--graph",
            str(graph),
            "--snapshot",
            "current",
            "--json",
        ],
    )
    assert current.exit_code == 0, current.stdout + current.stderr
    assert [item for item in calls if item is None] == [None]
    calls.clear()
    hist = runner.invoke(
        app,
        [
            "shortest-path",
            "demo:old",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
            "--json",
        ],
    )
    assert hist.exit_code == 0, hist.stdout + hist.stderr
    assert calls == [older.name]
    assert json.loads(hist.stdout)["source"] == "demo:old"


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


def _shortest_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from graphrag_code import graph_query
    from typer.testing import CliRunner

    orig = graph_query.dumps_shortest_path_json

    def wrap_dumps(result):
        payload = orig(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_shortest_path_json = wrap_dumps
    runner = CliRunner()
    result = runner.invoke(
        graph_query.app,
        ["shortest-path", "A", "B", "--graph", graph, "--json"],
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


def test_publisher_waits_through_serialization(tmp_path: Path):
    graph = _publish(tmp_path, [_entity("A"), _entity("B")], [_calls("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_shortest_json_hold, args=(str(graph), held, resume, q))
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


def test_producer_once_no_nested_query_and_mcp_stays_sixteen(
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

    for name in (
        "subgraph",
        "callers",
        "callees",
        "impact",
        "type_closure",
        "components",
        "strong_components",
        "condensation",
        "dependency_order",
        "degree_ranking",
    ):
        monkeypatch.setattr(g, name, track(name))
    producer_calls = 0
    orig = compute_shortest_path

    def counted(ents, rels, source_title, target_title, **kwargs):
        nonlocal producer_calls
        producer_calls += 1
        return orig(ents, rels, source_title, target_title, **kwargs)

    monkeypatch.setattr("graphrag_code.byog_graph.compute_shortest_path", counted)
    result = g.shortest_path("A", "B")
    assert result["found"] is True
    assert producer_calls == 1
    assert called == []

    src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_shortest_path":
            fn = ast.get_source_segment(src, node)
    assert fn is not None
    assert fn.count(".shortest_path(") == 1
    assert "subprocess" not in fn
    assert "networkx" not in fn
    assert "compute_bounded_subgraph" not in fn
    write_at = fn.find("print(dumps_shortest_path_json(result), flush=True)")
    human_at = fn.find("print(format_shortest_path_human(result), flush=True)")
    with_at = fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < human_at

    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    session = build_session(graph, "python")
    server = build_mcp_server(session)
    assert not hasattr(session, "shortest_path")
    assert "shortest_path" not in TOOL_NAMES
    assert "shortest-path" not in TOOL_NAMES
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

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert "shortest_path" not in names
            assert "shortest-path" not in names

    anyio_run(_body)
