"""Cooperative per-staging writer-lease protocol.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_staging_writer_lease.py -q
"""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    StagingWriterLockContention,
    StagingWriterLockUnsafe,
    _try_acquire_exclusive_lock,
    is_published_snapshot_id,
    probe_staging_writer_lease,
    publish_byog_snapshot,
    staging_writer_lease,
)
from byog_snapshot_graph_audit import audit_graph_root  # type: ignore
from graphrag_code.mcp_server import TOOL_NAMES  # type: ignore
from graphrag_code.snapshot_staging import snapshot_staging  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")


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
            if process.is_alive():
                process.kill()
                process.join(timeout=5)


def _rows(marker: str):
    ents = [
        {
            "id": f"ent:{marker}",
            "title": f"demo:{marker}",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
            "description": f"desc-{marker}",
        }
    ]
    rels = [
        {
            "id": f"rel:{marker}",
            "source": f"demo:{marker}.py",
            "target": f"demo:{marker}",
            "type": "contains",
            "extractor": "tree-sitter-python",
        }
    ]
    tus = [
        {
            "id": f"tu:{marker}",
            "title": f"{marker}.py",
            "source_file": f"{marker}.py",
            "entity_id": f"ent:{marker}",
        }
    ]
    return ents, rels, tus


def _publish(graph: Path, marker: str, *, keep_last: int = 10) -> Path:
    ents, rels, tus = _rows(marker)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"writer-lease: {marker}\n",
        keep_last=keep_last,
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _staging_dirs(graph: Path) -> list[Path]:
    snaps = graph / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(
        path
        for path in snaps.iterdir()
        if path.name.startswith(STAGING_NAME_PREFIX)
    )


def _entry(result: dict, name: str) -> dict:
    matches = [item for item in result["staging_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def _paused_publisher(graph: str, marker: str, paused, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    orig = byog._after_staging_writer_lease

    def wrapped(stage_dir):
        paused.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
            raise RuntimeError("publisher resume timed out")
        return orig(stage_dir)

    byog._after_staging_writer_lease = wrapped
    ents, rels, tus = _rows(marker)
    try:
        snap = byog.publish_byog_snapshot(
            pd.DataFrame(ents),
            pd.DataFrame(rels),
            pd.DataFrame(tus),
            ChildPath(graph),
            settings_text=f"writer-lease: {marker}\n",
            keep_last=10,
        )
        q.put(("pub", snap.name))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _failing_publisher(graph: str, marker: str, paused, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    orig_after = byog._after_staging_writer_lease
    orig_write = byog._write_snapshot_payload

    def after(stage_dir):
        paused.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
            raise RuntimeError("publisher resume timed out")
        return orig_after(stage_dir)

    def boom(*args, **kwargs):
        raise RuntimeError("payload construction failed")

    byog._after_staging_writer_lease = after
    byog._write_snapshot_payload = boom
    ents, rels, tus = _rows(marker)
    try:
        byog.publish_byog_snapshot(
            pd.DataFrame(ents),
            pd.DataFrame(rels),
            pd.DataFrame(tus),
            ChildPath(graph),
            keep_last=10,
        )
        q.put("unexpected-success")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _publish_worker(graph: str, marker: str, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    ents, rels, tus = _rows(marker)
    try:
        snap = byog.publish_byog_snapshot(
            pd.DataFrame(ents),
            pd.DataFrame(rels),
            pd.DataFrame(tus),
            ChildPath(graph),
            settings_text=f"writer-lease: {marker}\n",
            keep_last=10,
        )
        q.put(("pub", snap.name))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_live_publisher_reports_held_writer_lease(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "base")
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    pub = CTX.Process(
        target=_paused_publisher, args=(str(graph), "live", paused, resume, q)
    )
    try:
        pub.start()
        assert paused.wait(timeout=TIMEOUT)
        staging = _staging_dirs(graph)
        assert len(staging) == 1
        lock = staging[0] / STAGING_WRITER_LOCK_NAME
        assert lock.is_file() and not lock.is_symlink()
        started = time.monotonic()
        result = snapshot_staging(graph)
        elapsed = time.monotonic() - started
        assert elapsed < 5
        entry = _entry(result, staging[0].name)
        assert entry["writer_lease_protocol"] == "cooperative_v1"
        assert entry["writer_lease_state"] == "held_by_cooperating_writer"
        assert entry["writer_lock_present"] is True
        assert entry["writer_lock_regular"] is True
        assert entry["ownership_status"] == "unknown"
        assert entry["cleanup_eligible"] is False
        assert result["schema_version"] == 2
        resume.set()
        pub.join(timeout=TIMEOUT)
        assert not pub.is_alive()
        published = q.get(timeout=TIMEOUT)
        assert published[0] == "pub"
        final = graph / "snapshots" / published[1]
        assert final.is_dir()
        assert not _staging_dirs(graph)
        assert not (final / STAGING_WRITER_LOCK_NAME).exists()
        manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
        assert STAGING_WRITER_LOCK_NAME not in manifest["files"]
        assert is_published_snapshot_id(final.name)
        assert _current(graph) == final.name
        assert first.name != final.name
        assert audit_graph_root(graph)["ok"] is True
    finally:
        _cleanup_processes(pub, release=resume)


def _paused_publisher_events_only(graph: str, marker: str, paused, resume) -> None:
    """Like ``_paused_publisher`` but without a Queue so kill cannot hang teardown."""
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    orig = byog._after_staging_writer_lease

    def wrapped(stage_dir):
        paused.set()
        resume.wait(timeout=TIMEOUT)
        return orig(stage_dir)

    byog._after_staging_writer_lease = wrapped
    ents, rels, tus = _rows(marker)
    byog.publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        ChildPath(graph),
        settings_text=f"writer-lease: {marker}\n",
        keep_last=10,
    )


def test_killed_writer_leaves_lock_and_reports_not_held(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    paused = CTX.Event()
    resume = CTX.Event()
    pub = CTX.Process(
        target=_paused_publisher_events_only,
        args=(str(graph), "crash", paused, resume),
    )
    try:
        pub.start()
        assert paused.wait(timeout=TIMEOUT)
        staging = _staging_dirs(graph)
        assert len(staging) == 1
        lock = staging[0] / STAGING_WRITER_LOCK_NAME
        assert lock.is_file() and not lock.is_symlink()
        os.kill(pub.pid, 9)
        pub.join(timeout=TIMEOUT)
        assert not pub.is_alive()
        assert staging[0].is_dir()
        assert lock.is_file()
        result = snapshot_staging(graph)
        entry = _entry(result, staging[0].name)
        assert entry["writer_lease_protocol"] == "cooperative_v1"
        assert entry["writer_lease_state"] == "not_held_at_scan"
        assert entry["writer_lock_present"] is True
        assert entry["writer_lock_regular"] is True
        assert entry["ownership_status"] == "unknown"
        assert entry["cleanup_eligible"] is False
        lowered = json.dumps(result).lower()
        for word in FORBIDDEN_WORDS:
            assert word not in lowered
    finally:
        if pub.is_alive():
            os.kill(pub.pid, 9)
        pub.join(timeout=5)


def test_legacy_and_creation_window_are_unverifiable(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    legacy = graph / "snapshots" / f"{STAGING_NAME_PREFIX}20240101-000000-legacy"
    legacy.mkdir()
    (legacy / "manifest.json").write_text("{}\n", encoding="utf-8")
    result = snapshot_staging(graph)
    entry = _entry(result, legacy.name)
    assert entry["writer_lease_protocol"] == "legacy_absent"
    assert entry["writer_lease_state"] == "unverifiable"
    assert entry["writer_lock_present"] is False
    assert entry["writer_lock_regular"] is False
    assert entry["ownership_status"] == "unknown"
    assert entry["cleanup_eligible"] is False
    assert any(
        notice["code"] == "writer_lock_legacy_absent" for notice in entry["notices"]
    )


def _lease_holder(stage_dir: str, held, done, queue) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import staging_writer_lease as hold

    try:
        with hold(ChildPath(stage_dir)):
            held.set()
            if not done.wait(timeout=TIMEOUT):
                queue.put("timeout")
                return
            queue.put("held")
    except Exception as exc:
        queue.put(f"error:{type(exc).__name__}:{exc}")


def test_diagnostic_probe_is_nonblocking(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}20240101-000000-block"
    staging.mkdir()
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    proc = CTX.Process(target=_lease_holder, args=(str(staging), paused, resume, q))
    try:
        proc.start()
        assert paused.wait(timeout=TIMEOUT)
        started = time.monotonic()
        observation = probe_staging_writer_lease(staging)
        elapsed = time.monotonic() - started
        assert elapsed < 2
        assert observation["writer_lease_state"] == "held_by_cooperating_writer"
        fd = os.open(str(staging / STAGING_WRITER_LOCK_NAME), os.O_RDONLY)
        try:
            with pytest.raises(StagingWriterLockContention):
                _try_acquire_exclusive_lock(fd)
        finally:
            os.close(fd)
    finally:
        _cleanup_processes(proc, release=resume)


def test_existing_managed_writer_reacquisition_is_nonblocking(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}20240101-000000-reacquire"
    staging.mkdir()
    (staging / STAGING_WRITER_LOCK_NAME).write_bytes(b"")
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    proc = CTX.Process(target=_lease_holder, args=(str(staging), held, resume, q))
    try:
        proc.start()
        assert held.wait(timeout=TIMEOUT)
        started = time.monotonic()
        with pytest.raises(StagingWriterLockContention):
            with staging_writer_lease(staging):
                pytest.fail("contended managed reacquisition must not block")
        assert time.monotonic() - started < 2
    finally:
        _cleanup_processes(proc, release=resume)


def test_writer_lock_create_race_and_pre_promotion_disappearance_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as byog

    staging = tmp_path / f"{STAGING_NAME_PREFIX}20240101-000000-create-race"
    staging.mkdir()
    lock = staging / STAGING_WRITER_LOCK_NAME
    original_open = os.open
    raced = {"done": False}

    def insert_before_exclusive_create(path, flags, *args, **kwargs):
        if Path(path) == lock and not raced["done"]:
            raced["done"] = True
            lock.write_bytes(b"foreign")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(byog.os, "open", insert_before_exclusive_create)
    with pytest.raises(StagingWriterLockUnsafe, match="changed|unsafe"):
        with staging_writer_lease(staging):
            pytest.fail("raced lock pathname must not be adopted")
    assert raced["done"] is True
    assert lock.read_bytes() == b"foreign"

    monkeypatch.setattr(byog.os, "open", original_open)
    lock.unlink()
    with staging_writer_lease(staging) as held:
        lock.unlink()
        with pytest.raises(StagingWriterLockUnsafe, match="disappeared before promotion"):
            held.release_and_remove()

    portable = tmp_path / f"{STAGING_NAME_PREFIX}20240101-000000-no-nofollow"
    portable.mkdir()
    monkeypatch.delattr(byog.os, "O_NOFOLLOW", raising=False)
    with staging_writer_lease(portable):
        pass
    observation = probe_staging_writer_lease(portable)
    assert observation["writer_lease_protocol"] == "cooperative_v1"
    assert observation["writer_lease_state"] == "not_held_at_scan"


def test_publisher_exception_releases_lease_and_removes_own_staging(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "base")
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    pub = CTX.Process(
        target=_failing_publisher, args=(str(graph), "boom", paused, resume, q)
    )
    other = graph / "snapshots" / f"{STAGING_NAME_PREFIX}foreign"
    other.mkdir()
    (other / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        pub.start()
        assert paused.wait(timeout=TIMEOUT)
        live = [path for path in _staging_dirs(graph) if path.name != other.name]
        assert len(live) == 1
        resume.set()
        pub.join(timeout=TIMEOUT)
        assert not pub.is_alive()
        message = q.get(timeout=TIMEOUT)
        assert "payload construction failed" in str(message)
        assert not any(path.name != other.name for path in _staging_dirs(graph))
        assert other.is_dir()
        assert (other / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert (graph / "snapshots" / first.name).is_dir()
        assert _current(graph) == first.name
    finally:
        _cleanup_processes(pub, release=resume)


def test_concurrent_publishers_keep_unique_staging_and_publish(tmp_path: Path):
    graph = tmp_path / "g"
    q = CTX.Queue()
    procs = [
        CTX.Process(target=_publish_worker, args=(str(graph), f"p{index}", q))
        for index in range(3)
    ]
    try:
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=TIMEOUT)
            assert not proc.is_alive()
        names = {q.get(timeout=TIMEOUT)[1] for _ in procs}
        assert len(names) == 3
        assert _current(graph) in names
        for name in names:
            snap = graph / "snapshots" / name
            assert snap.is_dir()
            assert not (snap / STAGING_WRITER_LOCK_NAME).exists()
            manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
            assert STAGING_WRITER_LOCK_NAME not in manifest["files"]
        assert not _staging_dirs(graph)
        assert audit_graph_root(graph)["ok"] is True
    finally:
        _cleanup_processes(*procs)


def test_mcp_remains_exactly_seventeen_and_has_no_staging_tool():
    assert len(TOOL_NAMES) == 17
    assert "snapshot_staging" not in TOOL_NAMES
    assert "staging_writer" not in " ".join(TOOL_NAMES)


def test_source_does_not_claim_cleanup_or_death():
    import ast

    source_path = ROOT / "src" / "graphrag_code" / "snapshot_staging.py"
    source = source_path.read_text(encoding="utf-8")
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert "probe_staging_writer_lease" in imported
    assert "staging_writer_lease" not in imported
    assert "staging_writer_lease" not in called
    assert "LOCK_NB" not in source
    backend = (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(
        encoding="utf-8"
    )
    assert "LOCK_NB" in backend
    assert "LOCKFILE_FAIL_IMMEDIATELY" in backend
    assert "STAGING_WRITER_LOCK_NAME" in backend
