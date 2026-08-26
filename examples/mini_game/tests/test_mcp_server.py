"""Read-only MCP stdio server for one BYOG graph.

Disposable graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_mcp_server.py -q
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import stat
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import anyio
import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))

from graphrag_code import index_c as pkg_index_c  # type: ignore
from graphrag_code import index_python as pkg_index_python  # type: ignore
from graphrag_code.byog_graph import (  # type: ignore
    DEFAULT_COMPONENTS_MAX_COMPONENTS,
    DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
    DEFAULT_SUBGRAPH_MAX_DEPTH,
    DEFAULT_SUBGRAPH_MAX_EDGES,
    DEFAULT_SUBGRAPH_MAX_NODES,
    HARD_MAX_COMPONENT_NODES,
    HARD_MAX_COMPONENTS,
    HARD_MAX_SUBGRAPH_DEPTH,
    HARD_MAX_SUBGRAPH_EDGES,
    HARD_MAX_SUBGRAPH_NODES,
    ByogGraph,
    compute_weakly_connected_components,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import (  # type: ignore
    HARD_MAX_ENVELOPE_BYTES,
    TOOL_NAMES,
    GraphMcpError,
    GraphMcpSession,
    build_mcp_server,
    build_session,
    preflight_graph,
    resolve_graph_root,
)
from graphrag_code.persisted_graph_doctor import audit_graph_root, audit_to_json  # type: ignore

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client


def _invoke(fn, **kwargs):
    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            fn(**kwargs)
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return SimpleNamespace(
        exit_code=code,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
        output=stdout.getvalue() + stderr.getvalue(),
    )


def _write_py(pkg: Path) -> Path:
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / "demo.py"
    path.write_text(
        "def add(left, right):\n"
        "    return left + right\n"
        "\n"
        "def caller():\n"
        "    return add(1, 2)\n"
    )
    return path


def _write_c(pkg: Path) -> Path:
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / "demo.c"
    path.write_text(
        "int add(int left, int right) { return left + right; }\n"
        "int caller(void) { return add(1, 2); }\n"
    )
    return path


def _index_py(pkg: Path, graph: Path):
    return _invoke(
        pkg_index_python.main,
        package=pkg,
        graph=graph,
        keep_snapshots=5,
        use_advanced=False,
        reuse_unchanged=False,
    )


def _index_c(pkg: Path, graph: Path):
    return _invoke(
        pkg_index_c.main,
        package=pkg,
        graph=graph,
        keep_snapshots=5,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
        reuse_unchanged=False,
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _graph_files(graph: Path) -> list[Path]:
    files = [path for path in graph.iterdir() if path.is_file()]
    snaps = graph / "snapshots"
    if snaps.is_dir():
        files.extend(sorted(snaps.rglob("*")))
    return files


def _payload_stats(graph: Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for path in _graph_files(graph):
        if not path.is_file() or path.is_symlink():
            continue
        info = path.lstat()
        out[path.relative_to(graph).as_posix()] = (
            info.st_size,
            info.st_mtime_ns,
            stat.S_IMODE(info.st_mode),
        )
    return out


def _payload_hashes(graph: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in _graph_files(graph):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(graph).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _py_graph(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    result = _index_py(pkg, graph)
    assert result.exit_code == 0, result.output
    return graph


def _c_graph(tmp_path: Path) -> Path:
    pkg = tmp_path / "cpkg"
    graph = tmp_path / "cgraph"
    _write_c(pkg)
    result = _index_c(pkg, graph)
    assert result.exit_code == 0, result.output
    return graph


def _session(graph: Path, indexer: str = "auto") -> GraphMcpSession:
    return build_session(graph, indexer)


def _run(fn):
    return anyio.run(fn)


def _payload(result) -> dict:
    assert result.is_error is False, getattr(result, "content", result)
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    assert isinstance(data, dict), data
    return data


def test_python_graph_startup_and_initialize(tmp_path: Path):
    graph = _py_graph(tmp_path)
    session = _session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == set(TOOL_NAMES)
            status = _payload(await client.call_tool("graph_status"))
            assert status["ok"] is True
            assert status["snapshot"] == _current(graph)
            assert status["data"]["indexer"] == "python"

    _run(_body)


def test_c_graph_startup_auto_indexer(tmp_path: Path):
    graph = _c_graph(tmp_path)
    session = _session(graph, "auto")
    assert session.resolved_indexer == "c"
    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            status = _payload(await client.call_tool("graph_status"))
            assert status["data"]["indexer"] == "c"
            assert status["data"]["indexer_configured"] == "auto"

    _run(_body)


def test_tools_list_is_exactly_documented(tmp_path: Path):
    graph = _py_graph(tmp_path)
    server = build_mcp_server(_session(graph))

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert len(names) == len(set(names)) == len(TOOL_NAMES) == 13
            assert names[names.index("neighbors") + 1] == "subgraph"
            assert names[names.index("subgraph") + 1] == "components"
            assert "snapshot_activate" not in names
            for tool in tools:
                assert tool.input_schema["additionalProperties"] is False
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.idempotent_hint is True
                assert tool.annotations.open_world_hint is False

    _run(_body)


def test_every_required_tool_via_sdk_client(tmp_path: Path):
    graph = _py_graph(tmp_path)
    view = ByogGraph(graph)
    symbol = view.symbol("add")["title"]
    server = build_mcp_server(_session(graph, "python"))

    async def _body():
        async with Client(server) as client:
            for name in TOOL_NAMES:
                if name in {
                    "graph_status",
                    "graph_doctor",
                    "snapshot_history",
                    "components",
                }:
                    result = await client.call_tool(name)
                elif name == "snapshot_diff":
                    result = await client.call_tool(name, {"from_snapshot": "current"})
                elif name == "type_closure":
                    result = await client.call_tool(name, {"symbol": symbol, "direction": "dependencies"})
                elif name == "context_pack":
                    result = await client.call_tool(name, {"symbol": symbol})
                else:
                    result = await client.call_tool(name, {"symbol": symbol})
                payload = _payload(result)
                assert payload["tool"] == name
                assert payload["ok"] is True
                assert payload["snapshot"] == _current(graph)

    _run(_body)


def test_graph_status_snapshot_and_counts(tmp_path: Path):
    graph = _py_graph(tmp_path)
    snap = _current(graph)
    manifest = json.loads((graph / "snapshots" / snap / "manifest.json").read_text())
    session = _session(graph, "python")
    status = session.graph_status()
    assert status["snapshot"] == snap
    assert status["data"]["counts"] == manifest["counts"]
    assert status["data"]["files"] == manifest["files"]
    assert status["data"]["schema_version"] == manifest["schema_version"]
    assert status["data"]["index_input_present"] is True
    assert status["data"]["reuse_supported"] is True


def test_graph_doctor_matches_package_doctor(tmp_path: Path):
    graph = _py_graph(tmp_path)
    session = _session(graph, "python")
    via_mcp = session.graph_doctor()
    report = audit_graph_root(graph, indexer="python")
    expected = json.loads(audit_to_json(report))
    assert via_mcp["data"] == expected
    assert via_mcp["ok"] is True


def test_query_and_graph_walks_match_byog_graph(tmp_path: Path):
    graph = _py_graph(tmp_path)
    view = ByogGraph(graph)
    symbol = view.symbol("add")["title"]
    session = _session(graph, "python")
    assert session.query_symbol("add")["data"]["title"] == view.symbol("add")["title"]
    assert session.callers(symbol)["data"] == view.callers(symbol)
    assert session.callees(symbol)["data"] == view.callees(symbol)
    assert session.neighbors(symbol)["data"] == view.neighbors(symbol)
    assert session.subgraph(symbol)["data"] == json.loads(
        json.dumps(view.subgraph(symbol), allow_nan=False, default=str)
    )
    assert session.components()["data"] == json.loads(
        json.dumps(view.components(), allow_nan=False, default=str)
    )
    assert session.impact(symbol)["data"] == view.impact(symbol)


def test_type_closure_bounds_and_invalid_direction(tmp_path: Path):
    graph = _py_graph(tmp_path)
    view = ByogGraph(graph)
    symbol = view.symbol("add")["title"]
    session = _session(graph, "python")
    ok = session.type_closure(symbol, direction="dependencies", max_depth=2)
    assert ok["data"]["direction"] == "dependencies"
    assert ok["data"]["max_depth"] == 2
    with pytest.raises(GraphMcpError, match="direction"):
        session.type_closure(symbol, direction="sideways")
    with pytest.raises(GraphMcpError, match="max_depth"):
        session.type_closure(symbol, max_depth=True)
    with pytest.raises(GraphMcpError, match="max_nodes"):
        session.type_closure(symbol, max_nodes=-1)
    with pytest.raises(GraphMcpError, match="max_edges"):
        session.type_closure(symbol, max_edges=10_000)


def test_context_pack_structured_and_text_bounds(tmp_path: Path, capsys):
    graph = _py_graph(tmp_path)
    view = ByogGraph(graph)
    symbol = view.symbol("add")["title"]
    session = _session(graph, "python")
    pack = session.context_pack(symbol, max_text_chars=12)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert pack["data"]["symbol"] == symbol
    assert "entity" in pack["data"]
    assert pack["data"]["truncation"]["max_text_chars"] == 12
    with pytest.raises(GraphMcpError, match="max_text_chars"):
        session.context_pack(symbol, max_text_chars=-3)
    with pytest.raises(GraphMcpError, match="max_text_chars"):
        session.context_pack(symbol, max_text_chars=0)
    with pytest.raises(GraphMcpError, match="type_depth"):
        session.context_pack(symbol, type_depth=0)


def test_max_items_truncation_is_deterministic(tmp_path: Path):
    ents = pd.DataFrame(
        [
            {"id": "ent:root", "title": "mod:root", "type": "function", "source_file": "m.py"},
            *[
                {
                    "id": f"ent:{i:03d}",
                    "title": f"mod:fn{i:03d}",
                    "type": "function",
                    "source_file": "m.py",
                }
                for i in range(8)
            ],
        ]
    )
    rels = pd.DataFrame(
        [
            {
                "id": f"rel:{i:03d}",
                "source": f"mod:fn{i:03d}",
                "target": "mod:root",
                "type": "calls",
            }
            for i in range(8)
        ]
    )
    tus = pd.DataFrame(
        [{"id": "tu:1", "title": "m.py", "source_file": "m.py", "entity_id": "ent:root"}]
    )
    graph = tmp_path / "many"
    publish_byog_snapshot(ents, rels, tus, graph, settings_text="mcp: true\n", keep_last=1)
    session = _session(graph, "python")
    first = session.callers("mod:root", max_items=3)
    second = session.callers("mod:root", max_items=3)
    assert first["data"] == second["data"] == sorted(f"mod:fn{i:03d}" for i in range(8))[:3]
    assert first["total"] == 8
    assert first["returned"] == 3
    assert first["truncated"] is True


def test_invalid_and_ambiguous_startup_exit_2(tmp_path: Path):
    missing = tmp_path / "missing"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.mcp_server",
            "--graph",
            str(missing),
            "--indexer",
            "python",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "graphrag-code mcp:" in proc.stderr
    assert "Traceback" not in proc.stderr

    unlocked = _py_graph(tmp_path / "unlocked")
    lock = unlocked / ".publish.lock"
    lock.unlink()
    before_stats = _payload_stats(unlocked)
    before_hashes = _payload_hashes(unlocked)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.mcp_server",
            "--graph",
            str(unlocked),
            "--indexer",
            "python",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "publication lock is missing" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not lock.exists()
    assert _payload_stats(unlocked) == before_stats
    assert _payload_hashes(unlocked) == before_hashes

    graph = _py_graph(tmp_path)
    snap = graph / "snapshots" / _current(graph)
    manifest = json.loads((snap / "manifest.json").read_text())
    manifest["counts"]["entities"] = 0
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with pytest.raises(SystemExit) as exc:
        preflight_graph(graph, "python")
    assert exc.value.code == 2

    mixed = tmp_path / "mixed"
    publish_byog_snapshot(
        pd.DataFrame(
            [
                {
                    "id": "ent:py",
                    "title": "mod:py",
                    "type": "function",
                    "source_file": "a.py",
                    "extractor": "tree-sitter-python",
                },
                {
                    "id": "ent:c",
                    "title": "mod:c",
                    "type": "function",
                    "source_file": "a.c",
                    "extractor": "tree-sitter-c",
                },
            ]
        ),
        pd.DataFrame(
            [{"id": "rel:1", "source": "mod:py", "target": "mod:c", "type": "contains"}]
        ),
        pd.DataFrame(
            [{"id": "tu:1", "title": "a.py", "source_file": "a.py", "entity_id": "ent:py"}]
        ),
        mixed,
        settings_text="mcp: true\n",
        keep_last=1,
    )
    with pytest.raises(SystemExit) as exc:
        preflight_graph(mixed, "auto")
    assert exc.value.code == 2


def test_unknown_and_invalid_arguments_are_tool_errors(tmp_path: Path):
    graph = _py_graph(tmp_path)
    server = build_mcp_server(_session(graph, "python"))

    async def _body():
        async with Client(server) as client:
            missing = await client.call_tool("query_symbol", {})
            assert missing.is_error is True
            extra = await client.call_tool(
                "query_symbol",
                {"symbol": "add", "graph": str(tmp_path / "other")},
            )
            assert extra.is_error is True
            unbounded = await client.call_tool(
                "context_pack",
                {"symbol": "add", "full_text": True},
            )
            assert unbounded.is_error is True
            bad_dir = await client.call_tool(
                "type_closure",
                {"symbol": "add", "direction": "sideways"},
            )
            assert bad_dir.is_error is True
            text = ""
            for item in bad_dir.content:
                text += getattr(item, "text", "") or ""
            assert "Traceback" not in text

    _run(_body)


def test_stdio_session_is_protocol_clean(tmp_path: Path):
    graph = _py_graph(tmp_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")

    async def _body():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "graphrag_code",
                "mcp",
                "--graph",
                str(graph),
                "--indexer",
                "python",
            ],
            env=env,
            cwd=str(tmp_path),
        )
        async with Client(stdio_client(params)) as client:
            status = _payload(await client.call_tool("graph_status"))
            assert status["ok"] is True
            assert status["graph"] == str(graph.resolve())

    _run(_body)


def test_oversized_tool_response_fails_closed(tmp_path: Path):
    description = "x" * (HARD_MAX_ENVELOPE_BYTES + 1)
    graph = tmp_path / "large"
    publish_byog_snapshot(
        pd.DataFrame(
            [
                {
                    "id": "ent:large",
                    "title": "m:large",
                    "type": "function",
                    "description": description,
                    "source_file": "m.py",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(
            columns=["id", "source", "target", "type"]
        ),
        pd.DataFrame(
            [{"id": "tu:1", "title": "m.py", "source_file": "m.py", "entity_id": "ent:large"}]
        ),
        graph,
        settings_text="mcp: true\n",
        keep_last=1,
    )
    with pytest.raises(GraphMcpError, match="response envelope exceeds hard limit"):
        _session(graph, "python").query_symbol("m:large")


def test_tools_cannot_redirect_graph(tmp_path: Path):
    graph = _py_graph(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    session = _session(graph, "python")
    import inspect

    assert "graph" not in inspect.signature(session.query_symbol).parameters
    with pytest.raises(TypeError):
        session.query_symbol("add", graph=str(other))  # type: ignore[call-arg]
    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            result = await client.call_tool(
                "callers",
                {"symbol": "add", "graph": str(other), "path": str(other / "x")},
            )
            assert result.is_error is True

    _run(_body)


def test_mcp_calls_do_not_mutate_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _py_graph(tmp_path)
    before_stats = _payload_stats(graph)
    before_hashes = _payload_hashes(graph)

    def boom(*_args, **_kwargs):
        raise AssertionError("extractor/compiler/publisher invoked from MCP")

    monkeypatch.setattr("graphrag_code.index_python.build_byog_for_package", boom)
    monkeypatch.setattr("graphrag_code.index_c.build_c_byog", boom)
    monkeypatch.setattr("graphrag_code.byog_graph.publish_byog_snapshot", boom)
    monkeypatch.setattr("graphrag_code.c_clang_ast_capture.capture_clang_ast_package", boom)

    session = _session(graph, "python")
    view = ByogGraph(graph)
    symbol = view.symbol("add")["title"]
    session.graph_status()
    session.graph_doctor()
    session.query_symbol(symbol)
    session.callers(symbol)
    session.callees(symbol)
    session.neighbors(symbol)
    session.subgraph(symbol)
    session.components()
    session.impact(symbol)
    session.type_closure(symbol)
    session.context_pack(symbol)
    assert _payload_stats(graph) == before_stats
    assert _payload_hashes(graph) == before_hashes


def test_one_call_stays_on_pinned_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = _py_graph(tmp_path)
    first = _current(graph)
    first_dir = graph / "snapshots" / first
    ents = pd.read_parquet(first_dir / "entities.parquet")
    rels = pd.read_parquet(first_dir / "relationships.parquet")
    tus = pd.read_parquet(first_dir / "text_units.parquet")
    second_dir = publish_byog_snapshot(
        ents,
        rels,
        tus,
        graph,
        settings_text="mcp: true\n",
        keep_last=5,
    )
    (graph / "current").write_text(first)
    import graphrag_code.snapshot_read as scope_mod

    real = scope_mod.resolve_snapshot
    seen = {"n": 0}

    def wrapped(graph_root, snapshot=None):
        seen["n"] += 1
        result = real(graph_root, snapshot)
        if seen["n"] == 1:
            (Path(graph_root) / "current").write_text(second_dir.name)
        return result

    monkeypatch.setattr(scope_mod, "resolve_snapshot", wrapped)
    session = GraphMcpSession(
        graph,
        configured_indexer="python",
        resolved_indexer="python",
        preflight={"indexer": "python", "indexer_resolution": {}},
    )
    payload = session.query_symbol("add")
    assert payload["snapshot"] == first
    assert _current(graph) == second_dir.name
    assert seen["n"] == 1


def _mcp_paused_query(graph: str, pinned, resume, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "src"))
    from contextlib import contextmanager
    import graphrag_code.snapshot_read as scope_mod
    from graphrag_code.mcp_server import GraphMcpSession

    orig = scope_mod.graph_read_lease

    @contextmanager
    def wrapped(root, *args, **kwargs):
        with orig(root, *args, **kwargs):
            pinned.set()
            if not resume.wait(timeout=20):
                q.put("timeout")
                return
            yield

    scope_mod.graph_read_lease = wrapped
    session = GraphMcpSession(
        Path(graph),
        configured_indexer="python",
        resolved_indexer="python",
        preflight={"indexer": "python", "indexer_resolution": {}},
    )
    payload = session.query_symbol("add")
    q.put(payload["snapshot"])


def test_mcp_call_blocks_publisher_until_release(tmp_path: Path):
    import multiprocessing

    graph = _py_graph(tmp_path)
    first = _current(graph)
    first_dir = graph / "snapshots" / first
    ctx = multiprocessing.get_context("spawn")
    pinned = ctx.Event()
    resume = ctx.Event()
    about = ctx.Event()
    got = ctx.Event()
    q = ctx.Queue()
    reader = ctx.Process(
        target=_mcp_paused_query,
        args=(str(graph), pinned, resume, q),
    )
    from test_reader_lease import _cleanup_processes, _publisher

    pub = ctx.Process(target=_publisher, args=(str(graph), "next", 1, about, got, q))
    try:
        reader.start()
        assert pinned.wait(timeout=20)
        pub.start()
        assert about.wait(timeout=20)
        assert not got.is_set()
        assert _current(graph) == first
        assert (first_dir / "entities.parquet").is_file()
        resume.set()
        reader.join(timeout=20)
        pub.join(timeout=20)
        assert not reader.is_alive() and not pub.is_alive()
        snaps = {q.get(timeout=5), q.get(timeout=5)}
        assert first in snaps
        assert _current(graph) != first
        assert not first_dir.exists()
    finally:
        _cleanup_processes(pub, reader, release=resume)


def test_installed_wheel_mcp_from_outside_checkout(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    wheel, _ = built_wheel_and_sdist
    env = install_wheel(wheel, tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    pkg = outside / "pkg"
    graph = outside / "graph"
    _write_py(pkg)
    indexed = subprocess.run(
        [
            "graphrag-code",
            "index-python",
            "--package",
            pkg.name,
            "--graph",
            graph.name,
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert indexed.returncode == 0, indexed.stderr

    async def _body():
        params = StdioServerParameters(
            command="graphrag-code",
            args=[
                "mcp",
                "--graph",
                graph.name,
                "--indexer",
                "python",
            ],
            env=env,
            cwd=str(outside),
        )
        async with Client(stdio_client(params)) as client:
            status = _payload(await client.call_tool("graph_status"))
            assert status["ok"] is True
            found = await client.call_tool("query_symbol", {"symbol": "add"})
            body = _payload(found)
            assert body["data"]["title"].endswith("add")

    _run(_body)
    loc = subprocess.run(
        [sys.executable, "-c", "import graphrag_code.mcp_server as m; print(m.__file__)"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert loc.returncode == 0, loc.stderr
    assert str(tmp_path / "site") in loc.stdout
    assert str(ROOT / "src") not in loc.stdout


def test_script_and_package_import_compat():
    scripts = str(ROOT / "scripts")
    if scripts in sys.path:
        sys.path.remove(scripts)
    sys.path.insert(0, scripts)
    import persisted_graph_doctor
    from graphrag_code import persisted_graph_doctor as packaged

    assert persisted_graph_doctor is packaged


def test_relative_graph_uses_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = _py_graph(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert resolve_graph_root(Path(graph.name)) == graph.resolve()


def _publish_walk(tmp_path: Path, *, marker: str = "cur") -> Path:
    ents = [
        {
            "id": f"ent:root:{marker}",
            "title": f"demo:{marker}",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
            "description": f"root-{marker}",
        },
        {
            "id": f"ent:mid:{marker}",
            "title": f"demo:{marker}_mid",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"ent:leaf:{marker}",
            "title": f"demo:{marker}_leaf",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"ent:user:{marker}",
            "title": f"demo:{marker}_user",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"ent:other:{marker}",
            "title": f"demo:{marker}_other",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
        },
    ]
    rels = [
        {
            "id": f"rel:calls:{marker}:root-mid",
            "source": f"demo:{marker}",
            "target": f"demo:{marker}_mid",
            "type": "calls",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"rel:calls:{marker}:mid-leaf",
            "source": f"demo:{marker}_mid",
            "target": f"demo:{marker}_leaf",
            "type": "calls",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"rel:uses:{marker}:root-leaf",
            "source": f"demo:{marker}",
            "target": f"demo:{marker}_leaf",
            "type": "uses_type",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"rel:calls:{marker}:user-root",
            "source": f"demo:{marker}_user",
            "target": f"demo:{marker}",
            "type": "calls",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"rel:contains:{marker}:file-root",
            "source": f"{marker}.py",
            "target": f"demo:{marker}",
            "type": "contains",
            "extractor": "tree-sitter-python",
        },
    ]
    tus = [
        {
            "id": f"tu:{marker}",
            "title": f"{marker}.py",
            "source_file": f"{marker}.py",
            "entity_id": f"ent:root:{marker}",
        }
    ]
    graph = tmp_path / f"byog_{marker}"
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"mcp: {marker}\n",
        keep_last=5,
    )
    return graph


def test_subgraph_mcp_schema_defaults_and_unknown_args(tmp_path: Path):
    graph = _publish_walk(tmp_path)
    session = _session(graph, "python")
    sig = inspect.signature(GraphMcpSession.subgraph)
    assert list(sig.parameters) == [
        "self",
        "symbol",
        "direction",
        "max_depth",
        "max_nodes",
        "max_edges",
        "edge_types",
        "snapshot",
    ]
    assert sig.parameters["direction"].default == "both"
    assert sig.parameters["max_depth"].default == DEFAULT_SUBGRAPH_MAX_DEPTH
    assert sig.parameters["max_nodes"].default == DEFAULT_SUBGRAPH_MAX_NODES
    assert sig.parameters["max_edges"].default == DEFAULT_SUBGRAPH_MAX_EDGES
    assert sig.parameters["edge_types"].default is None
    assert sig.parameters["snapshot"].default == "current"
    assert "graph" not in sig.parameters

    defaulted = session.subgraph("demo:cur")
    assert defaulted["tool"] == "subgraph"
    assert defaulted["ok"] is True
    assert defaulted["data"]["direction"] == "both"
    assert defaulted["data"]["max_depth"] == DEFAULT_SUBGRAPH_MAX_DEPTH
    assert defaulted["limits"]["direction"] == "both"
    assert defaulted["limits"]["max_depth"] == DEFAULT_SUBGRAPH_MAX_DEPTH
    assert defaulted["limits"]["max_nodes"] == DEFAULT_SUBGRAPH_MAX_NODES
    assert defaulted["limits"]["max_edges"] == DEFAULT_SUBGRAPH_MAX_EDGES
    assert defaulted["limits"]["edge_types"] is None
    assert defaulted["limits"]["max_envelope_bytes"] == HARD_MAX_ENVELOPE_BYTES

    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            sub = next(tool for tool in tools if tool.name == "subgraph")
            assert sub.annotations.read_only_hint is True
            assert sub.input_schema["additionalProperties"] is False
            props = sub.input_schema["properties"]
            assert props["direction"]["default"] == "both"
            assert props["max_depth"]["default"] == DEFAULT_SUBGRAPH_MAX_DEPTH
            extra = await client.call_tool(
                "subgraph",
                {"symbol": "demo:cur", "graph": str(tmp_path / "other")},
            )
            assert extra.is_error is True
            scalar = await client.call_tool(
                "subgraph",
                {"symbol": "demo:cur", "edge_types": "calls"},
            )
            assert scalar.is_error is True
            for invalid_args in (
                {"max_depth": True},
                {"max_depth": 1.0},
                {"max_nodes": True},
                {"max_edges": False},
                {"edge_types": ["calls", None]},
                {"edge_types": ["calls", 1]},
            ):
                invalid = await client.call_tool(
                    "subgraph",
                    {"symbol": "demo:cur", **invalid_args},
                )
                assert invalid.is_error is True, invalid_args

    _run(_body)


def test_subgraph_mcp_semantics_parity_snapshots_and_filters(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame(
            [
                {
                    "id": "ent:old",
                    "title": "demo:old",
                    "type": "function",
                    "source_file": "old.py",
                    "extractor": "tree-sitter-python",
                },
                {
                    "id": "ent:old_mid",
                    "title": "demo:old_mid",
                    "type": "function",
                    "source_file": "old.py",
                    "extractor": "tree-sitter-python",
                },
            ]
        ),
        pd.DataFrame(
            [
                {
                    "id": "rel:old",
                    "source": "demo:old",
                    "target": "demo:old_mid",
                    "type": "calls",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "id": "tu:old",
                    "title": "old.py",
                    "source_file": "old.py",
                    "entity_id": "ent:old",
                }
            ]
        ),
        graph,
        settings_text="mcp: old\n",
        keep_last=5,
    )
    newer = _publish_walk(tmp_path, marker="new")
    # Re-home the newer payload as a second snapshot of `graph`.
    newer_ents = pd.read_parquet(newer / "snapshots" / _current(newer) / "entities.parquet")
    newer_rels = pd.read_parquet(
        newer / "snapshots" / _current(newer) / "relationships.parquet"
    )
    newer_tus = pd.read_parquet(newer / "snapshots" / _current(newer) / "text_units.parquet")
    current = publish_byog_snapshot(
        newer_ents,
        newer_rels,
        newer_tus,
        graph,
        settings_text="mcp: new\n",
        keep_last=5,
    )
    assert _current(graph) == current.name
    before = _payload_hashes(graph)
    session = _session(graph, "python")
    view = ByogGraph(graph)

    outgoing = session.subgraph(
        "demo:new", direction="outgoing", max_depth=2, max_nodes=20, max_edges=20
    )
    incoming = session.subgraph(
        "demo:new", direction="incoming", max_depth=1, max_nodes=20, max_edges=20
    )
    both = session.subgraph(
        "demo:new", direction="both", max_depth=1, max_nodes=20, max_edges=20
    )
    filtered = session.subgraph(
        "demo:new",
        direction="outgoing",
        max_depth=2,
        edge_types=["uses_type", "calls", "calls"],
    )
    assert filtered["limits"]["edge_types"] == ["calls", "uses_type"]
    assert filtered["data"]["edge_types"] == ["calls", "uses_type"]
    assert {edge["type"] for edge in filtered["data"]["edges"]} <= {"calls", "uses_type"}

    titles_out = {node["title"] for node in outgoing["data"]["nodes"]}
    assert "demo:new" in titles_out and "demo:new_mid" in titles_out
    assert "demo:new_user" not in titles_out
    titles_in = {node["title"] for node in incoming["data"]["nodes"]}
    assert "demo:new_user" in titles_in
    assert all(
        edge["source"] == "demo:new_user" or edge["target"] == "demo:new"
        or edge["source"] == "demo:new"
        for edge in incoming["data"]["edges"]
    )

    capped = session.subgraph(
        "demo:new", direction="outgoing", max_depth=2, max_nodes=2, max_edges=10
    )
    assert capped["data"]["n_nodes_total"] > capped["data"]["n_nodes_returned"]
    assert capped["truncated"] is True
    assert capped["total"] == (
        capped["data"]["n_nodes_total"] + capped["data"]["n_edges_total"]
    )
    assert capped["returned"] == (
        capped["data"]["n_nodes_returned"] + capped["data"]["n_edges_returned"]
    )
    returned_titles = {node["title"] for node in capped["data"]["nodes"]}
    assert "demo:new" in returned_titles
    assert all(
        edge["source"] in returned_titles and edge["target"] in returned_titles
        for edge in capped["data"]["edges"]
    )

    missing = session.subgraph("does-not-exist")
    assert missing["data"]["resolved"] is False
    assert missing["data"]["nodes"] == []
    assert missing["total"] == 0
    amb = session.subgraph("demo:")
    assert amb["data"]["resolved"] is False

    direct = view.subgraph(
        "demo:new", direction="outgoing", max_depth=2, max_nodes=20, max_edges=20
    )
    assert outgoing["data"] == json.loads(json.dumps(direct, allow_nan=False, default=str))
    again = session.subgraph(
        "demo:new", direction="outgoing", max_depth=2, max_nodes=20, max_edges=20
    )
    assert again == outgoing

    historical = session.subgraph("demo:old", snapshot=older.name)
    assert historical["snapshot"] == older.name
    assert historical["data"]["root"] == "demo:old"
    assert "demo:new" not in json.dumps(historical)
    assert session.subgraph("demo:new", snapshot="current")["data"]["root"] == "demo:new"
    assert _current(graph) == current.name
    assert _payload_hashes(graph) == before
    assert both["data"]["direction"] == "both"

    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            payload = _payload(
                await client.call_tool(
                    "subgraph",
                    {
                        "symbol": "demo:old",
                        "snapshot": older.name,
                        "direction": "outgoing",
                    },
                )
            )
            assert payload["tool"] == "subgraph"
            assert payload["snapshot"] == older.name
            assert payload["data"]["root"] == "demo:old"

    _run(_body)


def test_subgraph_mcp_validation_malformed_and_envelope(tmp_path: Path):
    graph = _publish_walk(tmp_path)
    session = _session(graph, "python")
    with pytest.raises(GraphMcpError, match="direction"):
        session.subgraph("demo:cur", direction="sideways")
    with pytest.raises(GraphMcpError, match="max_depth"):
        session.subgraph("demo:cur", max_depth=True)
    with pytest.raises(GraphMcpError, match="max_depth"):
        session.subgraph("demo:cur", max_depth=1.5)
    with pytest.raises(GraphMcpError, match="max_depth"):
        session.subgraph("demo:cur", max_depth=float("nan"))
    with pytest.raises(GraphMcpError, match="max_nodes"):
        session.subgraph("demo:cur", max_nodes=math.inf)
    with pytest.raises(GraphMcpError, match="max_nodes"):
        session.subgraph("demo:cur", max_nodes=0)
    with pytest.raises(GraphMcpError, match="max_nodes"):
        session.subgraph("demo:cur", max_nodes=-1)
    with pytest.raises(GraphMcpError, match="max_depth"):
        session.subgraph("demo:cur", max_depth=HARD_MAX_SUBGRAPH_DEPTH + 1)
    with pytest.raises(GraphMcpError, match="max_edges"):
        session.subgraph("demo:cur", max_edges=HARD_MAX_SUBGRAPH_EDGES + 1)
    with pytest.raises(GraphMcpError, match="array of strings"):
        session.subgraph("demo:cur", edge_types="calls")
    with pytest.raises(GraphMcpError, match="invalid edge-type filter"):
        session.subgraph("demo:cur", edge_types=[""])
    with pytest.raises(GraphMcpError, match="invalid edge-type filter"):
        session.subgraph("demo:cur", edge_types=[" calls"])
    with pytest.raises(GraphMcpError, match="snapshot"):
        session.subgraph("demo:cur", snapshot="..")

    empty_ok = session.subgraph("demo:cur", edge_types=[])
    assert empty_ok["limits"]["edge_types"] is None
    assert empty_ok["data"]["edge_types"] is None

    bad = tmp_path / "bad"
    publish_byog_snapshot(
        pd.DataFrame(
            [
                {
                    "id": "ent:a",
                    "title": "A",
                    "type": "function",
                    "source_file": "a.py",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "id": "rel:bad",
                    "source": "A",
                    "target": None,
                    "type": "calls",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(
            [{"id": "tu:a", "title": "a.py", "source_file": "a.py", "entity_id": "ent:a"}]
        ),
        bad,
        settings_text="mcp: bad\n",
        keep_last=1,
    )
    with pytest.raises(GraphMcpError, match="invalid target"):
        _session(bad, "python").subgraph("A")

    huge = tmp_path / "huge"
    description = "x" * (HARD_MAX_ENVELOPE_BYTES + 1)
    publish_byog_snapshot(
        pd.DataFrame(
            [
                {
                    "id": "ent:huge",
                    "title": "m:huge",
                    "type": "function",
                    "description": description,
                    "source_file": "m.py",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(columns=["id", "source", "target", "type"]),
        pd.DataFrame(
            [{"id": "tu:1", "title": "m.py", "source_file": "m.py", "entity_id": "ent:huge"}]
        ),
        huge,
        settings_text="mcp: huge\n",
        keep_last=1,
    )
    with pytest.raises(GraphMcpError, match="response envelope exceeds hard limit"):
        _session(huge, "python").subgraph("m:huge")

    many = tmp_path / "many"
    n = 40
    ents = [
        {
            "id": "ent:root",
            "title": "mod:root",
            "type": "function",
            "source_file": "m.py",
            "extractor": "tree-sitter-python",
            "description": "ok",
        }
    ] + [
        {
            "id": f"ent:{i:03d}",
            "title": f"mod:n{i:03d}",
            "type": "function",
            "source_file": "m.py",
            "extractor": "tree-sitter-python",
        }
        for i in range(n)
    ]
    rels = [
        {
            "id": f"rel:{i:03d}",
            "source": "mod:root",
            "target": f"mod:n{i:03d}",
            "type": "calls",
            "extractor": "tree-sitter-python",
        }
        for i in range(n)
    ]
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(
            [{"id": "tu:1", "title": "m.py", "source_file": "m.py", "entity_id": "ent:root"}]
        ),
        many,
        settings_text="mcp: many\n",
        keep_last=1,
    )
    large = _session(many, "python").subgraph(
        "mod:root", direction="outgoing", max_depth=1, max_nodes=50, max_edges=50
    )
    assert large["ok"] is True
    assert large["data"]["n_nodes_total"] == n + 1
    assert large["truncated"] is False


def _mcp_paused_subgraph(graph: str, pinned, resume, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "src"))
    from contextlib import contextmanager
    import graphrag_code.snapshot_read as scope_mod
    from graphrag_code.mcp_server import GraphMcpSession

    orig = scope_mod.graph_read_lease

    @contextmanager
    def wrapped(root, *args, **kwargs):
        with orig(root, *args, **kwargs):
            pinned.set()
            if not resume.wait(timeout=20):
                q.put("timeout")
                return
            yield

    scope_mod.graph_read_lease = wrapped
    session = GraphMcpSession(
        Path(graph),
        configured_indexer="python",
        resolved_indexer="python",
        preflight={"indexer": "python", "indexer_resolution": {}},
    )
    payload = session.subgraph("demo:cur")
    q.put(payload["snapshot"])


def test_subgraph_mcp_publisher_wait_and_no_nested_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import multiprocessing

    graph = _publish_walk(tmp_path)
    first = _current(graph)
    first_dir = graph / "snapshots" / first
    before = _payload_hashes(graph)
    session = _session(graph, "python")

    def boom(*_args, **_kwargs):
        raise AssertionError("nested public query or CLI invoked from MCP subgraph")

    monkeypatch.setattr("graphrag_code.graph_query.subgraph", boom)
    monkeypatch.setattr("graphrag_code.graph_query.cli_subgraph", boom)
    monkeypatch.setattr("graphrag_code.cli.subgraph", boom)
    payload = session.subgraph("demo:cur", max_depth=1)
    assert payload["ok"] is True
    assert _payload_hashes(graph) == before

    ctx = multiprocessing.get_context("spawn")
    pinned = ctx.Event()
    resume = ctx.Event()
    about = ctx.Event()
    got = ctx.Event()
    q = ctx.Queue()
    reader = ctx.Process(
        target=_mcp_paused_subgraph, args=(str(graph), pinned, resume, q)
    )
    from test_reader_lease import _cleanup_processes, _publisher

    pub = ctx.Process(target=_publisher, args=(str(graph), "next", 1, about, got, q))
    try:
        reader.start()
        assert pinned.wait(timeout=20)
        pub.start()
        assert about.wait(timeout=20)
        assert not got.is_set()
        assert _current(graph) == first
        assert (first_dir / "entities.parquet").is_file()
        resume.set()
        reader.join(timeout=20)
        pub.join(timeout=20)
        assert not reader.is_alive() and not pub.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(pub, reader, release=resume)


def _component_entity(title: str, **extra) -> dict:
    row = {
        "id": extra.pop("id", f"ent:{title}"),
        "title": title,
        "type": extra.pop("type", "function"),
        "source_file": extra.pop("source_file", "a.py"),
        "extractor": extra.pop("extractor", "tree-sitter-python"),
    }
    row.update(extra)
    return row


def _component_rel(source: str, target: str, rel_type: str, *, rid: str | None = None) -> dict:
    return {
        "id": rid or f"rel:{rel_type}:{source}->{target}",
        "source": source,
        "target": target,
        "type": rel_type,
        "extractor": "tree-sitter-python",
    }


def _publish_components(tmp_path: Path, ents: list, rels: list, *, name: str = "comp") -> Path:
    graph = tmp_path / name
    tus = [
        {
            "id": f"tu:{row['title']}",
            "title": row.get("source_file", "a.py"),
            "source_file": row.get("source_file", "a.py"),
            "entity_id": row["id"],
        }
        for row in ents
    ]
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels) if rels else pd.DataFrame(columns=["id", "source", "target", "type"]),
        pd.DataFrame(tus) if tus else pd.DataFrame(columns=["id", "title", "source_file"]),
        graph,
        settings_text=f"mcp: {name}\n",
        keep_last=5,
    )
    return graph


def _assert_components_envelope(payload: dict, data: dict) -> None:
    ready = json.loads(json.dumps(data, allow_nan=False, default=str))
    returned_nodes = sum(int(item["n_nodes_returned"]) for item in ready["components"])
    assert payload["tool"] == "components"
    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    assert payload["data"] == ready
    assert payload["total"] == ready["n_components_total"] + ready["n_nodes_total"]
    assert payload["returned"] == ready["n_components_returned"] + returned_nodes
    assert payload["truncated"] is bool(
        ready["components_truncated"] or ready["nodes_truncated"]
    )
    assert payload["limits"]["max_envelope_bytes"] == HARD_MAX_ENVELOPE_BYTES


def test_components_mcp_schema_defaults_and_unknown_args(tmp_path: Path):
    graph = _publish_components(
        tmp_path,
        [_component_entity("A"), _component_entity("B")],
        [_component_rel("A", "B", "calls")],
    )
    session = _session(graph, "python")
    sig = inspect.signature(GraphMcpSession.components)
    assert list(sig.parameters) == [
        "self",
        "max_components",
        "max_nodes_per_component",
        "edge_types",
        "snapshot",
    ]
    assert sig.parameters["max_components"].default == DEFAULT_COMPONENTS_MAX_COMPONENTS
    assert (
        sig.parameters["max_nodes_per_component"].default
        == DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT
    )
    assert sig.parameters["edge_types"].default is None
    assert sig.parameters["snapshot"].default == "current"
    assert "graph" not in sig.parameters
    assert "format" not in sig.parameters
    assert "dot" not in sig.parameters

    defaulted = session.components()
    view = ByogGraph(graph).components()
    _assert_components_envelope(defaulted, view)
    assert defaulted["limits"]["max_components"] == DEFAULT_COMPONENTS_MAX_COMPONENTS
    assert (
        defaulted["limits"]["max_nodes_per_component"]
        == DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT
    )
    assert defaulted["limits"]["edge_types"] is None

    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            names = [tool.name for tool in tools]
            assert names == list(TOOL_NAMES)
            assert names[names.index("subgraph") + 1] == "components"
            tool = next(item for item in tools if item.name == "components")
            assert tool.annotations.read_only_hint is True
            assert tool.input_schema["additionalProperties"] is False
            props = tool.input_schema["properties"]
            assert list(props) == [
                "max_components",
                "max_nodes_per_component",
                "edge_types",
                "snapshot",
            ]
            assert props["max_components"]["type"] == "integer"
            assert props["max_nodes_per_component"]["type"] == "integer"
            assert props["max_components"]["default"] == DEFAULT_COMPONENTS_MAX_COMPONENTS
            assert (
                props["max_nodes_per_component"]["default"]
                == DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT
            )
            assert props["snapshot"]["default"] == "current"
            extra = await client.call_tool("components", {"graph": str(tmp_path / "other")})
            assert extra.is_error is True
            unknown = await client.call_tool("components", {"format": "json"})
            assert unknown.is_error is True
            dot = await client.call_tool("components", {"dot": True})
            assert dot.is_error is True
            symbol = await client.call_tool("components", {"symbol": "A"})
            assert symbol.is_error is True
            scalar = await client.call_tool("components", {"edge_types": "calls"})
            assert scalar.is_error is True
            for invalid_args in (
                {"max_components": True},
                {"max_components": 1.0},
                {"max_nodes_per_component": False},
                {"max_nodes_per_component": 1.5},
                {"edge_types": ["calls", None]},
                {"edge_types": ["calls", 1]},
            ):
                invalid = await client.call_tool("components", invalid_args)
                assert invalid.is_error is True, invalid_args

    _run(_body)


def test_components_mcp_semantics_parity_snapshots_and_filters(tmp_path: Path):
    graph = tmp_path / "g"
    older = publish_byog_snapshot(
        pd.DataFrame([_component_entity("demo:old", source_file="old.py")]),
        pd.DataFrame([_component_rel("demo:old", "demo:old", "calls", rid="rel:old")]),
        pd.DataFrame(
            [
                {
                    "id": "tu:old",
                    "title": "old.py",
                    "source_file": "old.py",
                    "entity_id": "ent:demo:old",
                }
            ]
        ),
        graph,
        settings_text="mcp: old\n",
        keep_last=5,
    )
    newer_ents = [
        _component_entity("A"),
        _component_entity("B"),
        _component_entity("C"),
        _component_entity("Isolated"),
        _component_entity("Zsmall"),
    ]
    newer_rels = [
        _component_rel("A", "B", "calls", rid="rel:ab"),
        _component_rel("B", "ghost", "calls", rid="rel:endpoint"),
        _component_rel("A", "A", "calls", rid="rel:self"),
        _component_rel("A", "B", "calls", rid="rel:parallel"),
        _component_rel("C", "Zsmall", "contains", rid="rel:contains"),
    ]
    current = publish_byog_snapshot(
        pd.DataFrame(newer_ents),
        pd.DataFrame(newer_rels),
        pd.DataFrame(
            [
                {
                    "id": f"tu:{row['title']}",
                    "title": "a.py",
                    "source_file": "a.py",
                    "entity_id": row["id"],
                }
                for row in newer_ents
            ]
        ),
        graph,
        settings_text="mcp: new\n",
        keep_last=5,
    )
    assert _current(graph) == current.name
    before = _payload_hashes(graph)
    session = _session(graph, "python")
    view = ByogGraph(graph)

    none = session.components()
    empty = session.components(edge_types=[])
    explicit_current = session.components(snapshot="current")
    _assert_components_envelope(none, view.components())
    assert empty["data"] == none["data"]
    assert empty["limits"]["edge_types"] is None
    assert explicit_current["data"] == none["data"]
    assert explicit_current["snapshot"] == current.name
    assert none["data"]["n_nodes_total"] == 6
    assert none["data"]["n_endpoint_only_nodes_total"] == 1
    reps = [item["representative"] for item in none["data"]["components"]]
    assert reps == ["A", "C", "Isolated"]
    big = none["data"]["components"][0]
    assert big["nodes"] == ["A", "B", "ghost"]
    assert big["n_edges_total"] == 4
    assert big["n_endpoint_only_nodes"] == 1
    iso = none["data"]["components"][2]
    assert iso["nodes"] == ["Isolated"]
    assert iso["n_edges_total"] == 0

    filtered = session.components(edge_types=["uses_type", "calls", "calls"])
    expected_filtered = view.components(edge_types=["uses_type", "calls", "calls"])
    assert filtered["limits"]["edge_types"] == ["calls", "uses_type"]
    assert filtered["data"] == json.loads(
        json.dumps(expected_filtered, allow_nan=False, default=str)
    )
    assert filtered["data"]["edge_types"] == ["calls", "uses_type"]

    capped = session.components(max_components=1, max_nodes_per_component=1)
    expected_capped = view.components(max_components=1, max_nodes_per_component=1)
    _assert_components_envelope(capped, expected_capped)
    assert capped["truncated"] is True
    assert capped["data"]["components_truncated"] is True
    assert capped["data"]["nodes_truncated"] is True

    historical = session.components(snapshot=older.name)
    old_ents = pd.read_parquet(graph / "snapshots" / older.name / "entities.parquet")
    old_rels = pd.read_parquet(
        graph / "snapshots" / older.name / "relationships.parquet"
    )
    old_expected = compute_weakly_connected_components(old_ents, old_rels)
    assert historical["snapshot"] == older.name
    assert historical["data"] == json.loads(
        json.dumps(old_expected, allow_nan=False, default=str)
    )
    assert historical["data"]["n_nodes_total"] == 1
    assert "demo:new" not in json.dumps(historical)
    assert "A" not in json.dumps(historical["data"])
    assert _current(graph) == current.name
    assert _payload_hashes(graph) == before

    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            payload = _payload(
                await client.call_tool("components", {"snapshot": older.name})
            )
            assert payload["tool"] == "components"
            assert payload["snapshot"] == older.name
            assert payload["data"] == historical["data"]

    _run(_body)


def test_components_mcp_validation_malformed_empty_and_envelope(tmp_path: Path):
    graph = _publish_components(
        tmp_path,
        [_component_entity("A"), _component_entity("B")],
        [_component_rel("A", "B", "calls")],
    )
    session = _session(graph, "python")
    with pytest.raises(GraphMcpError, match="max_components"):
        session.components(max_components=True)
    with pytest.raises(GraphMcpError, match="max_components"):
        session.components(max_components=1.5)
    with pytest.raises(GraphMcpError, match="max_components"):
        session.components(max_components=float("nan"))
    with pytest.raises(GraphMcpError, match="max_nodes_per_component"):
        session.components(max_nodes_per_component=math.inf)
    with pytest.raises(GraphMcpError, match="max_nodes_per_component"):
        session.components(max_nodes_per_component=0)
    with pytest.raises(GraphMcpError, match="max_components"):
        session.components(max_components=-1)
    with pytest.raises(GraphMcpError, match="max_components"):
        session.components(max_components=HARD_MAX_COMPONENTS + 1)
    with pytest.raises(GraphMcpError, match="max_nodes_per_component"):
        session.components(max_nodes_per_component=HARD_MAX_COMPONENT_NODES + 1)
    with pytest.raises(GraphMcpError, match="array of strings"):
        session.components(edge_types="calls")
    with pytest.raises(GraphMcpError, match="invalid edge-type filter"):
        session.components(edge_types=[""])
    with pytest.raises(GraphMcpError, match="invalid edge-type filter"):
        session.components(edge_types=[" calls"])
    with pytest.raises(GraphMcpError, match="snapshot"):
        session.components(snapshot="..")

    empty_ok = session.components(edge_types=[])
    assert empty_ok["limits"]["edge_types"] is None
    none_ok = session.components(edge_types=None)
    assert none_ok["data"] == empty_ok["data"]

    empty_graph = tmp_path / "empty"
    publish_byog_snapshot(
        pd.DataFrame(columns=["id", "title", "type", "source_file", "extractor"]),
        pd.DataFrame(columns=["id", "source", "target", "type", "extractor"]),
        pd.DataFrame(columns=["id", "title", "source_file"]),
        empty_graph,
        settings_text="mcp: empty\n",
        keep_last=1,
    )
    empty_session = GraphMcpSession(
        empty_graph,
        configured_indexer="python",
        resolved_indexer="python",
        preflight={"indexer": "python", "indexer_resolution": {}},
    )
    empty_payload = empty_session.components()
    expected_empty = json.loads(
        json.dumps(ByogGraph(empty_graph).components(), allow_nan=False, default=str)
    )
    _assert_components_envelope(empty_payload, expected_empty)
    assert empty_payload["data"]["components"] == []
    assert empty_payload["truncated"] is False

    bad = tmp_path / "bad"
    publish_byog_snapshot(
        pd.DataFrame([_component_entity("A")]),
        pd.DataFrame(
            [
                {
                    "id": "rel:bad",
                    "source": "A",
                    "target": None,
                    "type": "calls",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(
            [{"id": "tu:a", "title": "a.py", "source_file": "a.py", "entity_id": "ent:A"}]
        ),
        bad,
        settings_text="mcp: bad\n",
        keep_last=1,
    )
    with pytest.raises(GraphMcpError, match="invalid target"):
        GraphMcpSession(
            bad,
            configured_indexer="python",
            resolved_indexer="python",
            preflight={"indexer": "python", "indexer_resolution": {}},
        ).components()

    huge = tmp_path / "huge"
    title = "T" + ("x" * (HARD_MAX_ENVELOPE_BYTES + 1))
    publish_byog_snapshot(
        pd.DataFrame([_component_entity(title, id="ent:huge", source_file="m.py")]),
        pd.DataFrame(columns=["id", "source", "target", "type"]),
        pd.DataFrame(
            [{"id": "tu:1", "title": "m.py", "source_file": "m.py", "entity_id": "ent:huge"}]
        ),
        huge,
        settings_text="mcp: huge\n",
        keep_last=1,
    )
    with pytest.raises(GraphMcpError, match="response envelope exceeds hard limit"):
        GraphMcpSession(
            huge,
            configured_indexer="python",
            resolved_indexer="python",
            preflight={"indexer": "python", "indexer_resolution": {}},
        ).components(max_components=1, max_nodes_per_component=1)


def _mcp_paused_components(graph: str, pinned, resume, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "src"))
    import graphrag_code.mcp_server as mcp_mod

    orig_envelope = mcp_mod._envelope

    def wrapped_envelope(**kwargs):
        payload = orig_envelope(**kwargs)
        pinned.set()
        if not resume.wait(timeout=20):
            q.put("timeout")
        return payload

    mcp_mod._envelope = wrapped_envelope
    session = mcp_mod.GraphMcpSession(
        Path(graph),
        configured_indexer="python",
        resolved_indexer="python",
        preflight={"indexer": "python", "indexer_resolution": {}},
    )
    payload = session.components()
    q.put(payload["snapshot"])


def test_components_mcp_publisher_wait_and_no_nested_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import multiprocessing

    graph = _publish_components(
        tmp_path,
        [_component_entity("A"), _component_entity("B")],
        [_component_rel("A", "B", "calls")],
    )
    first = _current(graph)
    first_dir = graph / "snapshots" / first
    before = _payload_hashes(graph)
    session = _session(graph, "python")

    def boom(*_args, **_kwargs):
        raise AssertionError("nested public query or CLI invoked from MCP components")

    producer_calls = 0
    producer = ByogGraph.components

    def counted_producer(self, *args, **kwargs):
        nonlocal producer_calls
        producer_calls += 1
        return producer(self, *args, **kwargs)

    monkeypatch.setattr("graphrag_code.graph_query.components", boom)
    monkeypatch.setattr("graphrag_code.graph_query.cli_components", boom)
    monkeypatch.setattr("graphrag_code.cli.components", boom)
    monkeypatch.setattr("graphrag_code.graph_query.subgraph", boom)
    monkeypatch.setattr(ByogGraph, "components", counted_producer)
    payload = session.components()
    assert payload["ok"] is True
    assert producer_calls == 1
    assert _payload_hashes(graph) == before
    assert not list(graph.glob(".staging-*"))
    assert not list(tmp_path.glob("*.dot"))

    ctx = multiprocessing.get_context("spawn")
    pinned = ctx.Event()
    resume = ctx.Event()
    about = ctx.Event()
    got = ctx.Event()
    q = ctx.Queue()
    reader = ctx.Process(
        target=_mcp_paused_components, args=(str(graph), pinned, resume, q)
    )
    from test_reader_lease import _cleanup_processes, _publisher

    pub = ctx.Process(target=_publisher, args=(str(graph), "next", 1, about, got, q))
    try:
        reader.start()
        assert pinned.wait(timeout=20)
        pub.start()
        assert about.wait(timeout=20)
        assert not got.is_set()
        assert _current(graph) == first
        assert (first_dir / "entities.parquet").is_file()
        resume.set()
        reader.join(timeout=20)
        pub.join(timeout=20)
        assert not reader.is_alive() and not pub.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(pub, reader, release=resume)
