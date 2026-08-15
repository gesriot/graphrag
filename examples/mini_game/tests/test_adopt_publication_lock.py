"""Explicit offline adoption of graph-root ``.publish.lock``.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_adopt_publication_lock.py -q
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
    ByogGraph,
    graph_read_lease,
    publish_byog_snapshot,
)
from graphrag_code.adopt_publication_lock import (  # type: ignore
    adopt_publication_lock,
    main as adopt_main,
    result_to_json,
)
from graphrag_code.mcp_server import build_session  # type: ignore
from graphrag_code.persisted_graph_doctor import audit_graph_root  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
ADOPT_SCRIPT = ROOT / "scripts" / "adopt_publication_lock.py"
CLI_SCRIPT = ROOT / "scripts" / "graphrag_code.py"
ADOPT_MODULE = ROOT / "src" / "graphrag_code" / "adopt_publication_lock.py"
FORBIDDEN_NAMES = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
        "c_clang_ast_capture",
        "c_compiler_facts",
        "c_compiler_includes",
    }
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


def _py_rows():
    ents = [
        {
            "id": "ent:fn:demo.main",
            "title": "demo:main",
            "type": "function",
            "source_file": "demo.py",
            "extractor": "tree-sitter-python",
        }
    ]
    rels = [
        {
            "id": "rel:contains:1",
            "source": "demo:demo.py",
            "target": "demo:main",
            "type": "contains",
            "extractor": "tree-sitter-python",
        }
    ]
    tus = [
        {
            "id": "tu:1",
            "title": "demo.py",
            "source_file": "demo.py",
            "entity_id": "ent:fn:demo.main",
        }
    ]
    return ents, rels, tus


def _c_rows():
    ents = [
        {
            "id": "ent:fn:mod.main",
            "title": "mod:main",
            "type": "function",
            "source_file": "main.c",
            "extractor": "tree-sitter-c",
        }
    ]
    rels = [
        {
            "id": "rel:contains:1",
            "source": "mod:main.c",
            "target": "mod:main",
            "type": "contains",
            "extractor": "tree-sitter-c",
        }
    ]
    tus = [
        {
            "id": "tu:1",
            "title": "main.c",
            "source_file": "main.c",
            "entity_id": "ent:fn:mod.main",
        }
    ]
    return ents, rels, tus


def _publish(tmp_path: Path, rows=None, *, name: str = "g") -> Path:
    ents, rels, tus = rows if rows is not None else _py_rows()
    graph = tmp_path / name
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text="adopt: true\n",
        keep_last=2,
    )
    return graph


def _unlock(graph: Path) -> Path:
    lock = graph / PUBLICATION_LOCK_NAME
    if lock.exists() or lock.is_symlink():
        if lock.is_dir() and not lock.is_symlink():
            lock.rmdir()
        else:
            lock.unlink()
    return lock


def _prelock(tmp_path: Path, rows=None, *, name: str = "g") -> Path:
    graph = _publish(tmp_path, rows, name=name)
    _unlock(graph)
    return graph


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _lock_stat(lock: Path) -> tuple[int, int, int, int, int, bytes]:
    info = lock.lstat()
    return (
        info.st_ino,
        info.st_dev,
        info.st_size,
        info.st_mtime_ns,
        stat.S_IMODE(info.st_mode),
        lock.read_bytes(),
    )


def _payload_paths(graph: Path) -> list[Path]:
    paths = [path for path in graph.iterdir() if path.name != PUBLICATION_LOCK_NAME]
    snaps = graph / "snapshots"
    if snaps.is_dir() and not snaps.is_symlink():
        paths.extend(sorted(snaps.rglob("*")))
    return paths


def _payload_hashes(graph: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in _payload_paths(graph):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(graph).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _payload_stats(graph: Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for path in _payload_paths(graph):
        if path.is_file() and not path.is_symlink():
            info = path.lstat()
            out[path.relative_to(graph).as_posix()] = (
                info.st_size,
                info.st_mtime_ns,
                stat.S_IMODE(info.st_mode),
            )
    return out


def _listing(graph: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    root_names = tuple(sorted(p.name for p in graph.iterdir() if p.name != PUBLICATION_LOCK_NAME))
    snaps = graph / "snapshots"
    snap_names = tuple(sorted(p.name for p in snaps.iterdir())) if snaps.is_dir() else ()
    return root_names, snap_names


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _run_main(graph: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "graphrag_code.adopt_publication_lock", *args, "--graph", str(graph)],
        cwd=str(graph.parent),
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def test_missing_lock_without_offline_confirmed_is_exit_2_and_does_not_mutate(tmp_path: Path):
    graph = _prelock(tmp_path)
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    before_listing = _listing(graph)
    lock = graph / PUBLICATION_LOCK_NAME
    proc = _run_main(graph, "--indexer", "python")
    assert proc.returncode == 2, proc.stderr
    assert "offline-confirmed" in proc.stderr
    assert "cannot discover or prove" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not lock.exists()
    assert _payload_hashes(graph) == before_hashes
    assert _payload_stats(graph) == before_stats
    assert _listing(graph) == before_listing


def test_valid_python_managed_graph_creates_only_the_lock(tmp_path: Path):
    graph = _prelock(tmp_path)
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    before_listing = _listing(graph)
    snap = _current(graph)
    proc = _run_main(graph, "--indexer", "python", "--offline-confirmed", "--json")
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    lock = graph / PUBLICATION_LOCK_NAME
    assert body == {
        "ok": True,
        "status": "adopted",
        "graph": str(graph.resolve()),
        "lock": str(lock.resolve()),
        "lock_created": True,
        "indexer": "python",
        "snapshot": snap,
        "payload_unchanged": True,
        "offline_assumption": "operator-confirmed",
    }
    assert lock.is_file() and not lock.is_symlink()
    info = lock.lstat()
    assert stat.S_ISREG(info.st_mode)
    mask = os.umask(0)
    os.umask(mask)
    assert stat.S_IMODE(info.st_mode) == (0o644 & ~mask)
    assert info.st_size == 0
    names = {path.name for path in graph.iterdir()}
    assert PUBLICATION_LOCK_NAME in names
    assert _payload_hashes(graph) == before_hashes
    assert _payload_stats(graph) == before_stats
    assert _listing(graph) == before_listing
    with graph_read_lease(graph):
        assert ByogGraph(graph).symbol("demo:main")["title"] == "demo:main"


def test_valid_c_managed_graph_with_indexer_auto(tmp_path: Path):
    graph = _prelock(tmp_path, _c_rows(), name="cgraph")
    before_hashes = _payload_hashes(graph)
    proc = _run_main(graph, "--indexer", "auto", "--offline-confirmed", "--json")
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["ok"] is True
    assert body["status"] == "adopted"
    assert body["indexer"] == "c"
    assert body["lock_created"] is True
    assert body["snapshot"] == _current(graph)
    assert _payload_hashes(graph) == before_hashes
    with graph_read_lease(graph):
        assert ByogGraph(graph).symbol("mod:main")["title"] == "mod:main"


def test_payload_invariance_covers_current_snapshots_manifests_and_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = _prelock(tmp_path)
    snap = graph / "snapshots" / _current(graph)
    watched = [
        graph / "current",
        graph / "snapshots",
        snap,
        snap / "manifest.json",
        snap / "entities.parquet",
        snap / "relationships.parquet",
        snap / "text_units.parquet",
        snap / "settings.yaml",
    ]
    before = {
        path.as_posix(): (
            path.lstat().st_ino,
            path.lstat().st_dev,
            path.lstat().st_mtime_ns,
            path.lstat().st_size if path.is_file() else None,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in watched
    }
    listing = tuple(sorted(p.name for p in (graph / "snapshots").iterdir()))
    result = adopt_publication_lock(graph, "python", offline_confirmed=True)
    assert result["ok"] is True
    assert result["payload_unchanged"] is True
    after = {
        path.as_posix(): (
            path.lstat().st_ino,
            path.lstat().st_dev,
            path.lstat().st_mtime_ns,
            path.lstat().st_size if path.is_file() else None,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in watched
    }
    assert after == before
    assert tuple(sorted(p.name for p in (graph / "snapshots").iterdir())) == listing

    # Simulate a lock-ignoring actor switching between two already-valid
    # snapshots after preflight but before adoption creates the protocol file.
    observed = _publish(tmp_path, name="observed-change")
    first = _current(observed)
    _publish(tmp_path, name="observed-change")
    second = _current(observed)
    assert first != second
    (observed / "current").write_text(first, encoding="utf-8")
    _unlock(observed)
    import graphrag_code.adopt_publication_lock as adopt_mod

    real_open = adopt_mod._open_adoption_lock_fd

    def switch_then_open(lock_path):
        (observed / "current").write_text(second, encoding="utf-8")
        return real_open(lock_path)

    monkeypatch.setattr(adopt_mod, "_open_adoption_lock_fd", switch_then_open)
    changed = adopt_publication_lock(observed, "python", offline_confirmed=True)
    assert changed["payload_unchanged"] is False
    assert changed["snapshot"] == second


def test_second_invocation_is_already_adopted_and_does_not_touch_the_lock(tmp_path: Path):
    graph = _prelock(tmp_path)
    first = adopt_publication_lock(graph, "python", offline_confirmed=True)
    lock = graph / PUBLICATION_LOCK_NAME
    before = _lock_stat(lock)
    second = adopt_publication_lock(graph, "python", offline_confirmed=True)
    assert first["status"] == "adopted"
    assert first["lock_created"] is True
    assert second["status"] == "already_adopted"
    assert second["lock_created"] is False
    assert second["ok"] is True
    assert _lock_stat(lock) == before


def test_mcp_startup_and_tool_call_succeed_after_adoption(tmp_path: Path):
    graph = _prelock(tmp_path)
    adopt_publication_lock(graph, "python", offline_confirmed=True)
    session = build_session(graph, "python")
    status = session.graph_status()
    assert status["ok"] is True
    assert status["snapshot"] == _current(graph)
    assert status["data"]["indexer"] == "python"
    found = session.query_symbol("demo:main")
    assert found["ok"] is True
    assert found["data"]["title"] == "demo:main"
    report = audit_graph_root(graph, indexer="python", allow_unlocked_managed=False)
    assert report["ok"] is True


def test_invalid_persisted_graph_does_not_create_the_lock(tmp_path: Path):
    graph = _prelock(tmp_path)
    snap = graph / "snapshots" / _current(graph)
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"]["entities"] = 0
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    before = _payload_hashes(graph)
    proc = _run_main(graph, "--indexer", "python", "--offline-confirmed", "--json")
    assert proc.returncode == 1, proc.stderr
    assert "persisted integrity" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (graph / PUBLICATION_LOCK_NAME).exists()
    assert _payload_hashes(graph) == before


def test_flat_missing_partial_and_symlinked_layouts_fail_closed(tmp_path: Path):
    missing = tmp_path / "missing"
    proc = _run_main(missing, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "does not exist" in proc.stderr
    assert "Traceback" not in proc.stderr

    flat = tmp_path / "flat"
    flat.mkdir()
    for name, frame in (
        ("entities.parquet", pd.DataFrame(_py_rows()[0])),
        ("relationships.parquet", pd.DataFrame(_py_rows()[1])),
        ("text_units.parquet", pd.DataFrame(_py_rows()[2])),
    ):
        frame.to_parquet(flat / name)
    before_flat = _payload_hashes(flat)
    proc = _run_main(flat, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "flat-parquet" in proc.stderr
    assert not (flat / PUBLICATION_LOCK_NAME).exists()
    assert _payload_hashes(flat) == before_flat

    partial = tmp_path / "partial"
    (partial / "snapshots").mkdir(parents=True)
    proc = _run_main(partial, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "incomplete managed" in proc.stderr
    assert not (partial / PUBLICATION_LOCK_NAME).exists()

    only_current = tmp_path / "only-current"
    only_current.mkdir()
    (only_current / "current").write_text("snap\n", encoding="utf-8")
    proc = _run_main(only_current, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "incomplete managed" in proc.stderr
    assert not (only_current / PUBLICATION_LOCK_NAME).exists()

    aliased = tmp_path / "aliased-root"
    real = _prelock(tmp_path, name="real-root")
    aliased.symlink_to(real)
    before = _payload_hashes(real)
    proc = _run_main(aliased, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert not (real / PUBLICATION_LOCK_NAME).exists()
    assert _payload_hashes(real) == before

    bad_current = _prelock(tmp_path, name="bad-current")
    current = bad_current / "current"
    payload = current.read_text(encoding="utf-8")
    current.unlink()
    target = bad_current / "current-target"
    target.write_text(payload, encoding="utf-8")
    current.symlink_to(target)
    proc = _run_main(bad_current, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "symlinked current" in proc.stderr
    assert not (bad_current / PUBLICATION_LOCK_NAME).exists()

    bad_snaps = _prelock(tmp_path, name="bad-snaps")
    snaps = bad_snaps / "snapshots"
    real_snaps = bad_snaps / "real-snapshots"
    snaps.rename(real_snaps)
    snaps.symlink_to(real_snaps)
    proc = _run_main(bad_snaps, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "symlinked snapshots" in proc.stderr
    assert not (bad_snaps / PUBLICATION_LOCK_NAME).exists()


def test_existing_nonregular_lock_fails_closed_without_following(tmp_path: Path):
    graph = _prelock(tmp_path, name="sym")
    external = tmp_path / "external.lock"
    external.write_text("untouched", encoding="utf-8")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.symlink_to(external)
    before = external.read_text(encoding="utf-8")
    ext_stat = external.lstat()
    proc = _run_main(graph, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert lock.is_symlink()
    assert external.read_text(encoding="utf-8") == before
    assert external.lstat().st_mtime_ns == ext_stat.st_mtime_ns
    assert external.lstat().st_ino == ext_stat.st_ino

    graph_dir = _prelock(tmp_path, name="dirlock")
    lock_dir = graph_dir / PUBLICATION_LOCK_NAME
    lock_dir.mkdir()
    sentinel = lock_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    proc = _run_main(graph_dir, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "regular file" in proc.stderr
    assert lock_dir.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"

    graph_fifo = _prelock(tmp_path, name="fifo")
    fifo = graph_fifo / PUBLICATION_LOCK_NAME
    os.mkfifo(fifo)
    proc = _run_main(graph_fifo, "--indexer", "python", "--offline-confirmed")
    assert proc.returncode == 2
    assert "regular file" in proc.stderr
    assert stat.S_ISFIFO(fifo.lstat().st_mode)


def test_unsupported_lock_backend_fails_before_creating_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import redirect_stderr
    from io import StringIO

    import graphrag_code.adopt_publication_lock as adopt_mod
    import graphrag_code.byog_graph as byog

    graph = _prelock(tmp_path)
    monkeypatch.setattr(adopt_mod, "_available_lock_backend", lambda: None)
    monkeypatch.setattr(byog, "_available_lock_backend", lambda: None)
    stderr = StringIO()
    with redirect_stderr(stderr):
        code = adopt_main(
            ["--graph", str(graph), "--indexer", "python", "--offline-confirmed"]
        )
    assert code == 2
    assert "unsupported" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
    assert not (graph / PUBLICATION_LOCK_NAME).exists()


def _wait_barrier(barrier) -> None:
    try:
        barrier.wait(timeout=TIMEOUT)
    except Exception as exc:
        raise TimeoutError(f"create-race barrier failed: {type(exc).__name__}:{exc}") from exc


def _adopter_child(graph: str, barrier, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import os
    from io import StringIO
    from contextlib import redirect_stderr, redirect_stdout
    from pathlib import Path as ChildPath

    from graphrag_code.adopt_publication_lock import main as child_main

    orig = os.open

    def gated(path, flags, *args, **kwargs):
        exclusive = bool(flags & getattr(os, "O_EXCL", 0))
        if ChildPath(path).name == PUBLICATION_LOCK_NAME and exclusive:
            _wait_barrier(barrier)
        return orig(path, flags, *args, **kwargs)

    os.open = gated
    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = child_main(
                [
                    "--graph",
                    graph,
                    "--indexer",
                    "python",
                    "--offline-confirmed",
                    "--json",
                ]
            )
        q.put({"code": code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()})
    except Exception as exc:
        q.put({"error": f"{type(exc).__name__}:{exc}", "stderr": stderr.getvalue()})


def _publisher_child(graph: str, barrier, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import os
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    orig = os.open

    def gated(path, flags, *args, **kwargs):
        creating = bool(flags & os.O_CREAT)
        if ChildPath(path).name == PUBLICATION_LOCK_NAME and creating:
            _wait_barrier(barrier)
        return orig(path, flags, *args, **kwargs)

    os.open = gated
    ents = pd.DataFrame(
        [{"id": "ent:fn:other", "title": "other", "type": "function", "source_file": "x.py"}]
    )
    rels = pd.DataFrame(
        [{"id": "rel:2", "source": "x.py", "target": "other", "type": "contains"}]
    )
    tus = pd.DataFrame(
        [{"id": "tu:2", "title": "x.py", "source_file": "x.py", "entity_id": "ent:fn:other"}]
    )
    snap = byog.publish_byog_snapshot(ents, rels, tus, ChildPath(graph), keep_last=5)
    q.put(snap.name)


def test_create_race_with_cooperating_adopter_and_publisher(tmp_path: Path):
    graph = _prelock(tmp_path)
    barrier = CTX.Barrier(2)
    qa = CTX.Queue()
    qb = CTX.Queue()
    a = CTX.Process(target=_adopter_child, args=(str(graph), barrier, qa))
    b = CTX.Process(target=_adopter_child, args=(str(graph), barrier, qb))
    try:
        a.start()
        b.start()
        a.join(timeout=TIMEOUT)
        b.join(timeout=TIMEOUT)
        assert not a.is_alive() and not b.is_alive()
        first = qa.get(timeout=5)
        second = qb.get(timeout=5)
    finally:
        _cleanup_processes(a, b)

    assert "error" not in first and "error" not in second, (first, second)
    assert first["code"] == second["code"] == 0, (first, second)
    bodies = [json.loads(first["stdout"]), json.loads(second["stdout"])]
    statuses = {body["status"] for body in bodies}
    assert statuses <= {"adopted", "already_adopted"}
    assert sum(1 for body in bodies if body["lock_created"]) == 1
    lock = graph / PUBLICATION_LOCK_NAME
    assert lock.is_file() and not lock.is_symlink()
    assert stat.S_ISREG(lock.lstat().st_mode)

    barrier2 = CTX.Barrier(2)
    q_pub = CTX.Queue()
    q_ad = CTX.Queue()
    graph2 = _prelock(tmp_path, name="race2")
    publisher = CTX.Process(target=_publisher_child, args=(str(graph2), barrier2, q_pub))
    adopter = CTX.Process(target=_adopter_child, args=(str(graph2), barrier2, q_ad))
    try:
        publisher.start()
        adopter.start()
        publisher.join(timeout=TIMEOUT)
        adopter.join(timeout=TIMEOUT)
        assert not publisher.is_alive() and not adopter.is_alive()
        published = q_pub.get(timeout=5)
        adopted = q_ad.get(timeout=5)
    finally:
        _cleanup_processes(publisher, adopter)

    assert published and not str(published).startswith("timeout")
    assert adopted.get("code") == 0, adopted
    body = json.loads(adopted["stdout"])
    assert body["status"] in {"adopted", "already_adopted"}
    assert (graph2 / PUBLICATION_LOCK_NAME).is_file()
    with graph_read_lease(graph2):
        ByogGraph(graph2)


def test_product_cli_module_and_script_parity(tmp_path: Path):
    graph = _prelock(tmp_path)
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.adopt_publication_lock",
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--offline-confirmed",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == 0, module.stderr
    first = json.loads(module.stdout)
    assert first["status"] == "adopted"

    script = subprocess.run(
        [
            sys.executable,
            str(ADOPT_SCRIPT),
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--offline-confirmed",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI_SCRIPT),
            "adopt-publication-lock",
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--offline-confirmed",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert script.returncode == cli.returncode == 0, script.stderr + cli.stderr
    assert json.loads(script.stdout) == json.loads(cli.stdout)
    repeat = json.loads(script.stdout)
    assert repeat["status"] == "already_adopted"
    assert repeat["lock_created"] is False
    assert result_to_json(repeat) == script.stdout

    for cmd in (
        [sys.executable, "-m", "graphrag_code.adopt_publication_lock", "--help"],
        [sys.executable, str(ADOPT_SCRIPT), "--help"],
        [sys.executable, str(CLI_SCRIPT), "adopt-publication-lock", "--help"],
    ):
        help_proc = subprocess.run(
            cmd, cwd=tmp_path, capture_output=True, text=True, env=_child_env()
        )
        assert help_proc.returncode == 0, help_proc.stderr
        assert "--offline-confirmed" in help_proc.stdout
        assert "cannot prove" in help_proc.stdout


def test_installed_wheel_command_from_outside_checkout(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    wheel, _ = built_wheel_and_sdist
    env = install_wheel(wheel, tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    graph = _prelock(tmp_path / "graphs")
    installed = subprocess.run(
        [
            "graphrag-code",
            "adopt-publication-lock",
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--offline-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    body = json.loads(installed.stdout)
    assert body["status"] == "adopted"
    assert body["lock_created"] is True
    assert (graph / PUBLICATION_LOCK_NAME).is_file()
    assert str(ROOT / "src") not in installed.stdout
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.adopt_publication_lock",
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--offline-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert module.returncode == 0, module.stderr
    assert json.loads(module.stdout)["status"] == "already_adopted"


def test_adoption_implementation_does_not_invoke_producers():
    source = ADOPT_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[-1])
                imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    hits = (imported | called) & FORBIDDEN_NAMES
    assert hits == set(), hits
    assert "publish_byog_snapshot" not in source
    assert "cleanup_old_snapshots" not in source
    assert "index_python" not in source
    assert "index_c" not in source


def test_ambiguous_indexer_is_exit_2_and_does_not_create_lock(tmp_path: Path):
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
        keep_last=1,
    )
    _unlock(mixed)
    proc = _run_main(mixed, "--indexer", "auto", "--offline-confirmed")
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (mixed / PUBLICATION_LOCK_NAME).exists()


def test_human_output_is_concise_and_json_is_strict(tmp_path: Path):
    graph = _prelock(tmp_path)
    human = _run_main(graph, "--indexer", "python", "--offline-confirmed")
    assert human.returncode == 0, human.stderr
    assert human.stdout.startswith("adopt-publication-lock: adopted ")
    assert "graph=" in human.stdout
    machine = _run_main(graph, "--indexer", "python", "--offline-confirmed", "--json")
    body = json.loads(machine.stdout)
    assert list(body) == sorted(body)
    assert machine.stdout.endswith("\n")
    json.loads(
        machine.stdout,
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
