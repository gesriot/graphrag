"""Read-only snapshot transfer reconciliation.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_transfer_reconcile.py -q
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
import threading
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
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_plan import (  # type: ignore
    HASH_CHUNK_BYTES,
    export_revision_of,
)
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore
from graphrag_code.snapshot_transfer_apply import (  # type: ignore
    SnapshotTransferApplyError,
    result_to_json as apply_to_json,
    snapshot_transfer_apply,
)
from graphrag_code.snapshot_transfer_plan import (  # type: ignore
    ordered_graph_lease_pair,
    result_to_json as plan_to_json,
    snapshot_transfer_plan,
    transfer_revision_of,
)
from graphrag_code.snapshot_transfer_reconcile import (  # type: ignore
    MAX_INPUT_BYTES,
    SnapshotTransferReconcileError,
    SnapshotTransferReconcileIntegrityError,
    format_result,
    result_to_json,
    snapshot_transfer_reconcile,
)

SCRIPT = ROOT / "scripts" / "snapshot_transfer_reconcile.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_transfer_reconcile.py"
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
        "snapshot_transfer_apply",
        "snapshot_import_apply",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
REQUIRED_RESULT_KEYS = (
    "schema_version",
    "ok",
    "source_graph",
    "target_graph",
    "plan_file",
    "apply_result_file",
    "input_transfer_revision",
    "source_export_revision",
    "snapshot_id",
    "planned_files",
    "file_count",
    "total_size_bytes",
    "apply_result_supplied",
    "apply_result_valid",
    "declared_apply_outcome",
    "source_current",
    "source_current_matches_plan",
    "source_published_snapshots",
    "source_published_count",
    "source_history_matches_plan",
    "source_snapshot_present",
    "source_snapshot_state",
    "observed_source_export_revision",
    "source_snapshot_matches_plan",
    "target_current",
    "target_current_matches_plan",
    "target_published_snapshots",
    "target_published_count",
    "target_history_matches_plan_plus_snapshot",
    "target_snapshot_present",
    "target_snapshot_state",
    "observed_target_export_revision",
    "target_snapshot_matches_plan",
    "target_staging_name",
    "target_staging_present",
    "transfer_cause_proven",
    "staging_cause_proven",
    "recovery_performed",
    "source_graph_mutated",
    "target_graph_mutated",
    "activation_performed",
    "retention_performed",
    "fresh_plan_required_before_transfer",
    "notices",
)
NOTICE_CODES = (
    "reconciliation_is_observation_only",
    "source_absence_does_not_prove_apply_modified_source",
    "target_absence_does_not_prove_apply_failed",
    "target_presence_does_not_prove_apply_created",
    "revision_equality_is_observation_window_only",
    "staging_presence_does_not_prove_apply_left_it",
    "staging_absence_does_not_prove_apply_cleaned_it",
    "saved_apply_result_is_declaration_only",
    "current_equality_is_not_activation_history",
    "no_recovery_performed",
    "fresh_plan_required_before_transfer",
    "not_backup_authenticity_or_provenance",
    "advisory_locks_cooperating_only",
    "input_files_bounded",
    "cli_only_not_mcp",
)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _rows(marker: str, *, observations: bool = False):
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
    return ents, rels, tus, obs if observations else None


def _publish(graph: Path, marker: str, *, observations: bool = False) -> Path:
    ents, rels, tus, obs = _rows(marker, observations=observations)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"transfer-reconcile: {marker}\n",
        keep_last=10,
        call_observations_df=pd.DataFrame(obs) if obs is not None else None,
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _payload_paths(root: Path) -> list[Path]:
    paths = [path for path in root.iterdir()]
    snaps = root / "snapshots"
    if snaps.is_dir() and not snaps.is_symlink():
        paths.extend(sorted(snaps.rglob("*")))
    return paths


def _payload_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in _payload_paths(root):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _payload_stats(root: Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for path in _payload_paths(root):
        if path.is_file() and not path.is_symlink():
            info = path.lstat()
            out[path.relative_to(root).as_posix()] = (
                info.st_size,
                info.st_mtime_ns,
                stat.S_IMODE(info.st_mode),
            )
    return out


def _protected_state(graph: Path) -> dict[str, object]:
    registry = graph / OPERATOR_PINS_NAME
    lock = graph / PUBLICATION_LOCK_NAME
    return {
        "hashes": _payload_hashes(graph),
        "stats": _payload_stats(graph),
        "current": _current(graph),
        "listing": tuple(sorted(path.name for path in (graph / "snapshots").iterdir())),
        "registry_exists": registry.exists(),
        "registry": registry.read_bytes() if registry.is_file() else None,
        "lock": lock.read_bytes() if lock.is_file() else None,
        "lock_stat": (
            (
                lock.lstat().st_ino,
                lock.lstat().st_dev,
                lock.lstat().st_size,
                lock.lstat().st_mtime_ns,
            )
            if lock.exists()
            else None
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


def _prepare(tmp_path: Path, *, observations: bool = False):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "src", observations=observations)
    dest_live = _publish(target, "dst")
    plan = snapshot_transfer_plan(source, "current", target)
    return source, target, live, dest_live, plan


def _rewrite_same_size(path: Path) -> None:
    data = path.read_bytes()
    info = path.stat()
    replacement = bytes([data[0] ^ 1]) + data[1:]
    path.write_bytes(replacement)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))


def _assert_reconcile_shape(
    result: dict,
    plan: dict,
    source: Path,
    target: Path,
    plan_file: Path,
    *,
    apply_supplied: bool,
    declared_outcome: str,
    source_state: str,
    target_state: str,
    source_matches: bool,
    target_matches: bool,
    apply_file: Path | None = None,
) -> None:
    for key in REQUIRED_RESULT_KEYS:
        assert key in result
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["source_graph"] == str(source.resolve())
    assert result["target_graph"] == str(target.resolve())
    assert result["plan_file"] == str(plan_file)
    assert result["apply_result_file"] == (
        None if apply_file is None else str(apply_file)
    )
    assert result["input_transfer_revision"] == plan["transfer_revision"]
    assert result["source_export_revision"] == plan["source_export_revision"]
    assert result["snapshot_id"] == plan["snapshot_id"]
    assert result["planned_files"] == plan["files"]
    assert result["file_count"] == plan["file_count"]
    assert result["total_size_bytes"] == plan["total_size_bytes"]
    assert result["apply_result_supplied"] is apply_supplied
    assert result["apply_result_valid"] is apply_supplied
    assert result["declared_apply_outcome"] == declared_outcome
    assert result["source_snapshot_state"] == source_state
    assert result["target_snapshot_state"] == target_state
    assert result["source_snapshot_matches_plan"] is source_matches
    assert result["target_snapshot_matches_plan"] is target_matches
    assert result["source_snapshot_present"] is (source_state != "absent")
    assert result["target_snapshot_present"] is (target_state != "absent")
    assert result["transfer_cause_proven"] is False
    assert result["staging_cause_proven"] is False
    assert result["recovery_performed"] is False
    assert result["source_graph_mutated"] is False
    assert result["target_graph_mutated"] is False
    assert result["activation_performed"] is False
    assert result["retention_performed"] is False
    assert result["fresh_plan_required_before_transfer"] is True
    assert result["source_published_snapshots"] == sorted(
        result["source_published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["target_published_snapshots"] == sorted(
        result["target_published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["source_published_count"] == len(result["source_published_snapshots"])
    assert result["target_published_count"] == len(result["target_published_snapshots"])
    assert result["source_current"] in result["source_published_snapshots"]
    assert result["target_current"] in result["target_published_snapshots"]
    assert result["source_current_matches_plan"] is (
        result["source_current"] == plan["source_current"]
    )
    assert result["target_current_matches_plan"] is (
        result["target_current"] == plan["target_current"]
    )
    assert result["target_staging_name"] == plan["target_staging_name"]
    if source_state == "absent":
        assert result["observed_source_export_revision"] is None
        assert result["source_snapshot_matches_plan"] is False
    elif source_state == "matches_plan":
        assert result["observed_source_export_revision"] == plan["source_export_revision"]
        assert result["source_snapshot_matches_plan"] is True
    else:
        assert result["observed_source_export_revision"] != plan["source_export_revision"]
        assert result["observed_source_export_revision"] is not None
        assert result["source_snapshot_matches_plan"] is False
    if target_state == "absent":
        assert result["observed_target_export_revision"] is None
        assert result["target_snapshot_matches_plan"] is False
    elif target_state == "matches_plan":
        assert result["observed_target_export_revision"] == plan["source_export_revision"]
        assert result["target_snapshot_matches_plan"] is True
    else:
        assert result["observed_target_export_revision"] != plan["source_export_revision"]
        assert result["observed_target_export_revision"] is not None
        assert result["target_snapshot_matches_plan"] is False
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == list(NOTICE_CODES)
    text = format_result(result)
    assert str(source.resolve()) in text
    assert str(target.resolve()) in text
    assert plan["snapshot_id"] in text
    assert f"source_snapshot_state={source_state}" in text
    assert f"target_snapshot_state={target_state}" in text
    assert "observation-only" in text
    assert "not authorization" in text
    assert "backup" not in text.lower()
    assert "recoverable" not in text.lower()
    assert "authentic" not in text.lower()


def test_three_cli_surfaces_and_installed_packaging(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    source = here / "source"
    target = here / "target"
    live = _publish(source, "src")
    _publish(target, "dst")
    plan = snapshot_transfer_plan(source, "current", target)
    plan_file = _save_plan(here, plan, "plan.json")
    args = [
        "--source-graph",
        "source",
        "--target-graph",
        "target",
        "--plan-file",
        "plan.json",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_transfer_reconcile", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-transfer-reconcile", *args],
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
    assert bodies[0]["snapshot_id"] == live.name
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-transfer-reconcile",
            "--source-graph",
            str(source),
            "--target-graph",
            str(target),
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
    assert json.loads(installed.stdout)["input_transfer_revision"] == plan[
        "transfer_revision"
    ]

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_transfer_reconcile.py" in names
    help_proc = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "snapshot-transfer-reconcile" in help_proc.stdout


def test_complete_success_and_plan_only_reconciliation(tmp_path: Path):
    source, target, live, dest_live, plan = _prepare(tmp_path, observations=True)
    plan_file = _save_plan(tmp_path, plan)
    before_source = _protected_state(source)
    before_target = _protected_state(target)
    plan_only = snapshot_transfer_reconcile(source, target, plan_file)
    _assert_reconcile_shape(
        plan_only,
        plan,
        source,
        target,
        plan_file,
        apply_supplied=False,
        declared_outcome="not_supplied",
        source_state="matches_plan",
        target_state="absent",
        source_matches=True,
        target_matches=False,
    )
    assert plan_only["source_current"] == live.name
    assert plan_only["target_current"] == dest_live.name
    assert plan_only["source_history_matches_plan"] is True
    assert plan_only["target_history_matches_plan_plus_snapshot"] is False
    assert plan_only["target_staging_present"] is False
    assert _protected_state(source) == before_source
    assert _protected_state(target) == before_target

    applied = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    assert applied["ok"] is True
    apply_file = _save_apply(tmp_path, applied)
    before_source = _protected_state(source)
    before_target = _protected_state(target)
    result = snapshot_transfer_reconcile(source, target, plan_file, apply_file)
    _assert_reconcile_shape(
        result,
        plan,
        source,
        target,
        plan_file,
        apply_supplied=True,
        declared_outcome="complete",
        source_state="matches_plan",
        target_state="matches_plan",
        source_matches=True,
        target_matches=True,
        apply_file=apply_file,
    )
    assert result["target_history_matches_plan_plus_snapshot"] is True
    assert live.name in result["target_published_snapshots"]
    assert result["target_staging_present"] is False
    assert result["source_current_matches_plan"] is True
    assert result["target_current_matches_plan"] is True
    assert _protected_state(source) == before_source
    assert _protected_state(target) == before_target


def test_pre_and_post_publication_partial_saved_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_apply as apply_mod

    source, target, live, _dest, plan = _prepare(tmp_path)

    def boom_verify(*_args, **_kwargs):
        raise apply_mod.SnapshotTransferApplyIntegrityError("injected staged verify")

    original_verify = apply_mod._verify_staged_envelope
    monkeypatch.setattr(apply_mod, "_verify_staged_envelope", boom_verify)
    pre = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    assert pre["ok"] is False
    assert pre["partial"] is True
    assert pre["publication_performed"] is False
    plan_file = _save_plan(tmp_path, plan, "pre-plan.json")
    pre_file = _save_apply(tmp_path, pre, "pre.json")
    result = snapshot_transfer_reconcile(source, target, plan_file, pre_file)
    _assert_reconcile_shape(
        result,
        plan,
        source,
        target,
        plan_file,
        apply_supplied=True,
        declared_outcome="pre_publication_partial",
        source_state="matches_plan",
        target_state="absent",
        source_matches=True,
        target_matches=False,
        apply_file=pre_file,
    )

    post_case = tmp_path / "post"
    source_p, target_p, live_p, _d2, plan_p = _prepare(post_case)

    def boom_fsync(_fd):
        raise SnapshotTransferApplyError("fsync snapshots directory failed: injected")

    monkeypatch.setattr(apply_mod, "_verify_staged_envelope", original_verify)
    monkeypatch.setattr(apply_mod, "_fsync_snapshots_directory", boom_fsync)
    post = snapshot_transfer_apply(
        source_p,
        "current",
        target_p,
        plan_p["transfer_revision"],
        transfer_confirmed=True,
    )
    assert post["ok"] is False
    assert post["partial"] is True
    assert post["publication_performed"] is True
    post_plan = _save_plan(post_case, plan_p, "post-plan.json")
    post_file = _save_apply(post_case, post, "post.json")
    result = snapshot_transfer_reconcile(source_p, target_p, post_plan, post_file)
    _assert_reconcile_shape(
        result,
        plan_p,
        source_p,
        target_p,
        post_plan,
        apply_supplied=True,
        declared_outcome="post_publication_partial",
        source_state="matches_plan",
        target_state="matches_plan",
        source_matches=True,
        target_matches=True,
        apply_file=post_file,
    )
    assert (target_p / "snapshots" / live_p.name).is_dir()
    for field in ("source_current_unchanged", "target_current_unchanged"):
        impossible = dict(post)
        impossible[field] = False
        impossible_file = _save_apply(
            post_case, impossible, f"impossible-{field}.json"
        )
        with pytest.raises(
            SnapshotTransferReconcileError, match=f"{field} must exactly report"
        ):
            snapshot_transfer_reconcile(
                source_p, target_p, post_plan, impossible_file
            )


def test_blocked_plan_rejects_any_apply_result(tmp_path: Path):
    source, target, live, _dest, plan = _prepare(tmp_path)
    applied = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    blocked = snapshot_transfer_plan(source, "current", target)
    assert blocked["transfer_ready"] is False
    forged = json.loads(apply_to_json(applied))
    forged["source_graph"] = blocked["source_graph"]
    forged["target_graph"] = blocked["target_graph"]
    forged["requested_snapshot"] = blocked["requested_snapshot"]
    forged["snapshot_id"] = blocked["snapshot_id"]
    forged["expected_transfer_revision"] = blocked["transfer_revision"]
    forged["observed_transfer_revision"] = blocked["transfer_revision"]
    forged["source_export_revision"] = blocked["source_export_revision"]
    forged["planned_files"] = blocked["files"]
    forged["file_count"] = blocked["file_count"]
    forged["total_size_bytes"] = blocked["total_size_bytes"]
    forged["source_current_before"] = blocked["source_current"]
    forged["target_current_before"] = blocked["target_current"]
    proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(_save_plan(tmp_path, blocked, "blocked.json")),
        "--apply-result-file",
        str(_write_json(tmp_path / "blocked-apply.json", forged)),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "blocked" in proc.stderr or "impossible" in proc.stderr


def test_malformed_oversized_duplicate_nan_symlink_fifo_replaced_truncated_growing_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)

    missing = _run("--plan-file", str(plan_file), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")
    bad = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(malformed),
        "--json",
    )
    assert bad.returncode == 2
    assert bad.stdout == ""

    oversized = tmp_path / "huge.json"
    oversized.write_bytes(b"{" + b"a" * (MAX_INPUT_BYTES + 1) + b"}")
    huge = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(oversized),
        "--json",
    )
    assert huge.returncode == 2
    assert huge.stdout == ""
    assert str(MAX_INPUT_BYTES) in huge.stderr

    dup = tmp_path / "dup.json"
    dup.write_text('{"schema_version": 1, "schema_version": 1}\n', encoding="utf-8")
    dup_proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(dup),
        "--json",
    )
    assert dup_proc.returncode == 2
    assert dup_proc.stdout == ""
    assert "duplicate" in dup_proc.stderr or "valid JSON" in dup_proc.stderr

    nan_path = tmp_path / "nan.json"
    nan_path.write_text('{"schema_version": 1, "ok": NaN}\n', encoding="utf-8")
    nan_proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(nan_path),
        "--json",
    )
    assert nan_proc.returncode == 2
    assert nan_proc.stdout == ""

    linked = tmp_path / "link.json"
    linked.symlink_to(plan_file)
    symlink = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(linked),
        "--json",
    )
    assert symlink.returncode == 2
    assert symlink.stdout == ""
    assert "symlink" in symlink.stderr

    fifo_path = tmp_path / "fifo.json"
    os.mkfifo(fifo_path)
    fifo_proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(fifo_path),
        "--json",
    )
    assert fifo_proc.returncode == 2
    assert fifo_proc.stdout == ""

    apply_linked = tmp_path / "apply-link.json"
    apply_linked.symlink_to(plan_file)
    apply_symlink = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(apply_linked),
        "--json",
    )
    assert apply_symlink.returncode == 2
    assert apply_symlink.stdout == ""

    victim = tmp_path / "replaced.json"
    shutil.copyfile(plan_file, victim)

    def replace_after_lstat(path: Path) -> None:
        if path == victim:
            path.unlink()
            path.write_text('{"schema_version": 1}\n', encoding="utf-8")

    monkeypatch.setattr(reconcile_mod, "_after_input_path_lstat", replace_after_lstat)
    with pytest.raises(SnapshotTransferReconcileError, match="changed|unsafe"):
        snapshot_transfer_reconcile(source, target, victim)
    proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(victim),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    monkeypatch.setattr(
        reconcile_mod, "_after_input_path_lstat", reconcile_mod._after_input_path_lstat
    )

    truncated = tmp_path / "truncated.json"
    shutil.copyfile(plan_file, truncated)

    def truncate_after_read(path: Path, _digest: str) -> None:
        if path == truncated:
            os.truncate(path, max(1, path.stat().st_size // 2))

    monkeypatch.setattr(reconcile_mod, "_after_input_file_read", truncate_after_read)
    with pytest.raises(SnapshotTransferReconcileError, match="changed"):
        snapshot_transfer_reconcile(source, target, truncated)

    growing = tmp_path / "growing.json"
    shutil.copyfile(plan_file, growing)

    def grow_after_read(path: Path, _digest: str) -> None:
        if path == growing:
            with path.open("ab") as handle:
                handle.write(b"\n")

    monkeypatch.setattr(reconcile_mod, "_after_input_file_read", grow_after_read)
    with pytest.raises(SnapshotTransferReconcileError, match="changed"):
        snapshot_transfer_reconcile(source, target, growing)

    same = tmp_path / "same-size.json"
    shutil.copyfile(plan_file, same)
    original = same.read_bytes()
    original_stat = same.stat()

    def rewrite_same_size(path: Path, _digest: str) -> None:
        if path == same:
            mutated = json.loads(original.decode("utf-8"))
            mutated["ok"] = False
            rewritten = (
                json.dumps(mutated, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
            ).encode("utf-8")
            if len(rewritten) != len(original):
                rewritten = original[:-1] + bytes([original[-1] ^ 1])
            same.write_bytes(rewritten)
            os.utime(same, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(reconcile_mod, "_after_input_file_read", rewrite_same_size)
    with pytest.raises(SnapshotTransferReconcileError, match="changed"):
        snapshot_transfer_reconcile(source, target, same)


def test_plan_revision_tampering_and_apply_plan_mismatches(tmp_path: Path):
    source, target, live, _dest, plan = _prepare(tmp_path)
    applied = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)

    tampered = json.loads(plan_to_json(plan))
    tampered["source_export_revision"] = "sha256:" + ("ab" * 32)
    proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "src-rev.json", tampered)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "source_export_revision" in proc.stderr

    transfer_rev = json.loads(plan_to_json(plan))
    transfer_rev["transfer_revision"] = "sha256:" + ("cd" * 32)
    proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "xfer-rev.json", transfer_rev)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "transfer_revision" in proc.stderr

    files = json.loads(plan_to_json(plan))
    files["files"] = list(reversed(files["files"]))
    proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "order.json", files)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    performed = json.loads(plan_to_json(plan))
    performed["transfer_performed"] = True
    proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "performed.json", performed)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    mismatch_fields = [
        {"source_graph": str(target.resolve())},
        {"target_graph": str(source.resolve())},
        {"requested_snapshot": live.name if plan["requested_snapshot"] == "current" else "current"},
        {"snapshot_id": _current(target)},
        {"expected_transfer_revision": "sha256:" + ("11" * 32)},
        {"observed_transfer_revision": "sha256:" + ("22" * 32)},
        {"source_export_revision": "sha256:" + ("33" * 32)},
        {"file_count": applied["file_count"] + 1},
        {"total_size_bytes": applied["total_size_bytes"] + 1},
        {"source_current_before": _current(target)},
        {"target_current_before": live.name},
    ]
    planned = json.loads(apply_to_json(applied))
    planned_files = list(planned["planned_files"])
    planned_files[0] = dict(planned_files[0])
    planned_files[0]["content_revision"] = "sha256:" + ("44" * 32)
    mismatch_fields.append({"planned_files": planned_files})
    for index, updates in enumerate(mismatch_fields):
        forged = json.loads(apply_to_json(applied))
        forged.update(updates)
        proc = _run(
            "--source-graph",
            str(source),
            "--target-graph",
            str(target),
            "--plan-file",
            str(plan_file),
            "--apply-result-file",
            str(_write_json(tmp_path / f"mismatch-{index}.json", forged)),
            "--json",
        )
        assert proc.returncode in {1, 2}, updates
        assert proc.stdout == ""

    other = tmp_path / "other-source"
    other_target = tmp_path / "other-target"
    _publish(other, "other-src")
    _publish(other_target, "other-dst")
    with pytest.raises(SnapshotTransferReconcileIntegrityError, match="does not match"):
        snapshot_transfer_reconcile(other, other_target, plan_file)
    proc = _run(
        "--source-graph",
        str(other),
        "--target-graph",
        str(other_target),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_impossible_apply_outcome_flags(tmp_path: Path):
    source, target, _live, _dest, plan = _prepare(tmp_path)
    applied = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    cases = [
        {"ok": True, "partial": True},
        {"ok": False, "partial": False},
        {"transfer_confirmed": False},
        {"transfer_performed": False, "ok": True, "partial": False},
        {"publication_performed": False, "ok": True, "partial": False},
        {"staging_created": False},
        {"source_graph_mutated": True},
        {"activation_performed": True},
        {"retention_performed": True},
        {"retry_requires_fresh_plan": False},
        {"error": "unexpected"},
        {
            "ok": False,
            "partial": True,
            "transfer_performed": False,
            "publication_performed": False,
            "snapshot_verified_after_publication": False,
            "source_current_after": None,
            "source_current_unchanged": False,
            "target_current_after": None,
            "target_current_unchanged": False,
            "target_snapshots_fsync_confirmed": False,
            "error": "pre",
            "staging_remaining": False,
            "staging_cleanup_attempted": False,
        },
    ]
    for index, updates in enumerate(cases):
        forged = json.loads(apply_to_json(applied))
        forged.update(updates)
        proc = _run(
            "--source-graph",
            str(source),
            "--target-graph",
            str(target),
            "--plan-file",
            str(plan_file),
            "--apply-result-file",
            str(_write_json(tmp_path / f"impossible-{index}.json", forged)),
            "--json",
        )
        assert proc.returncode == 2, updates
        assert proc.stdout == ""


def test_same_graph_and_aliases_rejected_before_nested_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    calls = {"leases": 0}
    original = reconcile_mod.graph_shared_leases

    @contextmanager
    def counted(*args, **kwargs):
        calls["leases"] += 1
        with original(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(reconcile_mod, "graph_shared_leases", counted)
    identical = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(source),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert identical.returncode == 2
    assert identical.stdout == ""
    with pytest.raises(
        SnapshotTransferReconcileError, match="different directory identities"
    ):
        snapshot_transfer_reconcile(source, source, plan_file)
    with pytest.raises(
        SnapshotTransferReconcileError, match="different directory identities"
    ):
        snapshot_transfer_reconcile(source, source / ".", plan_file)
    assert calls["leases"] == 0


def test_deterministic_shared_lease_order_and_concurrent_ab_ba(tmp_path: Path, monkeypatch):
    import graphrag_code.byog_graph as graph_mod
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    aaa = tmp_path / "aaa"
    zzz = tmp_path / "zzz"
    _publish(aaa, "aaa")
    _publish(zzz, "zzz")
    plan_az = snapshot_transfer_plan(aaa, "current", zzz)
    plan_za = snapshot_transfer_plan(zzz, "current", aaa)
    plan_az_file = _save_plan(tmp_path, plan_az, "az.json")
    plan_za_file = _save_plan(tmp_path, plan_za, "za.json")
    original = reconcile_mod.graph_shared_leases
    observed: list[list[str]] = []

    @contextmanager
    def tracked(first, second):
        observed.append([str(Path(first).resolve()), str(Path(second).resolve())])
        with original(first, second) as lease:
            yield lease

    monkeypatch.setattr(reconcile_mod, "graph_shared_leases", tracked)
    forward = snapshot_transfer_reconcile(aaa, zzz, plan_az_file)
    reverse = snapshot_transfer_reconcile(zzz, aaa, plan_za_file)
    assert forward["ok"] is True
    assert reverse["ok"] is True
    assert len(observed) == 2
    assert observed[0] == observed[1]
    expected_first, expected_second = ordered_graph_lease_pair(
        aaa.resolve(),
        (aaa.stat().st_dev, aaa.stat().st_ino),
        zzz.resolve(),
        (zzz.stat().st_dev, zzz.stat().st_ino),
    )
    assert observed[0] == [str(expected_first), str(expected_second)]

    errors: list[BaseException] = []
    results: dict[str, dict] = {}

    def run_az():
        try:
            results["az"] = snapshot_transfer_reconcile(aaa, zzz, plan_az_file)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    def run_za():
        try:
            results["za"] = snapshot_transfer_reconcile(zzz, aaa, plan_za_file)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    first = threading.Thread(target=run_az)
    second = threading.Thread(target=run_za)
    first.start()
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert results["az"]["ok"] is True
    assert results["za"]["ok"] is True

    replaced = {"done": False}

    def replace_second_lock(root: Path) -> None:
        if replaced["done"] or Path(root).resolve() != expected_first:
            return
        replacement = expected_second / ".publish.lock.replacement"
        replacement.write_bytes(b"")
        os.replace(replacement, expected_second / PUBLICATION_LOCK_NAME)
        replaced["done"] = True

    monkeypatch.setattr(
        graph_mod, "_after_graph_shared_lease_one_held", replace_second_lock
    )
    with pytest.raises(
        SnapshotTransferReconcileError, match="publication lock changed"
    ):
        snapshot_transfer_reconcile(aaa, zzz, plan_az_file)
    assert replaced["done"] is True


def test_source_and_target_publishers_wait_while_shared_leases_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    held = {"source": None, "target": None}

    def waiter(graph: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "sys.path.insert(0, sys.argv[1]); "
                    "from graphrag_code.byog_graph import graph_exclusive_lease; "
                    "root = Path(sys.argv[2]); "
                    "graph_exclusive_lease(root).__enter__(); "
                    "print('acquired', flush=True)"
                ),
                str(ROOT / "src"),
                str(graph),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )

    def during_ready(*_args, **_kwargs):
        held["source"] = waiter(source)
        held["target"] = waiter(target)
        time.sleep(0.4)
        assert held["source"].poll() is None
        assert held["target"].poll() is None

    monkeypatch.setattr(reconcile_mod, "_after_result_ready", during_ready)
    result = snapshot_transfer_reconcile(source, target, plan_file)
    assert result["ok"] is True
    try:
        for proc in (held["source"], held["target"]):
            assert proc is not None
            stdout, stderr = proc.communicate(timeout=10)
            assert proc.returncode == 0, (stdout, stderr)
            assert "acquired" in stdout
    finally:
        for proc in (held["source"], held["target"]):
            if proc is not None and proc.poll() is None:
                proc.kill()


def test_stable_absent_match_mismatch_current_history_and_staging(tmp_path: Path):
    source, target, live, dest_live, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    absent = snapshot_transfer_reconcile(source, target, plan_file)
    assert absent["target_snapshot_state"] == "absent"
    assert absent["source_snapshot_state"] == "matches_plan"
    assert absent["target_staging_present"] is False

    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    matched = snapshot_transfer_reconcile(source, target, plan_file)
    assert matched["target_snapshot_state"] == "matches_plan"
    assert matched["source_snapshot_state"] == "matches_plan"

    _rewrite_same_size(source / "snapshots" / live.name / "settings.yaml")
    source_mismatch = snapshot_transfer_reconcile(source, target, plan_file)
    assert source_mismatch["ok"] is True
    assert source_mismatch["source_snapshot_state"] == "revision_mismatch"
    assert source_mismatch["target_snapshot_state"] == "matches_plan"

    _rewrite_same_size(target / "snapshots" / live.name / "settings.yaml")
    both_mismatch = snapshot_transfer_reconcile(source, target, plan_file)
    assert both_mismatch["ok"] is True
    assert both_mismatch["target_snapshot_state"] == "revision_mismatch"

    extra_source = _publish(source, "extra-src")
    extra_target = _publish(target, "extra-dst")
    drifted = snapshot_transfer_reconcile(source, target, plan_file)
    assert drifted["ok"] is True
    assert drifted["source_current"] == extra_source.name
    assert drifted["source_current_matches_plan"] is False
    assert drifted["source_history_matches_plan"] is False
    assert drifted["target_current"] == extra_target.name
    assert drifted["target_current_matches_plan"] is False
    assert drifted["target_history_matches_plan_plus_snapshot"] is False
    assert extra_source.name in drifted["source_published_snapshots"]
    assert extra_target.name in drifted["target_published_snapshots"]
    assert dest_live.name in drifted["target_published_snapshots"]
    assert drifted["transfer_cause_proven"] is False

    staging = target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}"
    staging.mkdir()
    present = snapshot_transfer_reconcile(source, target, plan_file)
    assert present["target_staging_present"] is True
    assert present["staging_cause_proven"] is False
    shutil.rmtree(staging)
    unrelated = target / "snapshots" / ".staging-19990101-000000-other"
    unrelated.mkdir()
    reported = snapshot_transfer_reconcile(source, target, plan_file)
    assert reported["ok"] is True
    assert reported["target_staging_present"] is False
    assert reported["staging_cause_proven"] is False
    assert reported["transfer_cause_proven"] is False
    shutil.rmtree(unrelated)


def test_unsafe_symlink_fifo_file_in_place_of_snapshot_or_staging(tmp_path: Path):
    source, target, live, _dest, plan = _prepare(tmp_path)
    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    published = target / "snapshots" / live.name
    linked = tmp_path / "linked-snap"
    shutil.copytree(published, linked)
    published.rename(tmp_path / "aside-published")
    published.symlink_to(linked, target_is_directory=True)
    with pytest.raises(SnapshotTransferReconcileIntegrityError, match="symlink"):
        snapshot_transfer_reconcile(source, target, plan_file)
    published.unlink()
    shutil.move(str(tmp_path / "aside-published"), published)

    source_published = source / "snapshots" / live.name
    source_linked = tmp_path / "linked-source"
    shutil.copytree(source_published, source_linked)
    source_published.rename(tmp_path / "aside-source")
    source_published.symlink_to(source_linked, target_is_directory=True)
    with pytest.raises(SnapshotTransferReconcileIntegrityError, match="symlink"):
        snapshot_transfer_reconcile(source, target, plan_file)
    source_published.unlink()
    shutil.move(str(tmp_path / "aside-source"), source_published)

    file_case = tmp_path / "file-case"
    f_source, f_target, f_live, _fd, f_plan = _prepare(file_case)
    snap = f_target / "snapshots" / f_live.name
    snap.write_text("not-a-directory\n", encoding="utf-8")
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="not a real directory"
    ):
        snapshot_transfer_reconcile(
            f_source, f_target, _save_plan(file_case, f_plan, "file.json")
        )

    fifo_case = tmp_path / "fifo-case"
    q_source, q_target, q_live, _qd, q_plan = _prepare(fifo_case)
    os.mkfifo(q_target / "snapshots" / f"{STAGING_NAME_PREFIX}{q_live.name}")
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="not a real directory|symlink"
    ):
        snapshot_transfer_reconcile(
            q_source, q_target, _save_plan(fifo_case, q_plan, "fifo.json")
        )

    src_fifo = tmp_path / "src-fifo"
    s_source, s_target, s_live, _sd, s_plan = _prepare(src_fifo)
    payload = s_source / "snapshots" / s_live.name / "pipe.fifo"
    os.mkfifo(payload)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="unexpected|not a regular"
    ):
        snapshot_transfer_reconcile(
            s_source, s_target, _save_plan(src_fifo, s_plan, "src-fifo.json")
        )


def test_root_lock_current_listing_snapshot_payload_and_staging_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, live, _dest, plan = _prepare(tmp_path)
    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)

    def switch_target_current(_s, _t, _ss, _ts):
        (target / "current").write_text(live.name + "\n", encoding="utf-8")

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", switch_target_current)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="current|listing|lock|staging"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)
    (target / "current").write_text(plan["target_current"] + "\n", encoding="utf-8")

    def switch_source_current(_s, _t, _ss, _ts):
        extra = source / "snapshots" / "19990101-000000-addedsrc"
        if not extra.exists():
            shutil.copytree(source / "snapshots" / live.name, extra)
        (source / "current").write_text(extra.name + "\n", encoding="utf-8")

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", switch_source_current)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="current|listing|lock|staging"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)
    (source / "current").write_text(plan["source_current"] + "\n", encoding="utf-8")
    shutil.rmtree(source / "snapshots" / "19990101-000000-addedsrc", ignore_errors=True)

    def replace_target_lock(_s, _t, _ss, _ts):
        lock = target / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-lock")

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", replace_target_lock)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="lock|current|listing"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)

    def replace_source_lock(_s, _t, _ss, _ts):
        lock = source / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-source-lock")

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", replace_source_lock)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="lock|current|listing"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)

    def add_exact_staging(_s, _t, _ss, _ts):
        staging = target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}"
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", add_exact_staging)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="staging|listing|lock|current"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)
    shutil.rmtree(
        target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}", ignore_errors=True
    )

    def add_unrelated_staging(_s, _t, _ss, _ts):
        staging = target / "snapshots" / ".staging-19990101-000000-other"
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", add_unrelated_staging)
    result = snapshot_transfer_reconcile(source, target, plan_file)
    assert result["ok"] is True
    assert result["target_staging_present"] is False
    shutil.rmtree(
        target / "snapshots" / ".staging-19990101-000000-other", ignore_errors=True
    )

    raced = tmp_path / "raced-source"
    raced_target = tmp_path / "raced-target"
    _publish(raced, "raced-src")
    _publish(raced_target, "raced-dst")
    raced_plan = snapshot_transfer_plan(raced, "current", raced_target)
    raced_plan_file = _save_plan(tmp_path, raced_plan, "raced.json")
    hidden = tmp_path / "hidden-source"
    replacement = tmp_path / "replacement-source"
    _publish(replacement, "other-src")

    def replace_source_root(path):
        if path == raced:
            raced.rename(hidden)
            raced.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(reconcile_mod, "_after_first_joint_scan", lambda *_: None)
    monkeypatch.setattr(
        reconcile_mod, "_after_graphs_identified", lambda s, t: replace_source_root(s)
    )
    with pytest.raises(
        (SnapshotTransferReconcileError, SnapshotTransferReconcileIntegrityError),
        match="symlink|changed|replaced|unsafe",
    ):
        snapshot_transfer_reconcile(raced, raced_target, raced_plan_file)

    replacement_snap = tmp_path / "replacement-target-snap"
    shutil.copytree(target / "snapshots" / live.name, replacement_snap)

    def replace_target_snapshot(_path, _info):
        published = target / "snapshots" / live.name
        aside = tmp_path / "aside-live-target"
        if published.exists() and not published.is_symlink():
            published.rename(aside)
            published.symlink_to(replacement_snap, target_is_directory=True)

    monkeypatch.setattr(reconcile_mod, "_after_graphs_identified", lambda *_: None)
    monkeypatch.setattr(
        reconcile_mod, "_after_target_snapshot_first_stat", replace_target_snapshot
    )
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="replaced|changed|symlink"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)
    published = target / "snapshots" / live.name
    if published.is_symlink() or not published.exists():
        published.unlink(missing_ok=True)
        shutil.move(str(tmp_path / "aside-live-target"), published)

    def rewrite_source_after_first(_path, _records):
        _rewrite_same_size(source / "snapshots" / live.name / "entities.parquet")

    monkeypatch.setattr(
        reconcile_mod, "_after_target_snapshot_first_stat", lambda *_: None
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_after_first_source_snapshot_observation",
        rewrite_source_after_first,
    )
    with pytest.raises(SnapshotTransferReconcileIntegrityError, match="changed"):
        snapshot_transfer_reconcile(source, target, plan_file)

    def rewrite_target_after_first(_path, _records):
        _rewrite_same_size(target / "snapshots" / live.name / "entities.parquet")

    monkeypatch.setattr(
        reconcile_mod,
        "_after_first_source_snapshot_observation",
        lambda *_: None,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_after_first_target_snapshot_observation",
        rewrite_target_after_first,
    )
    with pytest.raises(SnapshotTransferReconcileIntegrityError, match="changed"):
        snapshot_transfer_reconcile(source, target, plan_file)

    def rewrite_after_earlier_payload(_path, name):
        if name == "entities.parquet":
            sibling = _path / "relationships.parquet"
            if sibling.is_file():
                _rewrite_same_size(sibling)

    monkeypatch.setattr(
        reconcile_mod,
        "_after_first_target_snapshot_observation",
        lambda *_: None,
    )
    monkeypatch.setattr(
        reconcile_mod, "_after_payload_final_recheck", rewrite_after_earlier_payload
    )
    with pytest.raises(SnapshotTransferReconcileIntegrityError, match="changed"):
        snapshot_transfer_reconcile(source, target, plan_file)

    def replace_listing_after_second(_path, _entries):
        extra = _path / "alias.parquet"
        if not extra.exists() and (_path / "entities.parquet").exists():
            os.link(_path / "entities.parquet", extra)

    monkeypatch.setattr(
        reconcile_mod, "_after_payload_final_recheck", lambda *_: None
    )
    monkeypatch.setattr(reconcile_mod, "_after_second_listed", replace_listing_after_second)
    with pytest.raises(
        SnapshotTransferReconcileIntegrityError, match="changed|hardlink|unexpected"
    ):
        snapshot_transfer_reconcile(source, target, plan_file)


def test_missing_unmanaged_unlocked_and_input_before_graph_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)

    missing = _run(
        "--source-graph",
        str(tmp_path / "missing"),
        "--target-graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "entities.parquet").write_bytes(b"x")
    legacy = _run(
        "--source-graph",
        str(flat),
        "--target-graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert legacy.returncode == 2
    assert legacy.stdout == ""

    unlocked = tmp_path / "unlocked"
    _publish(unlocked, "lock")
    (unlocked / PUBLICATION_LOCK_NAME).unlink()
    unlocked_proc = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(unlocked),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert unlocked_proc.returncode == 2
    assert unlocked_proc.stdout == ""
    assert "adopt-publication-lock" in unlocked_proc.stderr
    assert not (unlocked / PUBLICATION_LOCK_NAME).exists()

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")

    def graph_inspection_is_forbidden(*_args, **_kwargs):
        raise AssertionError("graph was inspected before input validation")

    monkeypatch.setattr(
        reconcile_mod, "_resolve_existing_real_directory", graph_inspection_is_forbidden
    )
    monkeypatch.setattr(
        reconcile_mod, "_require_managed_graph", graph_inspection_is_forbidden
    )
    with pytest.raises(SnapshotTransferReconcileError, match="valid JSON"):
        snapshot_transfer_reconcile(source, target, malformed)


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, live, _dest, plan = _prepare(tmp_path)
    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    original_json = reconcile_mod.result_to_json
    original_format = reconcile_mod.format_result
    original_lease = reconcile_mod.graph_shared_leases
    state = {
        "source_fd": None,
        "target_fd": None,
        "source_snapshots_fd": None,
        "target_snapshots_fd": None,
        "source_snapshot_fd": None,
        "target_snapshot_fd": None,
        "source_payload_fds": {},
        "target_payload_fds": {},
        "lease": 0,
        "responses": 0,
        "flushes": 0,
        "leases": 0,
    }

    @contextmanager
    def tracked_lease(*args, **kwargs):
        state["leases"] += 2
        state["lease"] += 2
        try:
            with original_lease(*args, **kwargs) as lease:
                yield lease
        finally:
            state["lease"] -= 2

    def capture_ready(
        _source,
        _target,
        source_fd,
        target_fd,
        source_snapshots_fd,
        target_snapshots_fd,
        source_snapshot_fd,
        target_snapshot_fd,
        source_payload_fds,
        target_payload_fds,
        _result,
    ):
        state["source_fd"] = source_fd
        state["target_fd"] = target_fd
        state["source_snapshots_fd"] = source_snapshots_fd
        state["target_snapshots_fd"] = target_snapshots_fd
        state["source_snapshot_fd"] = source_snapshot_fd
        state["target_snapshot_fd"] = target_snapshot_fd
        state["source_payload_fds"] = dict(source_payload_fds)
        state["target_payload_fds"] = dict(target_payload_fds)
        os.fstat(source_fd)
        os.fstat(target_fd)
        os.fstat(source_snapshots_fd)
        os.fstat(target_snapshots_fd)
        assert source_snapshot_fd is not None
        assert target_snapshot_fd is not None
        os.fstat(source_snapshot_fd)
        os.fstat(target_snapshot_fd)
        for fd in list(source_payload_fds.values()) + list(target_payload_fds.values()):
            os.fstat(fd)
        assert state["lease"] == 2

    def _assert_held() -> None:
        assert state["lease"] == 2
        os.fstat(state["source_fd"])
        os.fstat(state["target_fd"])
        os.fstat(state["source_snapshots_fd"])
        os.fstat(state["target_snapshots_fd"])
        os.fstat(state["source_snapshot_fd"])
        os.fstat(state["target_snapshot_fd"])
        for fd in list(state["source_payload_fds"].values()) + list(
            state["target_payload_fds"].values()
        ):
            os.fstat(fd)

    def guarded_json(*args, **kwargs):
        _assert_held()
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        _assert_held()
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            _assert_held()
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            _assert_held()
            state["flushes"] += 1

    monkeypatch.setattr(reconcile_mod, "graph_shared_leases", tracked_lease)
    monkeypatch.setattr(reconcile_mod, "_after_result_ready", capture_ready)
    monkeypatch.setattr(reconcile_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(reconcile_mod, "format_result", guarded_format)
    monkeypatch.setattr(reconcile_mod.sys, "stdout", GuardedStdout())
    assert (
        reconcile_mod.main(
            [
                "--source-graph",
                str(source),
                "--target-graph",
                str(target),
                "--plan-file",
                str(plan_file),
                "--json",
            ]
        )
        == 0
    )
    assert (
        reconcile_mod.main(
            [
                "--source-graph",
                str(source),
                "--target-graph",
                str(target),
                "--plan-file",
                str(plan_file),
            ]
        )
        == 0
    )
    assert state["lease"] == 0
    assert state["leases"] == 4
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_no_mutation_no_producer_invocation_and_mcp_remains_fourteen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_apply as apply_mod
    import graphrag_code.snapshot_transfer_plan as plan_mod
    import graphrag_code.snapshot_transfer_reconcile as reconcile_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    before_source = _protected_state(source)
    before_target = _protected_state(target)
    calls = {"shared": 0}

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public mutating/read scope")

    original_lease = reconcile_mod.graph_shared_leases

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 2
        with original_lease(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(reconcile_mod, "graph_shared_leases", counted)
    monkeypatch.setattr(reconcile_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "snapshot_transfer_plan", boom)
    monkeypatch.setattr(apply_mod, "snapshot_transfer_apply", boom)
    result = snapshot_transfer_reconcile(source, target, plan_file)
    assert result["ok"] is True
    assert result["source_graph_mutated"] is False
    assert result["target_graph_mutated"] is False
    assert calls["shared"] == 2
    assert _protected_state(source) == before_source
    assert _protected_state(target) == before_target

    source_text = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
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
    assert "graph_shared_leases" in imported
    assert "export_revision_of" in imported
    assert "canonical_transfer_revision_payload" in imported
    assert "transfer_revision_of" in imported
    assert "snapshot_transfer_plan(" not in source_text
    assert "snapshot_transfer_apply(" not in source_text
    assert "graph_exclusive_lease" not in source_text
    assert "read_bytes" not in source_text
    assert HASH_CHUNK_BYTES <= 64 * 1024
    lowered = source_text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 14
    assert "snapshot_transfer_reconcile" not in TOOL_NAMES
    assert "snapshot_transfer_plan" not in TOOL_NAMES
    assert "snapshot_transfer_apply" not in TOOL_NAMES
    session = build_session(target, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 14
            assert "snapshot_transfer_reconcile" not in names

    anyio_run(_body)
    after = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert after == before


def test_repository_has_no_staging_or_export_artifacts():
    leftovers: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(ROOT, followlinks=False):
        rel = Path(dirpath).relative_to(ROOT)
        if ".git" in rel.parts or "output" in rel.parts:
            dirnames[:] = []
            continue
        if Path(dirpath).name.startswith((".staging-", ".graphrag-export-")):
            leftovers.append(Path(dirpath))
        leftovers.extend(
            Path(dirpath) / name
            for name in dirnames
            if name.startswith((".staging-", ".graphrag-export-"))
        )
    assert leftovers == []


def test_deterministic_json(tmp_path: Path):
    source, target, live, _dest, plan = _prepare(tmp_path)
    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    first = snapshot_transfer_reconcile(source, target, plan_file)
    second = snapshot_transfer_reconcile(source, target, plan_file)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    names = [item["path"] for item in first["planned_files"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    assert first["observed_source_export_revision"] == export_revision_of(
        {
            "schema_version": 1,
            "resolved_snapshot": live.name,
            "files": first["planned_files"],
        }
    )
    assert first["input_transfer_revision"] == transfer_revision_of(plan)
    assert first["observed_target_export_revision"] == first[
        "observed_source_export_revision"
    ]
