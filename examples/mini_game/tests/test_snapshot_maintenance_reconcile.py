"""Read-only post-apply snapshot maintenance reconciliation.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_maintenance_reconcile.py -q
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shutil
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
from graphrag_code.snapshot_maintenance_apply import (  # type: ignore
    snapshot_maintenance_apply,
)
from graphrag_code.snapshot_maintenance_plan import (  # type: ignore
    result_to_json as plan_to_json,
    snapshot_maintenance_plan,
)
from graphrag_code.snapshot_maintenance_reconcile import (  # type: ignore
    MAX_INPUT_BYTES,
    SnapshotMaintenanceReconcileError,
    SnapshotMaintenanceReconcileIntegrityError,
    format_result,
    result_to_json,
    snapshot_maintenance_reconcile,
)
from graphrag_code.snapshot_pins import (  # type: ignore
    ABSENT_REVISION,
    OPERATOR_PINS_NAME,
    snapshot_pin,
)

SCRIPT = ROOT / "scripts" / "snapshot_maintenance_reconcile.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_maintenance_reconcile.py"
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
CANDIDATE_STATES = (
    "absent_at_reconcile",
    "present_directory_at_reconcile",
    "present_non_directory_at_reconcile",
    "unsafe_symlink_at_reconcile",
    "changed_during_reconcile",
)


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
        settings_text=f"maintenance-reconcile: {marker}\n",
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


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return path


def _save_plan(directory: Path, plan: dict, name: str = "plan.json") -> Path:
    path = directory / name
    path.write_text(plan_to_json(plan), encoding="utf-8")
    return path


def _save_apply(directory: Path, result: dict, name: str = "apply.json") -> Path:
    from graphrag_code.snapshot_maintenance_apply import result_to_json as apply_to_json

    path = directory / name
    path.write_text(apply_to_json(result), encoding="utf-8")
    return path


def _forge_complete_result(plan: dict) -> dict:
    from graphrag_code.snapshot_maintenance_apply import result_to_json as apply_to_json
    import graphrag_code.snapshot_maintenance_apply as apply_mod

    retention = plan["retention_plan"]
    cleanup = plan["staging_cleanup_plan"]
    deleted_snapshots = list(retention["deletion_candidates"])
    deleted_staging = list(cleanup["deletion_candidates"])
    completed = []
    if deleted_staging:
        completed.append("snapshot-staging-cleanup")
    if deleted_snapshots:
        completed.append("snapshot-prune")
    forged = apply_mod._result(
        plan,
        expected=plan["maintenance_revision"],
        deleted_snapshots=deleted_snapshots,
        deleted_staging=deleted_staging,
        failed_snapshot=None,
        failed_staging_entry=None,
        not_attempted_snapshots=[],
        not_attempted_staging=[],
        completed_components=completed,
        stopped_on_component=None,
        not_attempted_components=[],
        error=None,
        partial=False,
    )
    json.loads(apply_to_json(forged))
    return forged


def _assert_observation_shape(result: dict, plan: dict, *, apply_supplied: bool) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["input_plan_valid"] is True
    assert result["input_plan_revision"] == plan["maintenance_revision"]
    assert result["graph"] == plan["graph"]
    assert result["keep_last"] == plan["keep_last"]
    assert result["reconciliation_is_observation_only"] is True
    assert result["deletion_cause_proven"] is False
    assert result["recovery_performed"] is False
    assert result["apply_result_supplied"] is apply_supplied
    assert result["apply_result_valid"] is (True if apply_supplied else None)
    assert result["input_byte_limit"] == MAX_INPUT_BYTES
    assert result["planned_deletion_snapshots"] == plan["retention_plan"][
        "deletion_candidates"
    ]
    assert result["planned_deletion_staging_entries"] == plan["staging_cleanup_plan"][
        "deletion_candidates"
    ]
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "reconciliation_is_observation_only",
        "deletion_cause_not_proven",
        "no_recovery_performed",
        "fresh_plan_required_before_mutation",
        "advisory_locks_cooperating_only",
        "input_files_bounded",
        "cli_only_not_mcp",
    ]
    for observation in (
        result["published_candidate_observations"]
        + result["staging_candidate_observations"]
    ):
        assert observation["state"] in CANDIDATE_STATES
        assert "deleted" != observation["state"]
    assert "reconciliation_is_observation_only=true" in format_result(result)


def test_plan_only_reconciliation_before_apply(tmp_path: Path):
    graph = tmp_path / "g"
    old = _publish(graph, "old")
    live = _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-before", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)
    before = _protected_state(graph)
    result = snapshot_maintenance_reconcile(graph, plan_file)
    _assert_observation_shape(result, plan, apply_supplied=False)
    assert result["current_matches_saved_plan"] is True
    assert result["observed_current"] == live.name
    assert result["result_consistent_with_observation"] is None
    assert result["all_planned_candidates_absent_at_reconcile"] is False
    published = {item["name"]: item for item in result["published_candidate_observations"]}
    staging = {item["name"]: item for item in result["staging_candidate_observations"]}
    assert published[old.name]["state"] == "present_directory_at_reconcile"
    assert staging[leftover.name]["state"] == "present_directory_at_reconcile"
    assert staging[leftover.name]["ownership_status"] == "unknown"
    assert staging[leftover.name]["writer_lease_state"] == "not_held_at_scan"
    assert all(item["declared_status"] is None for item in published.values())
    assert _protected_state(graph) == before
    assert old.is_dir() and leftover.is_dir() and live.is_dir()


def test_reconciliation_after_complete_apply(tmp_path: Path):
    graph = tmp_path / "g"
    old = _publish(graph, "old")
    live = _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-done", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)
    applied = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    apply_file = _save_apply(tmp_path, applied)
    before = _protected_state(graph)
    result = snapshot_maintenance_reconcile(graph, plan_file, apply_file)
    _assert_observation_shape(result, plan, apply_supplied=True)
    assert result["result_consistent_with_observation"] is True
    assert result["all_planned_candidates_absent_at_reconcile"] is True
    assert result["current_matches_saved_plan"] is True
    assert result["observed_current"] == live.name
    assert old.name not in result["observed_published_snapshots"]
    for item in (
        result["published_candidate_observations"]
        + result["staging_candidate_observations"]
    ):
        assert item["state"] == "absent_at_reconcile"
        assert item["declared_status"] == "declared_completely_deleted"
    assert not any(
        item["code"]
        in {"declared_deleted_but_present", "declared_remaining_but_absent"}
        for item in result["discrepancies"]
    )
    assert _protected_state(graph) == before
    assert not old.exists() and not leftover.exists() and live.is_dir()


def test_reconciliation_after_partial_cleanup_and_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_prune as prune_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "cleanup-partial"
    published = _publish(graph, "old")
    _publish(graph, "new")
    first = _cooperative_leftover(graph, "20240101-000000-aaa", complete=True)
    second = _cooperative_leftover(graph, "20240101-000000-zzz", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    orig_staging = cleanup_mod._remove_claimed_staging_entry
    calls = {"n": 0}

    def fail_later_staging(snapshots_dir, name, claim, expected):
        calls["n"] += 1
        if calls["n"] == 1:
            return orig_staging(snapshots_dir, name, claim, expected)
        raise RuntimeError(f"injected cleanup failure on {name}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later_staging)
    applied = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan, "cleanup-plan.json")
    apply_file = _save_apply(tmp_path, applied, "cleanup-apply.json")
    result = snapshot_maintenance_reconcile(graph, plan_file, apply_file)
    _assert_observation_shape(result, plan, apply_supplied=True)
    assert result["result_consistent_with_observation"] is True
    assert result["all_planned_candidates_absent_at_reconcile"] is False
    staging = {item["name"]: item for item in result["staging_candidate_observations"]}
    assert staging[first.name]["state"] == "absent_at_reconcile"
    assert staging[first.name]["declared_status"] == "declared_completely_deleted"
    assert staging[second.name]["state"] == "present_directory_at_reconcile"
    assert staging[second.name]["declared_status"] == "failed"
    published_obs = {
        item["name"]: item for item in result["published_candidate_observations"]
    }
    assert published_obs[published.name]["state"] == "present_directory_at_reconcile"
    assert published_obs[published.name]["declared_status"] == "not_attempted"

    graph2 = tmp_path / "prune-partial"
    _publish(graph2, "old")
    _publish(graph2, "mid")
    live = _publish(graph2, "live")
    leftover = _cooperative_leftover(graph2, "20240101-000000-ok", complete=True)
    plan2 = snapshot_maintenance_plan(graph2, 1)
    orig_prune = prune_mod._remove_published_snapshot_directory
    prune_calls = {"n": 0}

    def fail_later_prune(snapshots_dir, snap_id):
        prune_calls["n"] += 1
        if prune_calls["n"] == 1:
            return orig_prune(snapshots_dir, snap_id)
        raise RuntimeError(f"injected prune failure on {snap_id}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", orig_staging)
    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", fail_later_prune)
    applied2 = snapshot_maintenance_apply(
        graph2, 1, plan2["maintenance_revision"], maintenance_confirmed=True
    )
    result2 = snapshot_maintenance_reconcile(
        graph2,
        _save_plan(tmp_path, plan2, "prune-plan.json"),
        _save_apply(tmp_path, applied2, "prune-apply.json"),
    )
    _assert_observation_shape(result2, plan2, apply_supplied=True)
    assert result2["result_consistent_with_observation"] is True
    candidates = plan2["retention_plan"]["deletion_candidates"]
    published2 = {
        item["name"]: item for item in result2["published_candidate_observations"]
    }
    assert published2[candidates[0]]["state"] == "absent_at_reconcile"
    assert published2[candidates[1]]["state"] == "present_directory_at_reconcile"
    assert published2[candidates[1]]["declared_status"] == "failed"
    assert result2["staging_candidate_observations"][0]["state"] == "absent_at_reconcile"
    assert leftover.exists() is False
    assert live.is_dir()


def test_valid_result_with_remaining_and_declared_mismatches(tmp_path: Path):
    graph = tmp_path / "g"
    old = _publish(graph, "old")
    pinned = _publish(graph, "pinned")
    live = _publish(graph, "live")
    snapshot_pin(graph, pinned.name, ABSENT_REVISION, pin_confirmed=True)
    leftover = _cooperative_leftover(graph, "20240101-000000-remain", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    applied = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    remaining = snapshot_maintenance_reconcile(
        graph, _save_plan(tmp_path, plan), _save_apply(tmp_path, applied)
    )
    _assert_observation_shape(remaining, plan, apply_supplied=True)
    assert remaining["result_consistent_with_observation"] is True
    assert leftover.name in [
        item["name"] for item in remaining["staging_candidate_observations"]
    ]
    assert pinned.name in remaining["observed_published_snapshots"]
    assert live.name in remaining["observed_published_snapshots"]
    assert old.name not in remaining["observed_published_snapshots"]

    present_graph = tmp_path / "present"
    victim = _publish(present_graph, "old")
    _publish(present_graph, "new")
    leftover2 = _cooperative_leftover(
        present_graph, "20240101-000000-present", complete=True
    )
    present_plan = snapshot_maintenance_plan(present_graph, 1)
    forged = _forge_complete_result(present_plan)
    present_result = snapshot_maintenance_reconcile(
        present_graph,
        _save_plan(tmp_path, present_plan, "present-plan.json"),
        _save_apply(tmp_path, forged, "present-apply.json"),
    )
    assert present_result["ok"] is True
    assert present_result["result_consistent_with_observation"] is False
    codes = {item["code"] for item in present_result["discrepancies"]}
    assert "declared_deleted_but_present" in codes
    assert victim.is_dir() and leftover2.is_dir()

    absent_graph = tmp_path / "absent"
    gone = _publish(absent_graph, "old")
    _publish(absent_graph, "new")
    leftover3 = _cooperative_leftover(
        absent_graph, "20240101-000000-absent", complete=True
    )
    absent_plan = snapshot_maintenance_plan(absent_graph, 1)
    import graphrag_code.snapshot_maintenance_apply as apply_mod

    forged_absent = apply_mod._result(
        absent_plan,
        expected=absent_plan["maintenance_revision"],
        deleted_snapshots=[],
        deleted_staging=[],
        failed_snapshot=None,
        failed_staging_entry=leftover3.name,
        not_attempted_snapshots=[gone.name],
        not_attempted_staging=[],
        completed_components=[],
        stopped_on_component="snapshot-staging-cleanup",
        not_attempted_components=["snapshot-prune"],
        error="forged",
        partial=True,
    )
    shutil.rmtree(gone)
    shutil.rmtree(leftover3)
    absent_result = snapshot_maintenance_reconcile(
        absent_graph,
        _save_plan(tmp_path, absent_plan, "absent-plan.json"),
        _save_apply(tmp_path, forged_absent, "absent-apply.json"),
    )
    assert absent_result["ok"] is True
    assert absent_result["result_consistent_with_observation"] is False
    assert {
        item["code"] for item in absent_result["discrepancies"]
    } >= {"declared_remaining_but_not_directory"}

    moved_graph = tmp_path / "moved-current"
    prior = _publish(moved_graph, "prior")
    _publish(moved_graph, "planned-current")
    moved_plan = snapshot_maintenance_plan(moved_graph, 2)
    moved_apply = _forge_complete_result(moved_plan)
    (moved_graph / "current").write_text(prior.name + "\n", encoding="utf-8")
    moved_result = snapshot_maintenance_reconcile(
        moved_graph,
        _save_plan(tmp_path, moved_plan, "moved-plan.json"),
        _save_apply(tmp_path, moved_apply, "moved-apply.json"),
    )
    assert moved_result["result_consistent_with_observation"] is False
    assert "current_differs_from_saved_plan" in {
        item["code"] for item in moved_result["discrepancies"]
    }


def test_malformed_oversized_symlinked_and_replaced_inputs(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)
    before = _protected_state(graph)

    missing = _run("--graph", str(graph), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")
    bad = _run("--graph", str(graph), "--plan-file", str(malformed), "--json")
    assert bad.returncode == 2
    assert bad.stdout == ""

    oversized = tmp_path / "huge.json"
    oversized.write_bytes(b"{" + b"a" * (MAX_INPUT_BYTES + 1) + b"}")
    huge = _run("--graph", str(graph), "--plan-file", str(oversized), "--json")
    assert huge.returncode == 2
    assert huge.stdout == ""
    assert str(MAX_INPUT_BYTES) in huge.stderr

    linked = tmp_path / "link.json"
    linked.symlink_to(plan_file)
    symlink = _run("--graph", str(graph), "--plan-file", str(linked), "--json")
    assert symlink.returncode == 2
    assert symlink.stdout == ""
    assert "symlink" in symlink.stderr

    apply_linked = tmp_path / "apply-link.json"
    apply_linked.symlink_to(plan_file)
    apply_symlink = _run(
        "--graph",
        str(graph),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(apply_linked),
        "--json",
    )
    assert apply_symlink.returncode == 2
    assert apply_symlink.stdout == ""

    import graphrag_code.snapshot_maintenance_reconcile as reconcile_mod

    original_hook = reconcile_mod._after_input_path_lstat
    victim = tmp_path / "replaced.json"
    shutil.copyfile(plan_file, victim)

    def replace_after_lstat(path: Path) -> None:
        if path == victim:
            path.unlink()
            path.write_text('{"schema_version": 1}\n', encoding="utf-8")

    reconcile_mod._after_input_path_lstat = replace_after_lstat
    try:
        with pytest.raises(SnapshotMaintenanceReconcileError, match="changed|unsafe"):
            snapshot_maintenance_reconcile(graph, victim)
        proc = _run("--graph", str(graph), "--plan-file", str(victim), "--json")
        assert proc.returncode == 2
        assert proc.stdout == ""
    finally:
        reconcile_mod._after_input_path_lstat = original_hook
    assert _protected_state(graph) == before


def test_invalid_input_is_rejected_before_graph_root_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_reconcile as reconcile_mod

    malformed_plan = tmp_path / "malformed-plan.json"
    malformed_plan.write_text("{not-json", encoding="utf-8")

    def graph_inspection_is_forbidden(_graph):
        raise AssertionError("graph root was inspected before input validation")

    monkeypatch.setattr(
        reconcile_mod, "_resolve_graph_root", graph_inspection_is_forbidden
    )
    with pytest.raises(SnapshotMaintenanceReconcileError, match="valid JSON"):
        snapshot_maintenance_reconcile(tmp_path / "missing", malformed_plan)

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    malformed_apply = tmp_path / "malformed-apply.json"
    malformed_apply.write_text("[]", encoding="utf-8")
    with pytest.raises(SnapshotMaintenanceReconcileError, match="JSON object"):
        snapshot_maintenance_reconcile(
            graph, _save_plan(tmp_path, plan), malformed_apply
        )


def test_embedded_hashes_and_candidate_names_are_validated(tmp_path: Path):
    from graphrag_code.snapshot_maintenance_plan import maintenance_revision_of
    from graphrag_code.snapshot_staging_cleanup_plan import (
        plan_revision_of as cleanup_revision_of,
    )

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    before = _protected_state(graph)

    stale_component = copy.deepcopy(plan)
    stale_component["retention_plan"]["operator_pins"] = [plan["current"]]
    with pytest.raises(
        SnapshotMaintenanceReconcileError, match="retention plan_revision"
    ):
        snapshot_maintenance_reconcile(
            graph, _write_json(tmp_path / "stale-component.json", stale_component)
        )

    outside = tmp_path / "outside"
    outside.write_text("must-not-inspect-or-change\n", encoding="utf-8")
    forged = copy.deepcopy(plan)
    cleanup = forged["staging_cleanup_plan"]
    cleanup["deletion_candidates"] = ["../../outside"]
    cleanup["deletion_candidate_count"] = 1
    cleanup["plan_revision"] = cleanup_revision_of(cleanup)
    forged["actionable_components"] = ["snapshot-staging-cleanup"]
    forged["maintenance_revision"] = maintenance_revision_of(forged)
    outside_before = outside.stat()
    with pytest.raises(
        SnapshotMaintenanceReconcileError, match="non-canonical direct staging"
    ):
        snapshot_maintenance_reconcile(
            graph, _write_json(tmp_path / "traversal.json", forged)
        )
    outside_after = outside.stat()
    assert outside.read_text(encoding="utf-8") == "must-not-inspect-or-change\n"
    assert (outside_after.st_ino, outside_after.st_mtime_ns, outside_after.st_size) == (
        outside_before.st_ino,
        outside_before.st_mtime_ns,
        outside_before.st_size,
    )
    assert _protected_state(graph) == before


def test_plan_self_hash_and_result_plan_revision_mismatch(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "old")
    _publish(graph, "new")
    plan = snapshot_maintenance_plan(graph, 1)
    before = _protected_state(graph)

    tampered = json.loads(plan_to_json(plan))
    tampered["current"] = "19990101-000000-deadbeef"
    tampered["retention_plan"]["current"] = tampered["current"]
    tampered["staging_cleanup_plan"]["current"] = tampered["current"]
    tampered_file = _write_json(tmp_path / "tampered.json", tampered)
    self_hash = _run(
        "--graph", str(graph), "--plan-file", str(tampered_file), "--json"
    )
    assert self_hash.returncode == 2
    assert self_hash.stdout == ""
    assert "current" in self_hash.stderr or "maintenance_revision" in self_hash.stderr

    impossible = _forge_complete_result(plan)
    impossible["completed_components"] = []
    with pytest.raises(
        SnapshotMaintenanceReconcileError, match="complete success"
    ):
        snapshot_maintenance_reconcile(
            graph,
            _save_plan(tmp_path, plan, "impossible-plan.json"),
            _save_apply(tmp_path, impossible, "impossible-apply.json"),
        )

    other = tmp_path / "other"
    _publish(other, "a")
    _publish(other, "b")
    other_plan = snapshot_maintenance_plan(other, 1)
    other_apply = snapshot_maintenance_apply(
        other, 1, other_plan["maintenance_revision"], maintenance_confirmed=True
    )
    mismatch = _run(
        "--graph",
        str(graph),
        "--plan-file",
        str(_save_plan(tmp_path, plan)),
        "--apply-result-file",
        str(_save_apply(tmp_path, other_apply)),
        "--json",
    )
    assert mismatch.returncode == 1
    assert mismatch.stdout == ""
    assert "another plan" in mismatch.stderr
    with pytest.raises(
        SnapshotMaintenanceReconcileIntegrityError, match="another plan"
    ):
        snapshot_maintenance_reconcile(
            graph, _save_plan(tmp_path, plan, "p2.json"), _save_apply(tmp_path, other_apply, "a2.json")
        )
    assert _protected_state(graph) == before


def test_unsafe_candidate_symlink_and_non_directory(tmp_path: Path):
    graph = tmp_path / "g"
    old = _publish(graph, "old")
    _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-unsafe", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "keep").write_text("must-not-follow\n", encoding="utf-8")
    shutil.rmtree(old)
    old.symlink_to(outside)
    leftover_lock = leftover / STAGING_WRITER_LOCK_NAME
    leftover_lock.unlink()
    leftover.replace(tmp_path / "was-staging")
    leftover.write_bytes(b"not-a-directory")
    result = snapshot_maintenance_reconcile(graph, plan_file)
    _assert_observation_shape(result, plan, apply_supplied=False)
    published = {item["name"]: item for item in result["published_candidate_observations"]}
    staging = {item["name"]: item for item in result["staging_candidate_observations"]}
    assert published[old.name]["state"] == "unsafe_symlink_at_reconcile"
    assert published[old.name]["entry_kind"] == "symlink"
    assert staging[leftover.name]["state"] == "present_non_directory_at_reconcile"
    assert staging[leftover.name]["entry_kind"] == "file"
    assert (outside / "keep").read_text(encoding="utf-8") == "must-not-follow\n"
    assert old.is_symlink()


def test_concurrent_current_listing_candidate_and_lock_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_reconcile as reconcile_mod

    graph = tmp_path / "g"
    old = _publish(graph, "old")
    live = _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-race", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)

    def switch_current(_root, _scan):
        (graph / "current").write_text(old.name + "\n", encoding="utf-8")

    monkeypatch.setattr(reconcile_mod, "_after_first_reconcile_scan", switch_current)
    with pytest.raises(SnapshotMaintenanceReconcileIntegrityError, match="current|lock"):
        snapshot_maintenance_reconcile(graph, plan_file)
    (graph / "current").write_text(live.name + "\n", encoding="utf-8")

    extra = tmp_path / "extra"
    extra.mkdir()

    def add_published(_root, _scan):
        target = graph / "snapshots" / "19990101-000000-addedone"
        if not target.exists():
            shutil.copytree(live, target)

    monkeypatch.setattr(reconcile_mod, "_after_first_reconcile_scan", add_published)
    with pytest.raises(
        SnapshotMaintenanceReconcileIntegrityError, match="published|lock|current"
    ):
        snapshot_maintenance_reconcile(graph, plan_file)
    shutil.rmtree(graph / "snapshots" / "19990101-000000-addedone", ignore_errors=True)

    def replace_lock(_root, _scan):
        lock = graph / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-lock")

    monkeypatch.setattr(reconcile_mod, "_after_first_reconcile_scan", replace_lock)
    with pytest.raises(
        SnapshotMaintenanceReconcileIntegrityError, match="publication lock|lock|current"
    ):
        snapshot_maintenance_reconcile(graph, plan_file)

    def remove_candidate(_root, _scan):
        if leftover.exists():
            shutil.rmtree(leftover)

    monkeypatch.setattr(reconcile_mod, "_after_first_reconcile_scan", remove_candidate)
    changed = snapshot_maintenance_reconcile(graph, plan_file)
    staging = {item["name"]: item for item in changed["staging_candidate_observations"]}
    assert staging[leftover.name]["state"] == "changed_during_reconcile"
    assert changed["ok"] is True


def test_exactly_one_shared_lease_no_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod
    import graphrag_code.snapshot_maintenance_plan as plan_mod
    import graphrag_code.snapshot_maintenance_reconcile as reconcile_mod
    import graphrag_code.snapshot_prune as prune_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)
    calls = {"shared": 0}
    original = reconcile_mod.graph_read_lease

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 1
        with original(*args, **kwargs) as lease:
            yield lease

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or mutating public scope")

    monkeypatch.setattr(reconcile_mod, "graph_read_lease", counted)
    monkeypatch.setattr(reconcile_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "graph_read_lease", boom)
    monkeypatch.setattr(plan_mod, "snapshot_maintenance_plan", boom)
    monkeypatch.setattr(plan_mod, "_snapshot_maintenance_plan_scope", boom)
    monkeypatch.setattr(apply_mod, "snapshot_maintenance_apply", boom)
    monkeypatch.setattr(apply_mod, "_snapshot_maintenance_apply_scope", boom)
    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", boom)
    monkeypatch.setattr(prune_mod, "snapshot_prune", boom)
    monkeypatch.setattr(prune_mod, "_snapshot_prune_scope", boom)
    monkeypatch.setattr(cleanup_mod, "snapshot_staging_cleanup", boom)
    monkeypatch.setattr(cleanup_mod, "_snapshot_staging_cleanup_scope", boom)
    result = snapshot_maintenance_reconcile(graph, plan_file)
    assert result["ok"] is True
    assert calls["shared"] == 1


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "old")
    _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-relcwd", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(here, plan)
    args = ["--graph", "g", "--plan-file", plan_file.name, "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_maintenance_reconcile", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-maintenance-reconcile", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    assert leftover.name in [
        item["name"] for item in bodies[0]["staging_candidate_observations"]
    ]
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-maintenance-reconcile",
            "--graph",
            str(graph),
            "--plan-file",
            str(plan_file),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["input_plan_revision"] == plan["maintenance_revision"]

    sdist = built_wheel_and_sdist[1]
    import tarfile

    with tarfile.open(sdist, "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_maintenance_reconcile.py" in names


def test_cli_serializes_writes_and_flushes_under_shared_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_reconcile as reconcile_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    plan_file = _save_plan(tmp_path, plan)
    original_scope = reconcile_mod._snapshot_maintenance_reconcile_scope
    original_json = reconcile_mod.result_to_json
    original_format = reconcile_mod.format_result
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

    monkeypatch.setattr(
        reconcile_mod, "_snapshot_maintenance_reconcile_scope", tracked_scope
    )
    monkeypatch.setattr(reconcile_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(reconcile_mod, "format_result", guarded_format)
    monkeypatch.setattr(reconcile_mod.sys, "stdout", GuardedStdout())
    assert (
        reconcile_mod.main(
            ["--graph", str(graph), "--plan-file", str(plan_file), "--json"]
        )
        == 0
    )
    assert (
        reconcile_mod.main(["--graph", str(graph), "--plan-file", str(plan_file)]) == 0
    )
    assert state["active"] is False
    assert state["responses"] >= 2
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
    assert "graph_read_lease" in imported
    assert "probe_staging_writer_lease" in imported
    assert "maintenance_revision_of" in imported
    assert "snapshot_maintenance_plan" not in called
    assert "graph_exclusive_lease" not in source
    assert "publish_byog_snapshot" not in source
    assert "cleanup_old_snapshots" not in source
    assert "snapshot_maintenance_apply" not in source
    assert "snapshot_prune" not in source
    assert "from graphrag_code.snapshot_staging_cleanup import" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    for state in CANDIDATE_STATES:
        assert state in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_mcp_remains_exactly_fourteen(tmp_path: Path):
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 14
    assert "snapshot_maintenance_reconcile" not in TOOL_NAMES
    assert "snapshot_maintenance_apply" not in TOOL_NAMES
    graph = tmp_path / "g"
    _publish(graph, "a")
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 14
            assert "snapshot_maintenance_reconcile" not in names

    anyio_run(_body)
    fingerprints = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(fingerprints) == 15
