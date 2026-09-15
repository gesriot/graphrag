"""Bounded inventory of exact persisted module entities (CLI/Python only).

Canonical producer: compute_module_inventory. MCP remains 19 tools and
does not expose this query. No DOT, Graphviz, or second algorithm.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.byog_graph import (  # type: ignore
    DEFAULT_MODULE_INVENTORY_MAX_MEMBERS_PER_MODULE,
    DEFAULT_MODULE_INVENTORY_MAX_MODULES,
    HARD_MAX_MODULE_INVENTORY_MEMBERS_PER_MODULE,
    HARD_MAX_MODULE_INVENTORY_MODULES,
    ByogGraph,
    compute_module_inventory,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_modules_json,
    format_modules_human,
    modules as free_modules,
)
from graphrag_code.mcp_server import TOOL_NAMES  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
SCHEMA_KEYS = (
    "schema_version",
    "relationship_type",
    "max_modules",
    "max_members_per_module",
    "n_modules_total",
    "n_modules_returned",
    "n_members_total",
    "n_members_returned",
    "modules_truncated",
    "members_truncated",
    "modules",
)
MODULE_KEYS = (
    "id",
    "title",
    "source_file",
    "description",
    "n_members_total",
    "n_members_returned",
    "members_truncated",
    "members",
)
EMPTY_HUMAN = "modules (0/0):"


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


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_modules"
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
        keep_last=5,
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


def _assert_schema(result: dict) -> None:
    assert list(result) == list(SCHEMA_KEYS)
    assert result["schema_version"] == 1
    assert result["relationship_type"] == "contains"
    for item in result["modules"]:
        assert list(item) == list(MODULE_KEYS)


def test_producer_schema_exact_module_type_and_ordering():
    ents = pd.DataFrame(
        [
            _entity("pkg:z", "module", source_file="z.py", description="Z"),
            _entity("file.py", "file"),
            _entity("pkg:a", "module", source_file="a.py", description="A"),
            _entity("Module", "Module"),
            _entity("MODULE", "MODULE"),
            _entity(" module", " module"),
            _entity("pkg:fn", "function"),
        ]
    )
    rels = pd.DataFrame(
        [
            _rel("pkg:z", "zeta", "contains", rid="r-z"),
            _rel("pkg:a", "beta", "contains", rid="r-b"),
            _rel("pkg:a", "alpha", "contains", rid="r-a"),
            _rel("file.py", "pkg:fn", "contains", rid="r-file"),
            _rel("pkg:fn", "pkg:a", "calls", rid="r-call"),
            _rel("pkg:a", "pkg:z", "depends_on", rid="r-dep"),
            _rel("pkg:a", "T", "uses_type", rid="r-type"),
            _rel("pkg:a", "D", "uses_data", rid="r-data"),
            _rel("pkg:a", "pkg:z", "CONTAINS", rid="r-upper"),
        ]
    )
    shuffled_ents = ents.sample(frac=1, random_state=7).reset_index(drop=True)
    shuffled_rels = rels.sample(frac=1, random_state=11).reset_index(drop=True)
    result = compute_module_inventory(shuffled_ents, shuffled_rels)
    _assert_schema(result)
    assert [m["title"] for m in result["modules"]] == ["pkg:a", "pkg:z"]
    assert result["modules"][0]["members"] == ["alpha", "beta"]
    assert result["modules"][1]["members"] == ["zeta"]
    assert result["n_modules_total"] == 2
    assert result["n_modules_returned"] == 2
    assert result["n_members_total"] == 3
    assert result["n_members_returned"] == 3
    assert result["modules_truncated"] is False
    assert result["members_truncated"] is False
    assert result["max_modules"] == DEFAULT_MODULE_INVENTORY_MAX_MODULES
    assert (
        result["max_members_per_module"]
        == DEFAULT_MODULE_INVENTORY_MAX_MEMBERS_PER_MODULE
    )
    assert "pkg:fn" not in [m["title"] for m in result["modules"]]
    assert "file.py" not in [m["title"] for m in result["modules"]]


def test_contains_semantics_self_parallel_endpoint_and_non_module_source():
    ents = pd.DataFrame(
        [
            _entity("pkg:a", "module"),
            _entity("pkg:b", "module"),
            _entity("fn", "function"),
        ]
    )
    rels = pd.DataFrame(
        [
            _rel("pkg:a", "fn", "contains", rid="r1"),
            _rel("pkg:a", "fn", "contains", rid="r1b"),
            _rel("pkg:a", "ghost", "contains", rid="r-ghost"),
            _rel("pkg:a", "pkg:a", "contains", rid="r-self"),
            _rel("fn", "orphan", "contains", rid="r-fn"),
            _rel("ghost-src", "nope", "contains", rid="r-ghost-src"),
            _rel("pkg:b", "fn", "contains", rid="r-b"),
        ]
    )
    result = compute_module_inventory(ents, rels)
    by_title = {m["title"]: m for m in result["modules"]}
    assert by_title["pkg:a"]["members"] == ["fn", "ghost", "pkg:a"]
    assert by_title["pkg:a"]["n_members_total"] == 3
    assert by_title["pkg:b"]["members"] == ["fn"]
    assert result["n_members_total"] == 4
    assert "orphan" not in by_title["pkg:a"]["members"]
    assert "nope" not in by_title["pkg:a"]["members"]
    assert "ghost-src" not in [m["title"] for m in result["modules"]]


def test_utf8_order_independent_of_row_order():
    ents = pd.DataFrame(
        [
            _entity("é", "module"),
            _entity("A", "module"),
            _entity("Ā", "module"),
        ]
    )
    rels = pd.DataFrame(
        [
            _rel("A", "é", "contains", rid="m1"),
            _rel("A", "A", "contains", rid="m2"),
            _rel("A", "Ā", "contains", rid="m3"),
        ]
    )
    result = compute_module_inventory(
        ents.sample(frac=1, random_state=3).reset_index(drop=True),
        rels.sample(frac=1, random_state=5).reset_index(drop=True),
    )
    assert [m["title"] for m in result["modules"]] == ["A", "é", "Ā"]
    assert result["modules"][0]["members"] == ["A", "é", "Ā"]


def test_independent_caps_and_zero_members_cap():
    ents = pd.DataFrame(
        [
            _entity("pkg:b", "module"),
            _entity("pkg:a", "module"),
            _entity("pkg:c", "module"),
        ]
    )
    rels = pd.DataFrame(
        [
            _rel("pkg:a", "m1", "contains", rid="a1"),
            _rel("pkg:a", "m2", "contains", rid="a2"),
            _rel("pkg:b", "x", "contains", rid="b1"),
            _rel("pkg:c", "y", "contains", rid="c1"),
        ]
    )
    capped = compute_module_inventory(
        ents, rels, max_modules=1, max_members_per_module=1
    )
    assert [m["title"] for m in capped["modules"]] == ["pkg:a"]
    assert capped["n_modules_total"] == 3
    assert capped["n_modules_returned"] == 1
    assert capped["modules_truncated"] is True
    assert capped["n_members_total"] == 4
    assert capped["modules"][0]["members"] == ["m1"]
    assert capped["modules"][0]["n_members_total"] == 2
    assert capped["modules"][0]["n_members_returned"] == 1
    assert capped["modules"][0]["members_truncated"] is True
    assert capped["n_members_returned"] == 1
    assert capped["members_truncated"] is True

    zero = compute_module_inventory(ents, rels, max_members_per_module=0)
    assert zero["n_modules_returned"] == 3
    assert zero["n_members_total"] == 4
    assert zero["n_members_returned"] == 0
    assert zero["members_truncated"] is True
    assert all(item["members"] == [] for item in zero["modules"])
    assert all(item["n_members_returned"] == 0 for item in zero["modules"])

    member_only = compute_module_inventory(ents, rels, max_modules=2)
    assert [m["title"] for m in member_only["modules"]] == ["pkg:a", "pkg:b"]
    assert member_only["n_members_total"] == 4
    assert member_only["n_members_returned"] == 3
    assert member_only["members_truncated"] is False


def test_empty_graph_and_zero_member_module():
    empty = compute_module_inventory(None, None)
    _assert_schema(empty)
    assert empty["modules"] == []
    assert empty["n_modules_total"] == 0
    assert empty["n_members_total"] == 0
    assert empty["modules_truncated"] is False
    assert empty["members_truncated"] is False
    assert format_modules_human(empty) == EMPTY_HUMAN
    lone = compute_module_inventory(
        pd.DataFrame([_entity("pkg:a", "module")]),
        pd.DataFrame(columns=["id", "source", "target", "type"]),
    )
    assert lone["modules"][0]["title"] == "pkg:a"
    assert lone["modules"][0]["members"] == []
    assert lone["modules"][0]["n_members_total"] == 0
    assert lone["n_members_total"] == 0


def test_malformed_rows_and_invalid_limits():
    good_ents = pd.DataFrame([_entity("pkg:a", "module")])
    good_rels = pd.DataFrame([_rel("pkg:a", "fn", "contains", rid="r1")])
    with pytest.raises(ValueError, match="max_modules"):
        compute_module_inventory(good_ents, good_rels, max_modules=True)
    with pytest.raises(ValueError, match="max_modules"):
        compute_module_inventory(good_ents, good_rels, max_modules=0)
    with pytest.raises(ValueError, match="max_modules"):
        compute_module_inventory(
            good_ents, good_rels, max_modules=HARD_MAX_MODULE_INVENTORY_MODULES + 1
        )
    with pytest.raises(ValueError, match="max_members_per_module"):
        compute_module_inventory(good_ents, good_rels, max_members_per_module=-1)
    with pytest.raises(ValueError, match="max_members_per_module"):
        compute_module_inventory(good_ents, good_rels, max_members_per_module=1.5)
    with pytest.raises(ValueError, match="max_members_per_module"):
        compute_module_inventory(good_ents, good_rels, max_members_per_module=math.nan)
    with pytest.raises(ValueError, match="max_members_per_module"):
        compute_module_inventory(good_ents, good_rels, max_members_per_module=math.inf)
    missing_cols = pd.DataFrame([{"title": "pkg:a"}])
    with pytest.raises(ValueError, match="missing required columns"):
        compute_module_inventory(missing_cols, good_rels)
    malformed_type = pd.DataFrame(
        [_entity("pkg:a", "module"), _entity("other", "function", type=None)]
    )
    with pytest.raises(ValueError, match="invalid type"):
        compute_module_inventory(malformed_type, good_rels)
    dup_title = pd.DataFrame(
        [
            _entity("pkg:a", "module", id="ent:1"),
            _entity("pkg:a", "module", id="ent:2"),
        ]
    )
    with pytest.raises(ValueError, match="duplicate selected module title"):
        compute_module_inventory(dup_title, good_rels)
    dup_id = pd.DataFrame(
        [
            _entity("pkg:a", "module", id="ent:same"),
            _entity("pkg:b", "module", id="ent:same"),
        ]
    )
    with pytest.raises(ValueError, match="duplicate selected module id"):
        compute_module_inventory(dup_id, good_rels)
    bad_rel = pd.DataFrame(
        [
            {
                "id": "r-bad",
                "source": "pkg:a",
                "target": None,
                "type": "contains",
            }
        ]
    )
    with pytest.raises(ValueError, match="invalid target"):
        compute_module_inventory(good_ents, bad_rel)
    dup_rel = pd.DataFrame(
        [
            _rel("pkg:a", "fn", "contains", rid="dup"),
            _rel("pkg:a", "other", "calls", rid="dup"),
        ]
    )
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_module_inventory(good_ents, dup_rel)


def test_byoggraph_and_free_function_one_producer_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("pkg:a", "module"), _entity("fn", "function")],
        [_rel("pkg:a", "fn", "contains", rid="r1")],
    )
    g = ByogGraph(graph)
    import graphrag_code.byog_graph as byog
    import graphrag_code.graph_query as gq

    calls = {"n": 0}
    orig = byog.compute_module_inventory

    def counted(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(byog, "compute_module_inventory", counted)
    monkeypatch.setattr(gq, "compute_module_inventory", counted)
    via_graph = g.modules()
    assert calls["n"] == 1
    via_free = free_modules(g.ents, g.rels)
    assert calls["n"] == 2
    expected = orig(g.ents, g.rels)
    assert via_graph == via_free == expected
    assert inspect.signature(ByogGraph.modules).parameters["max_modules"].default == (
        DEFAULT_MODULE_INVENTORY_MAX_MODULES
    )
    assert inspect.signature(ByogGraph.modules).parameters[
        "max_members_per_module"
    ].default == DEFAULT_MODULE_INVENTORY_MAX_MEMBERS_PER_MODULE


def test_exact_human_json_bytes_and_cli_parity(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    ents = [
        _entity("pkg:b", "module", source_file=None, description=None),
        _entity("pkg:a", "module", source_file="a.py", description="alpha"),
    ]
    rels = [
        _rel("pkg:a", "é", "contains", rid="r-e"),
        _rel("pkg:a", "A", "contains", rid="r-a"),
    ]
    graph = _publish(tmp_path, ents, rels)
    expected = ByogGraph(graph).modules()
    human = format_modules_human(expected)
    assert human == (
        "modules (2/2):\n"
        "  pkg:a [a.py] members (2/2):\n"
        "    A\n"
        "    é\n"
        "  pkg:b members (0/0):"
    )
    empty_human = format_modules_human(compute_module_inventory(None, None))
    assert empty_human == EMPTY_HUMAN
    dumped = dumps_modules_json(expected)
    assert dumped == dumps_modules_json(json.loads(dumped))
    args = [
        "modules",
        "--graph",
        str(graph),
        "--max-modules",
        "50",
        "--max-members-per-module",
        "50",
    ]
    script = _run(sys.executable, str(QUERY), *args)
    module = _run(sys.executable, "-m", "graphrag_code.graph_query", *args)
    product = _run(sys.executable, str(CLI), *args)
    package = _run(sys.executable, "-m", "graphrag_code", *args)
    json_args = [*args, "--json"]
    script_j = _run(sys.executable, str(QUERY), *json_args)
    module_j = _run(sys.executable, "-m", "graphrag_code.graph_query", *json_args)
    product_j = _run(sys.executable, str(CLI), *json_args)
    package_j = _run(sys.executable, "-m", "graphrag_code", *json_args)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed_h = subprocess.run(
        ["graphrag-code", *args],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    installed_j = subprocess.run(
        ["graphrag-code", *json_args],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed_h.returncode == 0, installed_h.stderr
    assert installed_j.returncode == 0, installed_j.stderr
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed_h.stdout
        == (human + "\n")
    )
    assert (
        script_j.stdout
        == module_j.stdout
        == product_j.stdout
        == package_j.stdout
        == installed_j.stdout
        == (dumped + "\n")
    )
    empty_graph = _publish(tmp_path / "empty", [_entity("fn", "function")], [])
    empty_cli = _run(sys.executable, str(QUERY), "modules", "--graph", str(empty_graph))
    assert empty_cli.stdout == EMPTY_HUMAN + "\n"
    help_out = _run(sys.executable, str(QUERY), "modules", "--help")
    assert "--json" in help_out.stdout
    assert "--max-modules" in help_out.stdout
    assert "--max-members-per-module" in help_out.stdout
    assert "architecture" in help_out.stdout.lower() or "contains" in help_out.stdout.lower()


def test_invalid_args_snapshot_and_data_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "modules",
        "--graph",
        str(tmp_path / "missing"),
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    graph = _publish(
        tmp_path,
        [_entity("pkg:a", "module")],
        [_rel("pkg:a", "fn", "contains", rid="r1")],
    )
    bad_snap = _run(
        sys.executable,
        str(QUERY),
        "modules",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == ""
    bad_limit = _run(
        sys.executable,
        str(QUERY),
        "modules",
        "--graph",
        str(graph),
        "--max-modules",
        "0",
        check=False,
    )
    assert bad_limit.returncode == 2
    assert bad_limit.stdout == ""
    assert "max_modules" in bad_limit.stderr
    unknown = _run(
        sys.executable,
        str(QUERY),
        "modules",
        "--graph",
        str(graph),
        "--dot",
        check=False,
    )
    assert unknown.returncode == 2
    assert unknown.stdout == ""


def test_historical_snapshot_does_not_activate_or_consult_current(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old", "module")]),
        pd.DataFrame([_rel("demo:old", "legacy", "contains", rid="old")]),
        pd.DataFrame(
            [
                {
                    "id": "tu:old",
                    "text": "old",
                    "n_tokens": 1,
                    "document_ids": [],
                    "entity_ids": ["ent:module:demo:old"],
                    "relationship_ids": [],
                }
            ]
        ),
        graph,
        keep_last=5,
    )
    publish_byog_snapshot(
        pd.DataFrame([_entity("pkg:new", "module")]),
        pd.DataFrame([_rel("pkg:new", "fresh", "contains", rid="new")]),
        pd.DataFrame(
            [
                {
                    "id": "tu:new",
                    "text": "new",
                    "n_tokens": 1,
                    "document_ids": [],
                    "entity_ids": ["ent:module:pkg:new"],
                    "relationship_ids": [],
                }
            ]
        ),
        graph,
        keep_last=5,
    )
    current = (graph / "current").read_text(encoding="utf-8").strip()
    before = _payload_hashes(graph)
    hist = _run(
        sys.executable,
        str(QUERY),
        "modules",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    payload = json.loads(hist.stdout)
    assert [m["title"] for m in payload["modules"]] == ["demo:old"]
    assert payload["modules"][0]["members"] == ["legacy"]
    assert (graph / "current").read_text(encoding="utf-8").strip() == current
    assert _payload_hashes(graph) == before
    now = json.loads(
        _run(
            sys.executable,
            str(QUERY),
            "modules",
            "--graph",
            str(graph),
            "--json",
        ).stdout
    )
    assert [m["title"] for m in now["modules"]] == ["pkg:new"]


def test_one_load_one_producer_no_nested_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("pkg:a", "module")],
        [_rel("pkg:a", "fn", "contains", rid="r1")],
    )
    g = ByogGraph(graph)
    for name in (
        "subgraph",
        "components",
        "observations",
        "impact",
        "impact_graph",
        "dependency_order",
        "degree_ranking",
        "type_closure",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    result = g.modules()
    assert result["n_modules_total"] == 1
    src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cli_fn = None
    free_fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cli_modules":
            cli_fn = ast.get_source_segment(src, node)
        if isinstance(node, ast.FunctionDef) and node.name == "modules":
            free_fn = ast.get_source_segment(src, node)
    assert cli_fn is not None and free_fn is not None
    assert cli_fn.count(".modules(") == 1
    assert cli_fn.count("dumps_modules_json(") == 1
    assert cli_fn.count("format_modules_human(") == 1
    assert "flush=True" in cli_fn
    assert "with _scoped_graph" in cli_fn
    assert "dumps_" not in cli_fn.split("with _scoped_graph", 1)[0]
    assert free_fn.count("compute_module_inventory(") == 1
    producer = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(
        encoding="utf-8"
    )
    ptree = ast.parse(producer)
    helper = None
    for node in ptree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute_module_inventory":
            helper = ast.get_source_segment(producer, node)
    assert helper is not None
    assert "networkx" not in helper
    assert "graphviz" not in helper
    assert "subprocess" not in helper
    assert "tempfile" not in helper


def _modules_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from graphrag_code import graph_query
    from typer.testing import CliRunner

    orig = graph_query.dumps_modules_json

    def wrap_dumps(result):
        payload = orig(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_modules_json = wrap_dumps
    runner = CliRunner()
    result = runner.invoke(graph_query.app, ["modules", "--graph", graph, "--json"])
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


def test_publisher_waits_through_serialization_write_and_flush(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("pkg:a", "module")],
        [_rel("pkg:a", "fn", "contains", rid="r1")],
    )
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_modules_json_hold, args=(str(graph), held, resume, q))
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


def test_mcp_remains_nineteen_tools_without_modules(tmp_path: Path):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session

    graph = _publish(
        tmp_path,
        [_entity("pkg:a", "module")],
        [_rel("pkg:a", "fn", "contains", rid="r1")],
    )
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    assert not hasattr(session, "modules")
    assert "modules" not in TOOL_NAMES
    assert "module_inventory" not in TOOL_NAMES
    assert "module-inventory" not in TOOL_NAMES
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
            assert "modules" not in names
            assert "module_inventory" not in names
            assert "module-inventory" not in names

    anyio_run(_body)
