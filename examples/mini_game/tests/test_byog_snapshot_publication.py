"""Concurrent publication and retention for BYOG snapshots.

Disposable graphs only. No timing-dependent sleeps. Workers are separate
processes coordinated with multiprocessing Events.

Run:
  uv run python -m pytest examples/mini_game/tests/test_byog_snapshot_publication.py -q
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    ByogPublicationLockError,
    STAGING_NAME_PREFIX,
    StagingWriterLeaseError,
    _atomic_write_parquet,
    _atomic_write_text,
    cleanup_old_snapshots,
    is_published_snapshot_id,
    is_staging_snapshot_name,
    publish_byog_snapshot,
)
from byog_snapshot_graph_audit import (  # type: ignore
    audit_graph_root,
    main as audit_main,
)

CTX = multiprocessing.get_context("spawn")
REQUIRED_FINAL = {
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
    "manifest.json",
}


def _tiny_tables(*, n_obs: int = 1, marker: str = "x"):
    ents = pd.DataFrame(
        [{"id": f"ent:{marker}", "title": marker, "type": "function", "source_file": "d.py"}]
    )
    rels = pd.DataFrame(
        [{"id": f"rel:{marker}", "source": marker, "target": marker, "type": "contains"}]
    )
    tus = pd.DataFrame(
        [{"id": f"tu:{marker}", "title": marker, "source_file": "d.py", "entity_id": f"ent:{marker}"}]
    )
    obs = None
    if n_obs:
        obs = pd.DataFrame([{"id": f"obs:{marker}:{i}", "caller": marker} for i in range(n_obs)])
    return ents, rels, tus, obs


def _write_payload(path: Path, *, marker: str = "x", n_obs: int = 1) -> None:
    ents, rels, tus, obs = _tiny_tables(n_obs=n_obs, marker=marker)
    path.mkdir(parents=True, exist_ok=True)
    ents.to_parquet(path / "entities.parquet")
    rels.to_parquet(path / "relationships.parquet")
    tus.to_parquet(path / "text_units.parquet")
    if obs is not None:
        obs.to_parquet(path / "call_observations.parquet")


def _load_payload(path: Path):
    path = Path(path)
    ents = pd.read_parquet(path / "entities.parquet")
    rels = pd.read_parquet(path / "relationships.parquet")
    tus = pd.read_parquet(path / "text_units.parquet")
    obs_path = path / "call_observations.parquet"
    obs = pd.read_parquet(obs_path) if obs_path.is_file() else None
    return ents, rels, tus, obs


def _published_dirs(graph: Path) -> list[Path]:
    snaps = Path(graph) / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(
        d for d in snaps.iterdir() if d.is_dir() and is_published_snapshot_id(d.name)
    )


def _staging_dirs(graph: Path) -> list[Path]:
    snaps = Path(graph) / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(d for d in snaps.iterdir() if d.is_dir() and is_staging_snapshot_name(d.name))


def _assert_complete(snap: Path) -> None:
    names = {p.name for p in snap.iterdir() if p.is_file()}
    missing = REQUIRED_FINAL - names
    assert not missing, (snap, missing)


def _current_id(graph: Path) -> str:
    return (Path(graph) / "current").read_text(encoding="utf-8").strip()


def _legacy_cleanup(out_root: Path, keep_last: int = 1) -> int:
    """Pre-fix retention: every snapshots/ directory is a candidate."""
    keep_last = max(1, keep_last)
    snapshots_dir = Path(out_root) / "snapshots"
    if not snapshots_dir.exists():
        return 0
    current_file = Path(out_root) / "current"
    current_id = None
    if current_file.exists():
        current_id = current_file.read_text(encoding="utf-8").strip()
    snap_dirs = [d for d in snapshots_dir.iterdir() if d.is_dir()]
    snap_dirs.sort(key=lambda p: p.name)
    keep: set[Path] = set()
    if current_id:
        current_dir = snapshots_dir / current_id
        if current_dir.exists():
            keep.add(current_dir)
    slots_left = max(0, keep_last - len(keep))
    if slots_left > 0:
        candidates = [d for d in snap_dirs if d not in keep]
        keep.update(candidates[-slots_left:])
    deleted = 0
    for d in snap_dirs:
        if d not in keep:
            shutil.rmtree(d)
            deleted += 1
    return deleted


def _legacy_publish(
    out_root: Path,
    payload: Path,
    *,
    keep_last: int,
    pause_after: str | None = None,
    paused=None,
    resume=None,
) -> str:
    """Pre-fix publisher: writes directly into snapshots/<id>."""
    ents, rels, tus, obs = _load_payload(payload)
    out_root = Path(out_root)
    snapshots_dir = out_root / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    import uuid

    snap_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    snap_dir = snapshots_dir / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    steps = [
        ("entities.parquet", lambda: _atomic_write_parquet(ents, snap_dir / "entities.parquet")),
        ("relationships.parquet", lambda: _atomic_write_parquet(rels, snap_dir / "relationships.parquet")),
        ("text_units.parquet", lambda: _atomic_write_parquet(tus, snap_dir / "text_units.parquet")),
    ]
    if obs is not None and len(obs) > 0:
        steps.append(
            (
                "call_observations.parquet",
                lambda: _atomic_write_parquet(obs, snap_dir / "call_observations.parquet"),
            )
        )
    for name, write in steps:
        write()
        if pause_after == name:
            assert paused is not None and resume is not None
            paused.set()
            resume.wait(timeout=30)
    files_list = ["entities.parquet", "relationships.parquet", "text_units.parquet"]
    if obs is not None and len(obs) > 0:
        files_list.append("call_observations.parquet")
    manifest = {
        "id": snap_id,
        "created_at": "2026-08-14T00:00:00",
        "schema_version": 1,
        "counts": {
            "entities": len(ents),
            "relationships": len(rels),
            "text_units": len(tus),
            "call_observations": len(obs) if obs is not None else 0,
        },
        "files": files_list,
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    _atomic_write_text(json.dumps(manifest), snap_dir / "manifest.json")
    _atomic_write_text(snap_id, out_root / "current")
    _legacy_cleanup(out_root, keep_last=keep_last)
    return snap_id


def _legacy_worker_a(graph: str, payload: str, paused, resume, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))
    sid = _legacy_publish(
        Path(graph),
        Path(payload),
        keep_last=5,
        pause_after="entities.parquet",
        paused=paused,
        resume=resume,
    )
    q.put(("A", sid))


def _legacy_worker_b(graph: str, payload: str, paused, resume, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))
    if not paused.wait(timeout=30):
        q.put(("B", "timeout"))
        return
    sid = _legacy_publish(Path(graph), Path(payload), keep_last=1)
    resume.set()
    q.put(("B", sid))


def _publish_worker(graph: str, payload: str, keep_last: int, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))
    from byog_graph import publish_byog_snapshot as publish

    ents, rels, tus, obs = _load_payload(payload)
    snap = publish(
        ents,
        rels,
        tus,
        Path(graph),
        settings_text="k: 1\n",
        keep_last=keep_last,
        call_observations_df=obs,
    )
    q.put(snap.name)


def _pausing_publish_worker(graph: str, payload: str, keep_last: int, paused, resume, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))
    import byog_graph

    orig = byog_graph._atomic_write_parquet

    def wrapped(df, path):
        orig(df, path)
        if Path(path).name == "entities.parquet":
            paused.set()
            resume.wait(timeout=30)

    byog_graph._atomic_write_parquet = wrapped
    ents, rels, tus, obs = _load_payload(payload)
    snap = byog_graph.publish_byog_snapshot(
        ents,
        rels,
        tus,
        Path(graph),
        settings_text="k: 1\n",
        keep_last=keep_last,
        call_observations_df=obs,
    )
    q.put(snap.name)


def _cleanup_only_worker(graph: str, keep_last: int, q) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3] / "scripts"))
    from byog_graph import cleanup_old_snapshots as cleanup

    q.put(cleanup(Path(graph), keep_last=keep_last))


def test_legacy_interleaving_can_publish_incomplete_current(tmp_path: Path):
    """The pre-fix writer-A/writer-B deletion race is deterministic and harmful."""
    graph = tmp_path / "race"
    payload = tmp_path / "payload"
    _write_payload(payload, marker="legacy")
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    a = CTX.Process(target=_legacy_worker_a, args=(str(graph), str(payload), paused, resume, q))
    b = CTX.Process(target=_legacy_worker_b, args=(str(graph), str(payload), paused, resume, q))
    a.start()
    b.start()
    a.join(timeout=30)
    b.join(timeout=30)
    assert not a.is_alive() and not b.is_alive()
    results = {q.get(timeout=5), q.get(timeout=5)}
    by_role = dict(results)
    assert by_role["A"] != "timeout" and by_role["B"] != "timeout"
    current = _current_id(graph)
    assert current == by_role["A"]
    a_dir = graph / "snapshots" / current
    assert a_dir.is_dir()
    assert not (a_dir / "entities.parquet").is_file()
    assert (a_dir / "manifest.json").is_file()
    assert audit_main(["--graph", str(graph)]) in {1, 2}


def test_two_concurrent_publishers_keep_last(tmp_path: Path):
    for keep_last in (1, 2):
        graph = tmp_path / f"conc_{keep_last}"
        p1 = tmp_path / f"p1_{keep_last}"
        p2 = tmp_path / f"p2_{keep_last}"
        _write_payload(p1, marker=f"a{keep_last}")
        _write_payload(p2, marker=f"b{keep_last}")
        q = CTX.Queue()
        procs = [
            CTX.Process(target=_publish_worker, args=(str(graph), str(p1), keep_last, q)),
            CTX.Process(target=_publish_worker, args=(str(graph), str(p2), keep_last, q)),
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=30)
            assert not proc.is_alive()
        published = {q.get(timeout=5), q.get(timeout=5)}
        current = _current_id(graph)
        assert current in published
        survivors = _published_dirs(graph)
        assert {d.name for d in survivors} <= published
        assert len(survivors) <= keep_last
        assert any(d.name == current for d in survivors)
        for snap in survivors:
            _assert_complete(snap)
        assert not _staging_dirs(graph)
        report = audit_graph_root(graph)
        assert report["ok"] is True, report["anomalies"]
        assert report["directory_identity"] == "matched"


def test_pausing_writer_is_not_deleted_by_concurrent_publish(tmp_path: Path):
    graph = tmp_path / "pause"
    pa = tmp_path / "pa"
    pb = tmp_path / "pb"
    _write_payload(pa, marker="slow")
    _write_payload(pb, marker="fast")
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    a = CTX.Process(
        target=_pausing_publish_worker,
        args=(str(graph), str(pa), 1, paused, resume, q),
    )
    b = CTX.Process(target=_publish_worker, args=(str(graph), str(pb), 1, q))
    a.start()
    assert paused.wait(timeout=30)
    b.start()
    b.join(timeout=30)
    assert not b.is_alive()
    resume.set()
    a.join(timeout=30)
    assert not a.is_alive()
    ids = {q.get(timeout=5), q.get(timeout=5)}
    current = _current_id(graph)
    assert current in ids
    for snap in _published_dirs(graph):
        _assert_complete(snap)
    assert not _staging_dirs(graph)
    assert audit_graph_root(graph)["ok"] is True


def test_standalone_retention_ignores_active_staging(tmp_path: Path):
    graph = tmp_path / "ret"
    payload = tmp_path / "pr"
    _write_payload(payload, marker="base")
    publish_byog_snapshot(*_load_payload(payload)[:3], graph, keep_last=2)
    first = _current_id(graph)
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    writer = CTX.Process(
        target=_pausing_publish_worker,
        args=(str(graph), str(payload), 1, paused, resume, q),
    )
    writer.start()
    assert paused.wait(timeout=30)
    staging_before = {d.name for d in _staging_dirs(graph)}
    assert staging_before
    cleaner = CTX.Process(target=_cleanup_only_worker, args=(str(graph), 1, q))
    cleaner.start()
    cleaner.join(timeout=30)
    assert not cleaner.is_alive()
    assert {d.name for d in _staging_dirs(graph)} == staging_before
    assert (graph / "snapshots" / first).is_dir()
    resume.set()
    writer.join(timeout=30)
    assert not writer.is_alive()
    assert audit_graph_root(graph)["ok"] is True
    assert not _staging_dirs(graph)


def test_failures_leave_prior_current_and_only_private_artifacts(tmp_path: Path, monkeypatch):
    graph = tmp_path / "fail"
    ents, rels, tus, obs = _tiny_tables(marker="ok")
    first = publish_byog_snapshot(
        ents, rels, tus, graph, settings_text="ok: 1\n", call_observations_df=obs, keep_last=3
    )
    prior = first.name
    prior_bytes = {
        p.relative_to(graph).as_posix(): p.read_bytes()
        for p in graph.rglob("*")
        if p.is_file() and p.name != ".publish.lock"
    }

    def _assert_prior_intact() -> None:
        assert _current_id(graph) == prior
        assert {d.name for d in _published_dirs(graph)} == {prior}
        assert not _staging_dirs(graph)
        after = {
            p.relative_to(graph).as_posix(): p.read_bytes()
            for p in graph.rglob("*")
            if p.is_file() and p.name != ".publish.lock"
        }
        for key, value in prior_bytes.items():
            assert after.get(key) == value, key
        for snap in _published_dirs(graph):
            _assert_complete(snap)

    import byog_graph

    orig_parquet = byog_graph._atomic_write_parquet
    orig_text = byog_graph._atomic_write_text
    orig_rename = os.rename

    def fail_named(target: str, kind: str):
        def parquet(df, path):
            if Path(path).name == target:
                raise RuntimeError(f"fail {target}")
            return orig_parquet(df, path)

        def text(text, path):
            if Path(path).name == target:
                raise RuntimeError(f"fail {target}")
            return orig_text(text, path)

        def rename(src, dst):
            if kind == "rename" and is_staging_snapshot_name(Path(src).name):
                raise OSError("fail promotion")
            return orig_rename(src, dst)

        if kind == "parquet":
            monkeypatch.setattr(byog_graph, "_atomic_write_parquet", parquet)
        elif kind == "text":
            monkeypatch.setattr(byog_graph, "_atomic_write_text", text)
        else:
            monkeypatch.setattr(os, "rename", rename)

    cases = [
        ("entities.parquet", "parquet"),
        ("relationships.parquet", "parquet"),
        ("text_units.parquet", "parquet"),
        ("call_observations.parquet", "parquet"),
        ("settings.yaml", "text"),
        ("manifest.json", "text"),
        ("promote", "rename"),
        ("current", "text"),
    ]
    fresh = _tiny_tables(marker="new", n_obs=1)
    for target, kind in cases:
        fail_named(target, kind)
        with pytest.raises((RuntimeError, OSError), match="fail"):
            publish_byog_snapshot(
                fresh[0],
                fresh[1],
                fresh[2],
                graph,
                settings_text="new: 1\n",
                call_observations_df=fresh[3],
                keep_last=3,
            )
        _assert_prior_intact()
        monkeypatch.undo()


def test_pinned_and_keep_last_clamp(tmp_path: Path, monkeypatch):
    import byog_graph

    ents, rels, tus, obs = _tiny_tables(marker="p")
    graph = tmp_path / "byog_sqlparse"
    first = publish_byog_snapshot(ents, rels, tus, graph, keep_last=5, call_observations_df=obs)
    second = publish_byog_snapshot(ents, rels, tus, graph, keep_last=5, call_observations_df=obs)
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {first.name})
    deleted = cleanup_old_snapshots(graph, keep_last=0)
    survivors = {d.name for d in _published_dirs(graph)}
    assert first.name in survivors
    assert second.name in survivors
    assert deleted == 0
    third = publish_byog_snapshot(ents, rels, tus, graph, keep_last=1, call_observations_df=obs)
    survivors = {d.name for d in _published_dirs(graph)}
    assert first.name in survivors
    assert third.name in survivors
    assert second.name not in survivors


def test_staging_names_are_not_published_ids(tmp_path: Path, capsys):
    assert is_staging_snapshot_name(".staging-20260814-000000-abcd1234")
    assert not is_published_snapshot_id(".staging-20260814-000000-abcd1234")
    assert not is_published_snapshot_id(".publish.lock")
    assert is_published_snapshot_id("20260814-000000-abcd1234")
    graph = tmp_path / "ids"
    ents, rels, tus, obs = _tiny_tables()
    publish_byog_snapshot(ents, rels, tus, graph, call_observations_df=obs)
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}20260814-000000-abcd1234"
    staging.mkdir()
    (staging / "manifest.json").write_text("{}", encoding="utf-8")
    assert audit_main(["--graph", str(graph), "--snapshot", staging.name]) == 2
    err = capsys.readouterr().err
    assert "staging path is not a published snapshot" in err
    cleanup_old_snapshots(graph, keep_last=1)
    assert staging.is_dir()


def test_publish_retention_does_not_deadlock(tmp_path: Path):
    graph = tmp_path / "lock"
    payload = tmp_path / "pl"
    _write_payload(payload, marker="deadlock")
    q = CTX.Queue()
    proc = CTX.Process(target=_publish_worker, args=(str(graph), str(payload), 1, q))
    proc.start()
    proc.join(timeout=30)
    assert not proc.is_alive(), "publish deadlocked while retaining"
    assert q.get(timeout=5) == _current_id(graph)
    assert audit_graph_root(graph)["ok"] is True


def test_unsupported_lock_is_explicit_and_not_a_noop(tmp_path: Path, monkeypatch):
    import byog_graph

    ents, rels, tus, obs = _tiny_tables()
    monkeypatch.setattr(byog_graph, "_available_lock_backend", lambda: None)
    with pytest.raises(
        (ByogPublicationLockError, StagingWriterLeaseError), match="unsupported"
    ):
        publish_byog_snapshot(ents, rels, tus, tmp_path / "nolock", call_observations_df=obs)
    assert not _published_dirs(tmp_path / "nolock")
    assert not _staging_dirs(tmp_path / "nolock")
    graph = tmp_path / "has"
    monkeypatch.undo()
    publish_byog_snapshot(ents, rels, tus, graph, call_observations_df=obs)
    monkeypatch.setattr(byog_graph, "_available_lock_backend", lambda: None)
    with pytest.raises(ByogPublicationLockError, match="unsupported"):
        cleanup_old_snapshots(graph, keep_last=1)
    assert _published_dirs(graph)


def test_crash_staging_is_not_age_reaped(tmp_path: Path):
    ents, rels, tus, obs = _tiny_tables()
    graph = tmp_path / "stale"
    publish_byog_snapshot(ents, rels, tus, graph, call_observations_df=obs, keep_last=1)
    leftover = graph / "snapshots" / f"{STAGING_NAME_PREFIX}crashed-owner"
    leftover.mkdir()
    (leftover / "entities.parquet").write_text("partial", encoding="utf-8")
    cleanup_old_snapshots(graph, keep_last=1)
    assert leftover.is_dir()
    assert (leftover / "entities.parquet").is_file()


def test_retention_path_safety_and_missing_root_noop(tmp_path: Path):
    missing = tmp_path / "missing"
    assert cleanup_old_snapshots(missing, keep_last=1) == 0
    assert not missing.exists()

    external = tmp_path / "external-snapshots"
    victim = external / "20260814-000000-deadbeef"
    victim.mkdir(parents=True)
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    aliased = tmp_path / "aliased"
    aliased.mkdir()
    (aliased / "snapshots").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked snapshots"):
        cleanup_old_snapshots(aliased, keep_last=1)
    ents, rels, tus, obs = _tiny_tables(marker="unsafe")
    with pytest.raises(ValueError, match="symlinked snapshots"):
        publish_byog_snapshot(
            ents, rels, tus, aliased, call_observations_df=obs, keep_last=1
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"

    graph = tmp_path / "lock-alias"
    publish_byog_snapshot(
        ents, rels, tus, graph, call_observations_df=obs, keep_last=1
    )
    lock_path = graph / ".publish.lock"
    lock_path.unlink()
    external_lock = tmp_path / "external.lock"
    external_lock.write_text("untouched", encoding="utf-8")
    lock_path.symlink_to(external_lock)
    with pytest.raises(ByogPublicationLockError, match="symlinked publication lock"):
        cleanup_old_snapshots(graph, keep_last=1)
    assert external_lock.read_text(encoding="utf-8") == "untouched"

    lock_path.unlink()
    lock_path.mkdir()
    with pytest.raises(ByogPublicationLockError, match="not a regular file"):
        cleanup_old_snapshots(graph, keep_last=1)
    assert lock_path.is_dir()


def test_envelope_audit_still_detects_current_switch(tmp_path: Path, monkeypatch):
    ents, rels, tus, obs = _tiny_tables(n_obs=0)
    graph = tmp_path / "switch"
    first = publish_byog_snapshot(ents, rels, tus, graph, keep_last=2)
    second = publish_byog_snapshot(ents, rels, tus, graph, keep_last=2)
    (graph / "current").write_text(first.name, encoding="utf-8")
    import byog_snapshot_graph_audit as audit_module  # type: ignore

    original = audit_module.read_only_fingerprint

    def switch_before_fingerprint(graph_root, snap_dir):
        if switch_before_fingerprint.calls == 0:
            (Path(graph_root) / "current").write_text(second.name, encoding="utf-8")
        switch_before_fingerprint.calls += 1
        return original(graph_root, snap_dir)

    switch_before_fingerprint.calls = 0
    monkeypatch.setattr(audit_module, "read_only_fingerprint", switch_before_fingerprint)
    report = audit_module.audit_graph_root(graph, max_anomaly_samples=1)
    assert report["ok"] is False
    assert report["read_only_verification"]["changed_inputs"] == ["graph/current_selection"]


def test_published_mutable_roots_remain_read_only():
    names = (
        "byog_mini_game",
        "byog_mini_lang",
        "byog_jsmn",
        "byog_inih",
        "byog_sqlparse",
        "byog_semver",
        "byog_dmp",
        "byog_charset_normalizer",
        "byog_cjson",
        "byog_jsonpatch",
        "byog_humanize",
    )
    for name in names:
        graph = ROOT / name
        if not (graph / "current").is_file():
            pytest.skip(f"{name} is absent locally")
        before = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        report = audit_graph_root(graph)
        after = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        assert after == before, name
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["read_only_verification"]["verified"] is True
