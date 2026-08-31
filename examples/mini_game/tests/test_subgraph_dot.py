"""Deterministic Graphviz DOT export for the bounded subgraph CLI.

Renders the existing ByogGraph.subgraph result. Does not add a Graphviz
runtime, a second traversal, an MCP format parameter, or an output file.
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
    ByogGraph,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_subgraph_dot,
    dumps_subgraph_json,
    format_subgraph_human,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import (  # type: ignore
    HARD_MAX_SUBGRAPH_DOT_BYTES,
    SUBGRAPH_DOT_SCHEMA_VERSION,
    SubgraphDotError,
    dumps_subgraph_dot as direct_dumps,
    quote_dot_string,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

GOLDEN_RESOLVED = """\
digraph graphrag_subgraph {
  graph [
    schema_version="1",
    resolved="true",
    root="A",
    direction="outgoing",
    max_depth="1",
    max_nodes="50",
    max_edges="100",
    edge_types="null",
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
  n0000 -> n0001 [label="calls", id="rel:1", type="calls", depth="1"];
}
"""

GOLDEN_UNRESOLVED = """\
digraph graphrag_subgraph {
  graph [
    schema_version="1",
    resolved="false",
    direction="both",
    max_depth="3",
    max_nodes="50",
    max_edges="100",
    edge_types="null",
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


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_subgraph_dot"
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


def _base_result(**overrides: object) -> dict:
    result: dict = {
        "root": "A",
        "resolved": True,
        "direction": "outgoing",
        "max_depth": 1,
        "max_nodes": 50,
        "max_edges": 100,
        "edge_types": None,
        "nodes": [
            {
                "title": "A",
                "depth": 0,
                "type": "function",
                "id": "ent:function:A",
                "description": "secret-description",
                "source_file": "a.py",
                "span": "1:0-2:0",
                "confidence": 1.0,
            },
            {
                "title": "B",
                "depth": 1,
                "type": "function",
                "id": "ent:function:B",
                "description": "other-secret",
            },
        ],
        "edges": [
            {
                "id": "rel:1",
                "source": "A",
                "target": "B",
                "type": "calls",
                "depth": 1,
                "description": "secret-edge",
                "weight": 9.5,
                "span": "3:0-4:0",
                "confidence": 0.5,
            }
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
        "direction": "both",
        "max_depth": 3,
        "max_nodes": 50,
        "max_edges": 100,
        "edge_types": None,
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


def test_quote_dot_string_escapes_and_rejects_unencodable():
    assert quote_dot_string("plain") == '"plain"'
    assert quote_dot_string('a"b\\c') == '"a\\"b\\\\c"'
    assert quote_dot_string("a\nb\tc\rd") == '"a\\nb\\tc\\rd"'
    assert quote_dot_string("\x00\x01\x1f\x7f\x85") == '"\\x00\\x01\\x1f\\x7f\\x85"'
    assert quote_dot_string("é☃") == '"é☃"'
    injection = 'x"]; attacker -> node ['
    quoted = quote_dot_string(injection)
    assert quoted == '"x\\"]; attacker -> node ["'
    with pytest.raises(SubgraphDotError, match="strict UTF-8"):
        quote_dot_string("\ud800")
    with pytest.raises(SubgraphDotError, match="must be str"):
        quote_dot_string(b"bytes")  # type: ignore[arg-type]


def test_golden_deterministic_and_byte_identical():
    payload = dumps_subgraph_dot(_base_result())
    assert payload == GOLDEN_RESOLVED
    assert payload == dumps_subgraph_dot(_base_result())
    assert payload == direct_dumps(_base_result())
    assert payload.endswith("\n") and not payload.endswith("\n\n")
    assert payload.encode("utf-8").decode("utf-8") == payload
    assert "schema_version=" + quote_dot_string(str(SUBGRAPH_DOT_SCHEMA_VERSION)) in payload
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
    assert dumps_subgraph_dot(_unresolved_result()) == GOLDEN_UNRESOLVED


def test_producer_order_ids_orientation_parallel_self_and_root():
    result = _base_result(
        nodes=[
            {"title": "Zroot", "depth": 0, "type": "function"},
            {"title": "Achild", "depth": 1},
            {"title": "Mchild", "depth": 1, "type": "module"},
        ],
        root="Zroot",
        edges=[
            {
                "id": "rel:self",
                "source": "Zroot",
                "target": "Zroot",
                "type": "calls",
                "depth": 0,
            },
            {
                "id": "rel:p1",
                "source": "Zroot",
                "target": "Achild",
                "type": "calls",
                "depth": 1,
            },
            {
                "id": "rel:p2",
                "source": "Zroot",
                "target": "Achild",
                "type": "calls",
                "depth": 1,
            },
            {
                "id": "rel:back",
                "source": "Mchild",
                "target": "Zroot",
                "type": "contains",
                "depth": 1,
            },
        ],
        n_nodes_total=3,
        n_nodes_returned=3,
        n_edges_total=4,
        n_edges_returned=4,
        direction="both",
    )
    payload = dumps_subgraph_dot(result)
    nodes = _node_statements(payload)
    edges = _edge_statements(payload)
    assert nodes[0].startswith("n0000 [")
    assert 'title="Zroot"' in nodes[0]
    assert 'is_root="true"' in nodes[0]
    assert 'peripheries="2"' in nodes[0]
    assert nodes[1].startswith("n0001 [")
    assert 'title="Achild"' in nodes[1]
    assert "type=" not in nodes[1]
    assert 'is_root="false"' in nodes[1]
    assert "peripheries=" not in nodes[1]
    assert nodes[2].startswith("n0002 [")
    assert 'type="module"' in nodes[2]
    assert edges == [
        'n0000 -> n0000 [label="calls", id="rel:self", type="calls", depth="0"];',
        'n0000 -> n0001 [label="calls", id="rel:p1", type="calls", depth="1"];',
        'n0000 -> n0001 [label="calls", id="rel:p2", type="calls", depth="1"];',
        'n0002 -> n0000 [label="contains", id="rel:back", type="contains", depth="1"];',
    ]
    assert payload.count("n0000 -> n0000") == 1
    assert payload.count("n0000 -> n0001") == 2
    assert "n0001 -> n0000" not in payload


def test_live_graph_preserves_order_orientation_and_edge_type_metadata(tmp_path: Path):
    entities = [
        _entity("pkg:root", "function"),
        _entity("pkg:mid", "function"),
        _entity("pkg:user", "function"),
        _entity("pkg:pkg", "module"),
    ]
    rels = [
        _calls("pkg:root", "pkg:mid", hid=1),
        _calls("pkg:root", "pkg:mid", hid=2, rid="rel:parallel-root-mid"),
        _calls("pkg:user", "pkg:root", hid=3),
        _rel("pkg:pkg", "pkg:root", "contains", hid=4),
        _rel("pkg:root", "pkg:root", "calls", rid="rel:self", hid=5),
        _rel("pkg:root", "pkg:mid", "uses_type", hid=6),
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    outgoing = g.subgraph("pkg:root", direction="outgoing", max_depth=1)
    incoming = g.subgraph("pkg:root", direction="incoming", max_depth=1)
    filtered = g.subgraph(
        "pkg:root",
        direction="outgoing",
        max_depth=1,
        edge_types=["uses_type", "calls", "calls"],
    )
    out_dot = dumps_subgraph_dot(outgoing)
    in_dot = dumps_subgraph_dot(incoming)
    filt_dot = dumps_subgraph_dot(filtered)
    out_ids = [f"n{i:04d}" for i in range(outgoing["n_nodes_returned"])]
    assert [line.split()[0] for line in _node_statements(out_dot)] == out_ids
    title_by_ident = {}
    for node, line in zip(outgoing["nodes"], _node_statements(out_dot)):
        assert f"title={quote_dot_string(node['title'])}" in line
        title_by_ident[line.split()[0]] = node["title"]
    for edge, line in zip(outgoing["edges"], _edge_statements(out_dot)):
        src, _, tgt, *_ = line.split()
        assert title_by_ident[src] == edge["source"]
        assert title_by_ident[tgt] == edge["target"]
        assert f"id={quote_dot_string(edge['id'])}" in line
    user_edge = next(e for e in incoming["edges"] if e["source"] == "pkg:user")
    assert user_edge["target"] == "pkg:root"
    assert "pkg:user -> pkg:root" not in in_dot
    assert any(
        f"id={quote_dot_string(user_edge['id'])}" in line
        for line in _edge_statements(in_dot)
    )
    assert 'edge_types="[\\"calls\\",\\"uses_type\\"]"' in filt_dot
    assert filtered["edge_types"] == ["calls", "uses_type"]
    assert 'edge_types="null"' in out_dot


def test_truncated_totals_and_flags():
    result = _base_result(
        nodes=[{"title": "A", "depth": 0, "type": "function"}],
        edges=[],
        n_nodes_total=6,
        n_edges_total=5,
        n_nodes_returned=1,
        n_edges_returned=0,
        nodes_truncated=True,
        edges_truncated=True,
        max_nodes=1,
        max_edges=0,
    )
    payload = dumps_subgraph_dot(result)
    assert 'n_nodes_total="6"' in payload
    assert 'n_edges_total="5"' in payload
    assert 'n_nodes_returned="1"' in payload
    assert 'n_edges_returned="0"' in payload
    assert 'nodes_truncated="true"' in payload
    assert 'edges_truncated="true"' in payload
    assert _edge_statements(payload) == []
    assert len(_node_statements(payload)) == 1


def test_unresolved_and_ambiguous_are_valid_empty_digraphs(tmp_path: Path):
    entities = [_entity("pkg:a", "function"), _entity("pkg:b", "function")]
    graph = _publish(tmp_path, entities, [_calls("pkg:a", "pkg:b")])
    g = ByogGraph(graph)
    missing = dumps_subgraph_dot(g.subgraph("does-not-exist"))
    amb = dumps_subgraph_dot(g.subgraph("pkg:"))
    for payload, source in ((missing, g.subgraph("does-not-exist")), (amb, g.subgraph("pkg:"))):
        assert source["resolved"] is False
        assert payload.startswith("digraph graphrag_subgraph {")
        assert 'resolved="false"' in payload
        assert "root=" not in payload
        assert 'n_nodes_total="0"' in payload
        assert 'n_edges_total="0"' in payload
        assert _node_statements(payload) == []
        assert _edge_statements(payload) == []
        assert payload.endswith("\n")
    proc = _run(
        sys.executable,
        str(QUERY),
        "subgraph",
        "does-not-exist",
        "--graph",
        str(graph),
        "--dot",
    )
    assert proc.returncode == 0
    assert proc.stdout == missing


def test_escaping_injection_unicode_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "ok /* comment */ // still"
    result = _base_result(
        root=nasty,
        nodes=[
            {
                "title": nasty,
                "depth": 0,
                "type": comment,
            },
            {
                "title": "é☃\n\t\\",
                "depth": 1,
                "type": "function",
            },
        ],
        edges=[
            {
                "id": 'rel:"id"\n',
                "source": nasty,
                "target": "é☃\n\t\\",
                "type": 'calls"]; attacker -> n9999 [',
                "depth": 1,
            }
        ],
    )
    payload = dumps_subgraph_dot(result)
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
    assert quote_dot_string(nasty) in payload
    for line in _node_statements(payload) + _edge_statements(payload):
        assert line.endswith("];")
        assert line.startswith("n")


def test_missing_endpoint_and_invalid_input_fail_closed():
    missing = _base_result(
        edges=[
            {
                "id": "rel:ghost",
                "source": "A",
                "target": "ghost",
                "type": "calls",
                "depth": 1,
            }
        ]
    )
    with pytest.raises(SubgraphDotError, match="endpoint"):
        dumps_subgraph_dot(missing)
    with pytest.raises(SubgraphDotError):
        dumps_subgraph_dot("not-a-mapping")  # type: ignore[arg-type]
    bad_root = _base_result(nodes=[{"title": "B", "depth": 0}, {"title": "A", "depth": 1}])
    with pytest.raises(SubgraphDotError, match="root"):
        dumps_subgraph_dot(bad_root)
    surrogate = _base_result(
        nodes=[
            {"title": "A", "depth": 0, "type": "function"},
            {"title": "\ud800", "depth": 1, "type": "function"},
        ]
    )
    with pytest.raises(SubgraphDotError, match="UTF-8"):
        dumps_subgraph_dot(surrogate)
    with pytest.raises(SubgraphDotError):
        dumps_subgraph_dot(_unresolved_result(nodes=[{"title": "A", "depth": 0}]))
    with pytest.raises(SubgraphDotError):
        dumps_subgraph_dot(_base_result(n_nodes_returned=3))
    for invalid in (
        _base_result(n_nodes_total=1),
        _base_result(n_edges_total=0),
        _base_result(nodes_truncated=True),
        _base_result(edges_truncated=True),
        _base_result(max_nodes=1),
        _base_result(max_edges=0),
        _base_result(max_depth=-1),
        _base_result(direction="sideways"),
        _base_result(
            nodes=[{"title": "A", "depth": 1}],
            n_nodes_total=1,
            n_nodes_returned=1,
            edges=[],
            n_edges_total=0,
            n_edges_returned=0,
        ),
    ):
        with pytest.raises(SubgraphDotError):
            dumps_subgraph_dot(invalid)

    duplicate_edge = _base_result(
        edges=[_base_result()["edges"][0]] * 2,
        n_edges_total=2,
        n_edges_returned=2,
    )
    with pytest.raises(SubgraphDotError, match="duplicate subgraph edge id"):
        dumps_subgraph_dot(duplicate_edge)

    with pytest.raises(SubgraphDotError, match="non-empty"):
        dumps_subgraph_dot(_base_result(edge_types=[]))
    with pytest.raises(SubgraphDotError, match="canonical"):
        dumps_subgraph_dot(_base_result(edge_types=["uses_type", "calls"]))

    unfiltered = dumps_subgraph_dot(_base_result(edge_types=None))
    literal_all = dumps_subgraph_dot(_base_result(edge_types=["all"]))
    comma_types = dumps_subgraph_dot(_base_result(edge_types=["a,b", "c"]))
    split_types = dumps_subgraph_dot(_base_result(edge_types=["a", "b,c"]))
    assert 'edge_types="null"' in unfiltered
    assert 'edge_types="[\\"all\\"]"' in literal_all
    assert 'edge_types="[\\"a,b\\",\\"c\\"]"' in comma_types
    assert 'edge_types="[\\"a\\",\\"b,c\\"]"' in split_types
    assert len({unfiltered, literal_all, comma_types, split_types}) == 4


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_subgraph_dot(_base_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.subgraph_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_SUBGRAPH_DOT_BYTES", size)
    assert dumps_subgraph_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_SUBGRAPH_DOT_BYTES", size + 1)
    assert dumps_subgraph_dot(_base_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_SUBGRAPH_DOT_BYTES", size - 1)
    with pytest.raises(SubgraphDotError, match="hard limit"):
        dumps_subgraph_dot(_base_result())
    assert HARD_MAX_SUBGRAPH_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("A", "function"), _entity("B", "function")],
        [_calls("A", "B")],
    )
    common = [
        "subgraph",
        "A",
        "--graph",
        str(graph),
        "--direction",
        "outgoing",
        "--max-depth",
        "1",
    ]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_subgraph_json(payload) + "\n" == json_out.stdout
    assert format_subgraph_human(payload).strip() == human.stdout.strip()
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
    import graphrag_code.subgraph_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_SUBGRAPH_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["subgraph", "A", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "subgraph", "--help"],
        [sys.executable, str(CLI), "subgraph", "--help"],
        [sys.executable, "-m", "graphrag_code.graph_query", "subgraph", "--help"],
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
        [_entity("A", "function"), _entity("B", "function")],
        [_calls("A", "B")],
    )
    args = [
        "subgraph",
        "A",
        "--graph",
        str(graph),
        "--direction",
        "outgoing",
        "--max-depth",
        "1",
        "--dot",
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
    expected = dumps_subgraph_dot(
        ByogGraph(graph).subgraph("A", direction="outgoing", max_depth=1)
    )
    assert script.stdout == module.stdout == product.stdout == installed.stdout == expected
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_current_and_historical_snapshot_dot_reads_do_not_mutate(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old", "function")]),
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
        pd.DataFrame([_entity("demo:new", "function")]),
        pd.DataFrame([_calls("demo:new", "demo:new", rid="rel:new")]),
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
        "subgraph",
        "demo:new",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--dot",
    )
    assert 'root="demo:new"' in cur.stdout
    hist = _run(
        sys.executable,
        str(CLI),
        "subgraph",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--dot",
    )
    assert 'root="demo:old"' in hist.stdout
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


def _subgraph_dot_hold(graph: str, symbol: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_subgraph_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_subgraph_dot = wrap_dumps

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
            ["subgraph", symbol, "--graph", graph, "--dot"],
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
    entities = [_entity("A", "function"), _entity("B", "function")]
    graph = _publish(tmp_path, entities, [_calls("A", "B")])
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_subgraph_dot_hold, args=(str(graph), "A", held, resume, q)
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


def test_no_nested_traversal_graphviz_network_or_tempfiles_and_single_producer_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("A", "function"), _entity("B", "function")],
        [_calls("A", "B")],
    )
    g = ByogGraph(graph)
    calls: list[str] = []
    orig = g.subgraph

    def wrapped(*args, **kwargs):
        calls.append("subgraph")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "subgraph", wrapped)
    result = g.subgraph("A", max_depth=1)
    assert len(calls) == 1
    dumps_subgraph_dot(result)
    assert len(calls) == 1

    dot_src = (ROOT / "src" / "graphrag_code" / "subgraph_dot.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(dot_src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "json", "typing"}
    assert "subprocess" not in imported
    assert "graphviz" not in imported
    assert "pydot" not in imported
    assert "pygraphviz" not in imported
    assert "networkx" not in imported
    assert "tempfile" not in imported
    assert "socket" not in imported
    assert "urllib" not in imported
    assert "byog_graph" not in imported
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    subgraph_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_subgraph":
            subgraph_fn = ast.get_source_segment(gq_src, node)
    assert subgraph_fn is not None
    assert "subprocess" not in subgraph_fn
    assert "compute_bounded_subgraph" not in subgraph_fn
    assert subgraph_fn.count(".subgraph(") == 1
    assert "sys.stdout.write(dumps_subgraph_dot(result))" in subgraph_fn
    assert "sys.stdout.flush()" in subgraph_fn
    write_at = subgraph_fn.find("sys.stdout.write(dumps_subgraph_dot(result))")
    flush_at = subgraph_fn.find("sys.stdout.flush()")
    with_at = subgraph_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < flush_at


def test_mcp_remains_fifteen_tools_without_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(
        tmp_path,
        [_entity("A", "function"), _entity("B", "function")],
        [_calls("A", "B")],
    )
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.subgraph).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == [
        "self",
        "symbol",
        "direction",
        "max_depth",
        "max_nodes",
        "max_edges",
        "edge_types",
        "snapshot",
    ]

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 15
            sub = next(tool for tool in tools if tool.name == "subgraph")
            props = (sub.input_schema.get("properties") or {})
            assert "dot" not in props
            assert "format" not in props
            components = next(tool for tool in tools if tool.name == "components")
            cprops = components.input_schema.get("properties") or {}
            assert "dot" not in cprops
            assert "format" not in cprops
            result = await client.call_tool(
                "subgraph",
                {"symbol": "A", "direction": "outgoing", "max_depth": 1},
            )
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "subgraph"
            assert body["data"]["root"] == "A"
            assert "digraph" not in json.dumps(body)

    anyio_run(_body)
