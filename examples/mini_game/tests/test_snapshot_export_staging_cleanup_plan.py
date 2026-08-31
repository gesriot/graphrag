"""Read-only snapshot-export staging cleanup plan.

Disposable tmp parents only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_staging_cleanup_plan.py -q
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
from graphrag_code.snapshot_export_plan import snapshot_export_plan  # type: ignore
from graphrag_code.snapshot_export_staging import snapshot_export_staging  # type: ignore
from graphrag_code.snapshot_export_staging_cleanup_plan import (  # type: ignore
    SnapshotExportStagingCleanupPlanError,
    SnapshotExportStagingCleanupPlanIntegrityError,
    _classify_entry,
    canonical_plan_revision_text,
    format_result,
    plan_revision_of,
    result_to_json,
    snapshot_export_staging_cleanup_plan,
)
from graphrag_code.snapshot_export_writer_lease import (  # type: ignore
    EXPORT_STAGING_WRITER_LOCK_NAME,
)

SCRIPT = ROOT / "scripts" / "snapshot_export_staging_cleanup_plan.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_staging_cleanup_plan.py"
STAGING_MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_staging.py"
BYOG_ROOTS = tuple(
    sorted(path for path in ROOT.iterdir() if path.is_dir() and path.name.startswith("byog_"))
)
FORBIDDEN = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
        "snapshot_activate",
        "snapshot_pin",
        "snapshot_unpin",
        "snapshot_prune",
        "snapshot_staging_cleanup",
        "snapshot_maintenance_apply",
        "snapshot_maintenance_plan",
        "snapshot_export_apply",
        "snapshot_export_plan",
        "snapshot_export_verify",
        "snapshot_export_reconcile",
        "snapshot_export_staging_cleanup",
        "snapshot_history",
        "snapshot_diff",
        "retained_snapshot_read",
        "audit_graph_root",
        "resolve_snapshot",
        "doctor_fingerprint",
        "validate_persisted_graph_integrity",
        "c_clang_ast_capture",
        "c_compiler_facts",
        "graph_read_lease",
        "graph_exclusive_lease",
        "_publication_lock",
        "probe_staging_writer_lease",
        "staging_writer_lease",
        "acquire_export_writer_lease",
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
        "mkdir",
        "chmod",
        "truncate",
        "read_bytes",
        "write_bytes",
        "readlink",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
HEX_A = "a" * 32
HEX_B = "b" * 32
HEX_C = "c" * 32
HEX_D = "d" * 32
HEX_E = "e" * 32
HEX_F = "f" * 32
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


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def _staging_name(suffix: str) -> str:
    return f".graphrag-export-{suffix}"


def _make_staging(parent: Path, suffix: str) -> Path:
    path = parent / _staging_name(suffix)
    path.mkdir()
    return path


def _write_candidate_lock(staging: Path) -> Path:
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")
    lock.chmod(0o600)
    return lock


def _entry(result: dict, name: str) -> dict:
    matches = [item for item in result["staging_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def _blocked(result: dict, name: str) -> dict:
    matches = [item for item in result["blocked_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def _notice_codes(result: dict) -> list[str]:
    return [notice["code"] for notice in result["notices"]]


def _assert_plan_shape(result: dict, parent: Path) -> None:
    assert result["schema_version"] == 2
    assert result["ok"] is True
    assert result["parent"] == str(parent.resolve())
    assert result["staging_count"] == len(result["staging_entries"])
    assert result["unrecognized_prefixed_count"] == len(
        result["unrecognized_prefixed_entries"]
    )
    assert result["deletion_candidate_count"] == len(result["deletion_candidates"])
    assert result["blocked_count"] == len(result["blocked_entries"])
    assert result["ownership_inference"] is False
    assert result["cleanup_applied"] is False
    assert result["apply_supported"] is True
    assert result["plan_revision"] == plan_revision_of(result)
    assert result["plan_revision"].startswith("sha256:")
    assert len(result["plan_revision"]) == len("sha256:") + 64
    assert result["observed_inventory_revision"].startswith("sha256:")
    assert _notice_codes(result) == [
        "plan_not_authorization",
        "observed_non_contention_not_claim",
        "apply_is_separate_cas_command",
        "inventory_semantics_unchanged",
        "cli_only_not_mcp",
    ]
    names = [entry["name"] for entry in result["staging_entries"]]
    assert names == sorted(names, key=os.fsencode)
    unrec = [entry["name"] for entry in result["unrecognized_prefixed_entries"]]
    assert unrec == sorted(unrec, key=os.fsencode)
    assert result["deletion_candidates"] == sorted(
        result["deletion_candidates"], key=os.fsencode
    )
    blocked_names = [item["name"] for item in result["blocked_entries"]]
    assert blocked_names == sorted(blocked_names, key=os.fsencode)
    for entry in result["staging_entries"]:
        assert entry["ownership"] == "unknown"
        assert entry["writer_activity"] == "unknown"
        assert entry["cleanup_eligible"] is False
        assert entry["contents_inspected"] is False
    text = format_result(result)
    assert str(parent.resolve()) in text
    assert "plan-only" in text
    assert "apply_supported=true" in text
    assert "snapshot-export-staging-cleanup is the separate CAS apply" in text
    assert "not authorization to delete" in text
    lowered = json.dumps(result).lower() + text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


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
        settings_text=f"export-cleanup-plan: {marker}\n",
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


def test_empty_parent(tmp_path: Path):
    parent = tmp_path / "empty"
    parent.mkdir()
    result = snapshot_export_staging_cleanup_plan(parent)
    _assert_plan_shape(result, parent)
    assert result["staging_entries"] == []
    assert result["deletion_candidates"] == []
    assert result["blocked_entries"] == []
    assert result["other_entry_count"] == 0
    inventory = snapshot_export_staging(parent)
    assert inventory["cleanup_supported"] is False
    assert inventory["cleanup_performed"] is False
    assert result["observed_inventory_revision"] == inventory["inventory_revision"]


def test_stable_empty_regular_nonheld_is_candidate(tmp_path: Path):
    parent = tmp_path / "candidate"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    lock = _write_candidate_lock(staging)
    payload = staging / "manifest.json"
    payload.write_bytes(b"must-not-open-payload")
    before = payload.stat()
    listing_before = sorted(path.name for path in parent.iterdir())
    result = snapshot_export_staging_cleanup_plan(parent)
    _assert_plan_shape(result, parent)
    assert result["deletion_candidates"] == [staging.name]
    assert result["blocked_entries"] == []
    entry = _entry(result, staging.name)
    assert entry["kind"] == "directory"
    assert entry["writer_lease_state"] == "not_held_at_scan"
    assert entry["writer_lease_metadata_present"] is True
    assert entry["writer_lease_contended"] is False
    assert entry["writer_lease_size"] == 0
    assert stat.S_IMODE(entry["writer_lease_mode"]) & 0o077 == 0
    assert entry["writer_activity"] == "unknown"
    assert entry["cleanup_eligible"] is False
    assert entry["contents_inspected"] is False
    assert payload.read_bytes() == b"must-not-open-payload"
    assert payload.stat().st_mtime_ns == before.st_mtime_ns
    assert lock.is_file()
    assert sorted(path.name for path in parent.iterdir()) == listing_before
    dumped = result_to_json(result)
    assert "must-not-open-payload" not in dumped
    inventory = snapshot_export_staging(parent)
    assert inventory["cleanup_supported"] is False
    assert _entry(inventory, staging.name)["cleanup_eligible"] is False
    assert result["observed_inventory_revision"] == inventory["inventory_revision"]
    incomplete = dict(entry)
    incomplete["writer_lease_ctime_ns"] = None
    assert _classify_entry(incomplete) == (
        "blocked",
        "unverifiable_writer_lease_state",
    )


def test_live_paused_apply_is_held_writer_lease(tmp_path: Path):
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
        started = time.monotonic()
        result = snapshot_export_staging_cleanup_plan(dest.parent)
        assert time.monotonic() - started < 5
        _assert_plan_shape(result, dest.parent)
        assert result["deletion_candidates"] == []
        assert _blocked(result, leftovers[0].name)["reason"] == "held_writer_lease"
        entry = _entry(result, leftovers[0].name)
        assert entry["writer_lease_state"] == "held_at_scan"
        assert entry["writer_lease_contended"] is True
        assert entry["writer_activity"] == "unknown"
        assert entry["cleanup_eligible"] is False
        resume.set()
        proc.join(timeout=TIMEOUT)
        assert not proc.is_alive()
        message = q.get(timeout=TIMEOUT)
        assert message[0] == "ok"
        assert dest.is_dir()
        assert not (dest / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
    finally:
        _cleanup_processes(proc, release=resume)


def test_classification_table_and_unrelated_entries(tmp_path: Path):
    parent = tmp_path / "kinds"
    parent.mkdir()
    absent = _make_staging(parent, HEX_A)
    candidate = _make_staging(parent, HEX_B)
    _write_candidate_lock(candidate)
    nonempty = _make_staging(parent, HEX_C)
    nonempty_lock = nonempty / EXPORT_STAGING_WRITER_LOCK_NAME
    nonempty_lock.write_bytes(b"xx")
    nonempty_lock.chmod(0o600)
    permissive = _make_staging(parent, HEX_D)
    permissive_lock = permissive / EXPORT_STAGING_WRITER_LOCK_NAME
    permissive_lock.write_bytes(b"")
    permissive_lock.chmod(0o644)
    named_file = parent / _staging_name(HEX_E)
    named_file.write_bytes(b"not-a-directory")
    unrec = parent / ".graphrag-export-short"
    unrec.mkdir()
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"must-not-follow")
    (unrec / EXPORT_STAGING_WRITER_LOCK_NAME).symlink_to(secret)
    (parent / "notes.txt").write_text("keep", encoding="utf-8")
    (parent / "current").write_text("not-a-graph-read", encoding="utf-8")
    listing_before = sorted(path.name for path in parent.iterdir())
    result = snapshot_export_staging_cleanup_plan(parent)
    _assert_plan_shape(result, parent)
    assert result["deletion_candidates"] == [candidate.name]
    reasons = {item["name"]: item["reason"] for item in result["blocked_entries"]}
    assert reasons[absent.name] == "writer_lease_metadata_absent"
    assert reasons[nonempty.name] == "nonempty_writer_lease_metadata"
    assert reasons[permissive.name] == "permissive_writer_lease_metadata"
    assert reasons[named_file.name] == "non_directory_staging_entry"
    assert reasons[unrec.name] == "unrecognized_staging_name"
    assert result["other_entry_count"] == 2
    dumped = result_to_json(result)
    assert "must-not-follow" not in dumped
    assert '"notes.txt"' not in dumped
    assert '"current"' not in dumped
    assert secret.read_bytes() == b"must-not-follow"
    assert not (absent / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
    assert sorted(path.name for path in parent.iterdir()) == listing_before
    for entry in result["staging_entries"]:
        assert entry["cleanup_eligible"] is False
        assert entry["writer_activity"] == "unknown"


def test_unsafe_writer_metadata_is_blocked_without_following(tmp_path: Path):
    parent = tmp_path / "unsafe"
    parent.mkdir()
    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"replacement-target")
    linked = _make_staging(parent, HEX_A)
    (linked / EXPORT_STAGING_WRITER_LOCK_NAME).symlink_to(secret)
    fifo_dir = _make_staging(parent, HEX_B)
    os.mkfifo(fifo_dir / EXPORT_STAGING_WRITER_LOCK_NAME)
    dir_lock = _make_staging(parent, HEX_C)
    (dir_lock / EXPORT_STAGING_WRITER_LOCK_NAME).mkdir()
    hard = _make_staging(parent, HEX_D)
    lock = _write_candidate_lock(hard)
    extra = tmp_path / "hard-extra"
    os.link(lock, extra)
    before = secret.stat()
    result = snapshot_export_staging_cleanup_plan(parent)
    _assert_plan_shape(result, parent)
    assert result["deletion_candidates"] == []
    reasons = {item["name"]: item["reason"] for item in result["blocked_entries"]}
    assert reasons[linked.name] == "writer_lease_metadata_unsafe"
    assert reasons[fifo_dir.name] == "writer_lease_metadata_unsafe"
    assert reasons[dir_lock.name] == "writer_lease_metadata_unsafe"
    assert reasons[hard.name] == "writer_lease_metadata_unsafe"
    assert secret.read_bytes() == b"replacement-target"
    assert secret.stat().st_mtime_ns == before.st_mtime_ns
    dumped = result_to_json(result)
    assert "replacement-target" not in dumped
    extra.unlink()


def test_lock_replacement_never_follows_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_writer_lease as lease_mod

    parent = tmp_path / "replace"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    lock = _write_candidate_lock(staging)
    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"replacement-target")
    before = secret.stat()

    def replace_before_open(_path):
        if lock.exists() and not lock.is_symlink():
            lock.unlink()
            lock.symlink_to(secret)

    monkeypatch.setattr(lease_mod, "_after_export_writer_lock_path_inspected", replace_before_open)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError, match="changed|unsafe"
    ):
        snapshot_export_staging_cleanup_plan(parent)
    assert secret.read_bytes() == b"replacement-target"
    assert secret.stat().st_mtime_ns == before.st_mtime_ns
    assert lock.is_symlink()

    lock.unlink()
    lock.write_bytes(b"")
    lock.chmod(0o600)
    planted = tmp_path / "planted-lock"
    planted.write_bytes(b"do-not-lock")

    def replace_after_open(_path, _lock_fd):
        lock.unlink()
        lock.symlink_to(planted)

    monkeypatch.setattr(lease_mod, "_after_export_writer_lock_path_inspected", lambda *_: None)
    monkeypatch.setattr(lease_mod, "_after_export_writer_lock_opened", replace_after_open)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError, match="changed|unsafe"
    ):
        snapshot_export_staging_cleanup_plan(parent)
    assert planted.read_bytes() == b"do-not-lock"
    assert lock.is_symlink()


def test_scan_to_scan_identity_and_lease_state_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod
    from graphrag_code.byog_graph import _release_lock, _try_acquire_exclusive_lock

    parent = tmp_path / "delta"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    lock = _write_candidate_lock(staging)

    def chmod_lock(_path, _scan):
        lock.chmod(0o640)

    monkeypatch.setattr(staging_mod, "_after_first_scan", chmod_lock)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError,
        match="writer-lease identity|entry metadata|entry identity",
    ):
        snapshot_export_staging_cleanup_plan(parent)
    lock.chmod(0o600)

    def rewrite_lock(_path, _scan):
        lock.write_bytes(b"x")

    monkeypatch.setattr(staging_mod, "_after_first_scan", rewrite_lock)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError,
        match="writer-lease identity|entry metadata|entry identity",
    ):
        snapshot_export_staging_cleanup_plan(parent)
    lock.write_bytes(b"")
    lock.chmod(0o600)

    def replace_inode(_path, _scan):
        other = tmp_path / "other-lock"
        other.write_bytes(b"")
        other.chmod(0o600)
        lock.unlink()
        other.rename(lock)

    monkeypatch.setattr(staging_mod, "_after_first_scan", replace_inode)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError,
        match="writer-lease identity|entry identity|entry metadata",
    ):
        snapshot_export_staging_cleanup_plan(parent)

    holder = {"fd": None, "backend": None}

    def take_lease(_path, _scan):
        fd = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW)
        holder["fd"] = fd
        holder["backend"] = _try_acquire_exclusive_lock(fd)

    monkeypatch.setattr(staging_mod, "_after_first_scan", take_lease)
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupPlanIntegrityError, match="writer-lease state"
        ):
            snapshot_export_staging_cleanup_plan(parent)
    finally:
        if holder["fd"] is not None:
            if holder["backend"] is not None:
                try:
                    _release_lock(holder["fd"], holder["backend"])
                except OSError:
                    pass
            os.close(holder["fd"])


def test_same_size_rewrite_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "rewrite"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"ab")
    lock.chmod(0o600)

    def rewrite_same_size(_path, _scan):
        before = lock.stat()
        lock.write_bytes(b"cd")
        os.utime(lock, ns=(before.st_atime_ns, before.st_mtime_ns))

    monkeypatch.setattr(staging_mod, "_after_first_scan", rewrite_same_size)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError,
        match="writer-lease identity|entry metadata",
    ):
        snapshot_export_staging_cleanup_plan(parent)
    assert lock.read_bytes() == b"cd"


def test_parent_replacement_and_rename_away_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "held"
    parent.mkdir()
    original = _make_staging(parent, HEX_A)
    _write_candidate_lock(original)
    replacement = tmp_path / "after-replacement"
    replacement.mkdir()
    planted = _make_staging(replacement, HEX_B)
    payload = planted / "payload.bin"
    payload.write_bytes(b"after-target")
    hidden = tmp_path / "held-hidden"

    def replace_after_open(path, parent_fd):
        if path == parent.resolve():
            os.fstat(parent_fd)
            parent.rename(hidden)
            parent.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(staging_mod, "_after_parent_opened", replace_after_open)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError, match="changed|symlink|unsafe"
    ):
        snapshot_export_staging_cleanup_plan(parent)
    assert payload.read_bytes() == b"after-target"
    assert (hidden / original.name).is_dir()
    assert parent.is_symlink()

    monkeypatch.setattr(staging_mod, "_after_parent_opened", lambda *_: None)
    bounced = tmp_path / "bounced"
    bounced.mkdir()
    _write_candidate_lock(_make_staging(bounced, HEX_A))
    parked = tmp_path / "bounced-parked"

    def rename_away_and_back(_path, _scan):
        before = bounced.stat()
        bounced.rename(parked)
        parked.rename(bounced)
        after = bounced.stat()
        if (
            after.st_ctime_ns == before.st_ctime_ns
            and after.st_mtime_ns == before.st_mtime_ns
        ):
            os.utime(bounced, ns=(after.st_atime_ns, after.st_mtime_ns + 1))

    monkeypatch.setattr(staging_mod, "_after_first_scan", rename_away_and_back)
    with pytest.raises(
        SnapshotExportStagingCleanupPlanIntegrityError,
        match="parent identity|parent changed",
    ):
        snapshot_export_staging_cleanup_plan(bounced)


def test_payload_contents_are_not_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_export_staging as staging_mod
    import graphrag_code.snapshot_export_writer_lease as lease_mod

    parent = tmp_path / "payloads"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    _write_candidate_lock(staging)
    secret = staging / "manifest.json"
    secret.write_bytes(b"unique-payload-bytes-not-for-classification")
    opened: list[str] = []
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        name = os.fsdecode(path) if isinstance(path, (bytes, bytearray)) else str(path)
        if "manifest.json" in Path(name).parts or Path(name).name == "manifest.json":
            opened.append(name)
            raise AssertionError(f"opened export payload: {name}")
        return original_open(path, flags, *args, **kwargs)

    def install_guard(_path, _parent_fd):
        monkeypatch.setattr(staging_mod.os, "open", guarded_open)
        monkeypatch.setattr(lease_mod.os, "open", guarded_open)
        supports = set(lease_mod.os.supports_dir_fd)
        supports.add(guarded_open)
        monkeypatch.setattr(lease_mod.os, "supports_dir_fd", supports)

    monkeypatch.setattr(staging_mod, "_after_parent_opened", install_guard)
    result = snapshot_export_staging_cleanup_plan(parent)
    assert result["deletion_candidates"] == [staging.name]
    assert opened == []
    dumped = result_to_json(result)
    assert "unique-payload-bytes-not-for-classification" not in dumped
    assert secret.read_bytes() == b"unique-payload-bytes-not-for-classification"


def test_raw_filesystem_byte_ordering(tmp_path: Path):
    parent = tmp_path / "order"
    parent.mkdir()
    later = _make_staging(parent, HEX_C)
    earlier = _make_staging(parent, HEX_A)
    mid = _make_staging(parent, HEX_B)
    _write_candidate_lock(later)
    _write_candidate_lock(earlier)
    _write_candidate_lock(mid)
    unrec_b = parent / ".graphrag-export-bbb"
    unrec_a = parent / ".graphrag-export-aaa"
    unrec_b.mkdir()
    unrec_a.mkdir()
    result = snapshot_export_staging_cleanup_plan(parent)
    _assert_plan_shape(result, parent)
    expected = sorted(
        [later.name, earlier.name, mid.name], key=os.fsencode
    )
    assert result["deletion_candidates"] == expected
    assert [entry["name"] for entry in result["staging_entries"]] == expected
    assert [entry["name"] for entry in result["unrecognized_prefixed_entries"]] == sorted(
        [unrec_a.name, unrec_b.name], key=os.fsencode
    )
    assert [item["name"] for item in result["blocked_entries"]] == sorted(
        [unrec_a.name, unrec_b.name], key=os.fsencode
    )


def test_missing_parent_and_rejected_apply_flags(tmp_path: Path):
    missing = _run("--parent", str(tmp_path / "missing"), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "does not exist" in missing.stderr

    extra = _run(
        "--parent",
        str(tmp_path),
        "--expected-plan-revision",
        "sha256:" + ("00" * 32),
        "--json",
    )
    assert extra.returncode == 2
    assert extra.stdout == ""

    confirmed = _run("--parent", str(tmp_path), "--confirmed", "--json")
    assert confirmed.returncode == 2
    assert confirmed.stdout == ""


def test_deterministic_plan_revision_and_json(tmp_path: Path):
    parent = tmp_path / "stable"
    parent.mkdir()
    first_dir = _make_staging(parent, HEX_B)
    second_dir = _make_staging(parent, HEX_A)
    _write_candidate_lock(first_dir)
    _write_candidate_lock(second_dir)
    (parent / "notes.txt").write_text("other", encoding="utf-8")
    (parent / ".graphrag-export-short").mkdir()
    first = snapshot_export_staging_cleanup_plan(parent)
    second = snapshot_export_staging_cleanup_plan(parent)
    _assert_plan_shape(first, parent)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    assert first["plan_revision"] == plan_revision_of(first)
    expected = (
        "sha256:"
        + hashlib.sha256(canonical_plan_revision_text(first).encode("utf-8")).hexdigest()
    )
    assert first["plan_revision"] == expected
    payload = json.loads(canonical_plan_revision_text(first))
    assert list(payload) == sorted(payload)
    assert set(payload) == {
        "apply_supported",
        "blocked_entries",
        "cleanup_applied",
        "deletion_candidates",
        "observed_inventory_revision",
        "ownership_inference",
        "schema_version",
    }
    assert "parent" not in payload
    assert "notices" not in payload
    assert "ok" not in payload
    assert "staging_count" not in payload
    assert "deletion_candidate_count" not in payload
    assert "blocked_count" not in payload
    assert "staging_entries" not in payload
    assert payload["schema_version"] == 2
    assert payload["deletion_candidates"] == first["deletion_candidates"]
    assert payload["blocked_entries"] == first["blocked_entries"]
    assert payload["observed_inventory_revision"] == first["observed_inventory_revision"]
    assert payload["ownership_inference"] is False
    assert payload["cleanup_applied"] is False
    assert payload["apply_supported"] is True
    encoded = result_to_json(first)
    assert encoded.endswith("\n")
    parsed = json.loads(encoded)
    assert list(parsed) == sorted(parsed)
    inventory = snapshot_export_staging(parent)
    assert first["observed_inventory_revision"] == inventory["inventory_revision"]


def test_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    parent = here / "parent"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    _write_candidate_lock(staging)
    args = ["--parent", "parent", "--json"]
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.snapshot_export_staging_cleanup_plan",
            *args,
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-staging-cleanup-plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, (
        module.stderr,
        script.stderr,
        cli.stderr,
    )
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["parent"] == str(parent.resolve())
    assert bodies[0]["deletion_candidates"] == [staging.name]
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-staging-cleanup-plan",
            "--parent",
            str(parent),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["plan_revision"] == bodies[0]["plan_revision"]

    import tarfile
    import zipfile

    with zipfile.ZipFile(built_wheel_and_sdist[0]) as zf:
        names = zf.namelist()
    assert "graphrag_code/snapshot_export_staging_cleanup_plan.py" in names
    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        snames = "\n".join(tf.getnames())
    assert "snapshot_export_staging_cleanup_plan.py" in snames


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod
    import graphrag_code.snapshot_export_staging_cleanup_plan as plan_mod

    parent = tmp_path / "held"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    _write_candidate_lock(staging)
    original_json = plan_mod.result_to_json
    original_format = plan_mod.format_result
    state = {
        "parent_fd": None,
        "staging_fd": None,
        "lock_fd": None,
        "writes": 0,
        "flushes": 0,
    }

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
    monkeypatch.setattr(plan_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(plan_mod, "format_result", guarded_format)
    monkeypatch.setattr(plan_mod.sys, "stdout", GuardedStdout())
    assert plan_mod.main(["--parent", str(parent), "--json"]) == 0
    assert plan_mod.main(["--parent", str(parent)]) == 0
    assert state["writes"] >= 2
    assert state["flushes"] == 2


def test_implementation_does_not_mutate_or_invoke_producers():
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert (imported | called) & FORBIDDEN == set()
    assert "graph_read_lease" not in source
    assert "graph_exclusive_lease" not in source
    assert "snapshot_export_staging(" not in source
    assert "snapshot_export_apply(" not in source
    assert "acquire_export_writer_lease" not in source
    assert "publish_byog_snapshot" not in source
    assert "read_bytes" not in source
    assert "write_bytes" not in source
    assert "export_staging_observation_scope" in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    human = format_result(
        {
            "parent": "/tmp/x",
            "staging_count": 0,
            "unrecognized_prefixed_count": 0,
            "other_entry_count": 0,
            "deletion_candidate_count": 0,
            "blocked_count": 0,
            "observed_inventory_revision": "sha256:" + ("00" * 32),
            "plan_revision": "sha256:" + ("11" * 32),
            "deletion_candidates": [],
            "blocked_entries": [],
            "ok": True,
        }
    )
    assert "plan-only" in human
    assert "apply_supported=true" in human
    assert "snapshot-export-staging-cleanup is the separate CAS apply" in human
    assert "not authorization to delete" in human


def test_mcp_remains_exactly_fifteen_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 15
    assert "snapshot_export_staging" not in TOOL_NAMES
    assert "snapshot_export_staging_cleanup_plan" not in TOOL_NAMES
    assert "snapshot_export_staging_cleanup" not in TOOL_NAMES
    graph = tmp_path / "g"
    _publish(graph, "a")
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 15
            assert "snapshot_export_staging_cleanup_plan" not in names
            assert "snapshot_export_staging_cleanup" not in names

    anyio_run(_body)
    after = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert after == before


def test_repository_has_no_export_staging_artifacts():
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
