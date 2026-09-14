"""Deterministic Graphviz DOT export for the unbounded dependency-order CLI.

Renders the existing ByogGraph.dependency_order title list. Does not add a
Graphviz runtime, a second containment-order algorithm, an MCP tool, or an
output file.
"""
from __future__ import annotations

import ast
import hashlib
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
    PUBLICATION_LOCK_NAME,
    ByogGraph,
    compute_containment_dependency_order,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_dependency_order_dot,
    dumps_dependency_order_json,
    format_dependency_order_human,
)
from graphrag_code.dependency_order_dot import (  # type: ignore
    DEPENDENCY_ORDER_DOT_SCHEMA_VERSION,
    HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES,
    DependencyOrderDotError,
    dumps_dependency_order_dot as direct_dumps,
)
from graphrag_code.mcp_server import TOOL_NAMES, GraphMcpSession  # type: ignore
from graphrag_code.subgraph_dot import quote_dot_string  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
GRAPH_ATTR_RE = re.compile(r"graph \[\n(.*?)\n  \];", re.DOTALL)
NODE_ATTR_RE = re.compile(r"n\d{4} \[([^\]]+)\];")

GOLDEN_MULTI = """\
digraph graphrag_dependency_order {
  graph [
    schema_version="1",
    n_nodes_total="6"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="Isolated", title="Isolated"];
  n0001 [label="X", title="X"];
  n0002 [label="A", title="A"];
  n0003 [label="B", title="B"];
  n0004 [label="C", title="C"];
  n0005 [label="Y", title="Y"];
}
"""

GOLDEN_EMPTY = """\
digraph graphrag_dependency_order {
  graph [
    schema_version="1",
    n_nodes_total="0"
  ];
  rankdir="LR";
  node [shape="box"];
}
"""

GOLDEN_UNSORTED = """\
digraph graphrag_dependency_order {
  graph [
    schema_version="1",
    n_nodes_total="2"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="Z", title="Z"];
  n0001 [label="A", title="A"];
}
"""

GOLDEN_UNICODE = """\
digraph graphrag_dependency_order {
  graph [
    schema_version="1",
    n_nodes_total="4"
  ];
  rankdir="LR";
  node [shape="box"];
  n0000 [label="A", title="A"];
  n0001 [label="Z", title="Z"];
  n0002 [label="é", title="é"];
  n0003 [label="Ω", title="Ω"];
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


def _contains(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "contains", hid=hid, **extra)


def _calls(source: str, target: str, hid: int = 1, **extra) -> dict:
    return _rel(source, target, "calls", hid=hid, **extra)


def _fixture_entities() -> list[dict]:
    return [_entity(title) for title in ("A", "B", "C", "X", "Isolated")]


def _fixture_rels() -> list[dict]:
    return [
        _contains("A", "A", rid="rel:self"),
        _contains("A", "B", rid="rel:ab1"),
        _contains("A", "B", rid="rel:ab2"),
        _contains("B", "C", rid="rel:bc"),
        _contains("C", "A", rid="rel:ca"),
        _contains("X", "A", rid="rel:xa"),
        _contains("B", "Y", rid="rel:by"),
        _calls("A", "ghost", rid="rel:call"),
    ]


def _unicode_entities() -> list[dict]:
    return [_entity(title) for title in ("é", "Z", "A", "Ω")]


def _unicode_rels() -> list[dict]:
    return [
        _contains("Z", "é", rid="rel:ze"),
        _contains("A", "Ω", rid="rel:ao"),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_dependency_order_dot"
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


def test_golden_order_empty_unicode_and_hash_seed_independent():
    ents = pd.DataFrame(_fixture_entities())
    rels = pd.DataFrame(_fixture_rels())
    produced = compute_containment_dependency_order(ents, rels)
    payload = dumps_dependency_order_dot(produced)
    assert produced == ["Isolated", "X", "A", "B", "C", "Y"]
    assert payload == GOLDEN_MULTI
    assert payload == dumps_dependency_order_dot(produced)
    assert payload == direct_dumps(produced)
    assert payload.endswith("\n") and not payload.endswith("\n\n")
    assert dumps_dependency_order_dot([]) == GOLDEN_EMPTY
    assert dumps_dependency_order_dot(["Z", "A"]) == GOLDEN_UNSORTED
    assert "n0000 [label=\"Z\"" in GOLDEN_UNSORTED
    uents = pd.DataFrame(_unicode_entities())
    urels = pd.DataFrame(_unicode_rels())
    shuffled = dumps_dependency_order_dot(
        compute_containment_dependency_order(
            uents.sample(frac=1, random_state=7).reset_index(drop=True),
            urels.sample(frac=1, random_state=11).reset_index(drop=True),
        )
    )
    assert shuffled == GOLDEN_UNICODE
    assert dumps_dependency_order_dot(
        compute_containment_dependency_order(uents, urels)
    ) == GOLDEN_UNICODE
    assert (
        "schema_version="
        + quote_dot_string(str(DEPENDENCY_ORDER_DOT_SCHEMA_VERSION))
        in payload
    )
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_containment_dependency_order
from graphrag_code.dependency_order_dot import dumps_dependency_order_dot
ents = pd.DataFrame([
    {"id": "eA", "title": "A", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eB", "title": "B", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eC", "title": "C", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eX", "title": "X", "type": "function", "source_file": "a.py", "extractor": "x"},
    {"id": "eI", "title": "Isolated", "type": "function", "source_file": "a.py", "extractor": "x"},
])
rels = pd.DataFrame([
    {"id": "rself", "source": "A", "target": "A", "type": "contains", "extractor": "x"},
    {"id": "rab1", "source": "A", "target": "B", "type": "contains", "extractor": "x"},
    {"id": "rab2", "source": "A", "target": "B", "type": "contains", "extractor": "x"},
    {"id": "rbc", "source": "B", "target": "C", "type": "contains", "extractor": "x"},
    {"id": "rca", "source": "C", "target": "A", "type": "contains", "extractor": "x"},
    {"id": "rxa", "source": "X", "target": "A", "type": "contains", "extractor": "x"},
    {"id": "rby", "source": "B", "target": "Y", "type": "contains", "extractor": "x"},
    {"id": "rcall", "source": "A", "target": "ghost", "type": "calls", "extractor": "x"},
])
print(dumps_dependency_order_dot(compute_containment_dependency_order(ents, rels)), end="")
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


def test_live_graph_preserves_producer_order_and_has_no_edges(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    produced = g.dependency_order()
    payload = dumps_dependency_order_dot(produced)
    assert produced == ["Isolated", "X", "A", "B", "C", "Y"]
    assert payload == GOLDEN_MULTI
    assert _node_ids(payload) == [
        "n0000",
        "n0001",
        "n0002",
        "n0003",
        "n0004",
        "n0005",
    ]
    assert _graph_attr_names(payload) == ["schema_version", "n_nodes_total"]
    for attrs in _node_attr_names(payload):
        assert attrs == ["label", "title"]
        assert "rank" not in attrs
    structural = _structural(payload)
    assert " -> " not in structural
    assert " -- " not in structural
    assert "invis" not in structural
    assert "constraint=" not in structural
    assert "rank=same" not in payload
    assert "subgraph" not in payload
    assert "cluster_" not in payload
    assert "ghost" not in payload
    assert 'n_nodes_total="6"' in payload
    src = (ROOT / "src" / "graphrag_code" / "dependency_order_dot.py").read_text(
        encoding="utf-8"
    )
    assert "not a claim that a rendered Graphviz layout will" in src
    assert "not an ordinal rank field" in src


def test_escaping_injection_controls_and_no_new_statements():
    nasty = 'x"]; attacker -> node [label="pwned'
    comment = "z /* comment */ // still"
    payload = dumps_dependency_order_dot(
        [nasty, comment, "é☃\n\t\\", "comma,value", "a -- b; { }"]
    )
    structural = _structural(payload)
    assert "attacker" not in structural
    assert "pwned" not in structural
    assert "n9999" not in structural
    assert "/*" not in structural
    assert "//" not in structural
    assert " -> " not in structural
    assert " -- " not in structural
    assert nasty not in structural
    assert quote_dot_string(nasty) in payload
    assert "\\n" in payload and "\t" not in payload
    assert _node_ids(payload) == ["n0000", "n0001", "n0002", "n0003", "n0004"]
    controlled = dumps_dependency_order_dot(["A\x01B", "mid\x9f", "C\x7fD", "Isolated\x85"])
    assert "\\x01" in controlled
    assert "\\x9f" in controlled
    assert "\\x7f" in controlled
    assert "\\x85" in controlled
    assert "\x01" not in controlled
    assert "\x7f" not in controlled
    cr = dumps_dependency_order_dot(["A\rB"])
    assert "\\r" in cr
    assert "\r" not in cr


def test_reject_non_list_non_string_empty_duplicate_and_surrogate():
    for invalid in (
        "A",
        ("A",),
        {"A"},
        {"titles": ["A"]},
        iter(["A"]),
        b"A",
        None,
        1,
    ):
        with pytest.raises(DependencyOrderDotError, match="must be a list"):
            dumps_dependency_order_dot(invalid)  # type: ignore[arg-type]
    with pytest.raises(DependencyOrderDotError, match="must be a string"):
        dumps_dependency_order_dot(["A", 1])  # type: ignore[list-item]
    with pytest.raises(DependencyOrderDotError, match="non-empty"):
        dumps_dependency_order_dot(["A", ""])
    with pytest.raises(DependencyOrderDotError, match="duplicate"):
        dumps_dependency_order_dot(["A", "A"])
    with pytest.raises(DependencyOrderDotError, match="UTF-8"):
        dumps_dependency_order_dot(["A", "\ud800"])


def test_byte_cap_boundaries(monkeypatch: pytest.MonkeyPatch):
    payload = dumps_dependency_order_dot(["Isolated", "X", "A", "B", "C", "Y"])
    size = len(payload.encode("utf-8"))
    import graphrag_code.dependency_order_dot as dot_mod

    monkeypatch.setattr(dot_mod, "HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES", size)
    assert dumps_dependency_order_dot(["Isolated", "X", "A", "B", "C", "Y"]) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES", size + 1)
    assert dumps_dependency_order_dot(["Isolated", "X", "A", "B", "C", "Y"]) == payload
    monkeypatch.setattr(dot_mod, "HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES", size - 1)
    with pytest.raises(DependencyOrderDotError, match="hard limit"):
        dumps_dependency_order_dot(["Isolated", "X", "A", "B", "C", "Y"])
    assert HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES == 1_000_000


def test_cli_json_dot_exclusive_human_json_unchanged_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    common = ["dependency-order", "--graph", str(graph)]
    human = _run(sys.executable, str(QUERY), *common)
    json_out = _run(sys.executable, str(QUERY), *common, "--json")
    payload = json.loads(json_out.stdout)
    assert dumps_dependency_order_json(payload) + "\n" == json_out.stdout
    assert format_dependency_order_human(payload) + "\n" == human.stdout
    both = _run(sys.executable, str(QUERY), *common, "--json", "--dot", check=False)
    assert both.returncode == 2
    assert both.stdout == ""
    assert "mutually exclusive" in both.stderr
    product_both = _run(
        sys.executable, str(CLI), *common, "--json", "--dot", check=False
    )
    assert product_both.returncode == 2
    assert product_both.stdout == ""
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
    empty_d = _run(
        sys.executable,
        str(QUERY),
        "dependency-order",
        "--graph",
        str(empty_graph),
        "--dot",
    )
    assert empty_h.stdout == ""
    assert empty_j.stdout == "[]\n"
    assert empty_d.stdout == GOLDEN_EMPTY
    from typer.testing import CliRunner

    from graphrag_code.graph_query import app
    import graphrag_code.graph_query as gq
    import graphrag_code.dependency_order_dot as dot_mod

    def boom(*_a, **_k):
        raise AssertionError("graph observed before json/dot rejection")

    monkeypatch.setattr(gq, "_scoped_graph", boom)
    rejected = CliRunner().invoke(
        app, ["dependency-order", "--graph", str(graph), "--json", "--dot"]
    )
    assert rejected.exit_code == 2
    assert rejected.stdout == ""
    monkeypatch.undo()
    monkeypatch.setattr(dot_mod, "HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES", 32)
    overflow = CliRunner().invoke(
        app, ["dependency-order", "--graph", str(graph), "--dot"]
    )
    assert overflow.exit_code == 2
    assert overflow.stdout == ""
    assert "hard limit" in overflow.stderr


def test_cli_help_mentions_dot_and_not_an_image():
    for args in (
        [sys.executable, str(QUERY), "dependency-order", "--help"],
        [sys.executable, str(CLI), "dependency-order", "--help"],
        [
            sys.executable,
            "-m",
            "graphrag_code.graph_query",
            "dependency-order",
            "--help",
        ],
        [sys.executable, "-m", "graphrag_code", "dependency-order", "--help"],
    ):
        help_out = _run(*args)
        assert "--dot" in help_out.stdout
        assert "--json" in help_out.stdout
        assert "Graphviz" in help_out.stdout
        assert "Mutually" in help_out.stdout
        assert "exclusive" in help_out.stdout
        assert "--max-nodes" not in help_out.stdout


def test_script_module_product_installed_dot_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    args = ["dependency-order", "--graph", str(graph), "--dot"]
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
    expected = dumps_dependency_order_dot(ByogGraph(graph).dependency_order())
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
        "dependency-order",
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
        "dependency-order",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        "--dot",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == ""
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    unlocked_explicit = _run(
        sys.executable,
        str(QUERY),
        "dependency-order",
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
        "dependency-order",
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
    current = (graph / "current").read_text(encoding="utf-8").strip()
    assert current == newer.name
    cur = _run(
        sys.executable,
        str(QUERY),
        "dependency-order",
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
        "dependency-order",
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


def _dependency_order_dot_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_dependency_order_dot

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_dependency_order_dot = wrap_dumps

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
            ["dependency-order", "--graph", graph, "--dot"],
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
        target=_dependency_order_dot_hold, args=(str(graph), held, resume, q)
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
    orig = g.dependency_order

    def wrapped(*args, **kwargs):
        calls.append("dependency_order")
        return orig(*args, **kwargs)

    monkeypatch.setattr(g, "dependency_order", wrapped)
    for name in (
        "condensation",
        "components",
        "strong_components",
        "shortest_path",
        "degree_ranking",
        "subgraph",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    result = g.dependency_order()
    assert calls == ["dependency_order"]
    dumps_dependency_order_dot(result)
    assert calls == ["dependency_order"]

    serializer_calls = 0
    orig_dumps = dumps_dependency_order_dot

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner

    monkeypatch.setattr(gq, "dumps_dependency_order_dot", counted_dumps)
    invoked = CliRunner().invoke(
        gq.app, ["dependency-order", "--graph", str(graph), "--dot"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert serializer_calls == 1
    assert invoked.stdout == GOLDEN_MULTI

    dot_src = (ROOT / "src" / "graphrag_code" / "dependency_order_dot.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(dot_src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "typing", "subgraph_dot"}
    assert "subprocess" not in imported
    assert "graphviz" not in imported
    assert "networkx" not in imported
    assert "tempfile" not in imported
    assert "byog_graph" not in imported
    assert "compute_containment_dependency_order(" not in dot_src
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    dep_fn = None
    gq_tree = ast.parse(gq_src)
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_dep_order":
            dep_fn = ast.get_source_segment(gq_src, node)
    assert dep_fn is not None
    assert "subprocess" not in dep_fn
    assert "compute_containment_dependency_order" not in dep_fn
    assert ".condensation(" not in dep_fn
    assert ".strong_components(" not in dep_fn
    assert "networkx" not in dep_fn
    assert dep_fn.count(".dependency_order(") == 1
    assert dep_fn.count("dumps_dependency_order_dot(") == 1
    assert "sys.stdout.write(dumps_dependency_order_dot(result))" in dep_fn
    assert "sys.stdout.flush()" in dep_fn
    write_at = dep_fn.find("sys.stdout.write(dumps_dependency_order_dot(result))")
    flush_at = dep_fn.find("sys.stdout.flush()")
    with_at = dep_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at < flush_at
    assert not list(tmp_path.glob("*.dot"))
    assert not list(Path.cwd().glob(".graphrag-export-*"))


def test_mcp_remains_seventeen_tools_without_dependency_order_or_dot(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    assert not hasattr(GraphMcpSession, "dependency_order")
    assert "dependency_order" not in TOOL_NAMES
    assert "dependency-order" not in TOOL_NAMES
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
            assert "dependency_order" not in names
            assert "dependency-order" not in names
            for tool in tools:
                props = tool.input_schema.get("properties") or {}
                assert "dot" not in props
                assert "format" not in props

    anyio_run(_body)
