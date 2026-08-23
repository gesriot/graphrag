"""CAS-verified snapshot-export staging cleanup apply.

Disposable tmp parents only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_staging_cleanup.py -q
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
from graphrag_code.snapshot_export_staging_cleanup import (  # type: ignore
    SnapshotExportStagingCleanupError,
    SnapshotExportStagingCleanupIntegrityError,
    format_result,
    parse_plan_revision,
    result_to_json,
    snapshot_export_staging_cleanup,
)
from graphrag_code.snapshot_export_staging_cleanup_plan import (  # type: ignore
    canonical_plan_revision_payload,
    snapshot_export_staging_cleanup_plan,
)
from graphrag_code.snapshot_export_writer_lease import (  # type: ignore
    EXPORT_STAGING_WRITER_LOCK_NAME,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_export_staging_cleanup.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_staging_cleanup.py"
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
        "snapshot_export_apply",
        "snapshot_export_plan",
        "snapshot_export_verify",
        "snapshot_export_reconcile",
        "graph_read_lease",
        "graph_exclusive_lease",
        "rmtree",
        "acquire_export_writer_lease",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
HEX_A = "a" * 32
HEX_B = "b" * 32
HEX_C = "c" * 32
HEX_D = "d" * 32
BYOG_ROOTS = tuple(
    sorted(path for path in ROOT.iterdir() if path.is_dir() and path.name.startswith("byog_"))
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


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def _apply_args(parent: Path, revision: str, *extra: str) -> list[str]:
    return [
        "--parent",
        str(parent),
        "--expected-plan-revision",
        revision,
        *extra,
    ]


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


def _candidate(parent: Path, suffix: str, *, payload: bytes = b"payload") -> Path:
    staging = _make_staging(parent, suffix)
    (staging / "manifest.json").write_bytes(payload)
    _write_candidate_lock(staging)
    return staging


def _schema1_revision(plan: dict) -> str:
    payload = canonical_plan_revision_payload(plan)
    payload["schema_version"] = 1
    payload["apply_supported"] = False
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


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
        settings_text=f"export-cleanup: {marker}\n",
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


def _root_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        info = os.lstat(dirpath)
        digest.update(rel_dir.encode())
        digest.update(str(stat.S_IMODE(info.st_mode)).encode())
        digest.update(str(info.st_ino).encode())
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            child = path.lstat()
            digest.update(rel.encode())
            digest.update(str(stat.S_IMODE(child.st_mode)).encode())
            digest.update(str(child.st_ino).encode())
            digest.update(str(child.st_size).encode())
            if stat.S_ISREG(child.st_mode):
                digest.update(path.read_bytes())
    return digest.hexdigest()


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


def _writer_lease_hold(staging_dir: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import _release_lock, _try_acquire_exclusive_lock

    lock = ChildPath(staging_dir) / EXPORT_STAGING_WRITER_LOCK_NAME
    fd = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW)
    backend = None
    try:
        backend = _try_acquire_exclusive_lock(fd)
        held.set()
        if not resume.wait(timeout=TIMEOUT):
            q.put("timeout")
            return
        q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")
    finally:
        if backend is not None:
            try:
                _release_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)


def _probe_state(staging_dir: str, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import (
        StagingWriterLockContention,
        _release_lock,
        _try_acquire_exclusive_lock,
    )

    lock = ChildPath(staging_dir) / EXPORT_STAGING_WRITER_LOCK_NAME
    fd = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW)
    backend = None
    try:
        backend = _try_acquire_exclusive_lock(fd)
        q.put("not_held")
    except StagingWriterLockContention:
        q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")
    finally:
        if backend is not None:
            try:
                _release_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)


def _cleanup_child(parent: str, revision: str, started, go, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_export_staging_cleanup import (
        snapshot_export_staging_cleanup as child_cleanup,
    )

    started.set()
    if not go.wait(timeout=TIMEOUT):
        q.put("timeout")
        return
    try:
        result = child_cleanup(
            ChildPath(parent), revision, cleanup_confirmed=True
        )
        q.put(
            (
                "ok",
                result["ok"],
                result["partial"],
                result["deleted_staging_entries"],
            )
        )
    except Exception as exc:
        q.put(("err", type(exc).__name__, str(exc)))


def test_missing_cleanup_confirmed_exits_2(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    listing = tuple(sorted(path.name for path in parent.iterdir()))
    missing = _run(*_apply_args(parent, plan["plan_revision"], "--json"))
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "--cleanup-confirmed" in missing.stderr
    with pytest.raises(SnapshotExportStagingCleanupError, match="cleanup-confirmed"):
        snapshot_export_staging_cleanup(
            parent, plan["plan_revision"], cleanup_confirmed=False
        )
    assert leftover.is_dir()
    assert tuple(sorted(path.name for path in parent.iterdir())) == listing


def test_empty_plan_still_requires_confirmation(tmp_path: Path):
    parent = tmp_path / "empty"
    parent.mkdir()
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert plan["deletion_candidates"] == []
    missing = _run(*_apply_args(parent, plan["plan_revision"], "--json"))
    assert missing.returncode == 2
    assert missing.stdout == ""


def test_malformed_revision_tokens(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    for token in (
        "",
        "sha256:",
        "SHA256:" + ("a" * 64),
        "sha256:" + ("A" * 64),
        "sha256:" + ("a" * 63),
        "sha256:" + ("a" * 65),
        " sha256:" + ("a" * 64),
        "sha256:" + ("a" * 64) + " ",
        "md5:" + ("a" * 32),
    ):
        with pytest.raises(SnapshotExportStagingCleanupError, match="sha256"):
            parse_plan_revision(token)
        proc = _run(*_apply_args(parent, token, "--cleanup-confirmed", "--json"))
        assert proc.returncode == 2, token
        assert proc.stdout == ""
        assert leftover.is_dir()


def test_stale_and_schema1_revisions_are_rejected(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert plan["schema_version"] == 2
    stale = "sha256:" + ("0" * 64)
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError, match="does not match"):
        snapshot_export_staging_cleanup(parent, stale, cleanup_confirmed=True)
    schema1 = _schema1_revision(plan)
    assert schema1 != plan["plan_revision"]
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError, match="does not match"):
        snapshot_export_staging_cleanup(parent, schema1, cleanup_confirmed=True)
    proc = _run(*_apply_args(parent, schema1, "--cleanup-confirmed", "--json"))
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert leftover.is_dir()


def test_empty_matching_plan_is_successful_noop(tmp_path: Path):
    parent = tmp_path / "empty"
    parent.mkdir()
    (parent / "notes.txt").write_text("keep", encoding="utf-8")
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert plan["deletion_candidates"] == []
    listing = tuple(sorted(path.name for path in parent.iterdir()))
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["ok"] is True
    assert result["schema_version"] == 1
    assert result["changed"] is False
    assert result["partial"] is False
    assert result["filesystem_may_have_changed"] is False
    assert result["deleted_staging_entries"] == []
    assert result["deleted_count"] == 0
    assert result["planned_deletion_candidates"] == []
    assert result["remaining_staging_entries"] == []
    assert result["retry_requires_fresh_plan"] is False
    assert result["cleanup_confirmed"] is True
    assert result["ownership_inference"] is False
    assert result["error"] is None
    assert result["parent"] == str(parent.resolve())
    assert tuple(sorted(path.name for path in parent.iterdir())) == listing
    assert (parent / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_successful_cleanup_of_one_and_multiple_candidates(tmp_path: Path):
    parent = tmp_path / "one"
    parent.mkdir()
    one = _candidate(parent, HEX_A, payload=b"unique-one")
    plan = snapshot_export_staging_cleanup_plan(parent)
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["changed"] is True
    assert result["filesystem_may_have_changed"] is True
    assert result["retry_requires_fresh_plan"] is False
    assert result["deleted_staging_entries"] == [one.name]
    assert result["deleted_count"] == 1
    assert result["remaining_staging_entries"] == []
    assert result["failed_staging_entry"] is None
    assert result["not_attempted_staging_entries"] == []
    assert "unique-one" not in result_to_json(result)
    assert not one.exists()

    many = tmp_path / "many"
    many.mkdir()
    first = _candidate(many, HEX_C)
    second = _candidate(many, HEX_A)
    third = _candidate(many, HEX_B)
    plan2 = snapshot_export_staging_cleanup_plan(many)
    seen: list[str] = []
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    original = cleanup_mod._remove_claimed_staging_entry

    def tracked(observation, name, claim, entry):
        seen.append(name)
        return original(observation, name, claim, entry)

    cleanup_mod._remove_claimed_staging_entry = tracked
    try:
        result2 = snapshot_export_staging_cleanup(
            many, plan2["plan_revision"], cleanup_confirmed=True
        )
    finally:
        cleanup_mod._remove_claimed_staging_entry = original
    assert seen == plan2["deletion_candidates"]
    assert seen == sorted(seen, key=os.fsencode)
    assert result2["deleted_staging_entries"] == seen
    assert not first.exists() and not second.exists() and not third.exists()


def test_blocked_unrecognized_and_unrelated_entries_remain(tmp_path: Path):
    parent = tmp_path / "mixed"
    parent.mkdir()
    leftover = _candidate(parent, HEX_B)
    absent = _make_staging(parent, HEX_A)
    unrec = parent / ".graphrag-export-short"
    unrec.mkdir()
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"must-not-follow")
    (unrec / EXPORT_STAGING_WRITER_LOCK_NAME).symlink_to(secret)
    named_file = parent / _staging_name(HEX_C)
    named_file.write_bytes(b"not-a-directory")
    notes = parent / "notes.txt"
    notes.write_text("keep", encoding="utf-8")
    current = parent / "current"
    current.write_text("not-a-graph-read", encoding="utf-8")
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert leftover.name in plan["deletion_candidates"]
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["deleted_staging_entries"] == [leftover.name]
    remaining = set(result["remaining_staging_entries"])
    assert leftover.name not in remaining
    assert absent.name in remaining
    assert unrec.name in remaining
    assert named_file.name in remaining
    assert not leftover.exists()
    assert absent.is_dir()
    assert unrec.is_dir()
    assert named_file.is_file()
    assert notes.read_text(encoding="utf-8") == "keep"
    assert current.read_text(encoding="utf-8") == "not-a-graph-read"
    assert secret.read_bytes() == b"must-not-follow"
    assert '"notes.txt"' not in result_to_json(result)
    assert "must-not-follow" not in result_to_json(result)


def test_live_paused_export_apply_is_never_deleted(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "live-out"
    parent = dest.parent
    leftover = _candidate(parent, HEX_A)
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
        live = [path for path in _leftover_staging(parent) if path != leftover]
        assert len(live) == 1
        cleanup_plan = snapshot_export_staging_cleanup_plan(parent)
        assert leftover.name in cleanup_plan["deletion_candidates"]
        assert live[0].name not in cleanup_plan["deletion_candidates"]
        result = snapshot_export_staging_cleanup(
            parent, cleanup_plan["plan_revision"], cleanup_confirmed=True
        )
        assert result["deleted_staging_entries"] == [leftover.name]
        assert live[0].name in result["remaining_staging_entries"]
        assert not leftover.exists()
        assert live[0].is_dir()
        resume.set()
        proc.join(timeout=TIMEOUT)
        assert not proc.is_alive()
        message = q.get(timeout=TIMEOUT)
        assert message[0] == "ok"
        assert dest.is_dir()
    finally:
        _cleanup_processes(proc, release=resume)


def test_all_claims_complete_before_first_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "order"
    parent.mkdir()
    first = _candidate(parent, HEX_A)
    second = _candidate(parent, HEX_C)
    plan = snapshot_export_staging_cleanup_plan(parent)
    phases: list[str] = []
    orig_claims = cleanup_mod._after_cleanup_writer_claims
    orig_revalidate = cleanup_mod._after_cleanup_revalidation
    orig_before = cleanup_mod._before_cleanup_deletion
    orig_remove = cleanup_mod._remove_claimed_staging_entry

    def after_claims(claims):
        phases.append(f"claims:{len(claims)}")
        return orig_claims(claims)

    def after_revalidate(observation, claims):
        phases.append(f"revalidate:{len(claims)}")
        assert first.is_dir() and second.is_dir()
        return orig_revalidate(observation, claims)

    def before_delete(observation):
        phases.append("before_delete")
        assert first.is_dir() and second.is_dir()
        return orig_before(observation)

    def tracked(observation, name, claim, entry):
        phases.append(f"delete:{name}")
        return orig_remove(observation, name, claim, entry)

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_writer_claims", after_claims)
    monkeypatch.setattr(cleanup_mod, "_after_cleanup_revalidation", after_revalidate)
    monkeypatch.setattr(cleanup_mod, "_before_cleanup_deletion", before_delete)
    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", tracked)
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert phases[:3] == ["claims:2", "revalidate:2", "before_delete"]
    assert phases[3:] == [f"delete:{name}" for name in plan["deletion_candidates"]]
    assert result["deleted_staging_entries"] == plan["deletion_candidates"]


def test_writer_lock_held_during_payload_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "held"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    lock = leftover / EXPORT_STAGING_WRITER_LOCK_NAME
    plan = snapshot_export_staging_cleanup_plan(parent)
    seen_held: list[str] = []
    orig_remove = cleanup_mod._remove_child_nofollow

    def tracked_remove(dir_fd, name):
        if name != EXPORT_STAGING_WRITER_LOCK_NAME:
            q = CTX.Queue()
            probe = CTX.Process(target=_probe_state, args=(str(leftover), q))
            probe.start()
            probe.join(timeout=TIMEOUT)
            seen_held.append(q.get(timeout=5))
            assert lock.exists()
        return orig_remove(dir_fd, name)

    released: list[str] = []
    orig_release = cleanup_mod.HeldExportWriterLease.release_and_remove

    def tracked_release(self):
        assert lock.exists()
        assert not (leftover / "manifest.json").exists()
        assert leftover.is_dir()
        released.append("before")
        orig_release(self)
        released.append("after")
        assert not lock.exists()
        assert leftover.is_dir()

    monkeypatch.setattr(cleanup_mod, "_remove_child_nofollow", tracked_remove)
    monkeypatch.setattr(
        cleanup_mod.HeldExportWriterLease, "release_and_remove", tracked_release
    )
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()
    assert seen_held
    assert set(seen_held) == {"held"}
    assert released == ["before", "after"]


def test_two_concurrent_cleanup_invocations_fail_safely(tmp_path: Path):
    parent = tmp_path / "race"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    started_a = CTX.Event()
    started_b = CTX.Event()
    go = CTX.Event()
    q_a = CTX.Queue()
    q_b = CTX.Queue()
    proc_a = CTX.Process(
        target=_cleanup_child,
        args=(str(parent), plan["plan_revision"], started_a, go, q_a),
    )
    proc_b = CTX.Process(
        target=_cleanup_child,
        args=(str(parent), plan["plan_revision"], started_b, go, q_b),
    )
    try:
        proc_a.start()
        proc_b.start()
        assert started_a.wait(timeout=TIMEOUT)
        assert started_b.wait(timeout=TIMEOUT)
        go.set()
        proc_a.join(timeout=TIMEOUT)
        proc_b.join(timeout=TIMEOUT)
        assert not proc_a.is_alive() and not proc_b.is_alive()
        outcomes = [q_a.get(timeout=5), q_b.get(timeout=5)]
        oks = [item for item in outcomes if item[0] == "ok" and item[1] is True]
        errs = [item for item in outcomes if item[0] == "err"]
        partials = [
            item for item in outcomes if item[0] == "ok" and item[2] is True
        ]
        assert len(oks) <= 1
        assert not leftover.exists() or leftover.is_dir()
        assert partials == []
        if leftover.exists():
            assert len(oks) == 0
            assert len(errs) == 2
        else:
            assert len(oks) == 1
            assert len(errs) == 1
    finally:
        _cleanup_processes(proc_a, proc_b, release=go)


def test_contention_after_plan_observation(tmp_path: Path):
    parent = tmp_path / "later"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    preview = snapshot_export_staging_cleanup_plan(parent)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_writer_lease_hold, args=(str(leftover), held, resume, q)
    )
    holder.start()
    try:
        assert held.wait(timeout=TIMEOUT)
        with pytest.raises(
            SnapshotExportStagingCleanupIntegrityError, match="does not match"
        ):
            snapshot_export_staging_cleanup(
                parent, preview["plan_revision"], cleanup_confirmed=True
            )
        assert leftover.is_dir()
    finally:
        _cleanup_processes(holder, release=resume)


def test_contention_after_recompute_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "claim-race"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_writer_lease_hold, args=(str(leftover), held, resume, q)
    )
    original = cleanup_mod._after_cleanup_plan_recompute

    def acquire_after_recompute(observation, recomputed):
        original(observation, recomputed)
        holder.start()
        assert held.wait(timeout=TIMEOUT)

    monkeypatch.setattr(
        cleanup_mod, "_after_cleanup_plan_recompute", acquire_after_recompute
    )
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupIntegrityError,
            match="held by a cooperating",
        ):
            snapshot_export_staging_cleanup(
                parent, plan["plan_revision"], cleanup_confirmed=True
            )
        assert leftover.is_dir()
    finally:
        _cleanup_processes(holder, release=resume)


def test_later_candidate_contention_releases_earlier_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "later-claim"
    parent.mkdir()
    first = _candidate(parent, HEX_A)
    second = _candidate(parent, HEX_C)
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert plan["deletion_candidates"] == [first.name, second.name]
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_writer_lease_hold, args=(str(second), held, resume, q)
    )
    original = cleanup_mod._after_cleanup_plan_recompute
    orig_claim = cleanup_mod._claim_candidate
    claims: list = []

    def acquire_second(observation, recomputed):
        original(observation, recomputed)
        holder.start()
        assert held.wait(timeout=TIMEOUT)

    def tracked_claim(observation, name, entry):
        claim = orig_claim(observation, name, entry)
        claims.append(claim)
        return claim

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_plan_recompute", acquire_second)
    monkeypatch.setattr(cleanup_mod, "_claim_candidate", tracked_claim)
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupIntegrityError,
            match="held by a cooperating",
        ):
            snapshot_export_staging_cleanup(
                parent, plan["plan_revision"], cleanup_confirmed=True
            )
        assert first.is_dir() and second.is_dir()
        assert len(claims) == 1
        assert claims[0].closed is True
    finally:
        _cleanup_processes(holder, release=resume)


def test_lock_mutations_fail_closed_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"must-not-follow")
    before = secret.stat()

    def apply_with_hook(hook_name, mutate, match="changed|unsafe|disappeared|held"):
        parent = tmp_path / f"lock-{hook_name}-{mutate.__name__}"
        parent.mkdir()
        leftover = _candidate(parent, HEX_A)
        plan = snapshot_export_staging_cleanup_plan(parent)
        monkeypatch.setattr(cleanup_mod, hook_name, mutate)
        with pytest.raises(SnapshotExportStagingCleanupIntegrityError, match=match):
            snapshot_export_staging_cleanup(
                parent, plan["plan_revision"], cleanup_confirmed=True
            )
        assert leftover.is_dir()
        assert secret.read_bytes() == b"must-not-follow"
        assert secret.stat().st_mtime_ns == before.st_mtime_ns
        return leftover

    def disappear(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        lock.unlink()

    apply_with_hook("_after_cleanup_plan_recompute", disappear, "disappeared|changed|unsafe")

    def replace_inode(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        other = tmp_path / f"other-{time.time_ns()}"
        other.write_bytes(b"")
        other.chmod(0o600)
        lock.unlink()
        other.rename(lock)

    apply_with_hook("_after_cleanup_plan_recompute", replace_inode)

    def make_symlink(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        lock.unlink()
        lock.symlink_to(secret)

    apply_with_hook("_after_cleanup_plan_recompute", make_symlink)

    extra_links: list[Path] = []

    def hardlink(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        extra = tmp_path / f"hard-{time.time_ns()}"
        os.link(lock, extra)
        extra_links.append(extra)

    leftover = apply_with_hook("_after_cleanup_plan_recompute", hardlink)
    for extra in extra_links:
        extra.unlink()
    assert leftover.is_dir()

    def chmod_lock(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        lock.chmod(0o644)

    apply_with_hook("_after_cleanup_plan_recompute", chmod_lock)

    def nonempty(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        lock.write_bytes(b"x")

    apply_with_hook("_after_cleanup_plan_recompute", nonempty)

    def same_size_restore_mtime(observation, plan):
        lock = observation.parent_path / plan["deletion_candidates"][0] / EXPORT_STAGING_WRITER_LOCK_NAME
        info = lock.stat()
        lock.write_bytes(b"")
        os.utime(lock, ns=(info.st_atime_ns, info.st_mtime_ns))

    apply_with_hook("_after_cleanup_plan_recompute", same_size_restore_mtime)

    def symlink_after_claim(claims):
        lock = claims[0]._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME
        parked = tmp_path / f"parked-{time.time_ns()}"
        lock.rename(parked)
        lock.symlink_to(secret)

    parent = tmp_path / "after-claim"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    monkeypatch.setattr(
        cleanup_mod, "_after_cleanup_plan_recompute", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(cleanup_mod, "_after_cleanup_writer_claims", symlink_after_claim)
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError):
        snapshot_export_staging_cleanup(
            parent, plan["plan_revision"], cleanup_confirmed=True
        )
    assert leftover.is_dir()
    assert secret.read_bytes() == b"must-not-follow"


def test_parent_replacement_and_rename_away_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "replace-parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    parked = tmp_path / "parked-parent"

    def replace_parent(observation, recomputed):
        parent.rename(parked)
        parent.mkdir()
        _candidate(parent, HEX_A)

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_plan_recompute", replace_parent)
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError, match="parent"):
        snapshot_export_staging_cleanup(
            parent, plan["plan_revision"], cleanup_confirmed=True
        )
    assert leftover.is_dir()

    parent2 = tmp_path / "bounce-parent"
    parent2.mkdir()
    leftover2 = _candidate(parent2, HEX_A)
    plan2 = snapshot_export_staging_cleanup_plan(parent2)
    bounced = tmp_path / "bounced-parent"

    def bounce(observation, recomputed):
        parent2.rename(bounced)
        bounced.rename(parent2)

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_plan_recompute", bounce)
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError, match="parent"):
        snapshot_export_staging_cleanup(
            parent2, plan2["plan_revision"], cleanup_confirmed=True
        )
    assert leftover2.is_dir()


def test_candidate_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "replace-cand"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    parked = tmp_path / "parked-cand"

    def replace_dir(observation, recomputed):
        leftover.rename(parked)
        leftover.mkdir()
        (leftover / "manifest.json").write_bytes(b"new")
        _write_candidate_lock(leftover)

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_plan_recompute", replace_dir)
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError):
        snapshot_export_staging_cleanup(
            parent, plan["plan_revision"], cleanup_confirmed=True
        )
    assert leftover.is_dir()
    assert parked.is_dir()


def test_nested_symlink_fifo_and_special_entries(tmp_path: Path):
    parent = tmp_path / "nested"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A, payload=b"unique-payload-bytes-not-for-auth")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must-not-follow-nested")
    nested = leftover / "nested"
    nested.mkdir()
    (nested / "inner.txt").write_bytes(b"inner")
    (nested / "link").symlink_to(outside)
    os.mkfifo(leftover / "pipe")
    plan = snapshot_export_staging_cleanup_plan(parent)
    started = time.monotonic()
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert time.monotonic() - started < 5
    assert result["ok"] is True
    assert not leftover.exists()
    assert outside.read_bytes() == b"must-not-follow-nested"
    dumped = result_to_json(result)
    assert "unique-payload-bytes-not-for-auth" not in dumped
    assert "must-not-follow-nested" not in dumped


def test_failure_before_mutation_versus_partial_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "pre"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    original = cleanup_mod._remove_claimed_staging_entry

    def boom_before(observation, name, claim, entry):
        raise SnapshotExportStagingCleanupIntegrityError("injected before unlink")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", boom_before)
    exit_code = cleanup_mod.main(
        _apply_args(parent, plan["plan_revision"], "--cleanup-confirmed", "--json")
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert leftover.is_dir()
    assert (leftover / "manifest.json").is_file()
    assert (leftover / EXPORT_STAGING_WRITER_LOCK_NAME).is_file()

    parent2 = tmp_path / "partial"
    parent2.mkdir()
    first = _candidate(parent2, HEX_A)
    second = _candidate(parent2, HEX_C)
    plan2 = snapshot_export_staging_cleanup_plan(parent2)
    calls = {"n": 0}

    def fail_later(observation, name, claim, entry):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(observation, name, claim, entry)
        raise RuntimeError(f"injected later failure on {name}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later)
    later = snapshot_export_staging_cleanup(
        parent2, plan2["plan_revision"], cleanup_confirmed=True
    )
    assert later["ok"] is False
    assert later["partial"] is True
    assert later["changed"] is True
    assert later["filesystem_may_have_changed"] is True
    assert later["retry_requires_fresh_plan"] is True
    assert later["deleted_staging_entries"] == [plan2["deletion_candidates"][0]]
    assert later["failed_staging_entry"] == plan2["deletion_candidates"][1]
    assert later["not_attempted_staging_entries"] == []
    assert not first.exists()
    assert second.is_dir()
    text = format_result(later)
    assert "PARTIAL FAILURE" in text
    assert "no rollback" in text.lower()
    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", original)
    with pytest.raises(SnapshotExportStagingCleanupIntegrityError, match="does not match"):
        snapshot_export_staging_cleanup(
            parent2, plan2["plan_revision"], cleanup_confirmed=True
        )
    fresh = snapshot_export_staging_cleanup_plan(parent2)
    retry = snapshot_export_staging_cleanup(
        parent2, fresh["plan_revision"], cleanup_confirmed=True
    )
    assert retry["ok"] is True
    assert retry["deleted_staging_entries"] == [second.name]
    assert not second.exists()


def test_failure_after_writer_lock_unlink_is_reported_as_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod
    import graphrag_code.snapshot_export_writer_lease as lease_mod

    parent = tmp_path / "post-lock-unlink"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)

    def fail_after_unlink(_staging_path, _lock_fd):
        raise cleanup_mod.ExportWriterLeaseIntegrityError(
            "injected failure after writer-lock unlink"
        )

    monkeypatch.setattr(
        lease_mod,
        "_after_export_writer_lock_removed_while_held",
        fail_after_unlink,
    )
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["changed"] is False
    assert result["filesystem_may_have_changed"] is True
    assert result["retry_requires_fresh_plan"] is True
    assert result["deleted_staging_entries"] == []
    assert result["failed_staging_entry"] == leftover.name
    assert result["not_attempted_staging_entries"] == []
    assert "after writer-lock unlink" in result["error"]
    assert leftover.is_dir()
    assert not (leftover / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
    assert not (leftover / "manifest.json").exists()


def test_injected_first_candidate_failure_is_partial_without_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "first-fail"
    parent.mkdir()
    first = _candidate(parent, HEX_A)
    second = _candidate(parent, HEX_C)
    plan = snapshot_export_staging_cleanup_plan(parent)

    def boom(observation, name, claim, entry):
        raise RuntimeError(f"injected failure on {name}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", boom)
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["changed"] is False
    assert result["filesystem_may_have_changed"] is True
    assert result["retry_requires_fresh_plan"] is True
    assert result["deleted_staging_entries"] == []
    assert result["failed_staging_entry"] == plan["deletion_candidates"][0]
    assert result["not_attempted_staging_entries"] == plan["deletion_candidates"][1:]
    assert first.is_dir() and second.is_dir()


def test_stdout_empty_on_pre_mutation_cli_failures(tmp_path: Path):
    missing = _run("--parent", str(tmp_path / "missing"), "--expected-plan-revision", "sha256:" + ("a" * 64), "--cleanup-confirmed", "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    stale = _run(*_apply_args(parent, "sha256:" + ("0" * 64), "--cleanup-confirmed", "--json"))
    assert stale.returncode == 1
    assert stale.stdout == ""
    assert leftover.is_dir()
    schema1 = _run(
        *_apply_args(parent, _schema1_revision(plan), "--cleanup-confirmed", "--json")
    )
    assert schema1.returncode == 1
    assert schema1.stdout == ""


def test_descriptors_and_claims_held_through_stdout_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_export_staging as staging_mod
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    parent = tmp_path / "flush"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    original_scope = cleanup_mod._snapshot_export_staging_cleanup_scope
    original_json = cleanup_mod.result_to_json
    original_format = cleanup_mod.format_result
    state = {
        "active": False,
        "parent_fd": None,
        "writes": 0,
        "flushes": 0,
    }

    def capture(_path, parent_fd, held):
        state["parent_fd"] = parent_fd
        os.fstat(parent_fd)
        for staging_fd, lock_fd in held.values():
            if staging_fd is not None:
                os.fstat(staging_fd)
            if lock_fd is not None:
                os.fstat(lock_fd)

    @contextmanager
    def tracked_scope(*args, **kwargs):
        with original_scope(*args, **kwargs) as result:
            state["active"] = True
            try:
                yield result
            finally:
                state["active"] = False

    def guarded_json(*args, **kwargs):
        assert state["active"]
        os.fstat(state["parent_fd"])
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        assert state["active"]
        os.fstat(state["parent_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            assert state["active"]
            os.fstat(state["parent_fd"])
            state["writes"] += 1
            return len(text)

        def flush(self) -> None:
            assert state["active"]
            os.fstat(state["parent_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(staging_mod, "_after_probe_descriptors_ready", capture)
    monkeypatch.setattr(cleanup_mod, "_snapshot_export_staging_cleanup_scope", tracked_scope)
    monkeypatch.setattr(cleanup_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(cleanup_mod, "format_result", guarded_format)
    monkeypatch.setattr(cleanup_mod.sys, "stdout", GuardedStdout())
    assert (
        cleanup_mod.main(
            _apply_args(parent, plan["plan_revision"], "--cleanup-confirmed", "--json")
        )
        == 0
    )
    leftover2 = _candidate(parent, HEX_B)
    plan2 = snapshot_export_staging_cleanup_plan(parent)
    original = cleanup_mod._remove_claimed_staging_entry

    def fail_later(observation, name, claim, entry):
        original(observation, name, claim, entry)
        raise RuntimeError("injected after success")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later)
    assert (
        cleanup_mod.main(_apply_args(parent, plan2["plan_revision"], "--cleanup-confirmed"))
        == 1
    )
    assert not leftover.exists()
    assert state["active"] is False
    assert state["writes"] >= 2
    assert state["flushes"] == 2


def test_does_not_inspect_graph_state_or_export_payload(tmp_path: Path):
    parent = tmp_path / "graphish"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A, payload=b"unique-export-payload-auth-bytes")
    (parent / "current").write_bytes(b"unique-current-bytes")
    (parent / ".publish.lock").write_bytes(b"unique-publish-lock-bytes")
    snaps = parent / "snapshots"
    snaps.mkdir()
    (snaps / "secret.parquet").write_bytes(b"unique-snapshot-bytes")
    plan = snapshot_export_staging_cleanup_plan(parent)
    result = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    dumped = result_to_json(result) + format_result(result)
    assert "unique-export-payload-auth-bytes" not in dumped
    assert "unique-current-bytes" not in dumped
    assert "unique-publish-lock-bytes" not in dumped
    assert "unique-snapshot-bytes" not in dumped
    assert not leftover.exists()
    assert (parent / "current").read_bytes() == b"unique-current-bytes"
    assert (parent / ".publish.lock").read_bytes() == b"unique-publish-lock-bytes"
    assert (snaps / "secret.parquet").read_bytes() == b"unique-snapshot-bytes"


def test_implementation_does_not_invoke_producers_or_graph_leases():
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
    assert "held_export_staging_observation_scope" in imported
    assert "claim_existing_export_writer_lease" in imported
    assert "_plan_from_inventory" in imported
    assert "graph_exclusive_lease" not in source
    assert "graph_read_lease" not in source
    assert "shutil.rmtree" not in source
    assert "snapshot_export_apply(" not in source
    assert "snapshot_export_plan(" not in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_cli_module_wrapper_installed_console_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    parent = here / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    args = [
        "--parent",
        "parent",
        "--expected-plan-revision",
        plan["plan_revision"],
        "--cleanup-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_export_staging_cleanup", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == 0, module.stderr
    body = json.loads(module.stdout)
    assert body["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()

    leftover2 = _candidate(parent, HEX_B)
    plan2 = snapshot_export_staging_cleanup_plan(parent)
    script = _run(
        "--parent",
        "parent",
        "--expected-plan-revision",
        plan2["plan_revision"],
        "--cleanup-confirmed",
        "--json",
        cwd=here,
    )
    leftover3 = _candidate(parent, HEX_C)
    plan3 = snapshot_export_staging_cleanup_plan(parent)
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-export-staging-cleanup",
            "--parent",
            "parent",
            "--expected-plan-revision",
            plan3["plan_revision"],
            "--cleanup-confirmed",
            "--json",
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert script.returncode == cli.returncode == 0, script.stderr + cli.stderr
    assert json.loads(script.stdout)["deleted_staging_entries"] == [leftover2.name]
    assert json.loads(cli.stdout)["deleted_staging_entries"] == [leftover3.name]
    assert result_to_json(json.loads(script.stdout)) == script.stdout

    leftover4 = _candidate(parent, HEX_D)
    plan4 = snapshot_export_staging_cleanup_plan(parent)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-staging-cleanup",
            "--parent",
            str(parent),
            "--expected-plan-revision",
            plan4["plan_revision"],
            "--cleanup-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["deleted_staging_entries"] == [leftover4.name]
    assert not leftover4.exists()

    import tarfile
    import zipfile

    with zipfile.ZipFile(built_wheel_and_sdist[0]) as zf:
        names = zf.namelist()
    assert "graphrag_code/snapshot_export_staging_cleanup.py" in names
    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        snames = "\n".join(tf.getnames())
    assert "snapshot_export_staging_cleanup.py" in snames


def test_mcp_remains_exactly_eleven_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 11
    assert "snapshot_export_staging_cleanup" not in TOOL_NAMES
    assert "snapshot_export_staging_cleanup_plan" not in TOOL_NAMES
    graph = tmp_path / "g"
    _publish(graph, "a")
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 11
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
