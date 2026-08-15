"""Shared-reader leases on graph-root ``.publish.lock``.

Disposable graphs only. No timing sleeps as the primary assertion.

Run:
  uv run python -m pytest examples/mini_game/tests/test_reader_lease.py -q
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    ByogGraph,
    ByogPublicationLockError,
    ByogReaderLockError,
    _publication_lock,
    graph_read_lease,
    is_managed_snapshot_layout,
    publish_byog_snapshot,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 20


def _cleanup_processes(*processes, release=None) -> None:
    """Release waiters and reap children even when a concurrency assertion fails."""
    if release is not None:
        release.set()
    for process in processes:
        if process.pid is None:
            continue
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def _tables(marker: str = "a"):
    ents = pd.DataFrame(
        [{"id": f"ent:{marker}", "title": marker, "type": "function", "source_file": "d.py"}]
    )
    rels = pd.DataFrame(
        [{"id": f"rel:{marker}", "source": marker, "target": marker, "type": "contains"}]
    )
    tus = pd.DataFrame(
        [{"id": f"tu:{marker}", "title": marker, "source_file": "d.py", "entity_id": f"ent:{marker}"}]
    )
    return ents, rels, tus


def _publish(graph: Path, marker: str, keep_last: int = 5) -> Path:
    ents, rels, tus = _tables(marker)
    return publish_byog_snapshot(ents, rels, tus, graph, keep_last=keep_last)


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _hashes(graph: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(graph.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(graph).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _stats(graph: Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for path in sorted(graph.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.lstat()
            out[path.relative_to(graph).as_posix()] = (
                info.st_size,
                info.st_mtime_ns,
                stat.S_IMODE(info.st_mode),
            )
    return out


def _reader_hold(graph: str, held, resume, raised, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "src"))
    from graphrag_code.byog_graph import graph_read_lease

    try:
        with graph_read_lease(Path(graph)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("ok")
    except Exception as exc:
        raised.set()
        q.put(f"error:{type(exc).__name__}:{exc}")


def _reader_boom(graph: str, held, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "src"))
    from graphrag_code.byog_graph import graph_read_lease

    try:
        with graph_read_lease(Path(graph)):
            held.set()
            raise RuntimeError("reader failed")
    except RuntimeError:
        q.put("raised")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _publisher(graph: str, marker: str, keep_last: int, about_to_lock, got_lock, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "src"))
    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about_to_lock.set()
        backend = orig(fd)
        got_lock.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    ents, rels, tus = (
        pd.DataFrame(
            [{"id": f"ent:{marker}", "title": marker, "type": "function", "source_file": "d.py"}]
        ),
        pd.DataFrame(
            [{"id": f"rel:{marker}", "source": marker, "target": marker, "type": "contains"}]
        ),
        pd.DataFrame(
            [{"id": f"tu:{marker}", "title": marker, "source_file": "d.py", "entity_id": f"ent:{marker}"}]
        ),
    )
    snap = byog.publish_byog_snapshot(ents, rels, tus, Path(graph), keep_last=keep_last)
    q.put(snap.name)


def test_two_readers_hold_shared_leases(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    held_a = CTX.Event()
    held_b = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    raised_a = CTX.Event()
    raised_b = CTX.Event()
    a = CTX.Process(target=_reader_hold, args=(str(graph), held_a, resume, raised_a, q))
    b = CTX.Process(target=_reader_hold, args=(str(graph), held_b, resume, raised_b, q))
    try:
        a.start()
        b.start()
        assert held_a.wait(timeout=TIMEOUT)
        assert held_b.wait(timeout=TIMEOUT)
        resume.set()
        a.join(timeout=TIMEOUT)
        b.join(timeout=TIMEOUT)
        assert not a.is_alive() and not b.is_alive()
        assert {q.get(timeout=5), q.get(timeout=5)} == {"ok", "ok"}
    finally:
        _cleanup_processes(a, b, release=resume)


def test_publisher_cannot_promote_while_reader_holds(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a", keep_last=1)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    raised = CTX.Event()
    reader = CTX.Process(target=_reader_hold, args=(str(graph), held, resume, raised, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "b", 1, about, got, q))
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == first.name
        assert (first / "entities.parquet").is_file()
        resume.set()
        pub.join(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not reader.is_alive()
        assert got.is_set()
        results = {q.get(timeout=5), q.get(timeout=5)}
        assert "ok" in results
        second = next(item for item in results if item != "ok")
        assert _current(graph) == second
        assert not first.exists()
    finally:
        _cleanup_processes(pub, reader, release=resume)


def test_keep_last_one_waits_for_reader_then_retains(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old", keep_last=1)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    raised = CTX.Event()
    reader = CTX.Process(target=_reader_hold, args=(str(graph), held, resume, raised, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 1, about, got, q))
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert _current(graph) == first.name
        assert ByogGraph(first).ents.iloc[0]["title"] == "old"
        resume.set()
        reader.join(timeout=TIMEOUT)
        pub.join(timeout=TIMEOUT)
        assert not reader.is_alive() and not pub.is_alive()
        assert _current(graph) != first.name
        assert not first.exists()
    finally:
        _cleanup_processes(pub, reader, release=resume)


def test_reader_exception_releases_lock(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a", keep_last=1)
    held = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_reader_boom, args=(str(graph), held, q))
    about = CTX.Event()
    got = CTX.Event()
    pub = CTX.Process(target=_publisher, args=(str(graph), "b", 1, about, got, q))
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not reader.is_alive()
        assert q.get(timeout=5) == "raised"
        pub.start()
        pub.join(timeout=TIMEOUT)
        assert not pub.is_alive()
        assert got.is_set()
        assert q.get(timeout=5)
    finally:
        _cleanup_processes(reader, pub)


def test_unsafe_lock_paths_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    before = _hashes(graph)
    with pytest.raises(ByogReaderLockError, match="lock is missing"):
        with graph_read_lease(graph):
            raise AssertionError("must not enter")
    # Explicit compatibility reads of immutable pre-lock evidence remain
    # possible, but do not claim a retention lease.
    with graph_read_lease(graph, allow_unlocked_managed=True):
        assert ByogGraph(graph).ents.iloc[0]["title"] == "a"
    assert not lock.exists()
    assert _hashes(graph) == before

    _publish(graph, "b")
    lock.unlink()
    lock.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ByogReaderLockError, match="symlink"):
        with graph_read_lease(graph):
            raise AssertionError("must not enter")
    assert lock.is_symlink()
    snap = graph / "snapshots" / _current(graph)
    assert (snap / "entities.parquet").is_file()

    lock.unlink()
    lock.mkdir()
    with pytest.raises(ByogReaderLockError, match="regular file"):
        with graph_read_lease(graph):
            raise AssertionError("must not enter")
    assert lock.is_dir()
    assert (snap / "entities.parquet").is_file()


def test_incomplete_and_symlinked_managed_layouts_fail_closed(tmp_path: Path):
    partial = tmp_path / "partial"
    (partial / "snapshots").mkdir(parents=True)
    with pytest.raises(ByogReaderLockError, match="incomplete managed"):
        with graph_read_lease(partial):
            raise AssertionError("must not enter")

    bad_current = tmp_path / "bad-current"
    (bad_current / "snapshots").mkdir(parents=True)
    (bad_current / "target").write_text("snapshot")
    (bad_current / "current").symlink_to(bad_current / "target")
    with pytest.raises(ByogReaderLockError, match="symlinked current"):
        with graph_read_lease(bad_current):
            raise AssertionError("must not enter")

    bad_snapshots = tmp_path / "bad-snapshots"
    bad_snapshots.mkdir()
    (bad_snapshots / "current").write_text("snapshot")
    (bad_snapshots / "real-snapshots").mkdir()
    (bad_snapshots / "snapshots").symlink_to(bad_snapshots / "real-snapshots")
    with pytest.raises(ByogReaderLockError, match="symlinked snapshots"):
        with graph_read_lease(bad_snapshots):
            raise AssertionError("must not enter")


def test_unsupported_backend_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = tmp_path / "g"
    _publish(graph, "a")
    before = _hashes(graph)
    monkeypatch.setattr("graphrag_code.byog_graph._available_lock_backend", lambda: None)
    with pytest.raises(ByogReaderLockError):
        with graph_read_lease(graph):
            raise AssertionError("must not enter")
    assert _hashes(graph) == before


def test_legacy_flat_layout_is_noop_and_creates_no_lock(tmp_path: Path):
    flat = tmp_path / "flat"
    ents, rels, tus = _tables("flat")
    flat.mkdir()
    ents.to_parquet(flat / "entities.parquet")
    rels.to_parquet(flat / "relationships.parquet")
    tus.to_parquet(flat / "text_units.parquet")
    assert not is_managed_snapshot_layout(flat)
    with graph_read_lease(flat):
        g = ByogGraph(flat)
        assert g.ents.iloc[0]["title"] == "flat"
    assert not (flat / PUBLICATION_LOCK_NAME).exists()


def test_reader_does_not_mutate_graph_metadata(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    before_h = _hashes(graph)
    before_s = _stats(graph)
    listing = sorted(p.name for p in (graph / "snapshots").iterdir())
    current = _current(graph)
    ByogGraph(graph)
    assert _hashes(graph) == before_h
    assert _stats(graph) == before_s
    assert sorted(p.name for p in (graph / "snapshots").iterdir()) == listing
    assert _current(graph) == current


def test_exclusive_lock_still_serializes_publishers(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "seed")
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    pub = CTX.Process(target=_publisher, args=(str(graph), "x", 5, about, got, q))
    try:
        with _publication_lock(graph):
            pub.start()
            assert about.wait(timeout=TIMEOUT)
            assert not got.is_set()
        pub.join(timeout=TIMEOUT)
        assert not pub.is_alive()
        assert got.is_set()
        assert q.get(timeout=5)
    finally:
        _cleanup_processes(pub)
