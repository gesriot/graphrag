"""Read-only snapshot-export staging cleanup reconciliation.

Disposable tmp parents only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_staging_cleanup_reconcile.py -q
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import multiprocessing
import os
import re
import shutil
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
from graphrag_code.snapshot_export_staging import inventory_revision_of  # type: ignore
from graphrag_code.snapshot_export_staging_cleanup import (  # type: ignore
    snapshot_export_staging_cleanup,
)
from graphrag_code.snapshot_export_staging_cleanup_plan import (  # type: ignore
    plan_revision_of,
    snapshot_export_staging_cleanup_plan,
)
from graphrag_code.snapshot_export_staging_cleanup_reconcile import (  # type: ignore
    MAX_INPUT_BYTES,
    SnapshotExportStagingCleanupReconcileError,
    SnapshotExportStagingCleanupReconcileIntegrityError,
    format_result,
    result_to_json,
    snapshot_export_staging_cleanup_reconcile,
)
from graphrag_code.snapshot_export_writer_lease import (  # type: ignore
    EXPORT_STAGING_WRITER_LOCK_NAME,
)

SCRIPT = ROOT / "scripts" / "snapshot_export_staging_cleanup_reconcile.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_staging_cleanup_reconcile.py"
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
        "claim_existing_export_writer_lease",
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


def _candidate(parent: Path, suffix: str, *, payload: bytes = b"payload") -> Path:
    staging = _make_staging(parent, suffix)
    (staging / "manifest.json").write_bytes(payload)
    _write_candidate_lock(staging)
    return staging


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(
        json.dumps(
            payload,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _save_plan(tmp_path: Path, plan: dict, name: str = "plan.json") -> Path:
    return _write_json(tmp_path / name, plan)


def _save_apply(tmp_path: Path, result: dict, name: str = "apply.json") -> Path:
    return _write_json(tmp_path / name, result)


def _reseal_saved_plan(plan: dict) -> dict:
    staging = list(plan["staging_entries"])
    inventory = {
        "schema_version": 1,
        "staging_entries": staging,
        "staging_count": plan["staging_count"],
        "unsafe_staging_count": sum(
            1 for entry in staging if entry.get("kind") != "directory"
        ),
        "unrecognized_prefixed_entries": list(
            plan["unrecognized_prefixed_entries"]
        ),
        "unrecognized_prefixed_count": plan["unrecognized_prefixed_count"],
        "other_entry_count": plan["other_entry_count"],
    }
    plan["observed_inventory_revision"] = inventory_revision_of(inventory)
    plan["plan_revision"] = plan_revision_of(plan)
    return plan


def _forge_complete(plan: dict) -> dict:
    deleted = list(plan["deletion_candidates"])
    remaining = sorted(
        [item["name"] for item in plan["blocked_entries"]],
        key=os.fsencode,
    )
    return {
        "schema_version": 1,
        "ok": True,
        "parent": plan["parent"],
        "expected_plan_revision": plan["plan_revision"],
        "observed_plan_revision": plan["plan_revision"],
        "planned_deletion_candidates": list(plan["deletion_candidates"]),
        "deleted_staging_entries": deleted,
        "deleted_count": len(deleted),
        "remaining_staging_entries": remaining,
        "changed": bool(deleted),
        "cleanup_confirmed": True,
        "partial": False,
        "filesystem_may_have_changed": bool(deleted),
        "retry_requires_fresh_plan": False,
        "failed_staging_entry": None,
        "not_attempted_staging_entries": [],
        "ownership_inference": False,
        "error": None,
        "notices": [{"code": "n", "kind": "notice", "message": "forged complete"}],
    }


def _forge_partial(plan: dict, deleted_count: int) -> dict:
    planned = list(plan["deletion_candidates"])
    deleted = planned[:deleted_count]
    failed = planned[deleted_count]
    not_attempted = planned[deleted_count + 1 :]
    remaining = sorted(
        dict.fromkeys(
            [
                *[item["name"] for item in plan["blocked_entries"]],
                failed,
                *not_attempted,
            ]
        ),
        key=os.fsencode,
    )
    return {
        "schema_version": 1,
        "ok": False,
        "parent": plan["parent"],
        "expected_plan_revision": plan["plan_revision"],
        "observed_plan_revision": plan["plan_revision"],
        "planned_deletion_candidates": planned,
        "deleted_staging_entries": deleted,
        "deleted_count": len(deleted),
        "remaining_staging_entries": remaining,
        "changed": bool(deleted),
        "cleanup_confirmed": True,
        "partial": True,
        "filesystem_may_have_changed": True,
        "retry_requires_fresh_plan": True,
        "failed_staging_entry": failed,
        "not_attempted_staging_entries": not_attempted,
        "ownership_inference": False,
        "error": f"injected partial failure at {failed}",
        "notices": [{"code": "n", "kind": "notice", "message": "forged partial"}],
    }


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
        settings_text=f"export-cleanup-reconcile: {marker}\n",
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


def _obs(result: dict, name: str) -> dict:
    matches = [
        item
        for item in result["candidate_observations"] + result["blocked_observations"]
        if item["name"] == name
    ]
    assert len(matches) == 1, name
    return matches[0]


def _assert_reconcile_shape(result: dict, parent: Path, plan: dict) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["parent"] == str(parent.resolve())
    assert result["input_plan_revision"] == plan["plan_revision"]
    assert result["input_observed_inventory_revision"] == plan["observed_inventory_revision"]
    assert result["reconciliation_is_observation_only"] is True
    assert result["deletion_cause_proven"] is False
    assert result["recovery_performed"] is False
    assert result["fresh_plan_required_before_mutation"] is True
    assert "plan_revision" not in result
    assert [item["name"] for item in result["candidate_observations"]] == list(
        plan["deletion_candidates"]
    )
    assert [item["name"] for item in result["blocked_observations"]] == [
        item["name"] for item in plan["blocked_entries"]
    ]
    text = format_result(result)
    assert "reconciliation_is_observation_only=true" in text
    assert "deletion_cause_proven=false" in text
    assert "fresh_plan_required_before_mutation=true" in text
    lowered = json.dumps(result).lower() + text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_three_equivalent_cli_surfaces_and_packaging(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    parent = here / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(here, plan, "plan.json")
    args = [
        "--parent",
        "parent",
        "--plan-file",
        "plan.json",
        "--json",
    ]
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.snapshot_export_staging_cleanup_reconcile",
            *args,
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-export-staging-cleanup-reconcile",
            *args,
        ],
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
    assert bodies[0]["candidate_observations"][0]["name"] == leftover.name
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-staging-cleanup-reconcile",
            "--parent",
            str(parent),
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
    assert json.loads(installed.stdout)["input_plan_revision"] == plan["plan_revision"]

    import tarfile
    import zipfile

    with zipfile.ZipFile(built_wheel_and_sdist[0]) as zf:
        names = zf.namelist()
    assert "graphrag_code/snapshot_export_staging_cleanup_reconcile.py" in names
    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        snames = "\n".join(tf.getnames())
    assert "snapshot_export_staging_cleanup_reconcile.py" in snames


def test_input_validation_completes_before_parent_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging_cleanup_reconcile as reconcile_mod

    malformed_plan = tmp_path / "malformed-plan.json"
    malformed_plan.write_text("{not-json", encoding="utf-8")

    def parent_inspection_is_forbidden(*_args, **_kwargs):
        raise AssertionError("parent was inspected before input validation")

    monkeypatch.setattr(
        reconcile_mod,
        "export_staging_observation_scope",
        parent_inspection_is_forbidden,
    )
    with pytest.raises(SnapshotExportStagingCleanupReconcileError, match="valid JSON"):
        snapshot_export_staging_cleanup_reconcile(
            tmp_path / "missing", malformed_plan
        )

    parent = tmp_path / "parent"
    parent.mkdir()
    _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    malformed_apply = tmp_path / "malformed-apply.json"
    malformed_apply.write_text("[]", encoding="utf-8")
    with pytest.raises(SnapshotExportStagingCleanupReconcileError, match="JSON object"):
        snapshot_export_staging_cleanup_reconcile(
            parent, _save_plan(tmp_path, plan), malformed_apply
        )


def test_valid_schema2_plan_and_schema1_rejection(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan)
    )
    _assert_reconcile_shape(result, parent, plan)
    assert result["apply_result_supplied"] is False
    assert result["apply_result_valid"] is None
    assert result["declared_apply_outcome"] == "not_supplied"
    assert result["result_consistent_with_observation"] is None
    assert _obs(result, leftover.name)["state"] == "present_candidate_at_reconcile"

    schema1 = copy.deepcopy(plan)
    schema1["schema_version"] = 1
    schema1["apply_supported"] = False
    schema1["plan_revision"] = plan_revision_of(schema1)
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(_write_json(tmp_path / "schema1.json", schema1)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "schema_version" in proc.stderr


def test_forged_plan_and_inventory_revisions(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)

    forged_plan = copy.deepcopy(plan)
    forged_plan["plan_revision"] = "sha256:" + ("ab" * 32)
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(_write_json(tmp_path / "forged-plan.json", forged_plan)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "plan_revision" in proc.stderr

    forged_inventory = copy.deepcopy(plan)
    forged_inventory["staging_entries"][0]["ctime_ns"] += 1
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(_write_json(tmp_path / "forged-inventory.json", forged_inventory)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "observed_inventory_revision" in proc.stderr


def test_invalid_counts_ordering_names_blocked_reasons_booleans_safety_flags(
    tmp_path: Path,
):
    parent = tmp_path / "parent"
    parent.mkdir()
    first = _candidate(parent, HEX_A)
    second = _candidate(parent, HEX_C)
    unrec = parent / ".graphrag-export-short"
    unrec.mkdir()
    plan = snapshot_export_staging_cleanup_plan(parent)

    cases = []
    counted = copy.deepcopy(plan)
    counted["staging_count"] = 99
    cases.append((counted, "staging_count"))
    reordered = copy.deepcopy(plan)
    reordered["deletion_candidates"] = list(reversed(reordered["deletion_candidates"]))
    reordered["plan_revision"] = plan_revision_of(reordered)
    cases.append((reordered, "deletion_candidates"))
    duplicated = copy.deepcopy(plan)
    duplicated["deletion_candidates"] = [first.name, first.name]
    duplicated["deletion_candidate_count"] = 2
    duplicated["plan_revision"] = plan_revision_of(duplicated)
    cases.append((duplicated, "unique"))
    bad_name = copy.deepcopy(plan)
    bad_name["deletion_candidates"] = ["../outside"]
    bad_name["deletion_candidate_count"] = 1
    cases.append((bad_name, "canonical|direct|current"))
    bad_reason = copy.deepcopy(plan)
    bad_reason["blocked_entries"][0]["reason"] = "abandoned"
    cases.append((bad_reason, "blocking reason|blocked_entries"))
    bool_int = copy.deepcopy(plan)
    bool_int["ok"] = 1
    cases.append((bool_int, "boolean"))
    safety = copy.deepcopy(plan)
    safety["cleanup_applied"] = True
    safety["plan_revision"] = plan_revision_of(safety)
    cases.append((safety, "cleanup_applied"))
    ownership = copy.deepcopy(plan)
    ownership["ownership_inference"] = True
    ownership["plan_revision"] = plan_revision_of(ownership)
    cases.append((ownership, "ownership_inference"))
    apply_flag = copy.deepcopy(plan)
    apply_flag["apply_supported"] = False
    apply_flag["plan_revision"] = plan_revision_of(apply_flag)
    cases.append((apply_flag, "apply_supported"))
    overlap = copy.deepcopy(plan)
    overlap["blocked_entries"].append({"name": second.name, "reason": "held_writer_lease"})
    overlap["blocked_count"] = len(overlap["blocked_entries"])
    cases.append((overlap, "unique|overlap"))
    extra_field = copy.deepcopy(plan)
    extra_field["invented"] = "not emitted by the producer"
    cases.append((extra_field, "producer fields"))
    presentation_tamper = copy.deepcopy(plan)
    presentation_tamper["staging_entries"][0]["ownership"] = "known"
    cases.append((_reseal_saved_plan(presentation_tamper), "ownership"))
    path_tamper = copy.deepcopy(plan)
    path_tamper["staging_entries"][0]["path"] = str(parent / "different")
    cases.append((_reseal_saved_plan(path_tamper), "path"))
    mode_tamper = copy.deepcopy(plan)
    mode_tamper["staging_entries"][0]["mode"] = stat.S_IFREG | 0o600
    cases.append((_reseal_saved_plan(mode_tamper), "kind"))
    lock_path_tamper = copy.deepcopy(plan)
    lock_path_tamper["staging_entries"][0]["writer_lease_path"] = str(
        parent / "wrong.lock"
    )
    cases.append((_reseal_saved_plan(lock_path_tamper), "writer_lease_path"))
    boolean_identity = copy.deepcopy(plan)
    boolean_identity["staging_entries"][0]["writer_lease_dev"] = True
    cases.append((_reseal_saved_plan(boolean_identity), "integer"))
    notice_tamper = copy.deepcopy(plan)
    notice_tamper["notices"][0]["message"] = "forged producer notice"
    cases.append((notice_tamper, "notices"))

    for payload, match in cases:
        proc = _run(
            "--parent",
            str(parent),
            "--plan-file",
            str(_write_json(tmp_path / "bad.json", payload)),
            "--json",
        )
        assert proc.returncode == 2, (match, proc.stderr)
        assert proc.stdout == ""
        assert re.search(match, proc.stderr), proc.stderr


def test_malformed_oversized_symlinked_replaced_truncated_and_same_size_mtime(
    tmp_path: Path,
):
    import graphrag_code.snapshot_export_staging_cleanup_reconcile as reconcile_mod

    parent = tmp_path / "parent"
    parent.mkdir()
    _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)

    missing = _run("--parent", str(parent), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")
    bad = _run(
        "--parent", str(parent), "--plan-file", str(malformed), "--json"
    )
    assert bad.returncode == 2
    assert bad.stdout == ""

    oversized = tmp_path / "huge.json"
    oversized.write_bytes(b"{" + b"a" * (MAX_INPUT_BYTES + 1) + b"}")
    huge = _run(
        "--parent", str(parent), "--plan-file", str(oversized), "--json"
    )
    assert huge.returncode == 2
    assert huge.stdout == ""
    assert str(MAX_INPUT_BYTES) in huge.stderr

    linked = tmp_path / "link.json"
    linked.symlink_to(plan_file)
    symlink = _run(
        "--parent", str(parent), "--plan-file", str(linked), "--json"
    )
    assert symlink.returncode == 2
    assert symlink.stdout == ""
    assert "symlink" in symlink.stderr

    directory_input = tmp_path / "dir-input"
    directory_input.mkdir()
    not_file = _run(
        "--parent", str(parent), "--plan-file", str(directory_input), "--json"
    )
    assert not_file.returncode == 2
    assert not_file.stdout == ""

    apply_linked = tmp_path / "apply-link.json"
    apply_linked.symlink_to(plan_file)
    apply_symlink = _run(
        "--parent",
        str(parent),
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
            path.write_text('{"schema_version": 2}\n', encoding="utf-8")

    original = reconcile_mod._after_input_path_lstat
    reconcile_mod._after_input_path_lstat = replace_after_lstat
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupReconcileError, match="changed|unsafe"
        ):
            snapshot_export_staging_cleanup_reconcile(parent, victim)
        proc = _run(
            "--parent", str(parent), "--plan-file", str(victim), "--json"
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
    finally:
        reconcile_mod._after_input_path_lstat = original

    truncated = tmp_path / "truncated.json"
    shutil.copyfile(plan_file, truncated)
    original_opened = reconcile_mod._after_input_opened

    def truncate_after_open(path: Path, _fd: int) -> None:
        if path == truncated:
            os.truncate(path, 8)

    reconcile_mod._after_input_opened = truncate_after_open
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupReconcileError, match="changed"
        ):
            snapshot_export_staging_cleanup_reconcile(parent, truncated)
    finally:
        reconcile_mod._after_input_opened = original_opened

    rewritten = tmp_path / "same-size.json"
    shutil.copyfile(plan_file, rewritten)
    original_text = rewritten.read_text(encoding="utf-8")
    replacement = original_text.replace("true", "tru0", 1)
    assert len(replacement.encode("utf-8")) == len(original_text.encode("utf-8"))

    def rewrite_same_size(path: Path) -> None:
        if path == rewritten:
            before = path.stat()
            path.write_text(replacement, encoding="utf-8")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    reconcile_mod._after_input_path_lstat = rewrite_same_size
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupReconcileError, match="changed|unsafe"
        ):
            snapshot_export_staging_cleanup_reconcile(parent, rewritten)
        proc = _run(
            "--parent", str(parent), "--plan-file", str(rewritten), "--json"
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
    finally:
        reconcile_mod._after_input_path_lstat = original


def test_complete_and_every_valid_partial_apply_result_boundary(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _candidate(parent, HEX_A)
    _candidate(parent, HEX_B)
    _candidate(parent, HEX_C)
    unrec = parent / ".graphrag-export-short"
    unrec.mkdir()
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert len(plan["deletion_candidates"]) == 3
    plan_file = _save_plan(tmp_path, plan)

    complete = snapshot_export_staging_cleanup_reconcile(
        parent, plan_file, _save_apply(tmp_path, _forge_complete(plan), "complete.json")
    )
    assert complete["declared_apply_outcome"] == "complete"
    assert complete["apply_result_valid"] is True

    for deleted_count in range(len(plan["deletion_candidates"])):
        forged = _forge_partial(plan, deleted_count)
        result = snapshot_export_staging_cleanup_reconcile(
            parent,
            plan_file,
            _save_apply(tmp_path, forged, f"partial-{deleted_count}.json"),
        )
        assert result["declared_apply_outcome"] == "partial"
        assert result["apply_result_valid"] is True

    skipped = _forge_partial(plan, 1)
    skipped["deleted_staging_entries"] = [plan["deletion_candidates"][1]]
    skipped["deleted_count"] = 1
    skipped["failed_staging_entry"] = plan["deletion_candidates"][2]
    skipped["not_attempted_staging_entries"] = []
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_save_apply(tmp_path, skipped, "skipped.json")),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    invented = _forge_complete(plan)
    invented["deleted_staging_entries"] = [*plan["deletion_candidates"], _staging_name(HEX_E)]
    invented["deleted_count"] = 4
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_save_apply(tmp_path, invented, "invented.json")),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    extra = _forge_complete(plan)
    extra["invented"] = "not emitted by the producer"
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_save_apply(tmp_path, extra, "extra.json")),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "producer fields" in proc.stderr


def test_apply_result_from_another_plan_or_parent(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    other = tmp_path / "other"
    other.mkdir()
    _candidate(other, HEX_B)
    other_plan = snapshot_export_staging_cleanup_plan(other)
    plan_file = _save_plan(tmp_path, plan)

    other_apply = _forge_complete(other_plan)
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_save_apply(tmp_path, other_apply, "other-apply.json")),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "another plan or parent" in proc.stderr

    wrong_parent = _forge_complete(plan)
    wrong_parent["parent"] = str(other.resolve())
    proc = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_save_apply(tmp_path, wrong_parent, "wrong-parent.json")),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""

    mismatch_parent = _run(
        "--parent",
        str(other),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert mismatch_parent.returncode == 1
    assert mismatch_parent.stdout == ""
    assert "does not match saved plan parent" in mismatch_parent.stderr


def test_no_result_complete_result_and_partial_result_reconciliation(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    first = _candidate(parent, HEX_A)
    second = _candidate(parent, HEX_C)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)

    none = snapshot_export_staging_cleanup_reconcile(parent, plan_file)
    _assert_reconcile_shape(none, parent, plan)
    assert none["declared_apply_outcome"] == "not_supplied"
    assert none["result_consistent_with_observation"] is None
    assert none["discrepancies"] == []
    assert none["all_planned_candidates_absent_at_reconcile"] is False

    applied = snapshot_export_staging_cleanup(
        parent, plan["plan_revision"], cleanup_confirmed=True
    )
    assert applied["ok"] is True
    complete = snapshot_export_staging_cleanup_reconcile(
        parent, plan_file, _save_apply(tmp_path, applied, "real-complete.json")
    )
    _assert_reconcile_shape(complete, parent, plan)
    assert complete["declared_apply_outcome"] == "complete"
    assert complete["all_planned_candidates_absent_at_reconcile"] is True
    assert complete["result_consistent_with_observation"] is True
    assert complete["discrepancies"] == []
    assert not first.exists()
    assert not second.exists()

    parent2 = tmp_path / "partial-parent"
    parent2.mkdir()
    keep = _candidate(parent2, HEX_A)
    later = _candidate(parent2, HEX_C)
    plan2 = snapshot_export_staging_cleanup_plan(parent2)
    import graphrag_code.snapshot_export_staging_cleanup as cleanup_mod

    original = cleanup_mod._remove_claimed_staging_entry
    calls = {"n": 0}

    def fail_later(observation, name, claim, entry):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(observation, name, claim, entry)
        raise RuntimeError(f"injected later failure on {name}")

    cleanup_mod._remove_claimed_staging_entry = fail_later
    try:
        partial_apply = snapshot_export_staging_cleanup(
            parent2, plan2["plan_revision"], cleanup_confirmed=True
        )
    finally:
        cleanup_mod._remove_claimed_staging_entry = original
    assert partial_apply["partial"] is True
    partial = snapshot_export_staging_cleanup_reconcile(
        parent2,
        _save_plan(tmp_path, plan2, "plan2.json"),
        _save_apply(tmp_path, partial_apply, "partial.json"),
    )
    _assert_reconcile_shape(partial, parent2, plan2)
    assert partial["declared_apply_outcome"] == "partial"
    assert _obs(partial, keep.name)["state"] == "absent_at_reconcile"
    assert _obs(partial, later.name)["state"] == "present_candidate_at_reconcile"
    assert later.is_dir()


def test_candidate_states_and_identity_change(tmp_path: Path):
    parent = tmp_path / "states"
    parent.mkdir()
    absent = _candidate(parent, HEX_A)
    still = _candidate(parent, HEX_B)
    blocked = _candidate(parent, HEX_C)
    nondir = _candidate(parent, HEX_D)
    linked = _candidate(parent, HEX_E)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)

    shutil.rmtree(absent)
    lock = blocked / EXPORT_STAGING_WRITER_LOCK_NAME
    lock.chmod(0o644)
    shutil.rmtree(nondir)
    (parent / nondir.name).write_bytes(b"file-now")
    target = tmp_path / "outside"
    target.mkdir()
    shutil.rmtree(linked)
    (parent / linked.name).symlink_to(target, target_is_directory=True)
    before = still.stat()
    os.utime(still, ns=(before.st_atime_ns, before.st_mtime_ns + 1))

    result = snapshot_export_staging_cleanup_reconcile(parent, plan_file)
    _assert_reconcile_shape(result, parent, plan)
    assert _obs(result, absent.name)["state"] == "absent_at_reconcile"
    assert _obs(result, absent.name)["identity_matches_saved_observation"] is None
    assert _obs(result, still.name)["state"] == "present_candidate_at_reconcile"
    assert _obs(result, still.name)["identity_matches_saved_observation"] is False
    assert _obs(result, blocked.name)["state"] == "present_blocked_at_reconcile"
    assert _obs(result, blocked.name)["blocking_reason"] == "permissive_writer_lease_metadata"
    assert _obs(result, nondir.name)["state"] == "present_non_directory_at_reconcile"
    assert _obs(result, linked.name)["state"] == "unsafe_symlink_at_reconcile"
    assert result["ok"] is True


def test_saved_blocked_entries_changing_state(tmp_path: Path):
    parent = tmp_path / "blocked"
    parent.mkdir()
    missing_lock = _make_staging(parent, HEX_A)
    candidate = _candidate(parent, HEX_B)
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert missing_lock.name in {item["name"] for item in plan["blocked_entries"]}
    _write_candidate_lock(missing_lock)
    shutil.rmtree(candidate)
    result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan)
    )
    _assert_reconcile_shape(result, parent, plan)
    assert _obs(result, missing_lock.name)["state"] == "present_candidate_at_reconcile"
    assert _obs(result, candidate.name)["state"] == "absent_at_reconcile"


def test_new_recognized_and_unrecognized_prefixed_entries(tmp_path: Path):
    parent = tmp_path / "new"
    parent.mkdir()
    original = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    fresh = _candidate(parent, HEX_C)
    unrec = parent / ".graphrag-export-zzz"
    unrec.mkdir()
    result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan)
    )
    _assert_reconcile_shape(result, parent, plan)
    assert result["new_prefixed_entries"] == sorted(
        [fresh.name, unrec.name], key=os.fsencode
    )
    assert original.name not in result["new_prefixed_entries"]
    assert result["result_consistent_with_observation"] is None

    complete = _forge_complete(plan)
    with_result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan, "plan2.json"), _save_apply(tmp_path, complete)
    )
    assert with_result["result_consistent_with_observation"] is False
    codes = {item["code"] for item in with_result["discrepancies"]}
    assert "new_prefixed_entry" in codes
    assert "declared_deleted_but_present" in codes


def test_live_held_writer_lease_remains_observation_only(tmp_path: Path):
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
        saved = snapshot_export_staging_cleanup_plan(dest.parent)
        started = time.monotonic()
        result = snapshot_export_staging_cleanup_reconcile(
            dest.parent, _save_plan(tmp_path, saved)
        )
        assert time.monotonic() - started < 5
        _assert_reconcile_shape(result, dest.parent, saved)
        assert result["candidate_observations"] == []
        assert _obs(result, leftovers[0].name)["state"] == "present_blocked_at_reconcile"
        assert _obs(result, leftovers[0].name)["blocking_reason"] == "held_writer_lease"
        assert leftovers[0].is_dir()
        resume.set()
        proc.join(timeout=TIMEOUT)
        assert not proc.is_alive()
        message = q.get(timeout=TIMEOUT)
        assert message[0] == "ok"
        assert dest.is_dir()
        assert not (dest / EXPORT_STAGING_WRITER_LOCK_NAME).exists()
    finally:
        _cleanup_processes(proc, release=resume)


def test_result_consistency_and_discrepancy_codes(tmp_path: Path):
    parent = tmp_path / "disc"
    parent.mkdir()
    deleted_but_present = _candidate(parent, HEX_A)
    remaining_dir = _make_staging(parent, HEX_B)
    plan = snapshot_export_staging_cleanup_plan(parent)
    assert deleted_but_present.name in plan["deletion_candidates"]
    assert remaining_dir.name in {item["name"] for item in plan["blocked_entries"]}

    complete = _forge_complete(plan)
    present = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan), _save_apply(tmp_path, complete)
    )
    codes = {item["code"] for item in present["discrepancies"]}
    assert "declared_deleted_but_present" in codes
    assert present["result_consistent_with_observation"] is False

    shutil.rmtree(remaining_dir)
    (parent / remaining_dir.name).write_bytes(b"now-file")
    changed = snapshot_export_staging_cleanup_reconcile(
        parent,
        _save_plan(tmp_path, plan, "plan2.json"),
        _save_apply(tmp_path, complete, "complete2.json"),
    )
    codes = {item["code"] for item in changed["discrepancies"]}
    assert "declared_remaining_but_non_directory" in codes

    shutil.rmtree(parent / remaining_dir.name, ignore_errors=True)
    if (parent / remaining_dir.name).is_file():
        (parent / remaining_dir.name).unlink()
    absent_remaining = snapshot_export_staging_cleanup_reconcile(
        parent,
        _save_plan(tmp_path, plan, "plan3.json"),
        _save_apply(tmp_path, complete, "complete3.json"),
    )
    codes = {item["code"] for item in absent_remaining["discrepancies"]}
    assert "declared_remaining_but_absent" in codes

    restored = _make_staging(parent, HEX_B)
    _write_candidate_lock(restored)
    identity = snapshot_export_staging_cleanup_reconcile(
        parent,
        _save_plan(tmp_path, plan, "plan4.json"),
        _save_apply(tmp_path, complete, "complete4.json"),
    )
    codes = {item["code"] for item in identity["discrepancies"]}
    assert "declared_remaining_but_identity_changed" in codes


def test_parent_replacement_and_rename_away_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "held"
    parent.mkdir()
    original = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)
    replacement = tmp_path / "after-replacement"
    replacement.mkdir()
    planted = _candidate(replacement, HEX_B)
    payload = planted / "manifest.json"
    hidden = tmp_path / "held-hidden"

    def replace_after_open(path, parent_fd):
        if path == parent.resolve():
            os.fstat(parent_fd)
            parent.rename(hidden)
            parent.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(staging_mod, "_after_parent_opened", replace_after_open)
    with pytest.raises(
        SnapshotExportStagingCleanupReconcileIntegrityError,
        match="changed|symlink|unsafe",
    ):
        snapshot_export_staging_cleanup_reconcile(parent, plan_file)
    assert payload.read_bytes() == b"payload"
    assert (hidden / original.name).is_dir()
    assert parent.is_symlink()

    monkeypatch.setattr(staging_mod, "_after_parent_opened", lambda *_: None)
    bounced = tmp_path / "bounced"
    bounced.mkdir()
    leftover = _candidate(bounced, HEX_A)
    bounced_plan = snapshot_export_staging_cleanup_plan(bounced)
    bounced_file = _save_plan(tmp_path, bounced_plan, "bounced.json")
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
        SnapshotExportStagingCleanupReconcileIntegrityError,
        match="parent identity|parent changed",
    ):
        snapshot_export_staging_cleanup_reconcile(bounced, bounced_file)
    assert leftover.is_dir()


def test_scan_to_scan_listing_identity_metadata_and_lease_state_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod
    from graphrag_code.byog_graph import _release_lock, _try_acquire_exclusive_lock

    parent = tmp_path / "delta"
    parent.mkdir()
    staging = _candidate(parent, HEX_A)
    lock = staging / EXPORT_STAGING_WRITER_LOCK_NAME
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)

    def chmod_lock(_path, _scan):
        lock.chmod(0o640)

    monkeypatch.setattr(staging_mod, "_after_first_scan", chmod_lock)
    with pytest.raises(
        SnapshotExportStagingCleanupReconcileIntegrityError,
        match="writer-lease identity|entry metadata|entry identity",
    ):
        snapshot_export_staging_cleanup_reconcile(parent, plan_file)
    lock.chmod(0o600)

    def add_listing(_path, _scan):
        extra = parent / _staging_name(HEX_B)
        if not extra.exists():
            extra.mkdir()
            _write_candidate_lock(extra)

    monkeypatch.setattr(staging_mod, "_after_first_scan", add_listing)
    with pytest.raises(
        SnapshotExportStagingCleanupReconcileIntegrityError, match="listing"
    ):
        snapshot_export_staging_cleanup_reconcile(parent, plan_file)

    extra = parent / _staging_name(HEX_B)
    if extra.exists():
        shutil.rmtree(extra)

    holder = {"fd": None, "backend": None}

    def take_lease(_path, _scan):
        fd = os.open(str(lock), os.O_RDONLY | os.O_NOFOLLOW)
        holder["fd"] = fd
        holder["backend"] = _try_acquire_exclusive_lock(fd)

    monkeypatch.setattr(staging_mod, "_after_first_scan", take_lease)
    try:
        with pytest.raises(
            SnapshotExportStagingCleanupReconcileIntegrityError,
            match="writer-lease state",
        ):
            snapshot_export_staging_cleanup_reconcile(parent, plan_file)
    finally:
        if holder["fd"] is not None:
            if holder["backend"] is not None:
                _release_lock(holder["fd"], holder["backend"])
            os.close(holder["fd"])


def test_raw_filesystem_byte_ordering(tmp_path: Path):
    parent = tmp_path / "order"
    parent.mkdir()
    later = _candidate(parent, HEX_C)
    earlier = _candidate(parent, HEX_A)
    mid = _candidate(parent, HEX_B)
    unrec_b = parent / ".graphrag-export-bbb"
    unrec_a = parent / ".graphrag-export-aaa"
    unrec_b.mkdir()
    unrec_a.mkdir()
    plan = snapshot_export_staging_cleanup_plan(parent)
    result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan)
    )
    expected = sorted([later.name, earlier.name, mid.name], key=os.fsencode)
    assert [item["name"] for item in result["candidate_observations"]] == expected
    assert [item["name"] for item in result["blocked_observations"]] == sorted(
        [unrec_a.name, unrec_b.name], key=os.fsencode
    )
    dumped = result_to_json(result)
    parsed = json.loads(dumped)
    assert list(parsed) == sorted(parsed)
    assert dumped.endswith("\n")


def test_no_payload_byte_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_export_staging as staging_mod
    import graphrag_code.snapshot_export_writer_lease as lease_mod

    parent = tmp_path / "payloads"
    parent.mkdir()
    staging = _candidate(parent, HEX_A, payload=b"unique-payload-bytes-not-for-reconcile")
    plan = snapshot_export_staging_cleanup_plan(parent)
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
    result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan)
    )
    assert opened == []
    dumped = result_to_json(result)
    assert "unique-payload-bytes-not-for-reconcile" not in dumped
    assert (staging / "manifest.json").read_bytes() == (
        b"unique-payload-bytes-not-for-reconcile"
    )


def test_no_graph_inspection_writer_claim_or_mutation(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A, payload=b"keep-payload")
    (parent / "current").write_text("not-a-graph\n", encoding="utf-8")
    (parent / "snapshots").mkdir()
    listing_before = sorted(path.name for path in parent.iterdir())
    payload_before = (leftover / "manifest.json").stat()
    lock_before = (leftover / EXPORT_STAGING_WRITER_LOCK_NAME).stat()
    plan = snapshot_export_staging_cleanup_plan(parent)
    result = snapshot_export_staging_cleanup_reconcile(
        parent, _save_plan(tmp_path, plan)
    )
    assert result["ok"] is True
    assert leftover.is_dir()
    assert (leftover / "manifest.json").read_bytes() == b"keep-payload"
    assert (leftover / "manifest.json").stat().st_mtime_ns == payload_before.st_mtime_ns
    assert (leftover / EXPORT_STAGING_WRITER_LOCK_NAME).stat().st_ino == lock_before.st_ino
    assert sorted(path.name for path in parent.iterdir()) == listing_before
    assert (parent / "current").read_text(encoding="utf-8") == "not-a-graph\n"


def test_descriptors_remain_held_through_json_human_stdout_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod
    import graphrag_code.snapshot_export_staging_cleanup_reconcile as reconcile_mod

    parent = tmp_path / "held"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)
    original_json = reconcile_mod.result_to_json
    original_format = reconcile_mod.format_result
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
    monkeypatch.setattr(reconcile_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(reconcile_mod, "format_result", guarded_format)
    monkeypatch.setattr(reconcile_mod.sys, "stdout", GuardedStdout())
    assert reconcile_mod.main(
        ["--parent", str(parent), "--plan-file", str(plan_file), "--json"]
    ) == 0
    assert reconcile_mod.main(
        ["--parent", str(parent), "--plan-file", str(plan_file)]
    ) == 0
    assert state["writes"] >= 2
    assert state["flushes"] == 2
    assert leftover.is_dir()


def test_exit_code_and_empty_stdout_contract(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    leftover = _candidate(parent, HEX_A)
    plan = snapshot_export_staging_cleanup_plan(parent)
    plan_file = _save_plan(tmp_path, plan)
    ok = _run("--parent", str(parent), "--plan-file", str(plan_file), "--json")
    assert ok.returncode == 0
    body = json.loads(ok.stdout)
    assert body["ok"] is True
    assert leftover.is_dir()

    missing_parent = _run(
        "--parent",
        str(tmp_path / "missing"),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert missing_parent.returncode == 2
    assert missing_parent.stdout == ""

    other = tmp_path / "other"
    other.mkdir()
    mismatch = _run(
        "--parent", str(other), "--plan-file", str(plan_file), "--json"
    )
    assert mismatch.returncode == 1
    assert mismatch.stdout == ""

    extra = _run(
        "--parent",
        str(parent),
        "--plan-file",
        str(plan_file),
        "--cleanup-confirmed",
        "--json",
    )
    assert extra.returncode == 2
    assert extra.stdout == ""


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
            if isinstance(node.func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert (imported | called) & FORBIDDEN == set()
    assert "graph_read_lease" not in source
    assert "graph_exclusive_lease" not in source
    assert "snapshot_export_staging(" not in source
    assert "snapshot_export_staging_cleanup(" not in source
    assert "snapshot_export_staging_cleanup_plan(" not in source
    assert "claim_existing_export_writer_lease" not in source
    assert "acquire_export_writer_lease" not in source
    assert "export_staging_observation_scope" in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    human = format_result(
        {
            "parent": "/tmp/x",
            "input_plan_revision": "sha256:" + ("00" * 32),
            "apply_result_supplied": False,
            "declared_apply_outcome": "not_supplied",
            "all_planned_candidates_absent_at_reconcile": False,
            "result_consistent_with_observation": None,
            "discrepancies": [],
            "ok": True,
        }
    )
    assert "reconciliation_is_observation_only=true" in human
    assert "deletion_cause_proven=false" in human
    assert "fresh_plan_required_before_mutation=true" in human


def test_mcp_remains_exactly_fifteen_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 15
    assert "snapshot_export_staging_cleanup_reconcile" not in TOOL_NAMES
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
            assert "snapshot_export_staging_cleanup_reconcile" not in names

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
