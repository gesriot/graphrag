"""Retained-snapshot-scoped read/query without mutating current.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_read.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import multiprocessing
import os
import stat
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import (  # type: ignore
    TOOL_NAMES,
    GraphMcpError,
    build_mcp_server,
    build_session,
)
from graphrag_code.snapshot_read import retained_snapshot_read  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
QUERY = ROOT / "scripts" / "graph_query.py"
PACK = ROOT / "scripts" / "context_pack.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_read.py"
FORBIDDEN = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
        "snapshot_activate",
        "c_clang_ast_capture",
        "c_compiler_facts",
    }
)
QUERY_COMMANDS = (
    "query-symbol",
    "callers",
    "callees",
    "types-used-by",
    "type-users",
    "type-closure",
    "neighbors",
    "subgraph",
    "dependency-order",
    "impact",
    "observations",
    "context-pack",
)


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


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _rows(marker: str):
    ents = [
        {
            "id": f"ent:fn:{marker}",
            "title": f"demo:{marker}",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
            "description": f"desc-{marker}",
        },
        {
            "id": f"ent:ty:{marker}",
            "title": f"demo:{marker}_T",
            "type": "class",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"ent:fn:{marker}_caller",
            "title": f"demo:{marker}_caller",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
        },
    ]
    rels = [
        {
            "id": f"rel:calls:{marker}",
            "source": f"demo:{marker}_caller",
            "target": f"demo:{marker}",
            "type": "calls",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"rel:uses:{marker}",
            "source": f"demo:{marker}",
            "target": f"demo:{marker}_T",
            "type": "uses_type",
            "extractor": "tree-sitter-python",
        },
        {
            "id": f"rel:contains:{marker}",
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
            "entity_id": f"ent:fn:{marker}",
            "text": f"text-for-{marker}",
        }
    ]
    obs = [
        {
            "id": f"obs:{marker}",
            "source": f"demo:{marker}",
            "display_target": f"dyn_{marker}",
            "confidence": "low",
            "reason": f"reason-{marker}",
        }
    ]
    return ents, rels, tus, obs


def _publish(graph: Path, marker: str, *, keep_last: int = 10) -> Path:
    ents, rels, tus, obs = _rows(marker)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"scope: {marker}\n",
        keep_last=keep_last,
        call_observations_df=pd.DataFrame(obs),
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


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


def _two(tmp_path: Path) -> tuple[Path, Path, Path]:
    graph = tmp_path / "g"
    older = _publish(graph, "old")
    newer = _publish(graph, "new")
    return graph, older, newer


def test_default_and_current_see_only_current_version(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    with retained_snapshot_read(graph, None, allow_unlocked_managed=True) as scope:
        g = scope.load_graph()
        assert g.symbol("demo:new")["title"] == "demo:new"
        assert g.symbol("demo:old") is None
        assert g.callers("demo:new") == ["demo:new_caller"]
        assert scope.snap_id == newer.name
    with retained_snapshot_read(graph, "current", allow_unlocked_managed=True) as scope:
        assert scope.snap_id == newer.name
        assert scope.load_graph().symbol("demo:old") is None


def test_explicit_old_id_sees_only_historical_data(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    with retained_snapshot_read(graph, older.name, allow_unlocked_managed=True) as scope:
        g = scope.load_graph()
        assert scope.snap_id == older.name
        assert g.symbol("demo:old")["title"] == "demo:old"
        assert g.symbol("demo:new") is None
        assert g.callers("demo:old") == ["demo:old_caller"]
        assert g.callees("demo:old_caller") == ["demo:old"]
        assert g.types_used_by("demo:old") == ["demo:old_T"]
        assert g.type_users("demo:old_T") == ["demo:old"]
        assert g.observations("demo:old")[0]["display_target"] == "dyn_old"
        assert "text-for-old" in str(g.tus.iloc[0]["text"])
    assert _current(graph) == newer.name


def test_historical_read_does_not_change_current(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    before = _payload_hashes(graph)
    stats = _payload_stats(graph)
    listing = tuple(sorted(path.name for path in (graph / "snapshots").iterdir()))
    lock_bytes = (graph / PUBLICATION_LOCK_NAME).read_bytes()
    proc = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["title"] == "demo:old"
    assert _current(graph) == newer.name
    assert _payload_hashes(graph) == before
    assert _payload_stats(graph) == stats
    assert tuple(sorted(path.name for path in (graph / "snapshots").iterdir())) == listing
    assert (graph / PUBLICATION_LOCK_NAME).read_bytes() == lock_bytes


def test_all_cli_query_commands_honor_snapshot(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    env = _child_env()
    for name in QUERY_COMMANDS:
        if name == "dependency-order":
            args = [name, "--graph", str(graph), "--snapshot", older.name]
        elif name == "type-closure":
            args = [
                name,
                "demo:old",
                "--graph",
                str(graph),
                "--snapshot",
                older.name,
                "--direction",
                "dependencies",
            ]
        elif name == "context-pack":
            args = [
                name,
                "demo:old",
                "--graph",
                str(graph),
                "--snapshot",
                older.name,
                "--json",
            ]
        else:
            symbol = "demo:old_T" if name == "type-users" else "demo:old"
            args = [name, symbol, "--graph", str(graph), "--snapshot", older.name]
        proc = subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, (name, proc.stderr)
        assert "demo:new" not in proc.stdout
        if name == "query-symbol":
            assert "demo:old" in proc.stdout
        if name == "context-pack":
            pack = json.loads(proc.stdout)
            assert pack["symbol"] == "demo:old"
    assert _current(graph) == newer.name


def test_graph_query_module_script_product_parity(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    args = [
        "symbol",
        "demo:old",
        "--graph",
        str(graph),
        "--snapshot",
        older.name,
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.graph_query", *args],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = subprocess.run(
        [sys.executable, str(QUERY), *args],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    cli = subprocess.run(
        [sys.executable, str(CLI), "query-symbol", "demo:old", "--graph", str(graph), "--snapshot", older.name, "--json"],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    assert json.loads(module.stdout) == json.loads(script.stdout) == json.loads(cli.stdout)


def test_context_pack_module_script_product_parity(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    args = ["demo:old", "--graph", str(graph), "--snapshot", older.name]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.context_pack", *args],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = subprocess.run(
        [sys.executable, str(PACK), *args],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "context-pack",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
            "--json",
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    assert json.loads(module.stdout) == json.loads(script.stdout) == json.loads(cli.stdout)
    assert json.loads(module.stdout)["symbol"] == "demo:old"


def test_installed_wheel_historical_query(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    graph, older, newer = _two(tmp_path)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = subprocess.run(
        [
            "graphrag-code",
            "query-symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["title"] == "demo:old"
    assert _current(graph) == newer.name


def test_mcp_query_tools_return_historical_snapshot(tmp_path: Path):
    from anyio import run as anyio_run

    graph, older, newer = _two(tmp_path)
    session = build_session(graph, "python")
    server = build_mcp_server(session)
    selectable = (
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
    )

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert tools == set(TOOL_NAMES)
            assert len(tools) == 16
            for name in selectable:
                if name in {
                    "graph_status",
                    "graph_doctor",
                    "components",
                    "strong_components",
                    "condensation",
                    "degree_ranking",
                }:
                    result = await client.call_tool(name, {"snapshot": older.name})
                elif name == "type_closure":
                    result = await client.call_tool(
                        name,
                        {
                            "symbol": "demo:old",
                            "direction": "dependencies",
                            "snapshot": older.name,
                        },
                    )
                elif name == "context_pack":
                    result = await client.call_tool(
                        name, {"symbol": "demo:old", "snapshot": older.name}
                    )
                else:
                    result = await client.call_tool(
                        name, {"symbol": "demo:old", "snapshot": older.name}
                    )
                assert result.is_error is False, getattr(result, "content", result)
                payload = result.structured_content
                if isinstance(payload, dict) and set(payload) == {"result"}:
                    payload = payload["result"]
                assert isinstance(payload, dict), (name, payload)
                assert payload["ok"] is True
                assert payload["snapshot"] == older.name
                assert payload["tool"] == name
            defaulted = await client.call_tool("graph_status")
            body = defaulted.structured_content
            if isinstance(body, dict) and set(body) == {"result"}:
                body = body["result"]
            assert body["snapshot"] == newer.name
            extra = await client.call_tool(
                "query_symbol",
                {"symbol": "demo:old", "graph": str(tmp_path / "other")},
            )
            assert extra.is_error is True

    anyio_run(_body)


def test_historical_status_resolves_indexer_from_selected_snapshot(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    for name in ("entities", "relationships", "text_units"):
        path = older / f"{name}.parquet"
        table = pd.read_parquet(path)
        if "source_file" in table.columns:
            table["source_file"] = table["source_file"].astype(str).str.replace(
                ".py", ".c", regex=False
            )
        if "extractor" in table.columns:
            table["extractor"] = "tree-sitter-c"
        table.to_parquet(path)

    session = build_session(graph, "auto")
    assert session.resolved_indexer == "python"
    status = session.graph_status(older.name)
    assert status["snapshot"] == older.name
    assert status["data"]["indexer"] == "c"
    assert status["data"]["indexer_resolution"]["resolved"] == "c"

    entities_path = older / "entities.parquet"
    entities = pd.read_parquet(entities_path)
    entities.loc[0, "source_file"] = "mixed.py"
    entities.loc[0, "extractor"] = "tree-sitter-python"
    entities.to_parquet(entities_path)
    with pytest.raises(GraphMcpError, match="auto-indexer is ambiguous"):
        session.graph_status(older.name)


def test_explicit_doctor_does_not_open_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    graph, older, _newer = _two(tmp_path)
    session = build_session(graph, "python")
    current = graph / "current"
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == current:
            raise AssertionError("explicit historical doctor opened current")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    report = session.graph_doctor(snapshot=older.name)
    assert report["snapshot"] == older.name
    assert "graph/current" not in report["data"]["read_only_verification"]["inputs"]


def test_unsafe_and_missing_refs_fail_closed(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    before = _payload_hashes(graph)
    for ref in (
        "../etc/passwd",
        "foo/bar",
        f"{STAGING_NAME_PREFIX}x",
        "",
        ".",
        "..",
        "id\\win",
        f" {older.name}",
        f"{older.name} ",
    ):
        proc = subprocess.run(
            [
                sys.executable,
                str(QUERY),
                "symbol",
                "demo:old",
                "--graph",
                str(graph),
                "--snapshot",
                ref,
            ],
            capture_output=True,
            text=True,
            env=_child_env(),
        )
        assert proc.returncode == 2, (ref, proc.stderr)
        assert "Traceback" not in proc.stderr
    missing = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            "20990101-000000-deadbeef",
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert missing.returncode != 0
    assert "Traceback" not in missing.stderr
    assert _current(graph) == newer.name
    assert _payload_hashes(graph) == before
    assert older.name != newer.name


def test_symlinked_snapshot_and_core_fail_closed(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    alias = graph / "snapshots" / "aliased"
    alias.symlink_to(older, target_is_directory=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            "aliased",
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    alias.unlink()

    core = older / "entities.parquet"
    payload = core.read_bytes()
    external = tmp_path / "entities.parquet"
    external.write_bytes(payload)
    core.unlink()
    core.symlink_to(external)
    proc = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert _current(graph) == newer.name


def test_legacy_flat_rejects_explicit_id(tmp_path: Path):
    graph = tmp_path / "flat"
    graph.mkdir()
    ents, rels, tus, _obs = _rows("flat")
    pd.DataFrame(ents).to_parquet(graph / "entities.parquet")
    pd.DataFrame(rels).to_parquet(graph / "relationships.parquet")
    pd.DataFrame(tus).to_parquet(graph / "text_units.parquet")
    before = _payload_hashes(graph)
    proc = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:flat",
            "--graph",
            str(graph),
            "--snapshot",
            "anything",
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode == 2
    assert "legacy" in proc.stderr
    defaulted = subprocess.run(
        [sys.executable, str(QUERY), "symbol", "demo:flat", "--graph", str(graph)],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert defaulted.returncode == 0, defaulted.stderr
    assert json.loads(defaulted.stdout)["title"] == "demo:flat"
    assert _payload_hashes(graph) == before


def test_only_omitted_selector_allows_pre_lock_compatibility(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    omitted = subprocess.run(
        [sys.executable, str(QUERY), "symbol", "demo:new", "--graph", str(graph)],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert omitted.returncode == 0, omitted.stderr
    assert json.loads(omitted.stdout)["title"] == "demo:new"
    for ref in ("current", older.name):
        explicit = subprocess.run(
            [
                sys.executable,
                str(QUERY),
                "symbol",
                "demo:old",
                "--graph",
                str(graph),
                "--snapshot",
                ref,
            ],
            capture_output=True,
            text=True,
            env=_child_env(),
        )
        assert explicit.returncode == 2, (ref, explicit.stderr)
        assert "publication lock is missing" in explicit.stderr
        assert "Traceback" not in explicit.stderr
    assert not lock.exists()
    assert _current(graph) == newer.name


def _reader_hold(graph: str, snap_id: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_read import retained_snapshot_read

    try:
        with retained_snapshot_read(ChildPath(graph), snap_id, allow_unlocked_managed=False):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("ok")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _activate_waiter(graph: str, snapshot: str, expected: str, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    try:
        from graphrag_code.snapshot_activate import snapshot_activate as activate

        result = activate(
            ChildPath(graph),
            snapshot,
            expected,
            activate_confirmed=True,
        )
        q.put(result["current"])
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


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


def test_historical_reader_blocks_keep_last_deletion(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "old", keep_last=1)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_reader_hold, args=(str(graph), older.name, held, resume, q)
    )
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 1, about, got, q))
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert older.is_dir()
        resume.set()
        pub.join(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not reader.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(reader, pub, release=resume)


def test_activation_waits_for_historical_reader(tmp_path: Path):
    graph, older, newer = _two(tmp_path)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(
        target=_reader_hold, args=(str(graph), older.name, held, resume, q)
    )
    about = CTX.Event()
    got = CTX.Event()
    actor = CTX.Process(
        target=_activate_waiter,
        args=(str(graph), older.name, newer.name, about, got, q),
    )
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        actor.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == newer.name
        resume.set()
        actor.join(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not actor.is_alive() and not reader.is_alive()
        assert got.is_set()
        assert _current(graph) == older.name
    finally:
        _cleanup_processes(reader, actor, release=resume)


def test_current_resolved_once_vs_zero_for_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_read as scope_mod
    from graphrag_code.byog_snapshot_graph_audit import resolve_snapshot as original

    graph, older, _newer = _two(tmp_path)
    calls: list[object] = []

    def wrapped(graph_root, snapshot=None):
        if snapshot is None:
            calls.append(None)
        return original(graph_root, snapshot)

    monkeypatch.setattr(scope_mod, "resolve_snapshot", wrapped)
    with retained_snapshot_read(graph, "current", allow_unlocked_managed=True):
        pass
    assert calls == [None]
    calls.clear()
    with retained_snapshot_read(graph, older.name, allow_unlocked_managed=True):
        pass
    assert calls == []


def test_scoped_loader_disables_background_parquet_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import graphrag_code.byog_graph as graph_mod

    graph, older, _newer = _two(tmp_path)
    original = graph_mod.pd.read_parquet
    calls: list[object] = []

    def tracked(*args, **kwargs):
        calls.append(kwargs.get("use_threads"))
        return original(*args, **kwargs)

    monkeypatch.setattr(graph_mod.pd, "read_parquet", tracked)
    with retained_snapshot_read(graph, older.name, allow_unlocked_managed=True) as scope:
        loaded = scope.load_graph()
        assert loaded.symbol("demo:old")["title"] == "demo:old"
    assert calls and calls == [False] * len(calls)


def test_implementation_does_not_invoke_producers():
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert (imported | called) & FORBIDDEN == set()
    assert "snapshot_activate" not in source
    assert "graph_exclusive_lease" not in source
    assert "publish_byog_snapshot" not in source


def test_omitted_selector_keeps_existing_symbol_shape(tmp_path: Path):
    graph, _older, newer = _two(tmp_path)
    omitted = subprocess.run(
        [sys.executable, str(QUERY), "symbol", "demo:new", "--graph", str(graph)],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    current = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:new",
            "--graph",
            str(graph),
            "--snapshot",
            "current",
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert omitted.returncode == current.returncode == 0
    body = json.loads(omitted.stdout)
    assert set(body) == set(json.loads(current.stdout))
    assert body["title"] == "demo:new"
    assert _current(graph) == newer.name


def test_lock_ignoring_deletion_is_controlled(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    import shutil

    shutil.rmtree(older)
    proc = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr


def test_malformed_parquet_is_a_controlled_error(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    (older / "entities.parquet").write_bytes(b"not a parquet file")
    proc = subprocess.run(
        [
            sys.executable,
            str(QUERY),
            "symbol",
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode == 1
    assert "malformed or unreadable" in proc.stderr
    assert "Traceback" not in proc.stderr
    packed = subprocess.run(
        [
            sys.executable,
            str(PACK),
            "demo:old",
            "--graph",
            str(graph),
            "--snapshot",
            older.name,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert packed.returncode == 1
    assert "malformed or unreadable" in packed.stderr
    assert "Traceback" not in packed.stderr


def test_context_pack_serializes_and_writes_under_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from contextlib import contextmanager

    import graphrag_code.context_pack as pack_mod

    graph, older, _newer = _two(tmp_path)
    output = tmp_path / "pack.json"
    original_scope = pack_mod._retained_context_pack
    original_dumps = pack_mod.json.dumps
    original_write_text = Path.write_text
    state = {"active": False, "serialized": False, "written": False}

    @contextmanager
    def tracked_scope(*args, **kwargs):
        with original_scope(*args, **kwargs) as data:
            state["active"] = True
            try:
                yield data
            finally:
                state["active"] = False

    def tracked_dumps(*args, **kwargs):
        assert state["active"]
        state["serialized"] = True
        return original_dumps(*args, **kwargs)

    def tracked_write_text(path, *args, **kwargs):
        if path == output:
            assert state["active"]
            state["written"] = True
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(pack_mod, "_retained_context_pack", tracked_scope)
    monkeypatch.setattr(pack_mod.json, "dumps", tracked_dumps)
    monkeypatch.setattr(Path, "write_text", tracked_write_text)
    pack_mod.pack(
        "demo:old",
        graph,
        "port-to-rust",
        output,
        300,
        False,
        True,
        20,
        5,
        1,
        older.name,
    )
    assert state == {"active": False, "serialized": True, "written": True}
    assert json.loads(output.read_text(encoding="utf-8"))["entity"]["title"] == "demo:old"


def test_multiple_historical_readers_coexist(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    held_a = CTX.Event()
    held_b = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    a = CTX.Process(target=_reader_hold, args=(str(graph), older.name, held_a, resume, q))
    b = CTX.Process(target=_reader_hold, args=(str(graph), older.name, held_b, resume, q))
    try:
        a.start()
        b.start()
        assert held_a.wait(timeout=TIMEOUT)
        assert held_b.wait(timeout=TIMEOUT)
        resume.set()
        a.join(timeout=TIMEOUT)
        b.join(timeout=TIMEOUT)
        assert not a.is_alive() and not b.is_alive()
        assert {q.get(timeout=5), q.get(timeout=5)} == {"ok"}
    finally:
        _cleanup_processes(a, b, release=resume)


def _exclusive_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import graph_exclusive_lease

    try:
        with graph_exclusive_lease(ChildPath(graph)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _reader_waiter(graph: str, snap_id: str, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    orig = byog._acquire_lock

    def wrapped(fd, *, exclusive):
        about.set()
        backend = orig(fd, exclusive=exclusive)
        got.set()
        return backend

    byog._acquire_lock = wrapped
    try:
        from graphrag_code.snapshot_read import retained_snapshot_read

        with retained_snapshot_read(ChildPath(graph), snap_id, allow_unlocked_managed=False) as scope:
            q.put(scope.snap_id)
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_replaced_lock_while_reader_waits_fails_closed(tmp_path: Path):
    graph, older, _newer = _two(tmp_path)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    reader = CTX.Process(
        target=_reader_waiter, args=(str(graph), older.name, about, got, q)
    )
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        reader.start()
        assert about.wait(timeout=TIMEOUT)
        lock = graph / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replacement-lock-domain")
        resume.set()
        reader.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not reader.is_alive() and not holder.is_alive()
        messages = [q.get(timeout=TIMEOUT), q.get(timeout=TIMEOUT)]
        assert any("publication lock changed" in str(message) for message in messages)
    finally:
        _cleanup_processes(holder, reader, release=resume)
