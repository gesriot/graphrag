"""Explicit CLI-only retained-snapshot activation.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_activate.py -q
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
    graph_exclusive_lease,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_activate import (  # type: ignore
    SnapshotActivateError,
    SnapshotActivateIntegrityError,
    result_to_json,
    snapshot_activate,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
ACTIVATE = ROOT / "scripts" / "snapshot_activate.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_activate.py"
FORBIDDEN = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
        "c_clang_ast_capture",
        "c_compiler_facts",
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
        settings_text=f"activate: {marker}\n",
        keep_last=keep_last,
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _payload_paths(graph: Path) -> list[Path]:
    paths = [path for path in graph.iterdir()]
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


def _protected_hashes(graph: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in _payload_hashes(graph).items()
        if key != "current"
    }


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ACTIVATE), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def _activate_args(graph: Path, snapshot: str, expected: str, *extra: str) -> list[str]:
    return [
        "--graph",
        str(graph),
        "--snapshot",
        snapshot,
        "--expected-current",
        expected,
        *extra,
    ]


def test_activate_older_then_newer_retained_snapshot(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    assert _current(graph) == newer.name
    before = _protected_hashes(graph)
    listing = tuple(sorted(path.name for path in (graph / "snapshots").iterdir()))
    lock_bytes = (graph / PUBLICATION_LOCK_NAME).read_bytes()

    backward = snapshot_activate(
        graph,
        older.name,
        newer.name,
        activate_confirmed=True,
    )
    assert backward["ok"] is True
    assert backward["changed"] is True
    assert backward["previous_current"] == newer.name
    assert backward["expected_current"] == newer.name
    assert backward["activated_snapshot"] == older.name
    assert backward["current"] == older.name
    assert _current(graph) == older.name
    assert backward["payload_unchanged"] is True
    assert backward["snapshots_listing_unchanged"] is True
    assert backward["target_integrity"] == {
        "ok": True,
        "status": "valid",
        "n_anomalies": 0,
    }
    assert backward["read_only_verification"]["verified"] is True
    assert backward["read_only_verification"]["changed_inputs"] == []

    forward = snapshot_activate(
        graph,
        newer.name,
        older.name,
        activate_confirmed=True,
    )
    assert forward["changed"] is True
    assert forward["previous_current"] == older.name
    assert forward["current"] == newer.name
    assert _current(graph) == newer.name
    assert _protected_hashes(graph) == before
    assert tuple(sorted(path.name for path in (graph / "snapshots").iterdir())) == listing
    assert (graph / PUBLICATION_LOCK_NAME).read_bytes() == lock_bytes
    assert older.is_dir() and newer.is_dir()


def test_idempotent_activation(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    result = snapshot_activate(
        graph,
        snap.name,
        snap.name,
        activate_confirmed=True,
    )
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["previous_current"] == result["current"] == snap.name
    assert _payload_hashes(graph) == before_hashes
    assert _payload_stats(graph) == before_stats


def test_expected_current_mismatch(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    before = _payload_hashes(graph)
    with pytest.raises(SnapshotActivateIntegrityError, match="expected-current"):
        snapshot_activate(
            graph,
            older.name,
            older.name,
            activate_confirmed=True,
        )
    assert _current(graph) == newer.name
    assert _payload_hashes(graph) == before


def test_missing_confirmation_changes_nothing(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    before = _payload_hashes(graph)
    proc = _run(*_activate_args(graph, older.name, newer.name, "--json"))
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "--activate-confirmed" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert _current(graph) == newer.name
    assert _payload_hashes(graph) == before


def test_missing_lock_does_not_create_lock(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    before = _payload_hashes(graph)
    proc = _run(*_activate_args(graph, snap.name, snap.name, "--activate-confirmed", "--json"))
    assert proc.returncode == 2
    assert "adopt-publication-lock" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not lock.exists()
    assert _payload_hashes(graph) == before


def test_symlinked_and_nonregular_lock_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    payload = lock.read_bytes()
    lock.unlink()
    external = tmp_path / "external.lock"
    external.write_bytes(payload)
    lock.symlink_to(external)
    before = _payload_hashes(graph)
    proc = _run(*_activate_args(graph, snap.name, snap.name, "--activate-confirmed"))
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert _current(graph) == snap.name
    lock.unlink()
    lock.mkdir()
    proc = _run(*_activate_args(graph, snap.name, snap.name, "--activate-confirmed"))
    assert proc.returncode == 2
    assert "regular file" in proc.stderr
    assert _current(graph) == snap.name
    assert _protected_hashes(graph) == {
        key: value
        for key, value in before.items()
        if key not in {PUBLICATION_LOCK_NAME, "current"}
    }


def test_unsafe_snapshot_ids_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    before = _payload_hashes(graph)
    for value in (
        "current",
        "../etc/passwd",
        "foo/bar",
        f"{STAGING_NAME_PREFIX}x",
        "",
        ".",
        "..",
        "id\\win",
    ):
        proc = _run(*_activate_args(graph, value, snap.name, "--activate-confirmed"))
        assert proc.returncode == 2, (value, proc.stderr)
        assert "Traceback" not in proc.stderr
        proc = _run(*_activate_args(graph, snap.name, value, "--activate-confirmed"))
        assert proc.returncode == 2, (value, proc.stderr)
    assert _payload_hashes(graph) == before


def test_missing_target_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    before = _payload_hashes(graph)
    with pytest.raises(SnapshotActivateIntegrityError, match="not a published"):
        snapshot_activate(
            graph,
            "20990101-000000-deadbeef",
            snap.name,
            activate_confirmed=True,
        )
    assert _payload_hashes(graph) == before


def test_invalid_target_envelope_leaves_current(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    manifest_path = older / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["entities"] = 0
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    before = _current(graph)
    with pytest.raises(SnapshotActivateIntegrityError, match="not ok|envelope"):
        snapshot_activate(
            graph,
            older.name,
            newer.name,
            activate_confirmed=True,
        )
    assert _current(graph) == before == newer.name


def test_symlinked_target_directory_and_core_file(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    alias = graph / "snapshots" / "aliased"
    alias.symlink_to(older, target_is_directory=True)
    proc = _run(*_activate_args(graph, older.name, newer.name, "--activate-confirmed"))
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    alias.unlink()

    core = older / "entities.parquet"
    payload = core.read_bytes()
    external = tmp_path / "entities.parquet"
    external.write_bytes(payload)
    core.unlink()
    core.symlink_to(external)
    proc = _run(*_activate_args(graph, older.name, newer.name, "--activate-confirmed"))
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert _current(graph) == newer.name


def test_unexpected_snapshots_entry_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    (graph / "snapshots" / "README.txt").write_text("nope", encoding="utf-8")
    proc = _run(*_activate_args(graph, older.name, newer.name, "--activate-confirmed"))
    assert proc.returncode == 2
    assert "unexpected" in proc.stderr
    assert _current(graph) == newer.name


def test_staging_is_notice_not_target(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}held"
    staging.mkdir()
    result = snapshot_activate(
        graph,
        older.name,
        newer.name,
        activate_confirmed=True,
    )
    assert result["current"] == older.name
    assert any(notice["code"] == "staging_present" for notice in result["publication_notices"])
    notice = next(
        item for item in result["publication_notices"] if item["code"] == "staging_present"
    )
    assert notice["n_staging"] == 1
    assert notice["names"] == [staging.name]
    assert notice["truncated"] is False
    proc = _run(
        *_activate_args(graph, staging.name, older.name, "--activate-confirmed")
    )
    assert proc.returncode == 2
    assert "staging" in proc.stderr
    assert _current(graph) == older.name
    assert staging.is_dir()


def test_atomic_replace_failure_preserves_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.byog_graph as byog

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    orig = byog.os.replace

    def boom(src, dst, *args, **kwargs):
        if Path(dst).name == "current":
            raise OSError("simulated replace failure")
        return orig(src, dst, *args, **kwargs)

    monkeypatch.setattr(byog.os, "replace", boom)
    before = _current(graph)
    with pytest.raises(SnapshotActivateError, match="failed to replace|simulated"):
        snapshot_activate(
            graph,
            older.name,
            newer.name,
            activate_confirmed=True,
        )
    assert _current(graph) == before
    leftovers = [
        path
        for path in graph.iterdir()
        if path.is_file() and path.name.endswith(".tmp")
    ]
    assert leftovers == []


def _reader_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import graph_read_lease

    try:
        with graph_read_lease(ChildPath(graph)):
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


def test_shared_reader_lease_blocks_activation(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_reader_hold, args=(str(graph), held, resume, q))
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

    # The pathname can be replaced by a lock-ignoring actor while the
    # activator is blocked on the old inode. After acquiring that old lock the
    # exclusive lease must revalidate its fd against the pathname and refuse
    # to enter a split locking domain.
    race_graph = tmp_path / "lock-replaced-while-waiting"
    race_older = _publish(race_graph, "older")
    race_newer = _publish(race_graph, "newer")
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_reader_hold, args=(str(race_graph), held, resume, q))
    actor = CTX.Process(
        target=_activate_waiter,
        args=(str(race_graph), race_older.name, race_newer.name, about, got, q),
    )
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        actor.start()
        assert about.wait(timeout=TIMEOUT)
        lock = race_graph / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replacement-lock-domain")
        resume.set()
        actor.join(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not actor.is_alive() and not reader.is_alive()
        assert got.is_set()
        messages = [q.get(timeout=TIMEOUT), q.get(timeout=TIMEOUT)]
        assert any("publication lock changed" in message for message in messages)
        assert _current(race_graph) == race_newer.name
    finally:
        _cleanup_processes(reader, actor, release=resume)


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


def test_activation_lease_blocks_publisher(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old", keep_last=1)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 1, about, got, q))
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == first.name
        assert first.is_dir()
        resume.set()
        pub.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not holder.is_alive()
        assert got.is_set()
        assert _current(graph) != first.name
    finally:
        _cleanup_processes(holder, pub, release=resume)


def test_lock_ignoring_mutation_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_activate as activate

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    orig = activate._fingerprint
    calls = {"n": 0}

    def after_baseline(*args, **kwargs):
        if calls["n"] == 1:
            (older / "entities.parquet").write_bytes(b"mutated-by-lock-ignorer")
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(activate, "_fingerprint", after_baseline)
    with pytest.raises(SnapshotActivateIntegrityError, match="changed during"):
        snapshot_activate(
            graph,
            older.name,
            newer.name,
            activate_confirmed=True,
        )
    assert calls["n"] >= 2

    before_graph = tmp_path / "before"
    first = _publish(before_graph, "first")
    second = _publish(before_graph, "second")
    baseline_calls = {"n": 0}

    def before_wrapped(*args, **kwargs):
        if baseline_calls["n"] == 0:
            (before_graph / "current").write_text(first.name, encoding="utf-8")
        baseline_calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(activate, "_fingerprint", before_wrapped)
    with pytest.raises(SnapshotActivateIntegrityError, match="discovery changed"):
        snapshot_activate(
            before_graph,
            first.name,
            second.name,
            activate_confirmed=True,
        )

    cas_graph = tmp_path / "cas-before-write"
    target = _publish(cas_graph, "target")
    interloper = _publish(cas_graph, "interloper")
    expected = _publish(cas_graph, "expected")
    cas_calls = {"n": 0}

    def between_prewrite_fingerprint_and_pointer_check(*args, **kwargs):
        result = orig(*args, **kwargs)
        if cas_calls["n"] == 1:
            (cas_graph / "current").write_text(interloper.name, encoding="utf-8")
        cas_calls["n"] += 1
        return result

    monkeypatch.setattr(
        activate,
        "_fingerprint",
        between_prewrite_fingerprint_and_pointer_check,
    )
    with pytest.raises(SnapshotActivateIntegrityError, match="changed before activation"):
        snapshot_activate(
            cas_graph,
            target.name,
            expected.name,
            activate_confirmed=True,
        )
    assert _current(cas_graph) == interloper.name


def test_current_resolves_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from graphrag_code.byog_snapshot_graph_audit import resolve_snapshot as original

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    calls: list[object] = []

    def wrapped(graph_root, snapshot=None):
        if snapshot is None:
            calls.append(None)
        return original(graph_root, snapshot)

    # Current is resolved through snapshot_compare helpers, not a local alias.
    import graphrag_code.snapshot_compare as compare

    monkeypatch.setattr(compare, "resolve_snapshot", wrapped)
    snapshot_activate(graph, older.name, newer.name, activate_confirmed=True)
    assert calls == [None]


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
    assert "publish_byog_snapshot" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "_publication_lock" not in source
    assert "graph_read_lease" not in source
    assert "graph_exclusive_lease" in source


def test_cli_module_script_and_wheel_parity(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    args = _activate_args(
        graph, older.name, newer.name, "--activate-confirmed", "--json"
    )
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_activate", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    # Restore current so the next two invocations are also a real transition.
    (graph / "current").write_text(newer.name, encoding="utf-8")
    script = subprocess.run(
        [sys.executable, str(ACTIVATE), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    (graph / "current").write_text(newer.name, encoding="utf-8")
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-activate",
            *args,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    assert result_to_json(bodies[0]) == module.stdout

    (graph / "current").write_text(newer.name, encoding="utf-8")
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        ["graphrag-code", "snapshot-activate", *args],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    body = json.loads(installed.stdout)
    assert body["ok"] is True
    assert body["current"] == older.name
    assert body["changed"] is True
    assert _current(graph) == older.name


def test_only_current_pointer_changes(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    listing = tuple(sorted(path.name for path in (graph / "snapshots").iterdir()))
    lock_stat = (graph / PUBLICATION_LOCK_NAME).lstat()
    snapshot_activate(graph, older.name, newer.name, activate_confirmed=True)
    after_hashes = _payload_hashes(graph)
    after_stats = _payload_stats(graph)
    assert after_hashes["current"] != before_hashes["current"]
    assert {key: value for key, value in after_hashes.items() if key != "current"} == {
        key: value for key, value in before_hashes.items() if key != "current"
    }
    assert {key: value for key, value in after_stats.items() if key != "current"} == {
        key: value for key, value in before_stats.items() if key != "current"
    }
    assert tuple(sorted(path.name for path in (graph / "snapshots").iterdir())) == listing
    after_lock = (graph / PUBLICATION_LOCK_NAME).lstat()
    assert (after_lock.st_ino, after_lock.st_dev, after_lock.st_size) == (
        lock_stat.st_ino,
        lock_stat.st_dev,
        lock_stat.st_size,
    )


def test_mcp_has_no_activation_tool(tmp_path: Path):
    from anyio import run as anyio_run

    graph = tmp_path / "g"
    _publish(graph, "a")
    assert "snapshot_activate" not in TOOL_NAMES
    assert len(TOOL_NAMES) == 17
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert "snapshot_activate" not in names
            assert len(names) == 17

    anyio_run(_body)


def test_legacy_flat_directory_rejected(tmp_path: Path):
    graph = tmp_path / "flat"
    graph.mkdir()
    pd.DataFrame([{"id": "e"}]).to_parquet(graph / "entities.parquet")
    pd.DataFrame([{"id": "r"}]).to_parquet(graph / "relationships.parquet")
    pd.DataFrame([{"id": "t"}]).to_parquet(graph / "text_units.parquet")
    before = _payload_hashes(graph)
    proc = _run(
        *_activate_args(graph, "anything", "anything", "--activate-confirmed")
    )
    assert proc.returncode == 2
    assert "legacy" in proc.stderr
    assert _payload_hashes(graph) == before


def test_graph_exclusive_lease_does_not_create_lock(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.chmod(0o444)
    before = lock.lstat()
    with graph_exclusive_lease(graph):
        assert lock.read_bytes() == b""
    after = lock.lstat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o444
    assert (after.st_dev, after.st_ino, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
    )
    lock.unlink()
    with pytest.raises(Exception, match="lock is missing"):
        with graph_exclusive_lease(graph):
            raise AssertionError("must not enter")
    assert not lock.exists()
