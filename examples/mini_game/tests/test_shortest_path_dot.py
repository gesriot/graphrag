"""Deterministic Graphviz DOT export for the directed shortest-path CLI.

Renders the existing ByogGraph.shortest_path result. Does not add a Graphviz
runtime, a second path algorithm, an MCP format parameter, or an output file.
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
    HARD_MAX_SHORTEST_PATH_DEPTH,
    ByogGraph,
    compute_shortest_path,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_shortest_path_dot,
    dumps_shortest_path_json,
    format_shortest_path_human,
)
from graphrag_code.shortest_path_dot import (  # type: ignore
    HARD_MAX_SHORTEST_PATH_DOT_BYTES,
    SHORTEST_PATH_DOT_SCHEMA_VERSION,
    ShortestPathDotError,
    dumps_shortest_path_dot as direct_dumps,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')

GOLDEN_PATH = """\
digraph graphrag_shortest_path {
  graph [
    schema_version="1",
    status="found",
    found="true",
    source="\\"A\\"",
    target="\\"C\\"",
    source_resolved="true",
    target_resolved="true",
    edge_types="null",
    max_depth="8",
    distance="2",
    n_nodes_returned="3",
    n_steps_returned="2",
    n_relationship_rows_on_path_total="3"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", path_index="0", is_source="true", is_target="false", peripheries="2"];
  n0001 [label="B", title="B", path_index="1", is_source="false", is_target="false"];
  n0002 [label="C", title="C", path_index="2", is_source="false", is_target="true", peripheries="2"];
  n0000 -> n0001 [label="rows 2", source="A", target="B", step_index="0", n_relationship_rows_total="2"];
  n0001 -> n0002 [label="rows 1", source="B", target="C", step_index="1", n_relationship_rows_total="1"];
}
"""

GOLDEN_ZERO = """\
digraph graphrag_shortest_path {
  graph [
    schema_version="1",
    status="found",
    found="true",
    source="\\"A\\"",
    target="\\"A\\"",
    source_resolved="true",
    target_resolved="true",
    edge_types="null",
    max_depth="8",
    distance="0",
    n_nodes_returned="1",
    n_steps_returned="0",
    n_relationship_rows_on_path_total="0"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A", path_index="0", is_source="true", is_target="true", peripheries="2"];
}
"""

GOLDEN_NOT_FOUND = """\
digraph graphrag_shortest_path {
  graph [
    schema_version="1",
    status="not_found_within_max_depth",
    found="false",
    source="\\"A\\"",
    target="\\"Z\\"",
    source_resolved="true",
    target_resolved="true",
    edge_types="null",
    max_depth="8",
    distance="null",
    n_nodes_returned="0",
    n_steps_returned="0",
    n_relationship_rows_on_path_total="0"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_UNRESOLVED_SOURCE = """\
digraph graphrag_shortest_path {
  graph [
    schema_version="1",
    status="unresolved_source",
    found="false",
    source="null",
    target="\\"C\\"",
    source_resolved="false",
    target_resolved="true",
    edge_types="null",
    max_depth="8",
    distance="null",
    n_nodes_returned="0",
    n_steps_returned="0",
    n_relationship_rows_on_path_total="0"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_UNRESOLVED_TARGET = """\
digraph graphrag_shortest_path {
  graph [
    schema_version="1",
    status="unresolved_target",
    found="false",
    source="\\"A\\"",
    target="null",
    source_resolved="true",
    target_resolved="false",
    edge_types="null",
    max_depth="8",
    distance="null",
    n_nodes_returned="0",
    n_steps_returned="0",
    n_relationship_rows_on_path_total="0"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_UNRESOLVED_BOTH = """\
digraph graphrag_shortest_path {
  graph [
    schema_version="1",
    status="unresolved_both",
    found="false",
    source="null",
    target="null",
    source_resolved="false",
    target_resolved="false",
    edge_types="null",
    max_depth="8",
    distance="null",
    n_nodes_returned="0",
    n_steps_returned="0",
    n_relationship_rows_on_path_total="0"
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


def _path_entities() -> list[dict]:
    return [_entity(title) for title in ("A", "B", "C")]


def _path_rels() -> list[dict]:
    return [
        _calls("A", "B", hid=1),
        _calls("A", "B", hid=2, rid="rel:parallel-a-b"),
        _calls("B", "C", hid=3),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_shortest_path_dot"
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


def _found_result(**overrides: object) -> dict:
    result: dict = {
        "source": "A",
        "target": "C",
        "source_resolved": True,
        "target_resolved": True,
        "status": "found",
        "found": True,
        "edge_types": None,
        "max_depth": 8,
        "distance": 2,
        "nodes": ["A", "B", "C"],
        "steps": [
            {"source": "A", "target": "B", "n_relationship_rows_total": 2},
            {"source": "B", "target": "C", "n_relationship_rows_total": 1},
        ],
        "n_nodes_returned": 3,
        "n_steps_returned": 2,
        "n_relationship_rows_on_path_total": 3,
    }
    result.update(copy.deepcopy(overrides))
    return result


def _zero_result(**overrides: object) -> dict:
    result = _found_result(
        target="A",
        distance=0,
        nodes=["A"],
        steps=[],
        n_nodes_returned=1,
        n_steps_returned=0,
        n_relationship_rows_on_path_total=0,
    )
    result.update(copy.deepcopy(overrides))
    return result


def _empty_result(
    *,
    status: str = "not_found_within_max_depth",
    source: str | None = "A",
    target: str | None = "Z",
    source_resolved: bool = True,
    target_resolved: bool = True,
    **overrides: object,
) -> dict:
    result: dict = {
        "source": source,
        "target": target,
        "source_resolved": source_resolved,
        "target_resolved": target_resolved,
        "status": status,
        "found": False,
        "edge_types": None,
        "max_depth": 8,
        "distance": None,
        "nodes": [],
        "steps": [],
        "n_nodes_returned": 0,
        "n_steps_returned": 0,
        "n_relationship_rows_on_path_total": 0,
    }
    result.update(copy.deepcopy(overrides))
    return result


def test_golden_found_zero_unresolved_and_hash_seed_independent():
    payload = dumps_shortest_path_dot(_found_result())
    assert payload == GOLDEN_PATH
    assert payload == dumps_shortest_path_dot(_found_result())
    assert payload == direct_dumps(_found_result())
    assert payload.endswith("\n") and not payload.endswith("\n\n")
    assert payload.encode("utf-8").decode("utf-8") == payload
    assert (
        "schema_version="
        + quote_dot_string(str(SHORTEST_PATH_DOT_SCHEMA_VERSION))
        in payload
    )
    assert dumps_shortest_path_dot(_zero_result()) == GOLDEN_ZERO
    assert dumps_shortest_path_dot(_empty_result()) == GOLDEN_NOT_FOUND
    assert (
        dumps_shortest_path_dot(
            _empty_result(
                status="unresolved_source",
                source=None,
                target="C",
                source_resolved=False,
                target_resolved=True,
            )
        )
        == GOLDEN_UNRESOLVED_SOURCE
    )
    assert (
        dumps_shortest_path_dot(
            _empty_result(
                status="unresolved_target",
                source="A",
                target=None,
                source_resolved=True,
                target_resolved=False,
            )
        )
        == GOLDEN_UNRESOLVED_TARGET
    )
    assert (
        dumps_shortest_path_dot(
            _empty_result(
                status="unresolved_both",
                source=None,
                target=None,
                source_resolved=False,
                target_resolved=False,
            )
        )
        == GOLDEN_UNRESOLVED_BOTH
    )
    assert "unreachable" not in GOLDEN_NOT_FOUND.lower()
    leaky = _found_result()
    leaky["secret"] = "should-not-appear"
    assert "should-not-appear" not in dumps_shortest_path_dot(leaky)
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_shortest_path
from graphrag_code.shortest_path_dot import dumps_shortest_path_dot
ents = pd.DataFrame([
    {"id": "eA", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eB", "title": "B", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eC", "title": "C", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "r1", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r2", "source": "A", "target": "B", "type": "calls", "extractor": "x"},
    {"id": "r3", "source": "B", "target": "C", "type": "calls", "extractor": "x"},
])
print(dumps_shortest_path_dot(compute_shortest_path(ents, rels, "A", "C")), end="")
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
    assert payloads[0] == payloads[1] == payloads[2] == GOLDEN_PATH


def test_live_graph_preserves_order_ids_parallel_rows_and_utf8_path(tmp_path: Path):
    graph = _publish(tmp_path, _path_entities(), _path_rels())
    result = ByogGraph(graph).shortest_path("A", "C")
    payload = dumps_shortest_path_dot(result)
    assert payload == GOLDEN_PATH
    nodes = _node_statements(payload)
    edges = _edge_statements(payload)
    assert [line.split()[0] for line in nodes] == ["n0000", "n0001", "n0002"]
    for title, line in zip(result["nodes"], nodes):
        assert f"title={quote_dot_string(title)}" in line
        assert title not in _structural(line).split()[0]
    for step, line in zip(result["steps"], edges):
        src, _, tgt, *_ = line.split()
        assert src.startswith("n") and tgt.startswith("n")
        assert step["source"] not in _structural(line).split()[0:3]
        assert (
            f"n_relationship_rows_total={quote_dot_string(str(step['n_relationship_rows_total']))}"
            in line
        )
    assert 'n_relationship_rows_total="2"' in edges[0]
    assert 'label="rows 2"' in edges[0]
    assert payload.count(" -> ") == 2

    diamond = _publish(
        tmp_path / "diamond",
        [_entity("S"), _entity("A"), _entity("B"), _entity("T")],
        [
            _calls("S", "A"),
            _calls("A", "T"),
            _calls("S", "B"),
            _calls("B", "T"),
        ],
    )
    chosen = ByogGraph(diamond).shortest_path("S", "T")
    assert chosen["nodes"] == ["S", "A", "T"]
    diamond_dot = dumps_shortest_path_dot(chosen)
    titles = [line.split("title=", 1)[1].split(",", 1)[0] for line in _node_statements(diamond_dot)]
    assert titles == [quote_dot_string("S"), quote_dot_string("A"), quote_dot_string("T")]
    assert quote_dot_string("B") not in "".join(_node_statements(diamond_dot))


def test_edge_types_none_literal_all_and_commas():
    unfiltered = dumps_shortest_path_dot(_found_result(edge_types=None))
    literal_all = dumps_shortest_path_dot(_found_result(edge_types=["all"]))
    comma_types = dumps_shortest_path_dot(_found_result(edge_types=["a,b", "c"]))
    split_types = dumps_shortest_path_dot(_found_result(edge_types=["a", "b,c"]))
    assert 'edge_types="null"' in unfiltered
    assert 'edge_types="[\\"all\\"]"' in literal_all
    assert 'edge_types="[\\"a,b\\",\\"c\\"]"' in comma_types
    assert 'edge_types="[\\"a\\",\\"b,c\\"]"' in split_types
    assert len({unfiltered, literal_all, comma_types, split_types}) == 4


def test_escaping_injection_unicode_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "z /* comment */ // still"
    result = _found_result(
        source=nasty,
        target="é☃\n\t\\",
        nodes=[nasty, comment, "é☃\n\t\\"],
        steps=[
            {"source": nasty, "target": comment, "n_relationship_rows_total": 2},
            {"source": comment, "target": "é☃\n\t\\", "n_relationship_rows_total": 1},
        ],
    )
    payload = dumps_shortest_path_dot(result)
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert "n9999" not in structural
    assert "/*" not in structural
    assert "//" not in structural
    assert nasty not in structural
    assert structural.count(" -> ") == 2
    assert len(_node_statements(payload)) == 3
    assert len(_edge_statements(payload)) == 2
    assert "\\n" in payload and "\t" not in payload
    assert quote_dot_string(nasty) in payload
    for line in _node_statements(payload) + _edge_statements(payload):
        assert line.endswith("];")
        assert line.startswith("n")
    controls = _found_result(
        source="A\x01B",
        target="C\x7fD",
        nodes=["A\x01B", "mid\x9f", "C\x7fD"],
        steps=[
            {"source": "A\x01B", "target": "mid\x9f", "n_relationship_rows_total": 1},
            {"source": "mid\x9f", "target": "C\x7fD", "n_relationship_rows_total": 1},
        ],
        n_relationship_rows_on_path_total=2,
    )
    controlled = dumps_shortest_path_dot(controls)
    assert "\\x01" in controlled
    assert "\\x9f" in controlled
    assert "\\x7f" in controlled
    assert "\x01" not in controlled
    assert "\x7f" not in controlled


def test_missing_malformed_invariants_and_lone_surrogate_fail_closed():
    for name in (
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
    ):
        missing = _found_result()
        del missing[name]
        with pytest.raises(ShortestPathDotError, match="missing"):
            dumps_shortest_path_dot(missing)
    with pytest.raises(ShortestPathDotError):
        dumps_shortest_path_dot("not-a-mapping")  # type: ignore[arg-type]
    surrogate = _found_result(nodes=["A", "\ud800", "C"])
    with pytest.raises(ShortestPathDotError, match="UTF-8"):
        dumps_shortest_path_dot(surrogate)
    for invalid in (
        _found_result(found=1),
        _found_result(source_resolved=1),
        _found_result(max_depth=True),
        _found_result(max_depth=1.5),
        _found_result(max_depth=math.nan),
        _found_result(max_depth=-1),
        _found_result(max_depth=HARD_MAX_SHORTEST_PATH_DEPTH + 1),
        _found_result(n_nodes_returned=True),
        _found_result(n_steps_returned=-1),
        _found_result(status="unreachable"),
        _found_result(found=False),
        _found_result(source_resolved=False, source=None),
        _found_result(status="not_found_within_max_depth", found=False),
        _empty_result(found=True, status="found", distance=0, nodes=["A"], n_nodes_returned=1),
        _found_result(distance=1),
        _found_result(nodes=["A", "C"], n_nodes_returned=2),
        _found_result(steps=[{"source": "A", "target": "C", "n_relationship_rows_total": 1}]),
        _found_result(
            steps=[
                {"source": "A", "target": "C", "n_relationship_rows_total": 2},
                {"source": "B", "target": "C", "n_relationship_rows_total": 1},
            ]
        ),
        _found_result(n_relationship_rows_on_path_total=99),
        _found_result(nodes=["A", "B", "A"], target="A"),
        _found_result(edge_types=[]),
        _found_result(edge_types=["uses_type", "calls"]),
        _empty_result(distance=0),
        _empty_result(nodes=["A"], n_nodes_returned=1),
        _empty_result(n_relationship_rows_on_path_total=1),
        _empty_result(status="unresolved_source", source="A", source_resolved=False),
        _empty_result(status="unresolved_target", target="Z", target_resolved=False),
    ):
        with pytest.raises(ShortestPathDotError):
            dumps_shortest_path_dot(invalid)
    extra_step = _found_result()
    extra_step["steps"][0] = {
        **extra_step["steps"][0],
        "id": "rel:secret",
    }
    with pytest.raises(ShortestPathDotError, match="extra or missing"):
        dumps_shortest_path_dot(extra_step)


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_shortest_path_dot(_found_result())
    size = len(payload.encode("utf-8"))
    import graphrag_code.shortest_path_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_SHORTEST_PATH_DOT_BYTES", size)
    assert dumps_shortest_path_dot(_found_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_SHORTEST_PATH_DOT_BYTES", size + 1)
    assert dumps_shortest_path_dot(_found_result()) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_SHORTEST_PATH_DOT_BYTES", size - 1)
    with pytest.raises(ShortestPathDotError, match="hard limit"):
        dumps_shortest_path_dot(_found_result())
    assert HARD_MAX_SHORTEST_PATH_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _path_entities(), _path_rels())
    common = ["shortest-path", "A", "C", "--graph", str(graph)]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_shortest_path_json(payload) + "\n" == json_out.stdout
    assert format_shortest_path_human(payload).strip() == human.stdout.strip()
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
    import graphrag_code.shortest_path_dot as dot_mod

    def boom(*_a, **_k):
        raise AssertionError("graph observed before json/dot rejection")

    monkeypatch.setattr(gq, "_scoped_graph", boom)
    rejected = CliRunner().invoke(
        app,
        ["shortest-path", "A", "C", "--graph", str(graph), "--json", "--dot"],
    )
    assert rejected.exit_code == 2
    assert rejected.stdout == ""
    monkeypatch.undo()
    monkeypatch.setattr(dot_mod, "HARD_MAX_SHORTEST_PATH_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["shortest-path", "A", "C", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "shortest-path", "--help"],
        [sys.executable, str(CLI), "shortest-path", "--help"],
        [sys.executable, "-m", "graphrag_code.graph_query", "shortest-path", "--help"],
        [sys.executable, "-m", "graphrag_code", "shortest-path", "--help"],
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

    graph = _publish(tmp_path, _path_entities(), _path_rels())
    args = ["shortest-path", "A", "C", "--graph", str(graph), "--dot"]
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
    expected = dumps_shortest_path_dot(ByogGraph(graph).shortest_path("A", "C"))
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed.stdout
        == expected
        == GOLDEN_PATH
    )
    assert not list(outside.glob("*.dot"))
    assert not list(tmp_path.glob("**/.staging-*"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))
    assert not (graph / ".publish.lock").is_symlink()


def test_invalid_graph_and_arguments_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "shortest-path",
        "A",
        "B",
        "--graph",
        str(tmp_path / "missing"),
        "--dot",
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    graph = _publish(tmp_path, _path_entities(), _path_rels())
    bad_snap = _run(
        sys.executable,
        str(QUERY),
        "shortest-path",
        "A",
        "C",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        "--dot",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == ""
    bad_depth = _run(
        sys.executable,
        str(QUERY),
        "shortest-path",
        "A",
        "C",
        "--graph",
        str(graph),
        "--max-depth",
        "-1",
        "--dot",
        check=False,
    )
    assert bad_depth.returncode == 2
    assert bad_depth.stdout == ""


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
        "shortest-path",
        "demo:new",
        "demo:other",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--dot",
    )
    assert quote_dot_string("demo:new") in cur.stdout
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
        "--dot",
    )
    assert quote_dot_string("demo:old") in hist.stdout
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


def _shortest_path_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_shortest_path_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_shortest_path_dot = wrap_dumps

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
            ["shortest-path", "A", "C", "--graph", graph, "--dot"],
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
    graph = _publish(tmp_path, _path_entities(), _path_rels())
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_shortest_path_dot_hold, args=(str(graph), held, resume, q)
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
    graph = _publish(tmp_path, _path_entities(), _path_rels())
    g = ByogGraph(graph)
    calls: list[str] = []
    orig = g.shortest_path

    def wrapped(*args, **kwargs):
        calls.append("shortest_path")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "shortest_path", wrapped)
    for name in (
        "condensation",
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
    result = g.shortest_path("A", "C")
    assert calls == ["shortest_path"]
    dumps_shortest_path_dot(result)
    assert calls == ["shortest_path"]

    serializer_calls = 0
    orig_dumps = dumps_shortest_path_dot

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner

    monkeypatch.setattr(gq, "dumps_shortest_path_dot", counted_dumps)
    invoked = CliRunner().invoke(
        gq.app, ["shortest-path", "A", "C", "--graph", str(graph), "--dot"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert serializer_calls == 1
    assert invoked.stdout == GOLDEN_PATH

    dot_src = (ROOT / "src" / "graphrag_code" / "shortest_path_dot.py").read_text(
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
    shortest_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_shortest_path":
            shortest_fn = ast.get_source_segment(gq_src, node)
    assert shortest_fn is not None
    assert "subprocess" not in shortest_fn
    assert "compute_shortest_path" not in shortest_fn
    assert "networkx" not in shortest_fn
    assert shortest_fn.count(".shortest_path(") == 1
    assert shortest_fn.count("dumps_shortest_path_dot(") == 1
    assert "sys.stdout.write(dumps_shortest_path_dot(result))" in shortest_fn
    assert "sys.stdout.flush()" in shortest_fn
    write_at = shortest_fn.find("sys.stdout.write(dumps_shortest_path_dot(result))")
    flush_at = shortest_fn.find("sys.stdout.flush()")
    with_at = shortest_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < flush_at
    assert not list(tmp_path.glob("*.dot"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_mcp_remains_seventeen_tools_without_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, _path_entities(), _path_rels())
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.shortest_path).parameters)
    assert "dot" not in params
    assert "format" not in params
    assert params == [
        "self",
        "source",
        "target",
        "max_depth",
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
        "shortest_path",
        "degree_ranking",
        "impact",
        "impact_graph",
        "type_closure",
        "context_pack",
        "snapshot_history",
        "snapshot_diff",
    ]
    assert len(TOOL_NAMES) == 18
    assert "shortest-path" not in TOOL_NAMES

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == 18
            tool = next(item for item in tools if item.name == "shortest_path")
            props = tool.input_schema.get("properties") or {}
            assert "dot" not in props
            assert "format" not in props
            assert list(props) == [
                "source",
                "target",
                "max_depth",
                "edge_types",
                "snapshot",
            ]
            result = await client.call_tool(
                "shortest_path", {"source": "A", "target": "C"}
            )
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "shortest_path"
            assert "digraph" not in json.dumps(body)

    anyio_run(_body)
