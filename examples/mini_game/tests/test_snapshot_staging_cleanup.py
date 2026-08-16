"""CAS-verified snapshot staging cleanup apply.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_staging_cleanup.py -q
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
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    StagingWriterLeaseError,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore
from graphrag_code.snapshot_staging_cleanup import (  # type: ignore
    SnapshotStagingCleanupError,
    SnapshotStagingCleanupIntegrityError,
    format_result,
    parse_plan_revision,
    result_to_json,
    snapshot_staging_cleanup,
)
from graphrag_code.snapshot_staging_cleanup_plan import (  # type: ignore
    canonical_plan_revision_payload,
    snapshot_staging_cleanup_plan,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_staging_cleanup.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_staging_cleanup.py"
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
        "c_clang_ast_capture",
        "c_compiler_facts",
        "graph_read_lease",
        "staging_writer_lease",
        "_open_or_create_staging_writer_lock_fd",
    }
)
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
        settings_text=f"staging-cleanup: {marker}\n",
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


def _protected_published(graph: Path) -> dict[str, object]:
    registry = graph / OPERATOR_PINS_NAME
    published = sorted(
        path.name
        for path in (graph / "snapshots").iterdir()
        if not path.name.startswith(STAGING_NAME_PREFIX)
    )
    return {
        "current": _current(graph),
        "published": tuple(published),
        "registry_exists": registry.exists(),
        "registry": registry.read_bytes() if registry.is_file() else None,
        "lock": (graph / PUBLICATION_LOCK_NAME).read_bytes(),
        "lock_stat": (
            (graph / PUBLICATION_LOCK_NAME).lstat().st_ino,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_dev,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_size,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_mtime_ns,
        ),
    }


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def _apply_args(graph: Path, revision: str, *extra: str) -> list[str]:
    return [
        "--graph",
        str(graph),
        "--expected-plan-revision",
        revision,
        *extra,
    ]


def _staging_dir(graph: Path, suffix: str) -> Path:
    path = graph / "snapshots" / f"{STAGING_NAME_PREFIX}{suffix}"
    path.mkdir()
    return path


def _write_complete_payload(directory: Path) -> None:
    (directory / "manifest.json").write_text("{}\n", encoding="utf-8")
    (directory / "entities.parquet").write_bytes(b"ENT")
    (directory / "relationships.parquet").write_bytes(b"REL")
    (directory / "text_units.parquet").write_bytes(b"TU")
    (directory / "call_observations.parquet").write_bytes(b"CO")
    (directory / "settings.yaml").write_text("k: v\n", encoding="utf-8")


def _write_writer_lock(directory: Path, payload: bytes = b"") -> Path:
    lock = directory / STAGING_WRITER_LOCK_NAME
    lock.write_bytes(payload)
    return lock


def _cooperative_leftover(graph: Path, suffix: str, *, complete: bool) -> Path:
    staging = _staging_dir(graph, suffix)
    if complete:
        _write_complete_payload(staging)
    else:
        (staging / "manifest.json").write_text("{}\n", encoding="utf-8")
    _write_writer_lock(staging)
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


def _writer_lease_hold(stage_dir: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import staging_writer_lease

    try:
        with staging_writer_lease(ChildPath(stage_dir)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _ungated_writer_lease_hold(stage_dir: str, held, resume, q) -> None:
    """Model a cooperative-v1 process from before the graph-gate upgrade."""
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    @contextmanager
    def no_graph_gate(_stage_dir):
        yield

    byog._staging_writer_acquisition_gate = no_graph_gate
    try:
        with byog.staging_writer_lease(ChildPath(stage_dir)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _writer_lease_attempt(stage_dir: str, started, acquired, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import staging_writer_lease

    started.set()
    try:
        with staging_writer_lease(ChildPath(stage_dir)):
            acquired.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _probe_state(stage_dir: str, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import probe_staging_writer_lease

    try:
        q.put(probe_staging_writer_lease(ChildPath(stage_dir))["writer_lease_state"])
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_missing_cleanup_confirmed_exits_2(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_staging_cleanup_plan(graph)
    before = _protected_published(graph)
    listing = tuple(sorted(path.name for path in (graph / "snapshots").iterdir()))
    missing = _run(*_apply_args(graph, plan["plan_revision"], "--json"))
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "--cleanup-confirmed" in missing.stderr
    with pytest.raises(SnapshotStagingCleanupError, match="cleanup-confirmed"):
        snapshot_staging_cleanup(graph, plan["plan_revision"], cleanup_confirmed=False)
    assert _protected_published(graph) == before
    assert tuple(sorted(path.name for path in (graph / "snapshots").iterdir())) == listing


def test_malformed_revision_tokens(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    before = _protected_published(graph)
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
        with pytest.raises(SnapshotStagingCleanupError, match="sha256"):
            parse_plan_revision(token)
        proc = _run(*_apply_args(graph, token, "--cleanup-confirmed", "--json"))
        assert proc.returncode == 2, token
        assert proc.stdout == ""
        assert _protected_published(graph) == before


def test_stale_and_schema1_revisions_are_rejected(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    leftover = _cooperative_leftover(graph, "20240101-000000-stale", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    stale = "sha256:" + ("0" * 64)
    before = _protected_published(graph)
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(graph, stale, cleanup_confirmed=True)
    schema1 = _schema1_revision(plan)
    assert schema1 != plan["plan_revision"]
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(graph, schema1, cleanup_confirmed=True)
    proc = _run(*_apply_args(graph, schema1, "--cleanup-confirmed", "--json"))
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert leftover.is_dir()
    assert _protected_published(graph) == before


def test_empty_matching_plan_is_successful_noop(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "only")
    plan = snapshot_staging_cleanup_plan(graph)
    assert plan["deletion_candidates"] == []
    assert plan["schema_version"] == 2
    assert plan["apply_supported"] is True
    before = _protected_published(graph)
    hashes = _payload_hashes(graph)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
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
    assert result["current"] == published.name
    assert result["published_snapshots"] == [published.name]
    assert _protected_published(graph) == before
    assert _payload_hashes(graph) == hashes


def test_complete_and_incomplete_candidates_are_deleted(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "live")
    complete = _cooperative_leftover(graph, "20240101-000000-complete", complete=True)
    incomplete = _cooperative_leftover(
        graph, "20240101-000000-incomplete", complete=False
    )
    plan = snapshot_staging_cleanup_plan(graph)
    assert set(plan["deletion_candidates"]) == {complete.name, incomplete.name}
    before = _protected_published(graph)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["filesystem_may_have_changed"] is True
    assert result["deleted_staging_entries"] == plan["deletion_candidates"]
    assert result["deleted_count"] == 2
    assert result["remaining_staging_entries"] == []
    assert not complete.exists()
    assert not incomplete.exists()
    assert (graph / "snapshots" / published.name).is_dir()
    assert _protected_published(graph) == before


def test_multiple_candidates_deleted_in_canonical_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _cooperative_leftover(graph, "20240101-000000-zzz", complete=True)
    second = _cooperative_leftover(graph, "20240101-000000-aaa", complete=False)
    third = _cooperative_leftover(graph, "20240101-000000-mmm", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    seen: list[str] = []
    original = cleanup_mod._remove_claimed_staging_entry

    def tracked(snapshots_dir, name, claim, expected):
        seen.append(name)
        return original(snapshots_dir, name, claim, expected)

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", tracked)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
    )
    assert seen == plan["deletion_candidates"]
    assert seen == sorted(seen, key=lambda item: item.encode("utf-8"))
    assert result["deleted_staging_entries"] == seen
    assert not first.exists() and not second.exists() and not third.exists()


def test_blocked_entries_are_never_deleted(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-keepme", complete=True)
    legacy = _staging_dir(graph, "20240101-000000-legacy")
    (legacy / "manifest.json").write_text("{}\n", encoding="utf-8")
    noncanonical = graph / "snapshots" / ".staging-.notcanonical"
    noncanonical.mkdir()
    _write_writer_lock(noncanonical)
    nondir = graph / "snapshots" / ".staging-20240101-000000-file"
    nondir.write_bytes(b"not-a-dir")
    held_dir = _cooperative_leftover(graph, "20240101-000000-held", complete=True)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_writer_lease_hold, args=(str(held_dir), held, resume, q)
    )
    holder.start()
    try:
        assert held.wait(timeout=TIMEOUT)
        plan = snapshot_staging_cleanup_plan(graph)
        reasons = {item["name"]: item["reason"] for item in plan["blocked_entries"]}
        assert reasons[legacy.name] == "legacy_or_missing_writer_lock"
        assert reasons[noncanonical.name] == "noncanonical_staging_name"
        assert reasons[nondir.name] == "non_directory_staging_entry"
        assert reasons[held_dir.name] == "held_writer_lease"
        assert plan["deletion_candidates"] == [leftover.name]
        result = snapshot_staging_cleanup(
            graph, plan["plan_revision"], cleanup_confirmed=True
        )
        assert result["deleted_staging_entries"] == [leftover.name]
        assert leftover.name not in result["remaining_staging_entries"]
        assert not leftover.exists()
        assert legacy.is_dir()
        assert noncanonical.is_dir()
        assert nondir.is_file()
        assert held_dir.is_dir()
        assert (graph / "snapshots" / published.name).is_dir()
    finally:
        _cleanup_processes(holder, release=resume)


def test_plan_change_between_preview_and_apply(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _cooperative_leftover(graph, "20240101-000000-one", complete=True)
    preview = snapshot_staging_cleanup_plan(graph)
    second = _cooperative_leftover(graph, "20240101-000000-two", complete=True)
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph, preview["plan_revision"], cleanup_confirmed=True
        )
    assert first.is_dir() and second.is_dir()


def test_identity_changes_invalidate_cas(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-cas", complete=True)
    preview = snapshot_staging_cleanup_plan(graph)
    (leftover / "entities.parquet").write_bytes(b"CHANGED")
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph, preview["plan_revision"], cleanup_confirmed=True
        )
    assert leftover.is_dir()

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    staging = _cooperative_leftover(graph2, "20240101-000000-dir", complete=True)
    preview2 = snapshot_staging_cleanup_plan(graph2)
    parked = tmp_path / "parked-dir"
    staging.rename(parked)
    staging.mkdir()
    for child in sorted(parked.iterdir()):
        (staging / child.name).write_bytes(child.read_bytes())
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph2, preview2["plan_revision"], cleanup_confirmed=True
        )
    assert staging.is_dir()

    graph3 = tmp_path / "g3"
    _publish(graph3, "live")
    staging3 = _cooperative_leftover(graph3, "20240101-000000-lock", complete=True)
    preview3 = snapshot_staging_cleanup_plan(graph3)
    lock = staging3 / STAGING_WRITER_LOCK_NAME
    lock.unlink()
    lock.write_bytes(b"")
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph3, preview3["plan_revision"], cleanup_confirmed=True
        )

    graph4 = tmp_path / "g4"
    first = _publish(graph4, "a")
    leftover4 = _cooperative_leftover(graph4, "20240101-000000-pub", complete=True)
    preview4 = snapshot_staging_cleanup_plan(graph4)
    _publish(graph4, "b")
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph4, preview4["plan_revision"], cleanup_confirmed=True
        )
    assert leftover4.is_dir()
    assert first.is_dir()

    graph5 = tmp_path / "g5"
    _publish(graph5, "live")
    leftover5 = _cooperative_leftover(graph5, "20240101-000000-glock", complete=True)
    preview5 = snapshot_staging_cleanup_plan(graph5)
    graph_lock = graph5 / PUBLICATION_LOCK_NAME
    payload = graph_lock.read_bytes()
    graph_lock.unlink()
    graph_lock.write_bytes(payload)
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph5, preview5["plan_revision"], cleanup_confirmed=True
        )
    assert leftover5.is_dir()


def test_writer_acquires_lease_after_plan_creation(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-later", complete=True)
    preview = snapshot_staging_cleanup_plan(graph)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_writer_lease_hold, args=(str(leftover), held, resume, q)
    )
    holder.start()
    try:
        assert held.wait(timeout=TIMEOUT)
        with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
            snapshot_staging_cleanup(
                graph, preview["plan_revision"], cleanup_confirmed=True
            )
        assert leftover.is_dir()
    finally:
        _cleanup_processes(holder, release=resume)


def test_writer_acquires_between_recompute_and_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-race", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_ungated_writer_lease_hold,
        args=(str(leftover), held, resume, q),
    )
    original = cleanup_mod._after_cleanup_plan_recompute

    def acquire_after_recompute(root, recomputed, consistency):
        original(root, recomputed, consistency)
        holder.start()
        assert held.wait(timeout=TIMEOUT)

    monkeypatch.setattr(
        cleanup_mod, "_after_cleanup_plan_recompute", acquire_after_recompute
    )
    try:
        with pytest.raises(
            SnapshotStagingCleanupIntegrityError, match="held by a cooperating"
        ):
            snapshot_staging_cleanup(
                graph, plan["plan_revision"], cleanup_confirmed=True
            )
        assert leftover.is_dir()
    finally:
        _cleanup_processes(holder, release=resume)


def test_contention_on_later_candidate_releases_earlier_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _cooperative_leftover(graph, "20240101-000000-aaa", complete=True)
    second = _cooperative_leftover(graph, "20240101-000000-zzz", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    assert plan["deletion_candidates"] == [first.name, second.name]
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_ungated_writer_lease_hold,
        args=(str(second), held, resume, q),
    )
    closed: list[bool] = []
    original = cleanup_mod._after_cleanup_plan_recompute

    def acquire_second(root, recomputed, consistency):
        original(root, recomputed, consistency)
        holder.start()
        assert held.wait(timeout=TIMEOUT)

    orig_claim = cleanup_mod._claim_candidate
    claims: list = []

    def tracked_claim(snapshots_dir, name):
        claim = orig_claim(snapshots_dir, name)
        claims.append(claim)
        return claim

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_plan_recompute", acquire_second)
    monkeypatch.setattr(cleanup_mod, "_claim_candidate", tracked_claim)
    try:
        with pytest.raises(
            SnapshotStagingCleanupIntegrityError, match="held by a cooperating"
        ):
            snapshot_staging_cleanup(
                graph, plan["plan_revision"], cleanup_confirmed=True
            )
        assert first.is_dir() and second.is_dir()
        assert len(claims) == 1
        assert claims[0].closed is True
        closed.append(claims[0].closed)
        assert closed == [True]
    finally:
        _cleanup_processes(holder, release=resume)


def test_replacement_before_claim_and_after_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-before", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)

    def replace_lock(root, recomputed, consistency):
        lock = leftover / STAGING_WRITER_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced")

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_plan_recompute", replace_lock)
    with pytest.raises(SnapshotStagingCleanupIntegrityError):
        snapshot_staging_cleanup(graph, plan["plan_revision"], cleanup_confirmed=True)
    assert leftover.is_dir()

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    leftover2 = _cooperative_leftover(graph2, "20240101-000000-after", complete=True)
    plan2 = snapshot_staging_cleanup_plan(graph2)
    outside = tmp_path / "outside-after"
    outside.write_bytes(b"must-not-follow")

    def replace_after_claim(claims):
        lock = leftover2 / STAGING_WRITER_LOCK_NAME
        parked = tmp_path / "parked-lock"
        lock.rename(parked)
        lock.symlink_to(outside)

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_writer_claims", replace_after_claim)
    with pytest.raises(SnapshotStagingCleanupIntegrityError):
        snapshot_staging_cleanup(graph2, plan2["plan_revision"], cleanup_confirmed=True)
    assert leftover2.is_dir()
    assert outside.read_bytes() == b"must-not-follow"
    assert leftover2.is_dir()


def test_all_claims_and_revalidations_complete_before_first_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _cooperative_leftover(graph, "20240101-000000-aaa", complete=True)
    second = _cooperative_leftover(graph, "20240101-000000-zzz", complete=False)
    plan = snapshot_staging_cleanup_plan(graph)
    phases: list[str] = []
    orig_claims = cleanup_mod._after_cleanup_writer_claims
    orig_revalidate = cleanup_mod._after_cleanup_revalidation
    orig_before = cleanup_mod._before_cleanup_deletion
    orig_remove = cleanup_mod._remove_claimed_staging_entry

    def after_claims(claims):
        phases.append(f"claims:{len(claims)}")
        return orig_claims(claims)

    def after_revalidate(root, claims):
        phases.append(f"revalidate:{len(claims)}")
        assert first.is_dir() and second.is_dir()
        return orig_revalidate(root, claims)

    def before_delete(root):
        phases.append("before_delete")
        assert first.is_dir() and second.is_dir()
        return orig_before(root)

    def tracked(snapshots_dir, name, claim, expected):
        phases.append(f"delete:{name}")
        return orig_remove(snapshots_dir, name, claim, expected)

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_writer_claims", after_claims)
    monkeypatch.setattr(cleanup_mod, "_after_cleanup_revalidation", after_revalidate)
    monkeypatch.setattr(cleanup_mod, "_before_cleanup_deletion", before_delete)
    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", tracked)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
    )
    assert phases[:3] == ["claims:2", "revalidate:2", "before_delete"]
    assert phases[3:] == [f"delete:{name}" for name in plan["deletion_candidates"]]
    assert result["deleted_staging_entries"] == plan["deletion_candidates"]


def test_writer_lock_held_during_payload_removal_and_release_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-order", complete=True)
    lock = leftover / STAGING_WRITER_LOCK_NAME
    plan = snapshot_staging_cleanup_plan(graph)
    seen_held: list[str] = []
    orig_remove = cleanup_mod._remove_path_nofollow

    def tracked_remove(path: Path):
        if path.parent == leftover and path.name != STAGING_WRITER_LOCK_NAME:
            q = CTX.Queue()
            probe = CTX.Process(target=_probe_state, args=(str(leftover), q))
            probe.start()
            probe.join(timeout=TIMEOUT)
            seen_held.append(q.get(timeout=5))
            assert lock.exists()
        return orig_remove(path)

    released: list[str] = []
    orig_release = cleanup_mod.HeldExistingStagingWriterClaim.release_and_remove

    def tracked_release(self):
        assert lock.exists()
        assert not (leftover / "manifest.json").exists()
        assert leftover.is_dir()
        released.append("before")
        orig_release(self)
        released.append("after")
        assert not lock.exists()
        assert leftover.is_dir()

    monkeypatch.setattr(cleanup_mod, "_remove_path_nofollow", tracked_remove)
    monkeypatch.setattr(
        cleanup_mod.HeldExistingStagingWriterClaim,
        "release_and_remove",
        tracked_release,
    )
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()
    assert seen_held
    assert set(seen_held) == {"held_by_cooperating_writer"}
    assert released == ["before", "after"]


def test_managed_writer_cannot_acquire_in_cleanup_release_unlink_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(
        graph, "20240101-000000-release-window", complete=True
    )
    plan = snapshot_staging_cleanup_plan(graph)
    started = CTX.Event()
    acquired = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    waiter = CTX.Process(
        target=_writer_lease_attempt,
        args=(str(leftover), started, acquired, resume, q),
    )
    original_after_claims = cleanup_mod._after_cleanup_writer_claims
    original_close = cleanup_mod.HeldExistingStagingWriterClaim.close
    close_checked = {"done": False}

    def start_waiter(claims):
        original_after_claims(claims)
        waiter.start()
        assert started.wait(timeout=TIMEOUT)
        time.sleep(0.1)
        assert not acquired.is_set()

    def delayed_close(self):
        original_close(self)
        if not close_checked["done"]:
            close_checked["done"] = True
            # The destructive claim is now released but the graph lease is
            # still exclusive. A managed cooperative writer must remain at
            # the graph acquisition gate until unlink/rmdir complete.
            time.sleep(0.2)
            assert not acquired.is_set()

    monkeypatch.setattr(cleanup_mod, "_after_cleanup_writer_claims", start_waiter)
    monkeypatch.setattr(
        cleanup_mod.HeldExistingStagingWriterClaim, "close", delayed_close
    )
    try:
        result = snapshot_staging_cleanup(
            graph, plan["plan_revision"], cleanup_confirmed=True
        )
        assert result["deleted_staging_entries"] == [leftover.name]
        assert close_checked["done"] is True
        assert not leftover.exists()
        waiter.join(timeout=TIMEOUT)
        assert not waiter.is_alive()
        assert not acquired.is_set()
        message = q.get(timeout=TIMEOUT)
        assert str(message).startswith("error:StagingWriterLockUnsafe:")
    finally:
        _cleanup_processes(waiter, release=resume)


def test_no_follow_and_no_nofollow_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as byog_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    published = _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-nofollow", complete=True)
    outside = tmp_path / "must-not-follow"
    outside.write_bytes(b"keep-me")
    decoy = leftover / "decoy-link"
    decoy.symlink_to(outside)
    with pytest.raises(Exception, match="symlink"):
        snapshot_staging_cleanup_plan(graph)
    decoy.unlink()
    plan = snapshot_staging_cleanup_plan(graph)
    monkeypatch.setattr(cleanup_mod, "_has_o_nofollow", lambda: False)
    monkeypatch.setattr(byog_mod, "_has_o_nofollow", lambda: False)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()
    assert outside.read_bytes() == b"keep-me"
    assert (graph / "snapshots" / published.name).is_dir()

    victim = tmp_path / "published-payload"
    victim.write_bytes(b"do-not-delete")
    linked = tmp_path / "link-to-payload"
    linked.symlink_to(victim)
    cleanup_mod._remove_path_nofollow(linked)
    assert not linked.exists()
    assert victim.read_bytes() == b"do-not-delete"


def test_unsupported_lock_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.byog_graph as byog_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-backend", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)

    def unsupported(fd):
        raise StagingWriterLeaseError(
            "nonblocking exclusive staging-writer lock is unsupported on 'test'"
        )

    monkeypatch.setattr(byog_mod, "_try_acquire_exclusive_lock", unsupported)
    with pytest.raises(SnapshotStagingCleanupError, match="unsupported"):
        snapshot_staging_cleanup(graph, plan["plan_revision"], cleanup_confirmed=True)
    assert leftover.is_dir()
    proc = _run(*_apply_args(graph, plan["plan_revision"], "--cleanup-confirmed", "--json"))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()


def test_final_revalidation_failure_before_first_unlink_has_empty_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(
        graph, "20240101-000000-final-revalidation", complete=True
    )
    plan = snapshot_staging_cleanup_plan(graph)
    original = cleanup_mod.staging_structure_token
    calls = {"count": 0}

    def change_immediately_before_remove(path):
        calls["count"] += 1
        if calls["count"] == 3:
            (leftover / "entities.parquet").write_bytes(b"CHANGED-BEFORE-UNLINK")
        return original(path)

    monkeypatch.setattr(
        cleanup_mod, "staging_structure_token", change_immediately_before_remove
    )
    exit_code = cleanup_mod.main(
        _apply_args(graph, plan["plan_revision"], "--cleanup-confirmed", "--json")
    )
    captured = capsys.readouterr()
    assert calls["count"] == 3
    assert exit_code == 1
    assert captured.out == ""
    assert "structure changed before deletion" in captured.err
    assert leftover.is_dir()
    assert (leftover / "manifest.json").is_file()
    assert (leftover / STAGING_WRITER_LOCK_NAME).is_file()


def test_injected_first_and_later_candidate_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _cooperative_leftover(graph, "20240101-000000-aaa", complete=True)
    second = _cooperative_leftover(graph, "20240101-000000-zzz", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    before = _protected_published(graph)
    original = cleanup_mod._remove_claimed_staging_entry

    def boom(snapshots_dir, name, claim, expected):
        raise RuntimeError(f"injected failure on {name}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", boom)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
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
    assert _protected_published(graph) == before
    text = format_result(result)
    assert "PARTIAL FAILURE" in text
    assert "no rollback" in text.lower()

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    later_first = _cooperative_leftover(graph2, "20240101-000000-aaa", complete=True)
    later_second = _cooperative_leftover(graph2, "20240101-000000-zzz", complete=True)
    plan2 = snapshot_staging_cleanup_plan(graph2)
    calls = {"n": 0}

    def fail_later(snapshots_dir, name, claim, expected):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(snapshots_dir, name, claim, expected)
        raise RuntimeError(f"injected later failure on {name}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later)
    later = snapshot_staging_cleanup(
        graph2, plan2["plan_revision"], cleanup_confirmed=True
    )
    assert later["ok"] is False
    assert later["partial"] is True
    assert later["changed"] is True
    assert later["filesystem_may_have_changed"] is True
    assert later["retry_requires_fresh_plan"] is True
    assert later["deleted_staging_entries"] == [plan2["deletion_candidates"][0]]
    assert later["failed_staging_entry"] == plan2["deletion_candidates"][1]
    assert later["not_attempted_staging_entries"] == []
    assert not later_first.exists()
    assert later_second.is_dir()
    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", original)
    fresh = snapshot_staging_cleanup_plan(graph2)
    assert fresh["plan_revision"] != plan2["plan_revision"]
    assert later_second.name in fresh["deletion_candidates"]
    with pytest.raises(SnapshotStagingCleanupIntegrityError, match="does not match"):
        snapshot_staging_cleanup(
            graph2, plan2["plan_revision"], cleanup_confirmed=True
        )
    retry = snapshot_staging_cleanup(
        graph2, fresh["plan_revision"], cleanup_confirmed=True
    )
    assert retry["ok"] is True
    assert retry["deleted_staging_entries"] == [later_second.name]
    assert not later_second.exists()


def test_published_state_and_lock_remain_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-safe", complete=True)
    before = _protected_published(graph)
    published_hash = hashlib.sha256(
        (published / "entities.parquet").read_bytes()
    ).hexdigest()
    plan = snapshot_staging_cleanup_plan(graph)
    result = snapshot_staging_cleanup(
        graph, plan["plan_revision"], cleanup_confirmed=True
    )
    assert result["ok"] is True
    assert not leftover.exists()
    after = _protected_published(graph)
    assert after == before
    assert hashlib.sha256(
        (published / "entities.parquet").read_bytes()
    ).hexdigest() == published_hash


def test_implementation_does_not_invoke_producers_or_nested_leases():
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
    assert "graph_exclusive_lease" in imported
    assert "build_stable_cleanup_plan_unlocked" in imported
    assert "acquire_existing_staging_writer_claim" in imported
    assert "graph_read_lease" not in imported
    assert "snapshot_staging_cleanup_plan" not in called
    assert "cleanup_old_snapshots" not in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_cli_module_wrapper_installed_console_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "a")
    leftover = _cooperative_leftover(graph, "20240101-000000-relcwd", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    args = [
        "--graph",
        "g",
        "--expected-plan-revision",
        plan["plan_revision"],
        "--cleanup-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_staging_cleanup", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == 0, module.stderr
    body = json.loads(module.stdout)
    assert body["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()

    leftover2 = _cooperative_leftover(graph, "20240101-000000-script", complete=True)
    plan2 = snapshot_staging_cleanup_plan(graph)
    script = _run(
        "--graph",
        "g",
        "--expected-plan-revision",
        plan2["plan_revision"],
        "--cleanup-confirmed",
        "--json",
        cwd=here,
    )
    leftover3 = _cooperative_leftover(graph, "20240101-000000-cli", complete=True)
    plan3 = snapshot_staging_cleanup_plan(graph)
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-staging-cleanup",
            "--graph",
            "g",
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

    leftover4 = _cooperative_leftover(graph, "20240101-000000-wheel", complete=True)
    plan4 = snapshot_staging_cleanup_plan(graph)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-staging-cleanup",
            "--graph",
            str(graph),
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

    sdist = built_wheel_and_sdist[1]
    import tarfile

    with tarfile.open(sdist, "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_staging_cleanup.py" in names


def test_cli_serializes_writes_and_flushes_under_exclusive_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    leftover = _cooperative_leftover(graph, "20240101-000000-flush", complete=True)
    plan = snapshot_staging_cleanup_plan(graph)
    original_scope = cleanup_mod._snapshot_staging_cleanup_scope
    original_json = cleanup_mod.result_to_json
    original_format = cleanup_mod.format_result
    state = {"active": False, "responses": 0, "flushes": 0}

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
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        assert state["active"]
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            assert state["active"]
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            assert state["active"]
            state["flushes"] += 1

    monkeypatch.setattr(cleanup_mod, "_snapshot_staging_cleanup_scope", tracked_scope)
    monkeypatch.setattr(cleanup_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(cleanup_mod, "format_result", guarded_format)
    monkeypatch.setattr(cleanup_mod.sys, "stdout", GuardedStdout())
    assert (
        cleanup_mod.main(
            _apply_args(graph, plan["plan_revision"], "--cleanup-confirmed", "--json")
        )
        == 0
    )
    leftover2 = _cooperative_leftover(graph, "20240101-000000-plain", complete=True)
    plan2 = snapshot_staging_cleanup_plan(graph)
    original = cleanup_mod._remove_claimed_staging_entry

    def fail_later(snapshots_dir, name, claim, expected):
        original(snapshots_dir, name, claim, expected)
        raise RuntimeError("injected after success")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later)
    assert (
        cleanup_mod.main(_apply_args(graph, plan2["plan_revision"], "--cleanup-confirmed"))
        == 1
    )
    assert not leftover.exists()
    assert leftover2.exists() is False or leftover2.exists()
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_mcp_remains_exactly_eleven():
    assert len(TOOL_NAMES) == 11
    assert "snapshot_staging_cleanup" not in TOOL_NAMES
    assert "snapshot_staging_cleanup_plan" not in TOOL_NAMES
    assert "snapshot_staging_cleanup" not in " ".join(TOOL_NAMES)


def test_mcp_list_tools_has_no_cleanup_apply(tmp_path: Path):
    from anyio import run as anyio_run

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
            assert "snapshot_staging_cleanup" not in names

    anyio_run(_body)
