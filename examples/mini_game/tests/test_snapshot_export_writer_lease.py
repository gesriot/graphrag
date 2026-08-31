"""Cooperative writer-lease protocol for private export staging.

Disposable tmp graphs and parents only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_writer_lease.py -q
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
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from byog_graph import publish_byog_snapshot  # type: ignore
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_apply import (  # type: ignore
    SnapshotExportApplyError,
    snapshot_export_apply,
)
from graphrag_code.snapshot_export_plan import snapshot_export_plan  # type: ignore
from graphrag_code.snapshot_export_reconcile import snapshot_export_reconcile  # type: ignore
from graphrag_code.snapshot_export_staging import (  # type: ignore
    SnapshotExportStagingIntegrityError,
    inventory_revision_of,
    result_to_json,
    snapshot_export_staging,
)
from graphrag_code.snapshot_export_verify import snapshot_export_verify  # type: ignore
from graphrag_code.snapshot_export_writer_lease import (  # type: ignore
    EXPORT_STAGING_WRITER_LOCK_NAME,
)

SCRIPT = ROOT / "scripts" / "snapshot_export_staging.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
APPLY_MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_apply.py"
STAGING_MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_staging.py"
HELPER_MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_writer_lease.py"
BYOG_ROOTS = tuple(
    sorted(path for path in ROOT.iterdir() if path.is_dir() and path.name.startswith("byog_"))
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
HEX_A = "a" * 32
HEX_B = "b" * 32
CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _root_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        info = os.lstat(dirpath)
        digest.update(rel_dir.encode())
        digest.update(str(stat.S_IMODE(info.st_mode)).encode())
        digest.update(str(info.st_uid).encode())
        digest.update(str(info.st_gid).encode())
        digest.update(str(info.st_nlink).encode())
        digest.update(str(info.st_mtime_ns).encode())
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            child = path.lstat()
            digest.update(rel.encode())
            digest.update(str(stat.S_IMODE(child.st_mode)).encode())
            digest.update(str(child.st_uid).encode())
            digest.update(str(child.st_gid).encode())
            digest.update(str(child.st_nlink).encode())
            digest.update(str(child.st_mtime_ns).encode())
            digest.update(str(child.st_size).encode())
            if stat.S_ISREG(child.st_mode):
                digest.update(path.read_bytes())
            elif stat.S_ISLNK(child.st_mode):
                digest.update(os.readlink(path).encode())
    return digest.hexdigest()


def _rows(marker: str):
    return (
        [
            {
                "id": f"ent:{marker}",
                "title": f"demo:{marker}",
                "type": "function",
                "source_file": f"{marker}.py",
                "extractor": "tree-sitter-python",
                "description": f"desc-{marker}",
            }
        ],
        [
            {
                "id": f"rel:{marker}",
                "source": f"demo:{marker}.py",
                "target": f"demo:{marker}",
                "type": "contains",
                "extractor": "tree-sitter-python",
            }
        ],
        [
            {
                "id": f"tu:{marker}",
                "title": f"{marker}.py",
                "source_file": f"{marker}.py",
                "entity_id": f"ent:{marker}",
            }
        ],
    )


def _publish(graph: Path, marker: str) -> Path:
    ents, rels, tus = _rows(marker)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"export-writer-lease: {marker}\n",
        keep_last=10,
    )


def _leftover_staging(parent: Path) -> list[Path]:
    if not parent.exists() or not parent.is_dir():
        return []
    return sorted(
        path
        for path in parent.iterdir()
        if path.name.startswith(".graphrag-export-")
    )


def _staging_name(suffix: str) -> str:
    return f".graphrag-export-{suffix}"


def _entry(result: dict, name: str) -> dict:
    matches = [item for item in result["staging_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


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


def _paused_apply(graph: str, dest: str, revision: str, hook: str, paused, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.snapshot_export_apply as apply_mod

    orig = getattr(apply_mod, hook)

    def wrapped(*args, **kwargs):
        paused.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
            raise RuntimeError("apply resume timed out")
        return orig(*args, **kwargs)

    setattr(apply_mod, hook, wrapped)
    try:
        result = apply_mod.snapshot_export_apply(
            ChildPath(graph),
            "current",
            ChildPath(dest),
            revision,
            export_confirmed=True,
        )
        q.put(("ok", result["observed_export_revision"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_apply_acquires_lease_before_payloads_and_inventory_sees_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "base")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "out"
    seen: dict[str, object] = {}

    def after_lease(parent_fd, staging_name, staging_fd, lock_fd):
        names = []
        with os.scandir(staging_fd) as iterator:
            names.extend(entry.name for entry in iterator)
        assert names == [EXPORT_STAGING_WRITER_LOCK_NAME]
        lock_info = os.fstat(lock_fd)
        assert stat.S_ISREG(lock_info.st_mode)
        assert lock_info.st_size == 0
        path_info = os.stat(
            EXPORT_STAGING_WRITER_LOCK_NAME, dir_fd=staging_fd, follow_symlinks=False
        )
        assert (path_info.st_dev, path_info.st_ino) == (
            lock_info.st_dev,
            lock_info.st_ino,
        )
        seen["staging"] = dest.parent / staging_name

    monkeypatch.setattr(apply_mod, "_after_export_apply_writer_lease", after_lease)
    result = snapshot_export_apply(
        graph, "current", dest, plan["export_revision"], export_confirmed=True
    )
    assert result["ok"] is True
    assert dest.is_dir()
    assert not (dest / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
    assert _leftover_staging(dest.parent) == []


def test_live_apply_reports_held_then_publication_removes_lock(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "live-out"
    paused = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    proc = CTX.Process(
        target=_paused_apply,
        args=(
            str(graph),
            str(dest),
            plan["export_revision"],
            "_after_export_apply_staged_verified",
            paused,
            resume,
            q,
        ),
    )
    try:
        proc.start()
        assert paused.wait(timeout=TIMEOUT)
        leftovers = _leftover_staging(dest.parent)
        assert len(leftovers) == 1
        lock = leftovers[0] / EXPORT_STAGING_WRITER_LOCK_NAME
        assert lock.is_file() and not lock.is_symlink()
        started = time.monotonic()
        inventory = snapshot_export_staging(dest.parent)
        assert time.monotonic() - started < 5
        entry = _entry(inventory, leftovers[0].name)
        assert entry["writer_lease_state"] == "held_at_scan"
        assert entry["writer_lease_metadata_present"] is True
        assert entry["writer_lease_contended"] is True
        assert entry["writer_activity"] == "unknown"
        assert entry["cleanup_eligible"] is False
        assert entry["contents_inspected"] is False
        resume.set()
        proc.join(timeout=TIMEOUT)
        assert not proc.is_alive()
        message = q.get(timeout=TIMEOUT)
        assert message[0] == "ok"
        assert dest.is_dir()
        assert not (dest / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
        assert _leftover_staging(dest.parent) == []
        verify = snapshot_export_verify(dest, plan["export_revision"])
        assert verify["ok"] is True
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        reconcile = snapshot_export_reconcile(plan_path, dest)
        assert reconcile["ok"] is True
        assert reconcile["observed_export_revision"] == plan["export_revision"]
    finally:
        _cleanup_processes(proc, release=resume)


def test_lock_removed_before_rename_and_prepublish_failure_is_not_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod
    import graphrag_code.snapshot_export_writer_lease as lease_mod

    graph = tmp_path / "g"
    _publish(graph, "rename")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "rename-out"
    seen: dict[str, Path] = {}
    release_order: list[str] = []
    original_release = lease_mod._release_lock

    def record_release(fd, backend):
        release_order.append("release")
        return original_release(fd, backend)

    def record_removed(_path, lock_fd):
        os.fstat(lock_fd)
        release_order.append("unlink")

    def after_removed(parent_fd, staging_name, staging_fd):
        names = []
        with os.scandir(staging_fd) as iterator:
            names.extend(entry.name for entry in iterator)
        assert EXPORT_STAGING_WRITER_LOCK_NAME not in names
        seen["staging"] = dest.parent / staging_name

    def boom(_parent_fd, _dest_name, _staging_name):
        raise SnapshotExportApplyError("injected pre-publication failure")

    monkeypatch.setattr(apply_mod, "_after_export_apply_writer_lease_removed", after_removed)
    monkeypatch.setattr(apply_mod, "_before_export_apply_publication", boom)
    monkeypatch.setattr(lease_mod, "_release_lock", record_release)
    monkeypatch.setattr(
        lease_mod, "_after_export_writer_lock_removed_while_held", record_removed
    )
    with pytest.raises(SnapshotExportApplyError, match="injected pre-publication failure"):
        snapshot_export_apply(
            graph, "current", dest, plan["export_revision"], export_confirmed=True
        )
    assert not dest.exists()
    assert _leftover_staging(dest.parent) == []
    assert release_order[:2] == ["unlink", "release"]
    if "staging" in seen:
        assert not seen["staging"].exists()


def test_subprocess_death_releases_lease_and_may_leave_metadata(tmp_path: Path):
    parent = tmp_path / "crash-parent"
    parent.mkdir()
    staging = parent / _staging_name(HEX_A)
    staging.mkdir()
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")
    ready = tmp_path / "ready"
    holder = tmp_path / "hold_lock.py"
    holder.write_text(
        "import fcntl\n"
        "import os\n"
        "import time\n"
        f"fd = os.open({str(lock)!r}, os.O_RDONLY | os.O_NOFOLLOW)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        f"open({str(ready)!r}, 'w', encoding='utf-8').write('1')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen([sys.executable, str(holder)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline
            assert proc.poll() is None
            time.sleep(0.05)
        held = snapshot_export_staging(parent)
        assert _entry(held, staging.name)["writer_lease_state"] == "held_at_scan"
        proc.kill()
        proc.wait(timeout=10)
        assert proc.poll() is not None
        inventory = snapshot_export_staging(parent)
        entry = _entry(inventory, staging.name)
        assert entry["writer_lease_state"] == "not_held_at_scan"
        assert entry["writer_lease_metadata_present"] is True
        assert entry["writer_lease_contended"] is False
        assert entry["cleanup_eligible"] is False
        assert lock.is_file()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_inventory_states_and_unprobed_entries(tmp_path: Path):
    parent = tmp_path / "states"
    parent.mkdir()
    absent = parent / _staging_name(HEX_A)
    absent.mkdir()
    stable = parent / _staging_name(HEX_B)
    stable.mkdir()
    (stable / EXPORT_STAGING_WRITER_LOCK_NAME).write_bytes(b"")
    unrec = parent / ".graphrag-export-short"
    unrec.mkdir()
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"must-not-read")
    (unrec / EXPORT_STAGING_WRITER_LOCK_NAME).symlink_to(secret)
    named_file = parent / _staging_name("c" * 32)
    named_file.write_bytes(b"not-a-directory")
    result = snapshot_export_staging(parent)
    absent_entry = _entry(result, absent.name)
    assert absent_entry["writer_lease_state"] == "metadata_absent"
    assert absent_entry["writer_lease_metadata_present"] is False
    assert absent_entry["writer_lease_contended"] is False
    assert absent_entry["writer_lease_dev"] is None
    assert absent_entry["cleanup_eligible"] is False
    assert not (absent / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
    stable_entry = _entry(result, stable.name)
    assert stable_entry["writer_lease_state"] == "not_held_at_scan"
    assert stable_entry["writer_lease_metadata_present"] is True
    assert stable_entry["writer_lease_contended"] is False
    assert stable_entry["cleanup_eligible"] is False
    file_entry = _entry(result, named_file.name)
    assert file_entry["kind"] == "file"
    assert "writer_lease_state" not in file_entry
    assert result["unrecognized_prefixed_count"] == 1
    assert "writer_lease_state" not in result["unrecognized_prefixed_entries"][0]
    assert secret.read_bytes() == b"must-not-read"
    dumped = result_to_json(result)
    assert "must-not-read" not in dumped

    linked = parent / _staging_name("d" * 32)
    linked.mkdir()
    (linked / EXPORT_STAGING_WRITER_LOCK_NAME).symlink_to(secret)
    fifo_dir = parent / _staging_name("e" * 32)
    fifo_dir.mkdir()
    os.mkfifo(fifo_dir / EXPORT_STAGING_WRITER_LOCK_NAME)
    dir_lock = parent / _staging_name("f" * 32)
    dir_lock.mkdir()
    (dir_lock / EXPORT_STAGING_WRITER_LOCK_NAME).mkdir()
    unsafe = snapshot_export_staging(parent)
    assert _entry(unsafe, linked.name)["writer_lease_state"] == "metadata_unsafe"
    assert _entry(unsafe, fifo_dir.name)["writer_lease_state"] == "metadata_unsafe"
    assert _entry(unsafe, dir_lock.name)["writer_lease_state"] == "metadata_unsafe"
    for name in (linked.name, fifo_dir.name, dir_lock.name):
        entry = _entry(unsafe, name)
        assert entry["cleanup_eligible"] is False
        assert entry["writer_activity"] == "unknown"
        assert entry["writer_lease_contended"] is False
    assert secret.read_bytes() == b"must-not-read"


def test_lock_replacement_never_follows_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_writer_lease as lease_mod

    parent = tmp_path / "replace"
    parent.mkdir()
    staging = parent / _staging_name(HEX_A)
    staging.mkdir()
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")
    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"replacement-target")
    before = secret.stat()

    def replace_before_open(_path):
        if lock.exists() and not lock.is_symlink():
            lock.unlink()
            lock.symlink_to(secret)

    monkeypatch.setattr(lease_mod, "_after_export_writer_lock_path_inspected", replace_before_open)
    with pytest.raises(SnapshotExportStagingIntegrityError, match="changed|unsafe"):
        snapshot_export_staging(parent)
    assert secret.read_bytes() == b"replacement-target"
    assert secret.stat().st_mtime_ns == before.st_mtime_ns
    assert lock.is_symlink()

    lock.unlink()
    lock.write_bytes(b"")
    planted = tmp_path / "planted-lock"
    planted.write_bytes(b"do-not-lock")

    def replace_after_open(_path, _lock_fd):
        lock.unlink()
        lock.symlink_to(planted)

    monkeypatch.setattr(lease_mod, "_after_export_writer_lock_path_inspected", lambda *_: None)
    monkeypatch.setattr(lease_mod, "_after_export_writer_lock_opened", replace_after_open)
    with pytest.raises(SnapshotExportStagingIntegrityError, match="changed|unsafe"):
        snapshot_export_staging(parent)
    assert planted.read_bytes() == b"do-not-lock"
    assert lock.is_symlink()


def test_lock_identity_and_state_changes_between_scans_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "delta"
    parent.mkdir()
    staging = parent / _staging_name(HEX_A)
    staging.mkdir()
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")

    def chmod_lock(_path, _scan):
        lock.chmod(0o640)

    monkeypatch.setattr(staging_mod, "_after_first_scan", chmod_lock)
    with pytest.raises(
        SnapshotExportStagingIntegrityError,
        match="writer-lease identity|entry metadata|entry identity",
    ):
        snapshot_export_staging(parent)
    lock.chmod(0o600)

    def rewrite_lock(_path, _scan):
        lock.write_bytes(b"x")

    monkeypatch.setattr(staging_mod, "_after_first_scan", rewrite_lock)
    with pytest.raises(
        SnapshotExportStagingIntegrityError,
        match="writer-lease identity|entry metadata|entry identity",
    ):
        snapshot_export_staging(parent)
    lock.write_bytes(b"")

    def bump_ctime(_path, _scan):
        current = lock.stat()
        os.utime(lock, ns=(current.st_atime_ns, current.st_mtime_ns + 1))

    monkeypatch.setattr(staging_mod, "_after_first_scan", bump_ctime)
    with pytest.raises(
        SnapshotExportStagingIntegrityError,
        match="writer-lease identity|entry metadata",
    ):
        snapshot_export_staging(parent)

    def replace_inode(_path, _scan):
        other = tmp_path / "other-lock"
        other.write_bytes(b"")
        lock.unlink()
        other.rename(lock)

    monkeypatch.setattr(staging_mod, "_after_first_scan", replace_inode)
    with pytest.raises(
        SnapshotExportStagingIntegrityError,
        match="writer-lease identity|entry identity|entry metadata",
    ):
        snapshot_export_staging(parent)


def test_lease_state_change_between_scans_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod
    from graphrag_code.byog_graph import _try_acquire_exclusive_lock

    parent = tmp_path / "state-change"
    parent.mkdir()
    staging = parent / _staging_name(HEX_A)
    staging.mkdir()
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")
    holder = {"fd": None, "backend": None}

    def take_lease(_path, _scan):
        fd = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW)
        holder["fd"] = fd
        holder["backend"] = _try_acquire_exclusive_lock(fd)

    monkeypatch.setattr(staging_mod, "_after_first_scan", take_lease)
    try:
        with pytest.raises(
            SnapshotExportStagingIntegrityError, match="writer-lease state"
        ):
            snapshot_export_staging(parent)
    finally:
        if holder["fd"] is not None:
            from graphrag_code.byog_graph import _release_lock

            if holder["backend"] is not None:
                try:
                    _release_lock(holder["fd"], holder["backend"])
                except OSError:
                    pass
            os.close(holder["fd"])


def test_deterministic_revision_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    parent = here / "parent"
    parent.mkdir()
    staging = parent / _staging_name(HEX_A)
    staging.mkdir()
    (staging / EXPORT_STAGING_WRITER_LOCK_NAME).write_bytes(b"")
    first = snapshot_export_staging(parent)
    second = snapshot_export_staging(parent)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    rebuilt = {
        key: first[key]
        for key in (
            "schema_version",
            "staging_entries",
            "staging_count",
            "unsafe_staging_count",
            "unrecognized_prefixed_entries",
            "unrecognized_prefixed_count",
            "other_entry_count",
        )
    }
    assert first["inventory_revision"] == inventory_revision_of(rebuilt)
    absent_parent = tmp_path / "absent-parent"
    absent_parent.mkdir()
    (absent_parent / _staging_name(HEX_A)).mkdir()
    absent = snapshot_export_staging(absent_parent)
    assert absent["inventory_revision"] != first["inventory_revision"]

    args = ["--parent", "parent", "--json"]
    env = _child_env()
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_export_staging", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=env,
    )
    script = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=env,
    )
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-staging", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=env,
    )
    assert module.returncode == script.returncode == cli.returncode == 0, (
        module.stderr,
        script.stderr,
        cli.stderr,
    )
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["inventory_revision"] == first["inventory_revision"]

    installed_env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    (tmp_path / "outside").mkdir()
    installed = subprocess.run(
        ["graphrag-code", "snapshot-export-staging", "--parent", str(parent), "--json"],
        cwd=tmp_path / "outside",
        capture_output=True,
        text=True,
        env=installed_env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["inventory_revision"] == first["inventory_revision"]

    import tarfile
    import zipfile

    with zipfile.ZipFile(built_wheel_and_sdist[0]) as zf:
        assert "graphrag_code/snapshot_export_writer_lease.py" in zf.namelist()
    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_export_writer_lease.py" in names


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "held"
    parent.mkdir()
    staging = parent / _staging_name(HEX_A)
    staging.mkdir()
    (staging / EXPORT_STAGING_WRITER_LOCK_NAME).write_bytes(b"")
    original_json = staging_mod.result_to_json
    original_format = staging_mod.format_result
    state = {"parent_fd": None, "staging_fd": None, "lock_fd": None, "writes": 0, "flushes": 0}

    def capture(_path, parent_fd, held):
        state["parent_fd"] = parent_fd
        assert held
        staging_fd, lock_fd = next(iter(held.values()))
        state["staging_fd"] = staging_fd
        state["lock_fd"] = lock_fd
        os.fstat(parent_fd)
        os.fstat(staging_fd)
        os.fstat(lock_fd)

    def guarded_json(*args, **kwargs):
        os.fstat(state["parent_fd"])
        os.fstat(state["staging_fd"])
        os.fstat(state["lock_fd"])
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        os.fstat(state["parent_fd"])
        os.fstat(state["staging_fd"])
        os.fstat(state["lock_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            os.fstat(state["parent_fd"])
            os.fstat(state["staging_fd"])
            os.fstat(state["lock_fd"])
            state["writes"] += 1
            return len(text)

        def flush(self) -> None:
            os.fstat(state["parent_fd"])
            os.fstat(state["staging_fd"])
            os.fstat(state["lock_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(staging_mod, "_after_probe_descriptors_ready", capture)
    monkeypatch.setattr(staging_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(staging_mod, "format_result", guarded_format)
    monkeypatch.setattr(staging_mod.sys, "stdout", GuardedStdout())
    assert staging_mod.main(["--parent", str(parent), "--json"]) == 0
    assert staging_mod.main(["--parent", str(parent)]) == 0
    assert state["writes"] >= 2
    assert state["flushes"] == 2


def test_implementation_boundaries_mcp_and_byog_roots(tmp_path: Path):
    apply_src = APPLY_MODULE.read_text(encoding="utf-8")
    staging_src = STAGING_MODULE.read_text(encoding="utf-8")
    helper_src = HELPER_MODULE.read_text(encoding="utf-8")
    for source in (apply_src, staging_src, helper_src):
        lowered = source.lower()
        for word in FORBIDDEN_WORDS:
            assert word not in lowered
    assert "EXPORT_STAGING_WRITER_LOCK_NAME" in helper_src
    assert helper_src.count('".export-writer.lock"') == 1
    staging_tree = ast.parse(staging_src)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(staging_tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert "probe_export_writer_lease" in imported
    assert "acquire_export_writer_lease" not in imported
    assert "acquire_export_writer_lease" not in called
    assert "probe_staging_writer_lease" not in imported
    assert "staging_writer_lease" not in imported
    assert "graph_read_lease" not in staging_src
    assert "readlink" not in staging_src
    assert "snapshot_export_plan(" not in staging_src
    assert "LOCK_NB" not in staging_src
    assert "LOCK_NB" in (ROOT / "src" / "graphrag_code" / "byog_graph.py").read_text(
        encoding="utf-8"
    )
    assert "graph_exclusive_lease" not in apply_src
    assert "staging_writer_lease(" not in apply_src
    assert len(TOOL_NAMES) == 15
    assert "snapshot_export_staging" not in TOOL_NAMES
    assert "snapshot_export_writer_lease" not in TOOL_NAMES

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    graph = tmp_path / "g"
    _publish(graph, "mcp")
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 15

    from anyio import run as anyio_run

    anyio_run(_body)
    after = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert after == before

    leftovers: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(ROOT, followlinks=False):
        rel = Path(dirpath).relative_to(ROOT)
        if ".git" in rel.parts:
            dirnames[:] = []
            continue
        leftovers.extend(
            Path(dirpath) / name
            for name in dirnames
            if name.startswith(".graphrag-export-")
        )
    assert leftovers == []
