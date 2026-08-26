"""Read-only composite snapshot maintenance plan.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_maintenance_plan.py -q
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
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import (  # type: ignore
    ABSENT_REVISION,
    OPERATOR_PINS_NAME,
    snapshot_pin,
)
from graphrag_code.snapshot_retention import snapshot_retention_plan  # type: ignore
from graphrag_code.snapshot_staging_cleanup_plan import (  # type: ignore
    snapshot_staging_cleanup_plan,
)
from graphrag_code.snapshot_maintenance_plan import (  # type: ignore
    SnapshotMaintenancePlanError,
    SnapshotMaintenancePlanIntegrityError,
    canonical_maintenance_revision_text,
    format_result,
    maintenance_revision_of,
    result_to_json,
    snapshot_maintenance_plan,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_maintenance_plan.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_maintenance_plan.py"
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
        "snapshot_retention_plan",
        "c_clang_ast_capture",
        "c_compiler_facts",
        "graph_exclusive_lease",
        "_publication_lock",
        "staging_writer_lease",
        "acquire_existing_staging_writer_claim",
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
        settings_text=f"maintenance-plan: {marker}\n",
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


def _blocked(result: dict, name: str) -> dict:
    matches = [
        item
        for item in result["staging_cleanup_plan"]["blocked_entries"]
        if item["name"] == name
    ]
    assert len(matches) == 1, name
    return matches[0]


def _assert_composite_shape(result: dict, *, keep_last: int, current: str) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["keep_last"] == keep_last
    assert result["current"] == current
    assert result["fresh_plan_required_after_any_apply"] is True
    assert result["current"] == result["retention_plan"]["current"]
    assert result["current"] == result["staging_cleanup_plan"]["current"]
    assert result["published_snapshots"] == result["retention_plan"]["published_snapshots"]
    assert result["published_snapshots"] == result["staging_cleanup_plan"]["published_snapshots"]
    assert result["maintenance_revision"].startswith("sha256:")
    assert len(result["maintenance_revision"]) == len("sha256:") + 64
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "composite_is_read_only",
        "no_recommended_apply_order",
        "fresh_plan_required_after_any_apply",
        "maintenance_revision_informational",
        "advisory_locks_cooperating_only",
    ]


def test_empty_maintenance_plan(tmp_path: Path):
    graph = tmp_path / "g"
    only = _publish(graph, "only")
    before = _protected_state(graph)
    byog_before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    result = snapshot_maintenance_plan(graph, 1)
    _assert_composite_shape(result, keep_last=1, current=only.name)
    assert result["graph"] == str(graph.resolve())
    assert result["published_snapshots"] == [only.name]
    assert result["retention_plan"]["deletion_candidates"] == []
    assert result["staging_cleanup_plan"]["deletion_candidates"] == []
    assert result["actionable_components"] == []
    assert result["staging_cleanup_plan"]["schema_version"] == 2
    assert result["staging_cleanup_plan"]["apply_supported"] is True
    assert result["retention_plan"]["schema_version"] == 1
    human = format_result(result)
    assert "fresh_plan_required_after_any_apply=true" in human
    assert result["retention_plan"]["plan_revision"] in human
    assert result["staging_cleanup_plan"]["plan_revision"] in human
    assert "deleted" not in human.lower()
    assert _protected_state(graph) == before
    assert {path.name: _root_fingerprint(path) for path in BYOG_ROOTS} == byog_before
    assert len(BYOG_ROOTS) == 15


def test_retention_candidates_only(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    before = _protected_state(graph)
    result = snapshot_maintenance_plan(graph, 1)
    _assert_composite_shape(result, keep_last=1, current=newer.name)
    assert result["retention_plan"]["deletion_candidates"] == [older.name]
    assert result["staging_cleanup_plan"]["deletion_candidates"] == []
    assert result["actionable_components"] == ["snapshot-prune"]
    assert _protected_state(graph) == before


def test_staging_cleanup_candidates_only(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "only")
    leftover = _cooperative_leftover(graph, "20240101-000000-left", complete=True)
    before = _protected_state(graph)
    result = snapshot_maintenance_plan(graph, 1)
    _assert_composite_shape(result, keep_last=1, current=published.name)
    assert result["retention_plan"]["deletion_candidates"] == []
    assert result["staging_cleanup_plan"]["deletion_candidates"] == [leftover.name]
    assert result["actionable_components"] == ["snapshot-staging-cleanup"]
    assert leftover.is_dir()
    assert _protected_state(graph) == before


def test_both_components_actionable(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    leftover = _cooperative_leftover(graph, "20240101-000000-both", complete=False)
    before = _protected_state(graph)
    result = snapshot_maintenance_plan(graph, 1)
    _assert_composite_shape(result, keep_last=1, current=newer.name)
    assert result["retention_plan"]["deletion_candidates"] == [older.name]
    assert result["staging_cleanup_plan"]["deletion_candidates"] == [leftover.name]
    assert result["actionable_components"] == [
        "snapshot-prune",
        "snapshot-staging-cleanup",
    ]
    assert result["actionable_components"] == sorted(
        result["actionable_components"], key=lambda item: item.encode("utf-8")
    )
    human = format_result(result)
    assert "actionable=snapshot-prune,snapshot-staging-cleanup" in human
    assert _protected_state(graph) == before


def test_operator_and_claim_pins_protected_by_embedded_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import byog_graph

    graph = tmp_path / "g"
    oldest = _publish(graph, "oldest")
    mid = _publish(graph, "mid")
    newest = _publish(graph, "newest")
    snapshot_pin(graph, oldest.name, ABSENT_REVISION, pin_confirmed=True)
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {mid.name})
    before = _protected_state(graph)
    result = snapshot_maintenance_plan(graph, 1)
    plan = result["retention_plan"]
    assert plan["current"] == newest.name
    assert plan["operator_pins"] == [oldest.name]
    assert plan["claim_pins"] == [mid.name]
    assert set(plan["retained_snapshots"]) == {oldest.name, mid.name, newest.name}
    assert plan["deletion_candidates"] == []
    assert result["actionable_components"] == []
    assert oldest.is_dir() and mid.is_dir() and newest.is_dir()
    assert _protected_state(graph) == before


def test_blocked_staging_entries_match_standalone(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "base")
    leftover = _cooperative_leftover(graph, "20240101-000000-ok", complete=True)
    legacy = _staging_dir(graph, "20240101-000000-legacy")
    _write_complete_payload(legacy)
    odd = _staging_dir(graph, ".not-an-id")
    _write_writer_lock(odd)
    held_dir = _cooperative_leftover(graph, "20240101-000000-held", complete=True)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_writer_lease_hold, args=(str(held_dir), held, resume, q))
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        before = _protected_state(graph)
        result = snapshot_maintenance_plan(graph, 1)
        standalone = snapshot_staging_cleanup_plan(graph)
        assert result["staging_cleanup_plan"] == standalone
        assert leftover.name in result["staging_cleanup_plan"]["deletion_candidates"]
        assert _blocked(result, held_dir.name)["reason"] == "held_writer_lease"
        assert _blocked(result, legacy.name)["reason"] == "legacy_or_missing_writer_lock"
        assert _blocked(result, odd.name)["reason"] == "noncanonical_staging_name"
        assert held_dir.name not in result["staging_cleanup_plan"]["deletion_candidates"]
        assert _protected_state(graph) == before
    finally:
        _cleanup_processes(holder, release=resume)

    file_graph = tmp_path / "file-g"
    _publish(file_graph, "base")
    named_file = file_graph / "snapshots" / f"{STAGING_NAME_PREFIX}20240101-000000-file"
    named_file.write_bytes(b"not-a-directory")
    with pytest.raises(SnapshotMaintenancePlanError, match="not a directory"):
        snapshot_maintenance_plan(file_graph, 1)
    standalone = snapshot_staging_cleanup_plan(file_graph)
    assert any(
        item["name"] == named_file.name and item["reason"] == "non_directory_staging_entry"
        for item in standalone["blocked_entries"]
    )
    assert named_file.is_file()


def test_exact_embedded_plan_equality(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    leftover = _cooperative_leftover(graph, "20240101-000000-eq", complete=True)
    before = _protected_state(graph)
    result = snapshot_maintenance_plan(graph, 1)
    assert result["retention_plan"] == snapshot_retention_plan(graph, 1)
    assert result["staging_cleanup_plan"] == snapshot_staging_cleanup_plan(graph)
    assert older.name in result["retention_plan"]["deletion_candidates"]
    assert leftover.name in result["staging_cleanup_plan"]["deletion_candidates"]
    assert newer.name == result["current"]
    assert _protected_state(graph) == before


def test_deterministic_maintenance_revision_and_keep_last(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "older")
    newer = _publish(graph, "newer")
    leftover = _cooperative_leftover(graph, "20240101-000000-rev", complete=True)
    first = snapshot_maintenance_plan(graph, 1)
    second = snapshot_maintenance_plan(graph, 1)
    assert first == second
    assert first["maintenance_revision"] == maintenance_revision_of(first)
    expected = (
        "sha256:"
        + hashlib.sha256(
            canonical_maintenance_revision_text(first).encode("utf-8")
        ).hexdigest()
    )
    assert first["maintenance_revision"] == expected
    payload = json.loads(canonical_maintenance_revision_text(first))
    assert set(payload) == {
        "schema_version",
        "keep_last",
        "current",
        "published_snapshots",
        "retention_plan",
        "staging_cleanup_plan",
        "actionable_components",
        "fresh_plan_required_after_any_apply",
    }
    assert payload["schema_version"] == 1
    assert payload["keep_last"] == 1
    assert payload["current"] == newer.name
    assert payload["retention_plan"] == {
        "plan_revision": first["retention_plan"]["plan_revision"]
    }
    assert payload["staging_cleanup_plan"] == {
        "plan_revision": first["staging_cleanup_plan"]["plan_revision"]
    }
    assert payload["actionable_components"] == first["actionable_components"]
    assert payload["fresh_plan_required_after_any_apply"] is True
    assert "graph" not in payload
    assert "notices" not in payload
    assert "ok" not in payload
    encoded = result_to_json(first)
    assert encoded.endswith("\n")
    assert list(json.loads(encoded)) == sorted(json.loads(encoded))
    two = snapshot_maintenance_plan(graph, 2)
    assert two["keep_last"] == 2
    assert two["retention_plan"]["deletion_candidates"] == []
    assert two["maintenance_revision"] != first["maintenance_revision"]
    assert leftover.is_dir()


def test_input_changes_change_or_invalidate_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_plan as plan_mod
    import graphrag_code.snapshot_staging as staging_mod

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    leftover = _cooperative_leftover(graph, "aaaaaaaa-000000-ffffffff", complete=True)
    base = snapshot_maintenance_plan(graph, 1)

    (graph / "current").write_text(older.name + "\n", encoding="utf-8")
    switched = snapshot_maintenance_plan(graph, 1)
    assert switched["current"] == older.name
    assert switched["maintenance_revision"] != base["maintenance_revision"]
    (graph / "current").write_text(newer.name + "\n", encoding="utf-8")

    third = _publish(graph, "third")
    more = snapshot_maintenance_plan(graph, 1)
    assert third.name in more["published_snapshots"]
    assert more["maintenance_revision"] != base["maintenance_revision"]

    snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=True)
    pinned = snapshot_maintenance_plan(graph, 1)
    assert older.name not in pinned["retention_plan"]["deletion_candidates"]
    assert pinned["maintenance_revision"] != more["maintenance_revision"]

    extra = _cooperative_leftover(graph, "bbbbbbbb-000000-eeeeeeee", complete=False)
    staged = snapshot_maintenance_plan(graph, 1)
    assert extra.name in staged["staging_cleanup_plan"]["deletion_candidates"]
    assert staged["maintenance_revision"] != pinned["maintenance_revision"]

    lock = leftover / STAGING_WRITER_LOCK_NAME
    parked = tmp_path / "parked-writer.lock"
    lock.rename(parked)
    lock.write_bytes(b"replaced")
    replaced_lock = snapshot_maintenance_plan(graph, 1)
    assert replaced_lock["maintenance_revision"] != staged["maintenance_revision"]
    lock.unlink()
    parked.rename(lock)

    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_writer_lease_hold, args=(str(leftover), held, resume, q))
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        held_plan = snapshot_maintenance_plan(graph, 1)
        assert leftover.name not in held_plan["staging_cleanup_plan"]["deletion_candidates"]
        assert _blocked(held_plan, leftover.name)["reason"] == "held_writer_lease"
        assert held_plan["maintenance_revision"] != replaced_lock["maintenance_revision"]
    finally:
        _cleanup_processes(holder, release=resume)

    def switch_current(_root: Path) -> None:
        (graph / "current").write_text(older.name + "\n", encoding="utf-8")

    monkeypatch.setattr(plan_mod, "_after_retention_plan", switch_current)
    with pytest.raises(
        SnapshotMaintenancePlanIntegrityError, match="disagree on current"
    ):
        snapshot_maintenance_plan(graph, 1)
    (graph / "current").write_text(newer.name + "\n", encoding="utf-8")
    monkeypatch.setattr(plan_mod, "_after_retention_plan", lambda _root: None)

    original = staging_mod._scan_inventory_state
    seen = {"n": 0}

    def mutate_after_first(root: Path):
        result = original(root)
        seen["n"] += 1
        if seen["n"] == 1:
            (graph / "snapshots" / f"{STAGING_NAME_PREFIX}cccccccc-000000-ffffffff").mkdir()
        return result

    monkeypatch.setattr(staging_mod, "_scan_inventory_state", mutate_after_first)
    with pytest.raises(SnapshotMaintenancePlanIntegrityError, match="staging_names"):
        snapshot_maintenance_plan(graph, 1)


def test_exactly_one_shared_lease_no_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_plan as plan_mod
    import graphrag_code.snapshot_retention as retention
    import graphrag_code.snapshot_staging_cleanup_plan as cleanup

    graph = tmp_path / "g"
    _publish(graph, "only")
    calls = {"read": 0}
    original = plan_mod.graph_read_lease

    @contextmanager
    def counted(*args, **kwargs):
        calls["read"] += 1
        assert kwargs.get("allow_unlocked_managed") is False
        with original(*args, **kwargs) as lease:
            yield lease

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease")

    monkeypatch.setattr(plan_mod, "graph_read_lease", counted)
    monkeypatch.setattr(retention, "graph_read_lease", boom)
    monkeypatch.setattr(cleanup, "graph_read_lease", boom)
    monkeypatch.setattr(retention, "snapshot_retention_plan", boom)
    monkeypatch.setattr(cleanup, "snapshot_staging_cleanup_plan", boom)
    monkeypatch.setattr(retention, "_snapshot_retention_plan_scope", boom)
    monkeypatch.setattr(cleanup, "_snapshot_staging_cleanup_plan_scope", boom)
    result = snapshot_maintenance_plan(graph, 1)
    assert result["ok"] is True
    assert calls["read"] == 1


def test_exclusive_mutators_wait_for_composite_lease(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old")
    second = _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-wait", complete=True)
    listed = snapshot_maintenance_plan(graph, 1)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    planner = CTX.Process(
        target=_plan_hold, args=(str(graph), 1, held, resume, q)
    )
    waiters: list[multiprocessing.Process] = []
    try:
        planner.start()
        assert held.wait(timeout=TIMEOUT)
        actions = (
            (
                "publish",
                {"marker": "pubwait", "keep_last": 10},
            ),
            (
                "activate",
                {"snapshot": first.name, "expected": second.name},
            ),
            (
                "pin",
                {"snapshot": first.name, "revision": ABSENT_REVISION},
            ),
            (
                "prune",
                {
                    "keep_last": 1,
                    "revision": listed["retention_plan"]["plan_revision"],
                },
            ),
            (
                "cleanup",
                {"revision": listed["staging_cleanup_plan"]["plan_revision"]},
            ),
        )
        for action, payload in actions:
            about = CTX.Event()
            got = CTX.Event()
            waiter = CTX.Process(
                target=_exclusive_waiter,
                args=(str(graph), action, payload, about, got, q),
            )
            waiters.append(waiter)
            waiter.start()
            assert about.wait(timeout=TIMEOUT), action
            assert not got.is_set(), action
            waiter.terminate()
            waiter.join(timeout=TIMEOUT)
            assert not waiter.is_alive(), action
            assert _current(graph) == second.name
        assert leftover.is_dir()
        assert first.is_dir() and second.is_dir()
    finally:
        _cleanup_processes(planner, *waiters, release=resume)

    about = CTX.Event()
    got = CTX.Event()
    pub = CTX.Process(
        target=_exclusive_waiter,
        args=(
            str(graph),
            "publish",
            {"marker": "after", "keep_last": 10},
            about,
            got,
            q,
        ),
    )
    try:
        planner = CTX.Process(
            target=_plan_hold, args=(str(graph), 1, held, resume, q)
        )
        held.clear()
        resume.clear()
        planner.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        staging = [
            path
            for path in (graph / "snapshots").iterdir()
            if path.name.startswith(STAGING_NAME_PREFIX)
        ]
        assert leftover in staging
        assert any(
            (path / STAGING_WRITER_LOCK_NAME).is_file()
            for path in staging
            if path != leftover
        ) or leftover.is_dir()
        resume.set()
        planner.join(timeout=TIMEOUT)
        pub.join(timeout=TIMEOUT)
        assert not planner.is_alive() and not pub.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(planner, pub, release=resume)


def test_cli_serializes_writes_and_flushes_under_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_plan as plan_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    original_scope = plan_mod._snapshot_maintenance_plan_scope
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

    monkeypatch.setattr(plan_mod, "_snapshot_maintenance_plan_scope", tracked_scope)
    monkeypatch.setattr(plan_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(plan_mod, "format_result", guarded_format)
    monkeypatch.setattr(plan_mod.sys, "stdout", GuardedStdout())
    assert plan_mod.main(["--graph", str(graph), "--keep-last", "1", "--json"]) == 0
    assert plan_mod.main(["--graph", str(graph), "--keep-last", "2"]) == 0
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_missing_symlinked_nonregular_replaced_lock(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    missing = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "adopt-publication-lock" in missing.stderr
    assert not (graph / PUBLICATION_LOCK_NAME).exists()

    _publish(graph, "b")
    lock.unlink()
    target = tmp_path / "external.lock"
    target.write_text("untouched", encoding="utf-8")
    lock.symlink_to(target)
    linked = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert linked.returncode == 2
    assert linked.stdout == ""
    assert "symlink" in linked.stderr.lower() or "unsafe" in linked.stderr.lower()
    assert target.read_text(encoding="utf-8") == "untouched"

    lock.unlink()
    lock.mkdir()
    nonreg = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert nonreg.returncode == 2
    assert nonreg.stdout == ""
    assert lock.is_dir()

    other = tmp_path / "g2"
    _publish(other, "base")
    import graphrag_code.snapshot_maintenance_plan as plan_mod

    def drop_lock(_root: Path) -> None:
        (other / PUBLICATION_LOCK_NAME).unlink()

    plan_mod._after_retention_plan = drop_lock
    try:
        with pytest.raises(
            SnapshotMaintenancePlanIntegrityError, match="disappeared"
        ):
            snapshot_maintenance_plan(other, 1)
    finally:
        plan_mod._after_retention_plan = lambda _root: None

    replaced = tmp_path / "g3"
    _publish(replaced, "base")
    parked = tmp_path / "parked.publish.lock"
    outside = tmp_path / "outside.publish.lock"
    outside.write_bytes(b"must-not-follow")

    def replace_lock(_root: Path) -> None:
        lock_path = replaced / PUBLICATION_LOCK_NAME
        lock_path.rename(parked)
        lock_path.symlink_to(outside)

    plan_mod._after_retention_plan = replace_lock
    try:
        with pytest.raises(SnapshotMaintenancePlanIntegrityError):
            snapshot_maintenance_plan(replaced, 1)
        assert outside.read_bytes() == b"must-not-follow"
    finally:
        plan_mod._after_retention_plan = lambda _root: None


def test_unsafe_metadata_and_invalid_keep_last(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    unexpected = graph / "snapshots" / "published-looking-file"
    unexpected.write_text("not a snapshot directory", encoding="utf-8")
    unsafe = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert unsafe.returncode == 2
    assert unsafe.stdout == ""
    assert "unexpected unsafe" in unsafe.stderr

    other = tmp_path / "g2"
    _publish(other, "live")
    staging = _staging_dir(other, "20240101-000000-symlink")
    target = tmp_path / "outside.lock"
    target.write_bytes(b"must-not-follow")
    (staging / STAGING_WRITER_LOCK_NAME).symlink_to(target)
    linked = _run("--graph", str(other), "--keep-last", "1", "--json")
    assert linked.returncode == 1
    assert linked.stdout == ""
    assert target.read_bytes() == b"must-not-follow"

    good = tmp_path / "g3"
    _publish(good, "ok")
    before = _protected_state(good)
    zero = _run("--graph", str(good), "--keep-last", "0", "--json")
    assert zero.returncode == 2
    assert zero.stdout == ""
    assert "positive integer" in zero.stderr
    negative = _run("--graph", str(good), "--keep-last", "-1", "--json")
    assert negative.returncode == 2
    assert negative.stdout == ""
    missing = _run("--graph", str(good), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    with pytest.raises(SnapshotMaintenancePlanError, match="positive integer"):
        snapshot_maintenance_plan(good, 0)
    assert _protected_state(good) == before


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    leftover = _cooperative_leftover(graph, "20240101-000000-relcwd", complete=True)
    args = ["--graph", "g", "--keep-last", "1", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_maintenance_plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-maintenance-plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    listed = snapshot_maintenance_plan(graph, 1)
    assert bodies[0]["maintenance_revision"] == listed["maintenance_revision"]
    assert leftover.name in bodies[0]["staging_cleanup_plan"]["deletion_candidates"]
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-maintenance-plan",
            "--graph",
            str(graph),
            "--keep-last",
            "1",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["maintenance_revision"] == listed[
        "maintenance_revision"
    ]

    sdist = built_wheel_and_sdist[1]
    import tarfile

    with tarfile.open(sdist, "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_maintenance_plan.py" in names


def test_implementation_does_not_invoke_mutations_or_producers():
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
    assert "build_stable_retention_plan_unlocked" in imported
    assert "build_stable_cleanup_plan_unlocked" in imported
    assert "graph_read_lease" in imported
    assert "snapshot_retention_plan" not in called
    assert "snapshot_staging_cleanup_plan" not in called
    assert "graph_exclusive_lease" not in source
    assert "publish_byog_snapshot" not in source
    assert "cleanup_old_snapshots" not in source
    assert "snapshot_prune" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_mcp_remains_exactly_thirteen(tmp_path: Path):
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 13
    assert "snapshot_maintenance_plan" not in TOOL_NAMES
    assert "snapshot_maintenance_plan" not in " ".join(TOOL_NAMES)
    graph = tmp_path / "g"
    _publish(graph, "a")
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 13
            assert "snapshot_maintenance_plan" not in names

    anyio_run(_body)
    assert {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(BYOG_ROOTS) == 15


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


def _plan_hold(graph: str, keep_last: int, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_maintenance_plan import (
        _snapshot_maintenance_plan_scope,
    )

    try:
        with _snapshot_maintenance_plan_scope(ChildPath(graph), keep_last) as result:
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put(("ok", result["maintenance_revision"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _exclusive_waiter(graph: str, action: str, payload: dict, about, got, q) -> None:
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
    root = ChildPath(graph)
    try:
        if action == "publish":
            ents, rels, tus = _rows(payload["marker"])
            byog.publish_byog_snapshot(
                pd.DataFrame(ents),
                pd.DataFrame(rels),
                pd.DataFrame(tus),
                root,
                keep_last=payload["keep_last"],
            )
        elif action == "activate":
            from graphrag_code.snapshot_activate import snapshot_activate

            snapshot_activate(
                root,
                payload["snapshot"],
                payload["expected"],
                activate_confirmed=True,
            )
        elif action == "pin":
            from graphrag_code.snapshot_pins import snapshot_pin

            snapshot_pin(
                root,
                payload["snapshot"],
                payload["revision"],
                pin_confirmed=True,
            )
        elif action == "prune":
            from graphrag_code.snapshot_prune import snapshot_prune

            snapshot_prune(
                root,
                payload["keep_last"],
                payload["revision"],
                prune_confirmed=True,
            )
        elif action == "cleanup":
            from graphrag_code.snapshot_staging_cleanup import snapshot_staging_cleanup

            snapshot_staging_cleanup(
                root,
                payload["revision"],
                cleanup_confirmed=True,
            )
        else:
            raise AssertionError(action)
        q.put(("ok", action))
    except Exception as exc:
        q.put(f"error:{action}:{type(exc).__name__}:{exc}")
