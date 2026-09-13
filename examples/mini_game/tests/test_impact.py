"""Canonical unbounded transitive-caller impact producer.

Unifies ByogGraph.impact and graph_query.impact onto
compute_transitive_call_impact. No DOT, no bounded graph, no new MCP tool.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
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
    PUBLICATION_LOCK_NAME,
    ByogGraph,
    compute_transitive_call_impact,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    dumps_impact_json,
    format_impact_human,
    impact as free_impact,
)
from graphrag_code.mcp_server import (  # type: ignore
    DEFAULT_MAX_ITEMS,
    TOOL_NAMES,
    GraphMcpSession,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
CLI = ROOT / "scripts" / "graphrag_code.py"

EMPTY_HUMAN = b"\n"
EMPTY_JSON = b"[]\n"
NONEMPTY_HUMAN = b"B\nC\n"
NONEMPTY_JSON = b'[\n  "B",\n  "C"\n]\n'


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
    return [
        _entity("A"),
        _entity("B"),
        _entity("C"),
        _entity("Isolated"),
        _entity("pkg:pkg", "module"),
    ]


def _fixture_rels() -> list[dict]:
    return [
        _calls("B", "A", hid=1, rid="rel:ba"),
        _calls("C", "B", hid=2, rid="rel:cb"),
        _calls("C", "A", hid=3, rid="rel:ca"),
        _calls("A", "A", hid=4, rid="rel:self"),
        _calls("B", "A", hid=5, rid="rel:ba2"),
        _calls("ghost", "A", hid=6, rid="rel:ghost"),
        _rel("A", "T", "uses_type", hid=7, rid="rel:uses"),
        _rel("pkg:pkg", "A", "contains", hid=8, rid="rel:contains"),
        _rel("A", "B", "depends_on", hid=9, rid="rel:dep"),
        _rel("Isolated", "A", "CALLS", hid=10, rid="rel:CALLS"),
        _calls("X", "Y", hid=11, rid="rel:xy"),
        _calls("Y", "X", hid=12, rid="rel:yx"),
    ]


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_impact"
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
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        list(args),
        capture_output=True,
        env=_child_env(),
        cwd=cwd,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr.decode() + proc.stdout.decode())
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


def test_public_surfaces_and_producer_are_identical(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    produced = compute_transitive_call_impact(g.rels, g.resolve("A"))
    assert produced == ["B", "C", "ghost"]
    assert g.impact("A") == produced
    assert free_impact(g.ents, g.rels, "A") == produced
    assert g.impact("A") is not produced
    assert inspect.signature(ByogGraph.impact).parameters["symbol"].kind
    assert list(inspect.signature(free_impact).parameters) == ["ents", "rels", "symbol"]
    assert list(inspect.signature(compute_transitive_call_impact).parameters) == [
        "rels",
        "root_title",
    ]


def test_direct_transitive_paths_cycles_self_loop_and_filters(tmp_path: Path):
    graph = _publish(tmp_path, _fixture_entities(), _fixture_rels())
    g = ByogGraph(graph)
    assert g.impact("A") == ["B", "C", "ghost"]
    assert g.impact("B") == ["C"]
    assert g.impact("C") == []
    assert g.impact("Isolated") == []
    # Cycle containing the root: A <-> B extra edge already has B -> A and C -> B.
    cycle_root = compute_transitive_call_impact(
        pd.DataFrame([_calls("A", "B", rid="r1"), _calls("B", "A", rid="r2")]),
        "A",
    )
    assert cycle_root == ["B"]
    # Cycle not containing the root.
    off_cycle = compute_transitive_call_impact(
        pd.DataFrame(
            [
                _calls("B", "A", rid="r1"),
                _calls("C", "B", rid="r2"),
                _calls("B", "C", rid="r3"),
            ]
        ),
        "A",
    )
    assert off_cycle == ["B", "C"]
    self_only = compute_transitive_call_impact(
        pd.DataFrame([_calls("A", "A", rid="self")]), "A"
    )
    assert self_only == []
    parallel = compute_transitive_call_impact(
        pd.DataFrame(
            [_calls("B", "A", rid="p1"), _calls("B", "A", rid="p2")]
        ),
        "A",
    )
    assert parallel == ["B"]
    assert "Isolated" not in g.impact("A")
    assert "T" not in g.impact("A")
    assert "pkg:pkg" not in g.impact("A")
    assert compute_transitive_call_impact(g.rels, "Y") == ["X"]
    assert compute_transitive_call_impact(g.rels, "X") == ["Y"]
    assert g.impact("Y") == []


def test_endpoint_only_unicode_empty_inputs_and_hash_seed():
    rels = pd.DataFrame(
        [
            _calls("é", "Z", rid="e1"),
            _calls("A", "Z", rid="e2"),
            _calls("ghost", "Z", rid="e3"),
        ]
    )
    produced = compute_transitive_call_impact(rels, "Z")
    assert produced == ["A", "ghost", "é"]
    assert produced == sorted(produced, key=lambda t: t.encode("utf-8"))
    shuffled = rels.sample(frac=1, random_state=7).reset_index(drop=True)
    assert compute_transitive_call_impact(shuffled, "Z") == produced
    assert compute_transitive_call_impact(None, "Z") == []
    assert compute_transitive_call_impact(pd.DataFrame(), "Z") == []
    assert compute_transitive_call_impact(None, None) == []
    code = r"""
import pandas as pd
from graphrag_code.byog_graph import compute_transitive_call_impact
rels = pd.DataFrame([
    {"id": "e1", "source": "é", "target": "Z", "type": "calls"},
    {"id": "e2", "source": "A", "target": "Z", "type": "calls"},
    {"id": "e3", "source": "ghost", "target": "Z", "type": "calls"},
])
print(repr(compute_transitive_call_impact(rels, "Z")))
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
    assert payloads[0] == payloads[1] == payloads[2] == "['A', 'ghost', 'é']\n"


def test_resolution_exact_alias_partial_ambiguous_and_missing(tmp_path: Path):
    entities = _fixture_entities() + [_entity("other:f"), _entity("m:f")]
    graph = _publish(tmp_path, entities, _fixture_rels())
    g = ByogGraph(graph)
    assert g.impact("A") == free_impact(g.ents, g.rels, "A")
    assert g.impact("pkg") == g.impact("pkg:pkg")
    assert g.impact("Isolat") == []
    assert g.impact("does-not-exist") == []
    assert g.impact("f") == []
    assert compute_transitive_call_impact(g.rels, None) == []
    malformed = pd.DataFrame([_calls("B", "A", rid="ok"), _rel("x", "y", "uses_type", rid="ok")])
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_transitive_call_impact(malformed, None)


def test_strict_malformed_relationship_table_fails_closed():
    good = [_calls("B", "A", rid="r1")]
    missing = pd.DataFrame([{"source": "B", "target": "A", "type": "calls"}])
    with pytest.raises(ValueError, match="missing required columns"):
        compute_transitive_call_impact(missing, "A")
    for field in ("id", "source", "target", "type"):
        row = _calls("B", "A", rid="r1")
        row[field] = None
        with pytest.raises(ValueError, match=field):
            compute_transitive_call_impact(pd.DataFrame([row]), "A")
        row = _calls("B", "A", rid="r1")
        row[field] = ""
        with pytest.raises(ValueError, match=field):
            compute_transitive_call_impact(pd.DataFrame([row]), "A")
        row = _calls("B", "A", rid="r1")
        row[field] = 1
        with pytest.raises(ValueError, match=field):
            compute_transitive_call_impact(pd.DataFrame([row]), "A")
    dup = pd.DataFrame(
        [_calls("B", "A", rid="dup"), _calls("C", "A", rid="dup")]
    )
    with pytest.raises(ValueError, match="duplicate relationship id"):
        compute_transitive_call_impact(dup, "A")
    non_call = _rel("X", "Y", "uses_type", rid="u1")
    non_call["source"] = None
    with pytest.raises(ValueError, match="source"):
        compute_transitive_call_impact(pd.DataFrame(good + [non_call]), "A")
    with pytest.raises(ValueError, match="source"):
        compute_transitive_call_impact(pd.DataFrame(good + [non_call]), None)
    lone = _calls("B", "A", rid="s1")
    lone["target"] = "\ud800"
    with pytest.raises(ValueError, match="target"):
        compute_transitive_call_impact(pd.DataFrame([lone]), "A")
    with pytest.raises(ValueError, match="root_title"):
        compute_transitive_call_impact(pd.DataFrame(good), "")
    with pytest.raises(ValueError, match="root_title"):
        compute_transitive_call_impact(pd.DataFrame(good), True)  # type: ignore[arg-type]


def test_old_duplicated_bfs_bodies_are_gone():
    byog_src = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(
        encoding="utf-8"
    )
    gq_src = (ROOT / "src" / "graphrag_code" / "graph_query.py").read_text(
        encoding="utf-8"
    )
    byog_tree = ast.parse(byog_src)
    gq_tree = ast.parse(gq_src)
    impact_fn = None
    producer_fn = None
    for node in byog_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ByogGraph":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "impact":
                    impact_fn = ast.get_source_segment(byog_src, item)
        if isinstance(node, ast.FunctionDef) and node.name == "compute_transitive_call_impact":
            producer_fn = ast.get_source_segment(byog_src, node)
    free_fn = None
    cli_fn = None
    for node in gq_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "impact":
            free_fn = ast.get_source_segment(gq_src, node)
        if isinstance(node, ast.FunctionDef) and node.name == "cli_impact":
            cli_fn = ast.get_source_segment(gq_src, node)
    assert impact_fn is not None and producer_fn is not None
    assert free_fn is not None and cli_fn is not None
    assert "compute_transitive_call_impact(" in impact_fn
    assert impact_fn.count("compute_transitive_call_impact(") == 1
    assert impact_fn.count("self.resolve(") == 1
    assert "defaultdict" not in impact_fn
    assert "deque" not in impact_fn
    assert "astype(str)" not in impact_fn
    assert "seen.discard" not in impact_fn
    assert "compute_transitive_call_impact(" in free_fn
    assert free_fn.count("compute_transitive_call_impact(") == 1
    assert "_resolve_symbol(" in free_fn
    assert "defaultdict" not in free_fn
    assert "deque" not in free_fn
    assert "seen.discard" not in free_fn
    assert "networkx" not in producer_fn
    assert "subprocess" not in producer_fn
    assert "tempfile" not in producer_fn
    assert "recursion" not in producer_fn
    assert "_component_selected_relationships(" in producer_fn
    assert "while " in producer_fn
    assert "def " not in producer_fn.split("while ", 1)[1]
    assert cli_fn.count(".impact(") == 1
    assert cli_fn.count("dumps_impact_json(") == 1
    assert cli_fn.count("format_impact_human(") == 1
    assert "sys.stdout.flush()" not in cli_fn or "flush=True" in cli_fn
    assert "flush=True" in cli_fn
    assert "with _scoped_graph" in cli_fn
    write_at = min(
        i
        for i in (
            cli_fn.find("print(dumps_impact_json(result), flush=True)"),
            cli_fn.find("print(format_impact_human(result), flush=True)"),
        )
        if i >= 0
    )
    with_at = cli_fn.find("with _scoped_graph")
    assert 0 <= with_at < write_at
    assert "--dot" not in cli_fn.split("typer.Option", 1)[0] or "no ``--dot``" in cli_fn


def test_human_json_byte_parity_and_empty_newline(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B"), _entity("C")],
        [_calls("B", "A", rid="ba"), _calls("C", "B", rid="cb")],
    )
    empty_graph = _publish(
        tmp_path / "empty",
        [_entity("A")],
        [_calls("A", "A", rid="self")],
    )
    args = ["impact", "A", "--graph", str(graph)]
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
        ["graphrag-code", *args], cwd=outside, capture_output=True, env=env
    )
    installed_j = subprocess.run(
        ["graphrag-code", *json_args], cwd=outside, capture_output=True, env=env
    )
    assert installed_h.returncode == 0, installed_h.stderr
    assert installed_j.returncode == 0, installed_j.stderr
    expected = ByogGraph(graph).impact("A")
    assert expected == ["B", "C"]
    assert (
        script.stdout
        == module.stdout
        == product.stdout
        == package.stdout
        == installed_h.stdout
        == NONEMPTY_HUMAN
        == (format_impact_human(expected) + "\n").encode()
    )
    assert (
        script_j.stdout
        == module_j.stdout
        == product_j.stdout
        == package_j.stdout
        == installed_j.stdout
        == NONEMPTY_JSON
        == (dumps_impact_json(expected) + "\n").encode()
    )
    empty_args = ["impact", "A", "--graph", str(empty_graph)]
    empty_h = _run(sys.executable, str(QUERY), *empty_args)
    empty_p = _run(sys.executable, str(CLI), *empty_args)
    empty_j = _run(sys.executable, str(QUERY), *empty_args, "--json")
    empty_pj = _run(sys.executable, str(CLI), *empty_args, "--json")
    assert empty_h.stdout == empty_p.stdout == EMPTY_HUMAN
    assert empty_j.stdout == empty_pj.stdout == EMPTY_JSON
    missing_h = _run(sys.executable, str(QUERY), "impact", "missing", "--graph", str(graph))
    missing_j = _run(
        sys.executable, str(CLI), "impact", "missing", "--graph", str(graph), "--json"
    )
    assert missing_h.stdout == EMPTY_HUMAN
    assert missing_j.stdout == EMPTY_JSON
    assert not list(outside.glob("*.dot"))
    assert not (graph / ".publish.lock").is_symlink()


def test_invalid_graph_snapshot_and_data_exit_2_empty_stdout(tmp_path: Path):
    missing = _run(
        sys.executable,
        str(QUERY),
        "impact",
        "A",
        "--graph",
        str(tmp_path / "missing"),
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == b""
    graph = _publish(tmp_path, [_entity("A")], [_calls("B", "A", rid="ba")])
    bad_snap = _run(
        sys.executable,
        str(QUERY),
        "impact",
        "A",
        "--graph",
        str(graph),
        "--snapshot",
        "..",
        check=False,
    )
    assert bad_snap.returncode == 2
    assert bad_snap.stdout == b""
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    unlocked = _run(
        sys.executable,
        str(QUERY),
        "impact",
        "A",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        check=False,
    )
    assert unlocked.returncode == 2
    assert unlocked.stdout == b""
    assert b"publication lock is missing" in unlocked.stderr
    rels_path = None
    for candidate in (graph / "current").resolve().parent.rglob("relationships.parquet"):
        rels_path = candidate
        break
    assert rels_path is not None
    # Recreate a locked graph with a malformed non-call row via a fresh publish.
    bad = _calls("B", "A", rid="ba")
    bad_uses = _rel("X", "Y", "uses_type", rid="u1")
    bad_uses["id"] = None
    bad_graph = tmp_path / "bad"
    # Direct producer failure is enough for CLI exit 2: publish requires valid parquet.
    # Use a published graph then monkeypatch through CLI runner.
    from typer.testing import CliRunner
    from graphrag_code.graph_query import app
    import graphrag_code.graph_query as gq

    def boom(*_a, **_k):
        raise ValueError("invalid source=None")

    gq_app_result = None
    runner = CliRunner()
    original = gq.dumps_impact_json
    try:
        import graphrag_code.byog_graph as byog

        orig = byog.compute_transitive_call_impact

        def raising(rels, root_title):
            raise ValueError("relationship at row 0 has invalid id=None")

        # Patch on the module used by ByogGraph.impact
        byog.compute_transitive_call_impact = raising  # type: ignore[assignment]
        invoked = runner.invoke(app, ["impact", "A", "--graph", str(graph)])
        gq_app_result = invoked
    finally:
        byog.compute_transitive_call_impact = orig  # type: ignore[assignment]
        gq.dumps_impact_json = original
    assert gq_app_result.exit_code == 2
    assert gq_app_result.stdout == ""
    assert "invalid id" in gq_app_result.stderr


def test_current_and_historical_snapshot_reads_do_not_mutate(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_entity("demo:old")]),
        pd.DataFrame([_calls("demo:caller", "demo:old", rid="rel:old")]),
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
        pd.DataFrame([_entity("demo:new")]),
        pd.DataFrame([_calls("demo:fresh", "demo:new", rid="rel:new")]),
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
        "impact",
        "demo:new",
        "--graph",
        str(graph),
        "--snapshot",
        "current",
    )
    assert cur.stdout == b"demo:fresh\n"
    hist = _run(
        sys.executable,
        str(CLI),
        "impact",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
        "--json",
    )
    assert hist.stdout == b'[\n  "demo:caller"\n]\n'
    assert b"demo:fresh" not in hist.stdout
    assert (graph / "current").read_text(encoding="utf-8").strip() == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert not list(graph.glob(".staging-*"))
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


def _impact_json_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import sys as sys_mod

    from graphrag_code import graph_query

    orig_dumps = graph_query.dumps_impact_json

    def wrap_dumps(result):
        payload = orig_dumps(result)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
        return payload

    graph_query.dumps_impact_json = wrap_dumps

    class HoldingStdout:
        encoding = "utf-8"
        errors = "strict"
        closed = False

        def __init__(self, inner):
            self._inner = inner
            self._wrote = False

        def write(self, data):
            written = self._inner.write(data)
            if isinstance(data, str) and data.startswith("["):
                self._wrote = True
            return written

        def flush(self):
            if self._wrote:
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
            ["impact", "A", "--graph", graph, "--json"],
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


def test_publisher_waits_through_producer_stdout_write_and_flush(tmp_path: Path):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="ba")],
    )
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_impact_json_hold, args=(str(graph), held, resume, q))
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


def test_one_load_resolve_producer_serialize_no_nested_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _publish(
        tmp_path,
        [_entity("A"), _entity("B")],
        [_calls("B", "A", rid="ba")],
    )
    g = ByogGraph(graph)
    for name in (
        "subgraph",
        "callers",
        "callees",
        "type_closure",
        "components",
        "strong_components",
        "condensation",
        "dependency_order",
        "degree_ranking",
        "shortest_path",
    ):
        monkeypatch.setattr(
            g,
            name,
            lambda *a, _n=name, **k: (_ for _ in ()).throw(
                AssertionError(f"nested public query {_n}")
            ),
        )
    resolves: list[str] = []
    orig_resolve = g.resolve

    def counted_resolve(query):
        resolves.append(query)
        return orig_resolve(query)

    monkeypatch.setattr(g, "resolve", counted_resolve)
    producer_calls = 0
    orig = compute_transitive_call_impact

    def counted_producer(rels, root_title):
        nonlocal producer_calls
        producer_calls += 1
        return orig(rels, root_title)

    import graphrag_code.byog_graph as byog
    import graphrag_code.graph_query as gq
    from typer.testing import CliRunner
    import graphrag_code.snapshot_read as snapshot_read

    monkeypatch.setattr(byog, "compute_transitive_call_impact", counted_producer)
    result = g.impact("A")
    assert result == ["B"]
    assert resolves == ["A"]
    assert producer_calls == 1
    dumps_impact_json(result)
    format_impact_human(result)
    assert producer_calls == 1

    loads = 0
    orig_load = snapshot_read.RetainedSnapshotScope.load_graph

    def counted_load(self):
        nonlocal loads
        loads += 1
        return orig_load(self)

    monkeypatch.setattr(snapshot_read.RetainedSnapshotScope, "load_graph", counted_load)
    serializer_calls = 0
    orig_dumps = dumps_impact_json

    def counted_dumps(mapping):
        nonlocal serializer_calls
        serializer_calls += 1
        return orig_dumps(mapping)

    monkeypatch.setattr(gq, "dumps_impact_json", counted_dumps)
    producer_calls = 0
    invoked = CliRunner().invoke(
        gq.app, ["impact", "A", "--graph", str(graph), "--json"]
    )
    assert invoked.exit_code == 0, invoked.stderr
    assert loads == 1
    assert serializer_calls == 1
    assert producer_calls == 1
    assert invoked.stdout == "[\n  \"B\"\n]\n"
    assert not list(tmp_path.glob("*.dot"))


def test_mcp_remains_seventeen_tools_unchanged_envelope_and_one_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from anyio import run as anyio_run
    from mcp import Client

    from graphrag_code.mcp_server import build_mcp_server, build_session
    import graphrag_code.byog_graph as byog

    entities = [_entity("A")] + [_entity(f"C{i}") for i in range(3)]
    rels = [_calls(f"C{i}", "A", rid=f"r{i}") for i in range(3)]
    graph = _publish(tmp_path, entities, rels)
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    params = list(inspect.signature(GraphMcpSession.impact).parameters)
    assert params == ["self", "symbol", "max_items", "snapshot"]
    expected = ByogGraph(graph).impact("A")
    assert expected == ["C0", "C1", "C2"]
    payload = session.impact("A")
    assert payload["tool"] == "impact"
    assert payload["data"] == expected
    assert payload["truncated"] is False
    assert payload["total"] == 3
    assert payload["returned"] == 3
    assert payload["limits"]["max_items"] == DEFAULT_MAX_ITEMS
    truncated = session.impact("A", max_items=1)
    assert truncated["data"] == ["C0"]
    assert truncated["truncated"] is True
    assert truncated["total"] == 3
    assert truncated["returned"] == 1
    producer_calls = 0
    orig = byog.compute_transitive_call_impact

    def counted(rels, root_title):
        nonlocal producer_calls
        producer_calls += 1
        return orig(rels, root_title)

    monkeypatch.setattr(byog, "compute_transitive_call_impact", counted)
    session.impact("A")
    assert producer_calls == 1
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
    assert len(TOOL_NAMES) == 17
    assert not (graph / ".publish.lock").is_symlink()

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            tool = next(item for item in tools if item.name == "impact")
            props = tool.input_schema.get("properties") or {}
            assert list(props) == ["symbol", "max_items", "snapshot"]
            assert "dot" not in props
            assert "format" not in props
            result = await client.call_tool("impact", {"symbol": "A"})
            body = result.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["tool"] == "impact"
            assert body["data"] == expected
            dumped = json.dumps(body)
            assert "digraph" not in dumped
            assert "--dot" not in dumped

    anyio_run(_body)
