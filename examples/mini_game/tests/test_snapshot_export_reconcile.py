"""Read-only standalone snapshot export reconciliation.

Disposable tmp graphs and export dirs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_reconcile.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import publish_byog_snapshot  # type: ignore
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_apply import (  # type: ignore
    SnapshotExportApplyError,
    result_to_json as apply_to_json,
    snapshot_export_apply,
)
from graphrag_code.snapshot_export_plan import (  # type: ignore
    ACCEPTED_PAYLOAD_FILES,
    HASH_CHUNK_BYTES,
    export_revision_of,
    result_to_json as plan_to_json,
    snapshot_export_plan,
)
from graphrag_code.snapshot_export_reconcile import (  # type: ignore
    MAX_INPUT_BYTES,
    SnapshotExportReconcileError,
    SnapshotExportReconcileIntegrityError,
    format_result,
    result_to_json,
    snapshot_export_reconcile,
)

SCRIPT = ROOT / "scripts" / "snapshot_export_reconcile.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_reconcile.py"
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
        "snapshot_export_verify",
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
        "staging_writer_lease",
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
        "mkdir",
        "read_bytes",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")


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
    obs = [
        {
            "id": f"obs:{marker}",
            "source": f"demo:{marker}.py",
            "target": f"demo:{marker}",
            "kind": "call",
        }
    ]
    return ents, rels, tus, obs


def _publish(graph: Path, marker: str, *, observations: bool = False) -> Path:
    ents, rels, tus, obs = _rows(marker)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"export-reconcile: {marker}\n",
        keep_last=10,
        call_observations_df=pd.DataFrame(obs) if observations else None,
    )


def _copy_standalone(
    src: Path, dest: Path, *, names: set[str] | None = None
) -> Path:
    dest.mkdir(parents=True)
    for path in src.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        if path.name not in ACCEPTED_PAYLOAD_FILES:
            continue
        if names is not None and path.name not in names:
            continue
        shutil.copyfile(path, dest / path.name)
    return dest


def _save_plan(directory: Path, plan: dict, name: str = "plan.json") -> Path:
    path = directory / name
    path.write_text(plan_to_json(plan), encoding="utf-8")
    return path


def _save_apply(directory: Path, result: dict, name: str = "apply.json") -> Path:
    path = directory / name
    path.write_text(apply_to_json(result), encoding="utf-8")
    return path


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return path


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


def _assert_reconcile_shape(
    result: dict,
    plan: dict,
    dest: Path,
    *,
    apply_supplied: bool,
    declared_outcome: str,
    destination_state: str,
    destination_present: bool,
    destination_matches_plan: bool,
    resolved: str,
) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["input_plan_valid"] is True
    assert result["input_plan_revision"] == plan["export_revision"]
    assert result["apply_result_supplied"] is apply_supplied
    assert result["apply_result_valid"] is apply_supplied
    assert result["declared_apply_outcome"] == declared_outcome
    assert result["destination"] == str(dest.resolve())
    assert result["destination_state"] == destination_state
    assert result["destination_present"] is destination_present
    assert result["destination_matches_plan"] is destination_matches_plan
    assert result["resolved_snapshot"] == resolved
    assert result["file_count"] == len(result["files"])
    assert result["total_size_bytes"] == sum(
        item["size_bytes"] for item in result["files"]
    )
    assert result["export_mutated"] is False
    assert result["graph_inspected"] is False
    assert result["recovery_performed"] is False
    assert result["creation_cause_proven"] is False
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "reconciliation_is_observation_only",
        "absence_does_not_prove_apply_failed",
        "presence_does_not_prove_apply_created",
        "revision_equality_is_observation_window_only",
        "fresh_plan_required_before_export",
        "no_recovery_performed",
        "input_files_bounded",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert str(dest.resolve()) in text
    assert resolved in text
    assert plan["export_revision"] in text
    assert f"destination_state={destination_state}" in text
    assert f"destination_matches_plan={str(destination_matches_plan).lower()}" in text
    assert "observation-only" in text
    assert "not authorization to delete" in text
    assert "backup" not in text.lower()
    assert "recoverable" not in text.lower()
    assert "authentic" not in text.lower()


def test_complete_apply_result_and_matching_destination(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "live", observations=True)
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "applied"
    applied = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    plan_file = _save_plan(tmp_path, plan)
    apply_file = _save_apply(tmp_path, applied)
    before = dest.stat()
    result = snapshot_export_reconcile(plan_file, dest, apply_file)
    _assert_reconcile_shape(
        result,
        plan,
        dest,
        apply_supplied=True,
        declared_outcome="complete",
        destination_state="matches_plan",
        destination_present=True,
        destination_matches_plan=True,
        resolved=live.name,
    )
    assert result["observed_export_revision"] == plan["export_revision"]
    assert result["files"] == plan["files"]
    assert result["files"] == applied["files"]
    assert dest.stat().st_mtime_ns == before.st_mtime_ns
    assert dest.stat().st_ino == before.st_ino


def test_partial_apply_result_and_matching_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    live = _publish(graph, "partial")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "partial-out"
    real = apply_mod._fsync_directory
    published = {"done": False}

    def mark_published(_parent_fd, _dest_name):
        published["done"] = True

    def fail_parent(fd):
        if published["done"]:
            raise SnapshotExportApplyError(
                "fsync export directory failed: injected parent"
            )
        return real(fd)

    monkeypatch.setattr(apply_mod, "_after_export_apply_published", mark_published)
    monkeypatch.setattr(apply_mod, "_fsync_directory", fail_parent)
    applied = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert applied["ok"] is False
    assert applied["partial"] is True
    result = snapshot_export_reconcile(
        _save_plan(tmp_path, plan),
        dest,
        _save_apply(tmp_path, applied),
    )
    _assert_reconcile_shape(
        result,
        plan,
        dest,
        apply_supplied=True,
        declared_outcome="partial",
        destination_state="matches_plan",
        destination_present=True,
        destination_matches_plan=True,
        resolved=live.name,
    )
    assert dest.is_dir()


def test_no_apply_result_and_absent_destination(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "only")
    plan = snapshot_export_plan(graph, live.name)
    dest = tmp_path / "missing-export"
    result = snapshot_export_reconcile(_save_plan(tmp_path, plan), dest)
    _assert_reconcile_shape(
        result,
        plan,
        dest,
        apply_supplied=False,
        declared_outcome="not_supplied",
        destination_state="absent",
        destination_present=False,
        destination_matches_plan=False,
        resolved=live.name,
    )
    assert result["observed_export_revision"] is None
    assert result["files"] == []
    assert result["file_count"] == 0
    assert result["total_size_bytes"] == 0
    assert not dest.exists()
    proc = _run(
        "--plan-file",
        str(_save_plan(tmp_path, plan, "absent-plan.json")),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["destination_state"] == "absent"
    assert body["ok"] is True
    assert result_to_json(body) == proc.stdout


def test_stable_valid_revision_mismatch(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "first")
    second = _publish(graph, "second")
    plan = snapshot_export_plan(graph, first.name)
    dest = _copy_standalone(second, tmp_path / "other")
    result = snapshot_export_reconcile(_save_plan(tmp_path, plan), dest)
    _assert_reconcile_shape(
        result,
        plan,
        dest,
        apply_supplied=False,
        declared_outcome="not_supplied",
        destination_state="revision_mismatch",
        destination_present=True,
        destination_matches_plan=False,
        resolved=second.name,
    )
    assert result["observed_export_revision"] != plan["export_revision"]
    assert result["ok"] is True
    proc = _run(
        "--plan-file",
        str(_save_plan(tmp_path, plan, "mismatch-plan.json")),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["destination_matches_plan"] is False
    assert body["ok"] is True


def test_tampered_plan_revision_files_counts_and_order(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    dest = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)

    revision = json.loads(plan_to_json(plan))
    revision["export_revision"] = "sha256:" + ("ab" * 32)
    proc = _run(
        "--plan-file",
        str(_write_json(tmp_path / "rev.json", revision)),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "export_revision" in proc.stderr

    files = json.loads(plan_to_json(plan))
    files["files"] = list(reversed(files["files"]))
    proc = _run(
        "--plan-file",
        str(_write_json(tmp_path / "order.json", files)),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    counts = json.loads(plan_to_json(plan))
    counts["file_count"] = counts["file_count"] + 1
    proc = _run(
        "--plan-file",
        str(_write_json(tmp_path / "count.json", counts)),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    sizes = json.loads(plan_to_json(plan))
    sizes["total_size_bytes"] = sizes["total_size_bytes"] + 1
    proc = _run(
        "--plan-file",
        str(_write_json(tmp_path / "size.json", sizes)),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    mutated = json.loads(plan_to_json(plan))
    mutated["files"][0]["content_revision"] = "sha256:" + ("cd" * 32)
    proc = _run(
        "--plan-file",
        str(_write_json(tmp_path / "hash.json", mutated)),
        "--destination",
        str(dest),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""


def test_impossible_apply_result_flag_combinations(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    dest = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    applied = snapshot_export_apply(
        graph,
        snap.name,
        tmp_path / "applied",
        plan["export_revision"],
        export_confirmed=True,
    )
    plan_file = _save_plan(tmp_path, plan)
    cases = [
        {"ok": True, "partial": True},
        {"ok": False, "partial": False},
        {"export_confirmed": False},
        {"export_performed": False},
        {"destination_created": False},
        {"source_unchanged": False},
        {"parent_fsync_confirmed": False},
        {"error": "unexpected"},
        {"ok": False, "partial": True, "error": None, "destination_verified": False},
        {
            "ok": False,
            "partial": True,
            "error": "post-publication",
            "destination_verified": True,
            "parent_fsync_confirmed": True,
        },
    ]
    for index, updates in enumerate(cases):
        forged = json.loads(apply_to_json(applied))
        forged.update(updates)
        proc = _run(
            "--plan-file",
            str(plan_file),
            "--destination",
            str(dest),
            "--apply-result-file",
            str(_write_json(tmp_path / f"impossible-{index}.json", forged)),
            "--json",
        )
        assert proc.returncode == 2, updates
        assert proc.stdout == ""


def test_apply_result_from_another_plan_or_destination(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "first")
    second = _publish(graph, "second")
    plan = snapshot_export_plan(graph, first.name)
    dest = tmp_path / "first-out"
    applied = snapshot_export_apply(
        graph,
        first.name,
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    other_dest = tmp_path / "second-out"
    other_plan = snapshot_export_plan(graph, second.name)
    other_applied = snapshot_export_apply(
        graph,
        second.name,
        other_dest,
        other_plan["export_revision"],
        export_confirmed=True,
    )
    other_plan_proc = _run(
        "--plan-file",
        str(_save_plan(tmp_path, plan, "p1.json")),
        "--destination",
        str(dest),
        "--apply-result-file",
        str(_save_apply(tmp_path, other_applied, "other-apply.json")),
        "--json",
    )
    assert other_plan_proc.returncode == 1
    assert other_plan_proc.stdout == ""
    assert "another plan" in other_plan_proc.stderr or "another destination" in (
        other_plan_proc.stderr
    )
    other_dest_proc = _run(
        "--plan-file",
        str(_save_plan(tmp_path, plan, "p2.json")),
        "--destination",
        str(other_dest),
        "--apply-result-file",
        str(_save_apply(tmp_path, applied, "first-apply.json")),
        "--json",
    )
    assert other_dest_proc.returncode == 1
    assert other_dest_proc.stdout == ""
    assert "another" in other_dest_proc.stderr
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="another plan|another destination"
    ):
        snapshot_export_reconcile(
            _save_plan(tmp_path, plan, "p3.json"),
            dest,
            _save_apply(tmp_path, other_applied, "a3.json"),
        )


def test_oversized_symlinked_nonregular_replaced_and_malformed_inputs(
    tmp_path: Path,
):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    dest = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    plan_file = _save_plan(tmp_path, plan)

    missing = _run("--destination", str(dest), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")
    bad = _run(
        "--plan-file",
        str(malformed),
        "--destination",
        str(dest),
        "--json",
    )
    assert bad.returncode == 2
    assert bad.stdout == ""

    oversized = tmp_path / "huge.json"
    oversized.write_bytes(b"{" + b"a" * (MAX_INPUT_BYTES + 1) + b"}")
    huge = _run(
        "--plan-file",
        str(oversized),
        "--destination",
        str(dest),
        "--json",
    )
    assert huge.returncode == 2
    assert huge.stdout == ""
    assert str(MAX_INPUT_BYTES) in huge.stderr

    linked = tmp_path / "link.json"
    linked.symlink_to(plan_file)
    symlink = _run(
        "--plan-file",
        str(linked),
        "--destination",
        str(dest),
        "--json",
    )
    assert symlink.returncode == 2
    assert symlink.stdout == ""
    assert "symlink" in symlink.stderr

    directory_input = tmp_path / "dir-input"
    directory_input.mkdir()
    not_file = _run(
        "--plan-file",
        str(directory_input),
        "--destination",
        str(dest),
        "--json",
    )
    assert not_file.returncode == 2
    assert not_file.stdout == ""

    apply_linked = tmp_path / "apply-link.json"
    apply_linked.symlink_to(plan_file)
    apply_symlink = _run(
        "--plan-file",
        str(plan_file),
        "--destination",
        str(dest),
        "--apply-result-file",
        str(apply_linked),
        "--json",
    )
    assert apply_symlink.returncode == 2
    assert apply_symlink.stdout == ""

    import graphrag_code.snapshot_export_reconcile as reconcile_mod

    victim = tmp_path / "replaced.json"
    shutil.copyfile(plan_file, victim)

    def replace_after_lstat(path: Path) -> None:
        if path == victim:
            path.unlink()
            path.write_text('{"schema_version": 1}\n', encoding="utf-8")

    original = reconcile_mod._after_input_path_lstat
    reconcile_mod._after_input_path_lstat = replace_after_lstat
    try:
        with pytest.raises(SnapshotExportReconcileError, match="changed|unsafe"):
            snapshot_export_reconcile(victim, dest)
        proc = _run(
            "--plan-file",
            str(victim),
            "--destination",
            str(dest),
            "--json",
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
    finally:
        reconcile_mod._after_input_path_lstat = original


def test_invalid_inputs_rejected_before_destination_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_reconcile as reconcile_mod

    malformed_plan = tmp_path / "malformed-plan.json"
    malformed_plan.write_text("{not-json", encoding="utf-8")

    def destination_inspection_is_forbidden(*_args, **_kwargs):
        raise AssertionError("destination was inspected before input validation")

    monkeypatch.setattr(
        reconcile_mod, "_destination_parts", destination_inspection_is_forbidden
    )
    monkeypatch.setattr(
        reconcile_mod, "_open_destination_parent", destination_inspection_is_forbidden
    )
    with pytest.raises(SnapshotExportReconcileError, match="valid JSON"):
        snapshot_export_reconcile(malformed_plan, tmp_path / "missing")

    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    dest = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    malformed_apply = tmp_path / "malformed-apply.json"
    malformed_apply.write_text("[]", encoding="utf-8")
    with pytest.raises(SnapshotExportReconcileError, match="JSON object"):
        snapshot_export_reconcile(_save_plan(tmp_path, plan), dest, malformed_apply)


def test_destination_symlink_nondirectory_and_invalid_envelope(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    export_dir = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    plan_file = _save_plan(tmp_path, plan)

    linked = tmp_path / "linked"
    linked.symlink_to(export_dir, target_is_directory=True)
    with pytest.raises(SnapshotExportReconcileIntegrityError, match="symlink"):
        snapshot_export_reconcile(plan_file, linked)
    linked_proc = _run(
        "--plan-file",
        str(plan_file),
        "--destination",
        str(linked),
        "--json",
    )
    assert linked_proc.returncode == 1
    assert linked_proc.stdout == ""

    file_dest = tmp_path / "file-dest"
    file_dest.write_text("not-a-directory\n", encoding="utf-8")
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="not a real directory"
    ):
        snapshot_export_reconcile(plan_file, file_dest)

    missing = _copy_standalone(snap, tmp_path / "missing")
    (missing / "entities.parquet").unlink()
    with pytest.raises(SnapshotExportReconcileIntegrityError, match="missing"):
        snapshot_export_reconcile(plan_file, missing)
    missing_proc = _run(
        "--plan-file",
        str(plan_file),
        "--destination",
        str(missing),
        "--json",
    )
    assert missing_proc.returncode == 1
    assert missing_proc.stdout == ""

    unexpected = _copy_standalone(snap, tmp_path / "unexpected")
    (unexpected / "notes.txt").write_text("nope\n", encoding="utf-8")
    with pytest.raises(SnapshotExportReconcileIntegrityError, match="unexpected"):
        snapshot_export_reconcile(plan_file, unexpected)


def test_payload_symlink_never_reads_outside_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    export_dir = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"must-not-follow")
    payload = export_dir / "entities.parquet"
    payload.unlink()
    payload.symlink_to(outside)
    outside_ino = outside.stat().st_ino
    original_read = plan_mod.os.read

    def reject_outside(fd, count):
        assert os.fstat(fd).st_ino != outside_ino
        return original_read(fd, count)

    monkeypatch.setattr(plan_mod.os, "read", reject_outside)
    with pytest.raises(SnapshotExportReconcileIntegrityError, match="symlink"):
        snapshot_export_reconcile(_save_plan(tmp_path, plan), export_dir)
    assert outside.read_bytes() == b"must-not-follow"


def test_destination_replacement_before_and_after_anchor_never_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_export_reconcile as reconcile_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "anchored")
    plan = snapshot_export_plan(graph, snap.name)
    plan_file = _save_plan(tmp_path, plan)
    original_read = plan_mod.os.read

    preanchor = _copy_standalone(snap, tmp_path / "preanchor")
    pre_replacement = _copy_standalone(snap, tmp_path / "pre-replacement")
    (pre_replacement / "entities.parquet").write_bytes(b"pre-replacement-bytes")
    pre_replacement_inodes = {
        path.stat().st_ino for path in pre_replacement.iterdir() if path.is_file()
    }
    pre_hidden = tmp_path / "pre-hidden-original"

    def replace_before_anchor(path):
        if path == preanchor:
            preanchor.rename(pre_hidden)
            preanchor.symlink_to(pre_replacement, target_is_directory=True)

    def reject_pre_replacement(fd, count):
        assert os.fstat(fd).st_ino not in pre_replacement_inodes
        return original_read(fd, count)

    monkeypatch.setattr(
        reconcile_mod, "_after_destination_path_inspected", replace_before_anchor
    )
    monkeypatch.setattr(plan_mod.os, "read", reject_pre_replacement)
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="changed|symlink|unsafe"
    ):
        snapshot_export_reconcile(plan_file, preanchor)
    assert (pre_replacement / "entities.parquet").read_bytes() == b"pre-replacement-bytes"

    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    dest = _copy_standalone(snap, parent_dir / "export")
    parent_replacement = tmp_path / "parent-replacement"
    parent_replacement.mkdir()
    _copy_standalone(snap, parent_replacement / "export")
    (parent_replacement / "export" / "entities.parquet").write_bytes(b"parent-target")
    parent_target_inodes = {
        path.stat().st_ino
        for path in (parent_replacement / "export").iterdir()
        if path.is_file()
    }
    hidden_parent = tmp_path / "hidden-parent"

    def replace_parent(_path):
        parent_dir.rename(hidden_parent)
        parent_dir.symlink_to(parent_replacement, target_is_directory=True)

    def reject_parent_target(fd, count):
        assert os.fstat(fd).st_ino not in parent_target_inodes
        return original_read(fd, count)

    monkeypatch.setattr(reconcile_mod, "_after_destination_path_inspected", replace_parent)
    monkeypatch.setattr(plan_mod.os, "read", reject_parent_target)
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="changed|symlink|unsafe"
    ):
        snapshot_export_reconcile(plan_file, dest)
    assert (
        parent_replacement / "export" / "entities.parquet"
    ).read_bytes() == b"parent-target"

    monkeypatch.setattr(reconcile_mod, "_after_destination_path_inspected", lambda *_: None)
    export_dir = _copy_standalone(snap, tmp_path / "export")
    replacement = _copy_standalone(snap, tmp_path / "replacement")
    (replacement / "entities.parquet").write_bytes(b"replacement-bytes")
    replacement_inodes = {
        path.stat().st_ino for path in replacement.iterdir() if path.is_file()
    }
    hidden = tmp_path / "hidden-original"

    def replace_after_parent(_parent, _parent_fd, destination):
        if destination == export_dir.resolve():
            export_dir.rename(hidden)
            export_dir.symlink_to(replacement, target_is_directory=True)

    def reject_replacement(fd, count):
        assert os.fstat(fd).st_ino not in replacement_inodes
        return original_read(fd, count)

    monkeypatch.setattr(
        reconcile_mod, "_after_destination_parent_opened", replace_after_parent
    )
    monkeypatch.setattr(plan_mod.os, "read", reject_replacement)
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="changed|symlink|unsafe|replaced"
    ):
        snapshot_export_reconcile(plan_file, export_dir)
    assert (replacement / "entities.parquet").read_bytes() == b"replacement-bytes"

    monkeypatch.setattr(reconcile_mod, "_after_destination_parent_opened", lambda *_: None)
    post = _copy_standalone(snap, tmp_path / "post")
    post_replacement = _copy_standalone(snap, tmp_path / "post-replacement")
    (post_replacement / "entities.parquet").write_bytes(b"post-replacement-bytes")
    post_inodes = {
        path.stat().st_ino for path in post_replacement.iterdir() if path.is_file()
    }
    post_hidden = tmp_path / "post-hidden"

    def replace_after_open(_destination, _records):
        post.rename(post_hidden)
        post.symlink_to(post_replacement, target_is_directory=True)

    def reject_post(fd, count):
        assert os.fstat(fd).st_ino not in post_inodes
        return original_read(fd, count)

    monkeypatch.setattr(reconcile_mod, "_after_first_observation", replace_after_open)
    monkeypatch.setattr(plan_mod.os, "read", reject_post)
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="replaced|changed|symlink"
    ):
        snapshot_export_reconcile(plan_file, post)
    assert (
        post_replacement / "entities.parquet"
    ).read_bytes() == b"post-replacement-bytes"

    monkeypatch.setattr(reconcile_mod, "_after_first_observation", lambda *_: None)
    bounced = _copy_standalone(snap, tmp_path / "bounced")
    bounced_hidden = tmp_path / "bounced-hidden"
    parent_before = bounced.parent.stat()

    def rename_away_and_back(_destination, _entries):
        bounced.rename(bounced_hidden)
        bounced_hidden.rename(bounced)
        parent_after = bounced.parent.stat()
        if parent_after.st_mtime_ns == parent_before.st_mtime_ns:
            os.utime(
                bounced.parent,
                ns=(parent_after.st_atime_ns, parent_after.st_mtime_ns + 1),
            )

    monkeypatch.setattr(
        reconcile_mod, "_after_first_observation", rename_away_and_back
    )
    with pytest.raises(
        SnapshotExportReconcileIntegrityError, match="parent changed"
    ):
        snapshot_export_reconcile(plan_file, bounced)


def test_same_size_payload_replacement_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_reconcile as reconcile_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "stable")
    export_dir = _copy_standalone(snap, tmp_path / "same-size")
    plan = snapshot_export_plan(graph, snap.name)
    target = export_dir / "entities.parquet"
    original = target.read_bytes()
    original_stat = target.stat()

    def rewrite_same_size(_destination, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        target.write_bytes(replacement)
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(reconcile_mod, "_after_first_observation", rewrite_same_size)
    with pytest.raises(SnapshotExportReconcileIntegrityError, match="changed"):
        snapshot_export_reconcile(_save_plan(tmp_path, plan), export_dir)


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    snap = _publish(graph, "only")
    dest = _copy_standalone(snap, here / "export")
    plan = snapshot_export_plan(graph, snap.name)
    plan_file = _save_plan(here, plan, "plan.json")
    args = [
        "--plan-file",
        "plan.json",
        "--destination",
        "export",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_export_reconcile", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-reconcile", *args],
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
    assert bodies[0]["destination"] == str(dest.resolve())
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-reconcile",
            "--plan-file",
            str(plan_file),
            "--destination",
            str(dest),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["observed_export_revision"] == plan["export_revision"]

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_export_reconcile.py" in names


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_reconcile as reconcile_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "held")
    dest = _copy_standalone(snap, tmp_path / "held")
    plan = snapshot_export_plan(graph, snap.name)
    original_json = reconcile_mod.result_to_json
    original_format = reconcile_mod.format_result
    state = {"parent_fd": None, "dest_fd": None, "responses": 0, "flushes": 0}

    def capture_ready(_destination, parent_fd, directory_fd, _result):
        state["parent_fd"] = parent_fd
        state["dest_fd"] = directory_fd
        os.fstat(parent_fd)
        assert directory_fd is not None
        os.fstat(directory_fd)

    def guarded_json(*args, **kwargs):
        os.fstat(state["parent_fd"])
        os.fstat(state["dest_fd"])
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        os.fstat(state["parent_fd"])
        os.fstat(state["dest_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            os.fstat(state["parent_fd"])
            os.fstat(state["dest_fd"])
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            os.fstat(state["parent_fd"])
            os.fstat(state["dest_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(reconcile_mod, "_after_result_ready", capture_ready)
    monkeypatch.setattr(reconcile_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(reconcile_mod, "format_result", guarded_format)
    monkeypatch.setattr(reconcile_mod.sys, "stdout", GuardedStdout())
    plan_file = _save_plan(tmp_path, plan)
    assert (
        reconcile_mod.main(
            ["--plan-file", str(plan_file), "--destination", str(dest), "--json"]
        )
        == 0
    )
    assert (
        reconcile_mod.main(["--plan-file", str(plan_file), "--destination", str(dest)])
        == 0
    )
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
    assert "export_revision_of" in imported
    assert "_payload_children" in imported
    assert "_stream_regular_file" in imported
    assert "_require_descriptor_reads" in imported
    assert "graph_read_lease" not in source
    assert "graph_exclusive_lease" not in source
    assert "snapshot_export_plan(" not in source
    assert "snapshot_export_apply(" not in source
    assert "snapshot_export_verify(" not in source
    assert "publish_byog_snapshot" not in source
    assert "read_bytes" not in source
    assert HASH_CHUNK_BYTES <= 64 * 1024
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    human = format_result(
        {
            "destination": "/tmp/x",
            "destination_state": "absent",
            "resolved_snapshot": "id",
            "input_plan_revision": "sha256:" + ("00" * 32),
            "observed_export_revision": None,
            "destination_matches_plan": False,
            "file_count": 0,
            "total_size_bytes": 0,
            "declared_apply_outcome": "not_supplied",
            "ok": True,
        }
    )
    assert "observation-only" in human
    assert "recoverable" not in human.lower()
    assert "authentic" not in human.lower()
    assert "backup" not in human.lower()


def test_mcp_remains_exactly_eleven_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 11
    assert "snapshot_export_reconcile" not in TOOL_NAMES
    assert "snapshot_export_verify" not in TOOL_NAMES
    assert "snapshot_export_plan" not in TOOL_NAMES
    assert "snapshot_export_apply" not in TOOL_NAMES
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
            assert "snapshot_export_reconcile" not in names

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
        if Path(dirpath).name.startswith(".graphrag-export-"):
            leftovers.append(Path(dirpath))
        leftovers.extend(
            Path(dirpath) / name
            for name in dirnames
            if name.startswith(".graphrag-export-")
        )
    assert leftovers == []


def test_deterministic_json(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "order")
    dest = _copy_standalone(snap, tmp_path / "order")
    plan = snapshot_export_plan(graph, snap.name)
    plan_file = _save_plan(tmp_path, plan)
    first = snapshot_export_reconcile(plan_file, dest)
    second = snapshot_export_reconcile(plan_file, dest)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    names = [item["path"] for item in first["files"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    assert first["observed_export_revision"] == export_revision_of(
        {
            "schema_version": 1,
            "resolved_snapshot": first["resolved_snapshot"],
            "files": first["files"],
        }
    )
