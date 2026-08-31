"""Read-only snapshot staging cleanup plan.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_staging_cleanup_plan.py -q
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
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore
from graphrag_code.snapshot_staging import (  # type: ignore
    MAX_CURRENT_BYTES,
    MAX_PUBLISHED_SNAPSHOTS,
    MAX_STAGING_ENTRIES,
    MAX_TOP_LEVEL_ENTRIES,
    snapshot_staging,
)
from graphrag_code.snapshot_staging_cleanup_plan import (  # type: ignore
    SnapshotStagingCleanupPlanError,
    SnapshotStagingCleanupPlanIntegrityError,
    canonical_plan_revision_text,
    format_result,
    plan_revision_of,
    result_to_json,
    snapshot_staging_cleanup_plan,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_staging_cleanup_plan.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_staging_cleanup_plan.py"
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
        "graph_exclusive_lease",
        "_publication_lock",
        "staging_writer_lease",
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
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
        settings_text=f"staging-cleanup-plan: {marker}\n",
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


def _protected_state(graph: Path) -> dict[str, object]:
    registry = graph / OPERATOR_PINS_NAME
    return {
        "hashes": _payload_hashes(graph),
        "stats": _payload_stats(graph),
        "current": _current(graph),
        "listing": tuple(sorted(path.name for path in (graph / "snapshots").iterdir())),
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


def _entry(result: dict, name: str) -> dict:
    matches = [item for item in result["staging_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def _blocked(result: dict, name: str) -> dict:
    matches = [item for item in result["blocked_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def _mutate_after_first(original, action):
    seen = {"n": 0}

    def wrapped(path: Path):
        result = original(path)
        seen["n"] += 1
        if seen["n"] == 1:
            action(path)
        return result

    return wrapped


def _paused_publisher_events_only(graph: str, marker: str, paused, resume) -> None:
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
        settings_text=f"staging-cleanup-plan: {marker}\n",
        keep_last=10,
    )


def _shared_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import graph_read_lease

    try:
        with graph_read_lease(ChildPath(graph), allow_unlocked_managed=False):
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
    ents, rels, tus = _rows(marker)
    try:
        snap = byog.publish_byog_snapshot(
            pd.DataFrame(ents),
            pd.DataFrame(rels),
            pd.DataFrame(tus),
            ChildPath(graph),
            keep_last=keep_last,
        )
        q.put(("pub", snap.name))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


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


def test_empty_plan(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "only")
    before = _protected_state(graph)
    result = snapshot_staging_cleanup_plan(graph)
    assert result["schema_version"] == 2
    assert result["ok"] is True
    assert result["graph"] == str(graph.resolve())
    assert result["current"] == published.name
    assert result["published_snapshots"] == [published.name]
    assert result["staging_count"] == 0
    assert result["staging_entries"] == []
    assert result["deletion_candidates"] == []
    assert result["deletion_candidate_count"] == 0
    assert result["blocked_entries"] == []
    assert result["blocked_count"] == 0
    assert result["ownership_inference"] is False
    assert result["cleanup_applied"] is False
    assert result["apply_supported"] is True
    assert result["observed_staging_revision"] == snapshot_staging(graph)["staging_revision"]
    assert result["staging_state_revision"].startswith("sha256:")
    assert len(result["staging_state_revision"]) == len("sha256:") + 64
    assert result["plan_revision"].startswith("sha256:")
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "plan_not_authorization",
        "observed_non_contention_not_claim",
        "apply_is_separate_cas_command",
        "inventory_cleanup_eligible_false",
    ]
    assert _protected_state(graph) == before


def test_killed_publisher_leftover_is_deletion_candidate(tmp_path: Path):
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
        staging = sorted(
            path
            for path in (graph / "snapshots").iterdir()
            if path.name.startswith(STAGING_NAME_PREFIX)
        )
        assert len(staging) == 1
        lock = staging[0] / STAGING_WRITER_LOCK_NAME
        assert lock.is_file() and not lock.is_symlink()
        os.kill(pub.pid, 9)
        pub.join(timeout=TIMEOUT)
        assert not pub.is_alive()
        assert staging[0].is_dir()
        assert lock.is_file()
        before = _protected_state(graph)
        result = snapshot_staging_cleanup_plan(graph)
        assert result["deletion_candidates"] == [staging[0].name]
        assert result["deletion_candidate_count"] == 1
        assert result["blocked_entries"] == []
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
        assert _protected_state(graph) == before
    finally:
        if pub.is_alive():
            os.kill(pub.pid, 9)
        pub.join(timeout=5)


def test_incomplete_cooperative_leftover_is_candidate(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    staging = _cooperative_leftover(graph, "20240101-000000-incompl", complete=False)
    result = snapshot_staging_cleanup_plan(graph)
    assert result["deletion_candidates"] == [staging.name]
    entry = _entry(result, staging.name)
    assert entry["complete_payload_candidate"] is False
    assert entry["cleanup_eligible"] is False
    assert result["blocked_count"] == 0


def test_live_publisher_is_blocked_held_writer_lease(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    paused = CTX.Event()
    resume = CTX.Event()
    pub = CTX.Process(
        target=_paused_publisher_events_only,
        args=(str(graph), "live", paused, resume),
    )
    try:
        pub.start()
        assert paused.wait(timeout=TIMEOUT)
        staging = [
            path
            for path in (graph / "snapshots").iterdir()
            if path.name.startswith(STAGING_NAME_PREFIX)
        ]
        assert len(staging) == 1
        started = time.monotonic()
        result = snapshot_staging_cleanup_plan(graph)
        assert time.monotonic() - started < 5
        assert result["deletion_candidates"] == []
        blocked = _blocked(result, staging[0].name)
        assert blocked["reason"] == "held_writer_lease"
        entry = _entry(result, staging[0].name)
        assert entry["writer_lease_state"] == "held_by_cooperating_writer"
        assert entry["cleanup_eligible"] is False
    finally:
        _cleanup_processes(pub, release=resume)


def test_publisher_waiting_for_promotion_lock_is_blocked(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old")
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    blocker = CTX.Process(target=_shared_hold, args=(str(graph), held, resume, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 10, about, got, q))
    try:
        blocker.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        staging = [
            path
            for path in (graph / "snapshots").iterdir()
            if path.name.startswith(STAGING_NAME_PREFIX)
        ]
        assert len(staging) == 1
        started = time.monotonic()
        result = snapshot_staging_cleanup_plan(graph)
        assert time.monotonic() - started < 5
        assert not got.is_set()
        assert result["deletion_candidates"] == []
        assert _blocked(result, staging[0].name)["reason"] == "held_writer_lease"
        assert _current(graph) == first.name
    finally:
        _cleanup_processes(pub, blocker, release=resume)


def test_legacy_missing_writer_lock_is_blocked(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    legacy = _staging_dir(graph, "20240101-000000-legacy")
    _write_complete_payload(legacy)
    result = snapshot_staging_cleanup_plan(graph)
    assert result["deletion_candidates"] == []
    assert _blocked(result, legacy.name)["reason"] == "legacy_or_missing_writer_lock"
    entry = _entry(result, legacy.name)
    assert entry["writer_lease_protocol"] == "legacy_absent"
    assert entry["writer_lease_state"] == "unverifiable"
    assert entry["cleanup_eligible"] is False


def test_noncanonical_and_non_directory_are_blocked(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    odd = _staging_dir(graph, ".not-an-id")
    _write_writer_lock(odd)
    named_file = graph / "snapshots" / f"{STAGING_NAME_PREFIX}20240101-000000-file"
    named_file.write_bytes(b"not-a-directory")
    result = snapshot_staging_cleanup_plan(graph)
    assert result["deletion_candidates"] == []
    assert _blocked(result, odd.name)["reason"] == "noncanonical_staging_name"
    assert _blocked(result, named_file.name)["reason"] == "non_directory_staging_entry"
    assert _entry(result, named_file.name)["cleanup_eligible"] is False


def test_symlinked_and_nonregular_writer_locks_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    staging = _staging_dir(graph, "20240101-000000-symlink")
    target = tmp_path / "outside.lock"
    target.write_bytes(b"must-not-follow")
    (staging / STAGING_WRITER_LOCK_NAME).symlink_to(target)
    linked = _run("--graph", str(graph), "--json")
    assert linked.returncode == 1
    assert linked.stdout == ""
    assert "symlink" in linked.stderr.lower() or "unsafe" in linked.stderr.lower()
    assert target.read_bytes() == b"must-not-follow"

    other = tmp_path / "g2"
    _publish(other, "base")
    fifo_dir = _staging_dir(other, "20240101-000000-fifo")
    os.mkfifo(fifo_dir / STAGING_WRITER_LOCK_NAME)
    fifo = _run("--graph", str(other), "--json")
    assert fifo.returncode == 1
    assert fifo.stdout == ""
    assert "unsafe" in fifo.stderr.lower() or "non-regular" in fifo.stderr.lower()


def test_two_scan_detects_writer_lock_and_state_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging as staging_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "aaaaaaaa-000000-ffffffff")
    original = staging_mod._scan_inventory_state
    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(
            original, lambda _root: _write_writer_lock(staging)
        ),
    )
    with pytest.raises(
        SnapshotStagingCleanupPlanIntegrityError, match="writer_lock_present|writer_lease"
    ):
        snapshot_staging_cleanup_plan(graph)

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    leftover = _cooperative_leftover(graph2, "bbbbbbbb-000000-ffffffff", complete=True)
    lock = leftover / STAGING_WRITER_LOCK_NAME
    original2 = staging_mod._scan_inventory_state
    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original2, lambda _root: lock.unlink()),
    )
    with pytest.raises(
        SnapshotStagingCleanupPlanIntegrityError,
        match="writer_lock_present|disappeared|writer_lease",
    ):
        snapshot_staging_cleanup_plan(graph2)

    graph3 = tmp_path / "g3"
    _publish(graph3, "live")
    held_dir = _cooperative_leftover(graph3, "cccccccc-000000-ffffffff", complete=True)
    q = CTX.Queue()
    held = CTX.Event()
    resume = CTX.Event()
    holder = CTX.Process(target=_writer_lease_hold, args=(str(held_dir), held, resume, q))
    try:
        monkeypatch.setattr(
            staging_mod,
            "_scan_inventory_state",
            _mutate_after_first(
                staging_mod._scan_inventory_state,
                lambda _root: (holder.start(), held.wait(timeout=TIMEOUT)),
            ),
        )
        with pytest.raises(
            SnapshotStagingCleanupPlanIntegrityError, match="writer_lease_state"
        ):
            snapshot_staging_cleanup_plan(graph3)
    finally:
        _cleanup_processes(holder, release=resume)

    graph4 = tmp_path / "g4"
    _publish(graph4, "live")
    typed = _cooperative_leftover(graph4, "dddddddd-000000-ffffffff", complete=True)
    lock4 = typed / STAGING_WRITER_LOCK_NAME
    original4 = staging_mod._scan_inventory_state

    def replace_with_dir(_root):
        lock4.unlink()
        lock4.mkdir()

    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original4, replace_with_dir),
    )
    with pytest.raises(
        SnapshotStagingCleanupPlanIntegrityError,
        match="unsafe|writer_lock_type|non-regular",
    ):
        snapshot_staging_cleanup_plan(graph4)


def test_two_scan_detects_staging_and_payload_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging as staging_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _cooperative_leftover(graph, "aaaaaaaa-000000-ffffffff", complete=True)
    added = graph / "snapshots" / f"{STAGING_NAME_PREFIX}bbbbbbbb-000000-ffffffff"
    original = staging_mod._scan_inventory_state
    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original, lambda _root: added.mkdir()),
    )
    with pytest.raises(SnapshotStagingCleanupPlanIntegrityError, match="staging_names"):
        snapshot_staging_cleanup_plan(graph)
    added.rmdir()

    payload = first / "entities.parquet"
    parked = tmp_path / "parked-entities.parquet"
    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(
            original,
            lambda _root: (payload.rename(parked), payload.write_bytes(b"ENT")),
        ),
    )
    with pytest.raises(
        SnapshotStagingCleanupPlanIntegrityError,
        match="staging_content_metadata|staging_identity|staging_metadata",
    ):
        snapshot_staging_cleanup_plan(graph)

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    staging = _cooperative_leftover(graph2, "cccccccc-000000-ffffffff", complete=True)
    parked_dir = tmp_path / "parked-staging"
    replacement = tmp_path / "replacement-staging"

    def replace_dir(_root):
        staging.rename(parked_dir)
        replacement.mkdir()
        _write_complete_payload(replacement)
        _write_writer_lock(replacement)
        replacement.rename(staging)

    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(staging_mod._scan_inventory_state, replace_dir),
    )
    with pytest.raises(
        SnapshotStagingCleanupPlanIntegrityError, match="staging_identity"
    ):
        snapshot_staging_cleanup_plan(graph2)


def test_staging_state_revision_changes_on_inode_replacement(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    staging = _cooperative_leftover(graph, "20240101-000000-inode", complete=True)
    first = snapshot_staging_cleanup_plan(graph)
    parked = tmp_path / "parked-inode"
    staging.rename(parked)
    staging.mkdir()
    for child in sorted(parked.iterdir()):
        (staging / child.name).write_bytes(child.read_bytes())
    second = snapshot_staging_cleanup_plan(graph)
    assert first["observed_staging_revision"] == second["observed_staging_revision"]
    assert first["staging_entries"] == second["staging_entries"]
    assert first["deletion_candidates"] == second["deletion_candidates"] == [staging.name]
    assert first["staging_state_revision"] != second["staging_state_revision"]
    assert first["plan_revision"] != second["plan_revision"]


def test_deterministic_plan_revision_and_output(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    leftover = _cooperative_leftover(graph, "20240101-000000-planrev", complete=True)
    legacy = _staging_dir(graph, "20240101-000000-legacy")
    first = snapshot_staging_cleanup_plan(graph)
    second = snapshot_staging_cleanup_plan(graph)
    assert first == second
    assert first["plan_revision"] == plan_revision_of(first)
    expected = (
        "sha256:"
        + hashlib.sha256(canonical_plan_revision_text(first).encode("utf-8")).hexdigest()
    )
    assert first["plan_revision"] == expected
    payload = json.loads(canonical_plan_revision_text(first))
    assert "graph" not in payload
    assert "notices" not in payload
    assert "staging_count" not in payload
    assert "deletion_candidate_count" not in payload
    assert "blocked_count" not in payload
    assert "staging_entries" not in payload
    assert payload["schema_version"] == 2
    assert payload["current"] == first["current"]
    assert payload["published_snapshots"] == first["published_snapshots"]
    assert payload["deletion_candidates"] == [leftover.name]
    assert payload["blocked_entries"] == [
        {"name": legacy.name, "reason": "legacy_or_missing_writer_lock"}
    ]
    assert payload["observed_staging_revision"] == first["observed_staging_revision"]
    assert payload["staging_state_revision"] == first["staging_state_revision"]
    assert payload["ownership_inference"] is False
    assert payload["cleanup_applied"] is False
    assert payload["apply_supported"] is True
    text = format_result(first)
    assert text.startswith("snapshot-staging-cleanup-plan:")
    assert first["plan_revision"] in text
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    encoded = result_to_json(first)
    assert encoded == result_to_json(second)
    assert encoded.endswith("\n")
    parsed = json.loads(encoded)
    assert list(parsed) == sorted(parsed)


def test_documented_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = tmp_path / "g"
    _publish(graph, "live")
    for index in range(MAX_STAGING_ENTRIES + 1):
        _staging_dir(graph, f"20240101-000000-{index:08x}")
    too_many = _run("--graph", str(graph), "--json")
    assert too_many.returncode == 2
    assert too_many.stdout == ""
    assert "exceeds bound" in too_many.stderr

    other = tmp_path / "g2"
    _publish(other, "live")
    wide = _staging_dir(other, "20240101-000000-aaaaaaaa")
    for index in range(MAX_TOP_LEVEL_ENTRIES + 1):
        (wide / f"file-{index:02d}.bin").write_bytes(b"x")
    too_wide = _run("--graph", str(other), "--json")
    assert too_wide.returncode == 2
    assert too_wide.stdout == ""
    assert "exceeds bound" in too_wide.stderr

    import graphrag_code.snapshot_staging as staging_mod

    published_graph = tmp_path / "published-bound"
    for marker in ("one", "two", "three"):
        _publish(published_graph, marker)
    monkeypatch.setattr(staging_mod, "MAX_PUBLISHED_SNAPSHOTS", 2)
    with pytest.raises(SnapshotStagingCleanupPlanError, match="published snapshot count"):
        snapshot_staging_cleanup_plan(published_graph)

    monkeypatch.setattr(staging_mod.os, "supports_fd", set())
    with pytest.raises(SnapshotStagingCleanupPlanError, match="descriptor-relative"):
        snapshot_staging_cleanup_plan(other)

    current_bound = tmp_path / "current-bound"
    _publish(current_bound, "live")
    (current_bound / "current").write_bytes(b"x" * (MAX_CURRENT_BYTES + 1))
    oversized_current = _run("--graph", str(current_bound), "--json")
    assert oversized_current.returncode == 2
    assert oversized_current.stdout == ""
    assert "current pointer exceeds bound" in oversized_current.stderr


def test_missing_replaced_unsafe_publish_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = tmp_path / "g"
    _publish(graph, "base")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    missing = _run("--graph", str(graph), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "publish.lock" in missing.stderr

    other = tmp_path / "g2"
    _publish(other, "base")
    other_lock = other / PUBLICATION_LOCK_NAME
    parked = tmp_path / "parked.publish.lock"
    outside = tmp_path / "outside.publish.lock"
    outside.write_bytes(b"must-not-follow")
    other_lock.rename(parked)
    other_lock.symlink_to(outside)
    linked = _run("--graph", str(other), "--json")
    assert linked.returncode == 2
    assert linked.stdout == ""
    assert "symlink" in linked.stderr.lower() or "unsafe" in linked.stderr.lower()
    assert outside.read_bytes() == b"must-not-follow"

    fifo_graph = tmp_path / "g3"
    _publish(fifo_graph, "base")
    fifo_lock = fifo_graph / PUBLICATION_LOCK_NAME
    fifo_lock.unlink()
    os.mkfifo(fifo_lock)
    fifo = _run("--graph", str(fifo_graph), "--json")
    assert fifo.returncode == 2
    assert fifo.stdout == ""


def test_no_staging_or_protected_files_mutated(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-keep", complete=True)
    legacy = _staging_dir(graph, "20240101-000000-legacy")
    before = _protected_state(graph)
    ok = snapshot_staging_cleanup_plan(graph)
    assert leftover.name in ok["deletion_candidates"]
    assert _blocked(ok, legacy.name)["reason"] == "legacy_or_missing_writer_lock"
    assert _protected_state(graph) == before

    missing = _run("--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert _protected_state(graph) == before

    with pytest.raises(SnapshotStagingCleanupPlanError):
        snapshot_staging_cleanup_plan(tmp_path / "missing")
    assert _protected_state(graph) == before
    assert "Traceback" not in missing.stderr


def test_writer_lock_bytes_and_metadata_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _cooperative_leftover(graph, "20240101-000000-meta", complete=True)
    lock = staging / STAGING_WRITER_LOCK_NAME
    before = lock.lstat()
    before_bytes = lock.read_bytes()
    result = snapshot_staging_cleanup_plan(graph)
    assert result["deletion_candidates"] == [staging.name]
    after = lock.lstat()
    assert lock.read_bytes() == before_bytes
    assert after.st_ino == before.st_ino
    assert after.st_dev == before.st_dev
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns


def test_implementation_does_not_invoke_cleanup_or_producers():
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
    assert "build_stable_staging_inventory_unlocked" in imported
    assert "staging_state_revision_of" in imported
    assert "graph_read_lease" in imported
    assert "snapshot_staging" not in called
    assert "_snapshot_staging_scope" not in imported
    assert "publish_byog_snapshot" not in source
    assert "snapshot_activate" not in source
    assert "snapshot_prune" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "graph_exclusive_lease" not in source
    assert "probe_staging_writer_lease" not in imported
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
    args = ["--graph", "g", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_staging_cleanup_plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-staging-cleanup-plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    listed = snapshot_staging_cleanup_plan(graph)
    assert bodies[0]["plan_revision"] == listed["plan_revision"]
    assert leftover.name in bodies[0]["deletion_candidates"]
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-staging-cleanup-plan",
            "--graph",
            str(graph),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["plan_revision"] == listed["plan_revision"]

    sdist = built_wheel_and_sdist[1]
    import tarfile

    with tarfile.open(sdist, "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_staging_cleanup_plan.py" in names


def test_cli_serializes_writes_and_flushes_under_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_staging_cleanup_plan as plan_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    original_scope = plan_mod._snapshot_staging_cleanup_plan_scope
    original_json = plan_mod.result_to_json
    original_format = plan_mod.format_result
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

    monkeypatch.setattr(plan_mod, "_snapshot_staging_cleanup_plan_scope", tracked_scope)
    monkeypatch.setattr(plan_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(plan_mod, "format_result", guarded_format)
    monkeypatch.setattr(plan_mod.sys, "stdout", GuardedStdout())
    assert plan_mod.main(["--graph", str(graph), "--json"]) == 0
    assert plan_mod.main(["--graph", str(graph)]) == 0
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_mcp_remains_exactly_fifteen():
    assert len(TOOL_NAMES) == 15
    assert "snapshot_staging" not in TOOL_NAMES
    assert "snapshot_staging_cleanup_plan" not in TOOL_NAMES
    assert "snapshot_staging_cleanup_plan" not in " ".join(TOOL_NAMES)


def test_mcp_list_tools_has_no_cleanup_plan(tmp_path: Path):
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
            assert len(names) == 15
            assert "snapshot_staging_cleanup_plan" not in names

    anyio_run(_body)
