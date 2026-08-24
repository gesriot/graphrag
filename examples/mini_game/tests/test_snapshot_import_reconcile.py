"""Read-only snapshot import reconciliation.

Disposable tmp graphs and export dirs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_import_reconcile.py -q
"""
from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import shutil
import socket
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
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_plan import (  # type: ignore
    ACCEPTED_PAYLOAD_FILES,
    HASH_CHUNK_BYTES,
    REQUIRED_PAYLOAD_FILES,
    export_revision_of,
)
from graphrag_code.snapshot_import_apply import (  # type: ignore
    SnapshotImportApplyError,
    result_to_json as apply_to_json,
    snapshot_import_apply,
)
from graphrag_code.snapshot_import_plan import (  # type: ignore
    import_revision_of,
    result_to_json as plan_to_json,
    snapshot_import_plan,
)
from graphrag_code.snapshot_import_reconcile import (  # type: ignore
    MAX_INPUT_BYTES,
    SnapshotImportReconcileError,
    SnapshotImportReconcileIntegrityError,
    format_result,
    result_to_json,
    snapshot_import_reconcile,
)
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore

SCRIPT = ROOT / "scripts" / "snapshot_import_reconcile.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_import_reconcile.py"
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
        "snapshot_import_apply",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
REQUIRED_RESULT_KEYS = (
    "schema_version",
    "ok",
    "graph",
    "plan_file",
    "apply_result_file",
    "input_import_revision",
    "source_export_revision",
    "snapshot_id",
    "planned_files",
    "file_count",
    "total_size_bytes",
    "apply_result_supplied",
    "apply_result_valid",
    "declared_apply_outcome",
    "current",
    "current_matches_plan",
    "snapshot_active",
    "published_snapshots",
    "published_count",
    "published_snapshot_present",
    "published_snapshot_state",
    "observed_snapshot_export_revision",
    "snapshot_matches_plan",
    "target_staging_name",
    "target_staging_present",
    "published_history_matches_plan_plus_target",
    "creation_cause_proven",
    "recovery_performed",
    "graph_mutated",
    "export_observed",
    "export_mutated",
    "activation_performed",
    "retention_performed",
    "fresh_plan_required_before_import",
    "notices",
)
NOTICE_CODES = (
    "reconciliation_is_observation_only",
    "absence_does_not_prove_apply_failed",
    "presence_does_not_prove_apply_created",
    "revision_equality_is_observation_window_only",
    "staging_presence_does_not_prove_apply_left_it",
    "staging_absence_does_not_prove_apply_cleaned_it",
    "snapshot_active_is_not_activation",
    "saved_apply_result_is_declaration_only",
    "source_export_not_observed",
    "fresh_plan_required_before_import",
    "no_recovery_performed",
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
        settings_text=f"import-reconcile: {marker}\n",
        keep_last=10,
        call_observations_df=pd.DataFrame(obs) if obs is not None else None,
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
    export_dir = _copy_standalone(live, tmp_path / "export")
    plan = snapshot_import_plan(target, export_dir)
    return source, target, live, dest_live, export_dir, plan


def _assert_reconcile_shape(
    result: dict,
    plan: dict,
    graph: Path,
    plan_file: Path,
    *,
    apply_supplied: bool,
    declared_outcome: str,
    snapshot_state: str,
    snapshot_matches_plan: bool,
    snapshot_present: bool,
    apply_file: Path | None = None,
) -> None:
    for key in REQUIRED_RESULT_KEYS:
        assert key in result
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["graph"] == str(graph.resolve())
    assert result["plan_file"] == str(plan_file)
    assert result["apply_result_file"] == (None if apply_file is None else str(apply_file))
    assert result["input_import_revision"] == plan["import_revision"]
    assert result["source_export_revision"] == plan["source_export_revision"]
    assert result["snapshot_id"] == plan["snapshot_id"]
    assert result["planned_files"] == plan["files"]
    assert result["file_count"] == plan["file_count"]
    assert result["total_size_bytes"] == plan["total_size_bytes"]
    assert result["apply_result_supplied"] is apply_supplied
    assert result["apply_result_valid"] is apply_supplied
    assert result["declared_apply_outcome"] == declared_outcome
    assert result["published_snapshot_state"] == snapshot_state
    assert result["published_snapshot_present"] is snapshot_present
    assert result["snapshot_matches_plan"] is snapshot_matches_plan
    assert result["creation_cause_proven"] is False
    assert result["recovery_performed"] is False
    assert result["graph_mutated"] is False
    assert result["export_observed"] is False
    assert result["export_mutated"] is False
    assert result["activation_performed"] is False
    assert result["retention_performed"] is False
    assert result["fresh_plan_required_before_import"] is True
    assert result["published_snapshots"] == sorted(
        result["published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["published_count"] == len(result["published_snapshots"])
    assert result["current"] in result["published_snapshots"]
    assert result["snapshot_active"] is (result["current"] == plan["snapshot_id"])
    assert result["current_matches_plan"] is (result["current"] == plan["current"])
    assert result["target_staging_name"] == plan["target_staging_name"]
    expected_history = set(plan["published_snapshots"]) | {plan["snapshot_id"]}
    assert result["published_history_matches_plan_plus_target"] is (
        set(result["published_snapshots"]) == expected_history
    )
    if snapshot_state == "absent":
        assert result["observed_snapshot_export_revision"] is None
        assert result["snapshot_matches_plan"] is False
        assert result["published_snapshot_present"] is False
    elif snapshot_state == "matches_plan":
        assert result["observed_snapshot_export_revision"] == plan["source_export_revision"]
        assert result["snapshot_matches_plan"] is True
    else:
        assert result["observed_snapshot_export_revision"] != plan["source_export_revision"]
        assert result["snapshot_matches_plan"] is False
        assert result["observed_snapshot_export_revision"] is not None
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == list(NOTICE_CODES)
    text = format_result(result)
    assert str(graph.resolve()) in text
    assert plan["snapshot_id"] in text
    assert f"published_snapshot_state={snapshot_state}" in text
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
    _source, target, live, _dest, export_dir, plan = _prepare(here)
    plan_file = _save_plan(here, plan, "plan.json")
    args = ["--graph", "target", "--plan-file", "plan.json", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_import_reconcile", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-import-reconcile", *args],
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
            "snapshot-import-reconcile",
            "--graph",
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
    assert json.loads(installed.stdout)["input_import_revision"] == plan["import_revision"]

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_import_reconcile.py" in names
    help_proc = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "snapshot-import-reconcile" in help_proc.stdout


def test_valid_saved_plan_without_apply_result_target_absent(tmp_path: Path):
    _source, target, live, dest_live, _export, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    before = _protected_state(target)
    result = snapshot_import_reconcile(target, plan_file)
    _assert_reconcile_shape(
        result,
        plan,
        target,
        plan_file,
        apply_supplied=False,
        declared_outcome="not_supplied",
        snapshot_state="absent",
        snapshot_matches_plan=False,
        snapshot_present=False,
    )
    assert result["current"] == dest_live.name
    assert result["snapshot_active"] is False
    assert result["target_staging_present"] is False
    assert result["published_history_matches_plan_plus_target"] is False
    assert live.name not in result["published_snapshots"]
    assert _protected_state(target) == before
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["published_snapshot_state"] == "absent"
    assert body["ok"] is True
    assert result_to_json(body) == proc.stdout


def test_complete_apply_result_matching_snapshot_and_history(tmp_path: Path):
    _source, target, live, dest_live, export_dir, plan = _prepare(
        tmp_path, observations=True
    )
    applied = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert applied["ok"] is True
    plan_file = _save_plan(tmp_path, plan)
    apply_file = _save_apply(tmp_path, applied)
    before = _protected_state(target)
    result = snapshot_import_reconcile(target, plan_file, apply_file)
    _assert_reconcile_shape(
        result,
        plan,
        target,
        plan_file,
        apply_supplied=True,
        declared_outcome="complete",
        snapshot_state="matches_plan",
        snapshot_matches_plan=True,
        snapshot_present=True,
        apply_file=apply_file,
    )
    assert result["current"] == dest_live.name == plan["current"]
    assert result["current_matches_plan"] is True
    assert result["snapshot_active"] is False
    assert result["target_staging_present"] is False
    assert result["published_history_matches_plan_plus_target"] is True
    assert live.name in result["published_snapshots"]
    assert result["observed_snapshot_export_revision"] == plan["source_export_revision"]
    assert _protected_state(target) == before


def test_pre_and_post_publication_partial_saved_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_import_apply as apply_mod

    _s, target, live, _d, export_dir, plan = _prepare(tmp_path)

    def boom_write(_fd, _data):
        raise OSError(errno.EIO, "injected write failure")

    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    pre = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert pre["ok"] is False
    assert pre["partial"] is True
    assert pre["import_performed"] is False
    for leftover in (target / "snapshots").iterdir():
        if leftover.name.startswith(STAGING_NAME_PREFIX):
            shutil.rmtree(leftover)
    monkeypatch.setattr(apply_mod, "_write_chunk", apply_mod.os.write)
    plan_file = _save_plan(tmp_path, plan, "pre-plan.json")
    pre_file = _save_apply(tmp_path, pre, "pre.json")
    result = snapshot_import_reconcile(target, plan_file, pre_file)
    _assert_reconcile_shape(
        result,
        plan,
        target,
        plan_file,
        apply_supplied=True,
        declared_outcome="pre_publication_partial",
        snapshot_state="absent",
        snapshot_matches_plan=False,
        snapshot_present=False,
        apply_file=pre_file,
    )

    post_case = tmp_path / "post"
    _s2, target_p, live_p, _d2, export_p, plan_p = _prepare(post_case)

    def boom_verify(*_args, **_kwargs):
        raise SnapshotImportApplyError("injected post-publication verification failure")

    monkeypatch.setattr(apply_mod, "_after_import_apply_published", boom_verify)
    post = snapshot_import_apply(
        target_p, export_p, plan_p["import_revision"], import_confirmed=True
    )
    assert post["ok"] is False
    assert post["partial"] is True
    assert post["publication_performed"] is True
    post_plan = _save_plan(post_case, plan_p, "post-plan.json")
    post_file = _save_apply(post_case, post, "post.json")
    result = snapshot_import_reconcile(target_p, post_plan, post_file)
    _assert_reconcile_shape(
        result,
        plan_p,
        target_p,
        post_plan,
        apply_supplied=True,
        declared_outcome="post_publication_partial",
        snapshot_state="matches_plan",
        snapshot_matches_plan=True,
        snapshot_present=True,
        apply_file=post_file,
    )
    assert (target_p / "snapshots" / live_p.name).is_dir()


def test_stable_valid_revision_mismatch_and_snapshot_active(tmp_path: Path):
    _source, target, live, dest_live, export_dir, plan = _prepare(tmp_path)
    snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    published = target / "snapshots" / live.name
    settings = published / "settings.yaml"
    original = settings.read_bytes()
    settings.write_bytes(original + b"# mismatch\n")
    plan_file = _save_plan(tmp_path, plan)
    result = snapshot_import_reconcile(target, plan_file)
    _assert_reconcile_shape(
        result,
        plan,
        target,
        plan_file,
        apply_supplied=False,
        declared_outcome="not_supplied",
        snapshot_state="revision_mismatch",
        snapshot_matches_plan=False,
        snapshot_present=True,
    )
    assert result["ok"] is True
    assert result["snapshot_active"] is False
    (target / "current").write_text(live.name + "\n", encoding="utf-8")
    active = snapshot_import_reconcile(target, plan_file)
    assert active["snapshot_active"] is True
    assert active["current"] == live.name
    assert active["current_matches_plan"] is False
    assert active["ok"] is True


def test_current_differs_and_other_history_changes(tmp_path: Path):
    _source, target, live, dest_live, export_dir, plan = _prepare(tmp_path)
    snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    extra = _publish(target, "extra")
    plan_file = _save_plan(tmp_path, plan)
    result = snapshot_import_reconcile(target, plan_file)
    assert result["ok"] is True
    assert result["current"] == extra.name
    assert result["current_matches_plan"] is False
    assert result["published_history_matches_plan_plus_target"] is False
    assert extra.name in result["published_snapshots"]
    assert live.name in result["published_snapshots"]
    assert dest_live.name in result["published_snapshots"]


def test_exact_staging_present_and_absent(tmp_path: Path):
    _source, target, live, _dest, _export, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    absent = snapshot_import_reconcile(target, plan_file)
    assert absent["target_staging_present"] is False
    staging = target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}"
    staging.mkdir()
    present = snapshot_import_reconcile(target, plan_file)
    assert present["target_staging_present"] is True
    assert present["published_snapshot_state"] == "absent"
    assert present["ok"] is True
    shutil.rmtree(staging)
    gone = snapshot_import_reconcile(target, plan_file)
    assert gone["target_staging_present"] is False


def test_invalid_tampered_and_contradictory_plan_fields(tmp_path: Path):
    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    _source, target, live, _dest, _export, plan = _prepare(tmp_path)
    dest = tmp_path / "ignored"

    revision = json.loads(plan_to_json(plan))
    revision["source_export_revision"] = "sha256:" + ("ab" * 32)
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "src-rev.json", revision)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "source_export_revision" in proc.stderr

    import_rev = json.loads(plan_to_json(plan))
    import_rev["import_revision"] = "sha256:" + ("cd" * 32)
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "imp-rev.json", import_rev)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "import_revision" in proc.stderr

    files = json.loads(plan_to_json(plan))
    files["files"] = list(reversed(files["files"]))
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "order.json", files)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    counts = json.loads(plan_to_json(plan))
    counts["file_count"] = counts["file_count"] + 1
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "count.json", counts)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    sizes = json.loads(plan_to_json(plan))
    sizes["total_size_bytes"] = sizes["total_size_bytes"] + 1
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "size.json", sizes)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    mutated = json.loads(plan_to_json(plan))
    mutated["files"][0]["content_revision"] = "sha256:" + ("ef" * 32)
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "hash.json", mutated)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    performed = json.loads(plan_to_json(plan))
    performed["import_performed"] = True
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "performed.json", performed)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    ready = json.loads(plan_to_json(plan))
    ready["import_ready"] = False
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "ready.json", ready)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert dest.exists() is False

    staging_bound = json.loads(plan_to_json(plan))
    staging_bound["staging_count"] = reconcile_mod.MAX_STAGING_ENTRIES + 1
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "staging-bound.json", staging_bound)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "staging_count" in proc.stderr

    history_bound = json.loads(plan_to_json(plan))
    history_bound["published_snapshots"] = [
        f"19990101-{index:06d}-bound"
        for index in range(reconcile_mod.MAX_PUBLISHED_SNAPSHOTS + 1)
    ]
    history_bound["current"] = history_bound["published_snapshots"][0]
    history_bound["published_count"] = len(history_bound["published_snapshots"])
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_write_json(tmp_path / "history-bound.json", history_bound)),
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "published snapshot count" in proc.stderr


def test_impossible_apply_outcome_flags_and_plan_mismatches(tmp_path: Path):
    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    applied = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    cases = [
        {"ok": True, "partial": True},
        {"ok": False, "partial": False},
        {"import_confirmed": False},
        {"import_performed": False, "ok": True, "partial": False},
        {"publication_performed": False, "ok": True, "partial": False},
        {"staging_created": False},
        {"export_mutated": True},
        {"activation_performed": True},
        {"retention_performed": True},
        {"error": "unexpected"},
        {
            "ok": False,
            "partial": True,
            "import_performed": False,
            "publication_performed": False,
            "snapshot_verified_after_publication": False,
            "current_after": None,
            "current_unchanged": False,
            "snapshots_fsync_confirmed": False,
            "retry_requires_fresh_plan": True,
            "error": "pre",
            "staging_remaining": False,
            "staging_cleanup_attempted": False,
        },
    ]
    for index, updates in enumerate(cases):
        forged = json.loads(apply_to_json(applied))
        forged.update(updates)
        proc = _run(
            "--graph",
            str(target),
            "--plan-file",
            str(plan_file),
            "--apply-result-file",
            str(_write_json(tmp_path / f"impossible-{index}.json", forged)),
            "--json",
        )
        assert proc.returncode == 2, updates
        assert proc.stdout == ""

    other_graph = tmp_path / "other"
    _publish(other_graph, "other")
    other_plan = snapshot_import_plan(
        other_graph, _copy_standalone(live, tmp_path / "other-export")
    )
    mismatch_apply = json.loads(apply_to_json(applied))
    mismatch_apply["graph"] = other_plan["graph"]
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_write_json(tmp_path / "other-graph.json", mismatch_apply)),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""

    other_id = json.loads(apply_to_json(applied))
    other_id["snapshot_id"] = _current(target)
    proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(_write_json(tmp_path / "other-id.json", other_id)),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""

    blocked_plan = snapshot_import_plan(target, export_dir)
    assert blocked_plan["import_ready"] is False
    forged_complete = json.loads(apply_to_json(applied))
    forged_complete["graph"] = blocked_plan["graph"]
    forged_complete["export_directory"] = blocked_plan["export_directory"]
    forged_complete["snapshot_id"] = blocked_plan["snapshot_id"]
    forged_complete["expected_import_revision"] = blocked_plan["import_revision"]
    forged_complete["observed_import_revision"] = blocked_plan["import_revision"]
    forged_complete["source_export_revision"] = blocked_plan["source_export_revision"]
    forged_complete["planned_files"] = blocked_plan["files"]
    forged_complete["file_count"] = blocked_plan["file_count"]
    forged_complete["total_size_bytes"] = blocked_plan["total_size_bytes"]
    forged_complete["current_before"] = blocked_plan["current"]
    blocked_proc = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(_save_plan(tmp_path, blocked_plan, "blocked.json")),
        "--apply-result-file",
        str(_write_json(tmp_path / "blocked-apply.json", forged_complete)),
        "--json",
    )
    assert blocked_proc.returncode == 1
    assert blocked_proc.stdout == ""
    assert "blocked" in blocked_proc.stderr or "impossible" in blocked_proc.stderr


def test_graph_plan_mismatch_is_integrity_error(tmp_path: Path):
    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    other = tmp_path / "other"
    _publish(other, "other")
    plan_file = _save_plan(tmp_path, plan)
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="does not match"):
        snapshot_import_reconcile(other, plan_file)
    proc = _run(
        "--graph",
        str(other),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_relative_paths_and_invalid_inputs_before_graph_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    here = tmp_path / "here"
    here.mkdir()
    _source, target, live, _dest, _export, plan = _prepare(here)
    plan_file = _save_plan(here, plan, "plan.json")
    proc = _run(
        "--graph",
        "target",
        "--plan-file",
        "plan.json",
        "--json",
        cwd=here,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["graph"] == str(target.resolve())
    assert body["plan_file"].endswith("plan.json")

    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")

    def graph_inspection_is_forbidden(*_args, **_kwargs):
        raise AssertionError("graph was inspected before input validation")

    monkeypatch.setattr(
        reconcile_mod, "_resolve_graph_root", graph_inspection_is_forbidden
    )
    monkeypatch.setattr(
        reconcile_mod, "_require_managed_graph", graph_inspection_is_forbidden
    )
    with pytest.raises(SnapshotImportReconcileError, match="valid JSON"):
        snapshot_import_reconcile(target, malformed)
    monkeypatch.setattr(
        reconcile_mod, "_resolve_graph_root", reconcile_mod._resolve_graph_root
    )


def test_oversized_symlinked_truncated_replaced_and_same_size_rewritten_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _source, target, live, _dest, _export, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)

    missing = _run("--plan-file", str(plan_file), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not-json", encoding="utf-8")
    bad = _run("--graph", str(target), "--plan-file", str(malformed), "--json")
    assert bad.returncode == 2
    assert bad.stdout == ""

    oversized = tmp_path / "huge.json"
    oversized.write_bytes(b"{" + b"a" * (MAX_INPUT_BYTES + 1) + b"}")
    huge = _run("--graph", str(target), "--plan-file", str(oversized), "--json")
    assert huge.returncode == 2
    assert huge.stdout == ""
    assert str(MAX_INPUT_BYTES) in huge.stderr

    linked = tmp_path / "link.json"
    linked.symlink_to(plan_file)
    symlink = _run("--graph", str(target), "--plan-file", str(linked), "--json")
    assert symlink.returncode == 2
    assert symlink.stdout == ""
    assert "symlink" in symlink.stderr

    directory_input = tmp_path / "dir-input"
    directory_input.mkdir()
    not_file = _run(
        "--graph", str(target), "--plan-file", str(directory_input), "--json"
    )
    assert not_file.returncode == 2
    assert not_file.stdout == ""

    apply_linked = tmp_path / "apply-link.json"
    apply_linked.symlink_to(plan_file)
    apply_symlink = _run(
        "--graph",
        str(target),
        "--plan-file",
        str(plan_file),
        "--apply-result-file",
        str(apply_linked),
        "--json",
    )
    assert apply_symlink.returncode == 2
    assert apply_symlink.stdout == ""

    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    victim = tmp_path / "replaced.json"
    shutil.copyfile(plan_file, victim)
    original_lstat_hook = reconcile_mod._after_input_path_lstat

    def replace_after_lstat(path: Path) -> None:
        if path == victim:
            path.unlink()
            path.write_text('{"schema_version": 1}\n', encoding="utf-8")

    monkeypatch.setattr(reconcile_mod, "_after_input_path_lstat", replace_after_lstat)
    with pytest.raises(SnapshotImportReconcileError, match="changed|unsafe"):
        snapshot_import_reconcile(target, victim)
    proc = _run("--graph", str(target), "--plan-file", str(victim), "--json")
    assert proc.returncode == 2
    assert proc.stdout == ""
    monkeypatch.setattr(reconcile_mod, "_after_input_path_lstat", original_lstat_hook)

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
                if len(rewritten) != len(original):
                    rewritten = original
            same.write_bytes(rewritten)
            os.utime(same, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(reconcile_mod, "_after_input_file_read", rewrite_same_size)
    with pytest.raises(SnapshotImportReconcileError, match="changed"):
        snapshot_import_reconcile(target, same)


def test_missing_unmanaged_and_unlocked_graphs(tmp_path: Path):
    source = tmp_path / "source"
    live = _publish(source, "only")
    export_dir = _copy_standalone(live, tmp_path / "export")
    dummy_target = tmp_path / "placeholder"
    dummy_target.mkdir()
    # A structurally valid plan still needs a real managed graph to be produced.
    real_target = tmp_path / "real"
    _publish(real_target, "dst")
    plan = snapshot_import_plan(real_target, export_dir)
    plan_file = _save_plan(tmp_path, plan)

    missing = _run(
        "--graph",
        str(tmp_path / "missing"),
        "--plan-file",
        str(plan_file),
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "entities.parquet").write_bytes(b"x")
    legacy = _run("--graph", str(flat), "--plan-file", str(plan_file), "--json")
    assert legacy.returncode == 2
    assert legacy.stdout == ""

    unlocked = tmp_path / "unlocked"
    _publish(unlocked, "lock")
    (unlocked / PUBLICATION_LOCK_NAME).unlink()
    unlocked_proc = _run(
        "--graph", str(unlocked), "--plan-file", str(plan_file), "--json"
    )
    assert unlocked_proc.returncode == 2
    assert unlocked_proc.stdout == ""
    assert "adopt-publication-lock" in unlocked_proc.stderr
    assert not (unlocked / PUBLICATION_LOCK_NAME).exists()


def test_graph_snapshots_current_lock_listing_and_staging_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)

    def switch_current(_root, _scan):
        (target / "current").write_text(live.name + "\n", encoding="utf-8")

    monkeypatch.setattr(reconcile_mod, "_after_first_target_scan", switch_current)
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="current|listing|lock|staging"
    ):
        snapshot_import_reconcile(target, plan_file)
    (target / "current").write_text(plan["current"] + "\n", encoding="utf-8")

    def add_published(_root, _scan):
        extra = target / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(target / "snapshots" / live.name, extra)

    monkeypatch.setattr(reconcile_mod, "_after_first_target_scan", add_published)
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="listing|lock|current|staging"
    ):
        snapshot_import_reconcile(target, plan_file)
    shutil.rmtree(target / "snapshots" / "19990101-000000-addedone", ignore_errors=True)

    def add_exact_staging(_root, _scan):
        staging = target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}"
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(reconcile_mod, "_after_first_target_scan", add_exact_staging)
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="staging|listing|lock|current"
    ):
        snapshot_import_reconcile(target, plan_file)
    shutil.rmtree(
        target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}", ignore_errors=True
    )

    def add_unrelated_staging(_root, _scan):
        staging = target / "snapshots" / ".staging-19990101-000000-other"
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(reconcile_mod, "_after_first_target_scan", add_unrelated_staging)
    result = snapshot_import_reconcile(target, plan_file)
    assert result["ok"] is True
    assert result["target_staging_present"] is False
    shutil.rmtree(
        target / "snapshots" / ".staging-19990101-000000-other", ignore_errors=True
    )

    def replace_lock(_root, _scan):
        lock = target / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-lock")

    monkeypatch.setattr(reconcile_mod, "_after_first_target_scan", replace_lock)
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="lock|current|listing"
    ):
        snapshot_import_reconcile(target, plan_file)

    raced = tmp_path / "raced-graph"
    _publish(raced, "raced")
    hidden = tmp_path / "hidden-graph"
    replacement = tmp_path / "replacement-graph"
    _publish(replacement, "other")
    raced_plan = snapshot_import_plan(
        raced, _copy_standalone(live, tmp_path / "raced-export")
    )
    raced_plan_file = _save_plan(tmp_path, raced_plan, "raced.json")

    def replace_graph(path):
        if path == raced:
            raced.rename(hidden)
            raced.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(reconcile_mod, "_after_first_target_scan", lambda *_: None)
    monkeypatch.setattr(reconcile_mod, "_after_graph_path_inspected", replace_graph)
    with pytest.raises(
        (SnapshotImportReconcileError, SnapshotImportReconcileIntegrityError),
        match="symlink|changed|replaced|unsafe",
    ):
        snapshot_import_reconcile(raced, raced_plan_file)

    raced_real = tmp_path / "raced-real-graph"
    _publish(raced_real, "raced-real")
    hidden_real = tmp_path / "hidden-real-graph"
    real_plan = snapshot_import_plan(
        raced_real, _copy_standalone(live, tmp_path / "real-export")
    )
    real_plan_file = _save_plan(tmp_path, real_plan, "real.json")

    def replace_graph_with_real_directory(path):
        if path == raced_real:
            raced_real.rename(hidden_real)
            shutil.copytree(replacement, raced_real)

    monkeypatch.setattr(
        reconcile_mod,
        "_after_graph_path_inspected",
        replace_graph_with_real_directory,
    )
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="changed|replaced"
    ):
        snapshot_import_reconcile(raced_real, real_plan_file)


def test_target_snapshot_symlink_nondirectory_replacement_and_payload_anomalies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    default_after_first_snapshot_observation = (
        reconcile_mod._after_first_snapshot_observation
    )
    default_after_target_snapshot_first_stat = (
        reconcile_mod._after_target_snapshot_first_stat
    )

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    published = target / "snapshots" / live.name

    linked = tmp_path / "linked-snap"
    shutil.copytree(published, linked)
    published.rename(tmp_path / "aside-published")
    published.symlink_to(linked, target_is_directory=True)
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="symlink"):
        snapshot_import_reconcile(target, plan_file)
    published.unlink()
    shutil.move(str(tmp_path / "aside-published"), published)

    file_graph = tmp_path / "file-graph"
    file_source = tmp_path / "file-source"
    file_live = _publish(file_source, "file")
    _publish(file_graph, "dst")
    file_export = _copy_standalone(file_live, tmp_path / "file-export")
    file_plan = snapshot_import_plan(file_graph, file_export)
    snapshot_import_apply(
        file_graph, file_export, file_plan["import_revision"], import_confirmed=True
    )
    snap_dir = file_graph / "snapshots" / file_live.name
    shutil.rmtree(snap_dir)
    snap_dir.write_text("not-a-directory\n", encoding="utf-8")
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="not a real directory"
    ):
        snapshot_import_reconcile(file_graph, _save_plan(tmp_path, file_plan, "file.json"))

    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"must-not-follow")
    payload = published / "entities.parquet"
    original = payload.read_bytes()
    payload.unlink()
    payload.symlink_to(outside)
    outside_ino = outside.stat().st_ino
    original_read = plan_mod.os.read

    def reject_outside(fd, count):
        assert os.fstat(fd).st_ino != outside_ino
        return original_read(fd, count)

    monkeypatch.setattr(plan_mod.os, "read", reject_outside)
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="symlink"):
        snapshot_import_reconcile(target, plan_file)
    assert outside.read_bytes() == b"must-not-follow"
    payload.unlink()
    payload.write_bytes(original)
    monkeypatch.setattr(plan_mod.os, "read", original_read)

    fifo_graph = tmp_path / "fifo-graph"
    fifo_source = tmp_path / "fifo-source"
    fifo_live = _publish(fifo_source, "fifo")
    _publish(fifo_graph, "dst")
    fifo_export = _copy_standalone(fifo_live, tmp_path / "fifo-export")
    fifo_plan = snapshot_import_plan(fifo_graph, fifo_export)
    snapshot_import_apply(
        fifo_graph, fifo_export, fifo_plan["import_revision"], import_confirmed=True
    )
    os.mkfifo(fifo_graph / "snapshots" / fifo_live.name / "pipe.fifo")
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="unexpected|not a regular"
    ):
        snapshot_import_reconcile(
            fifo_graph, _save_plan(tmp_path, fifo_plan, "fifo.json")
        )

    hard_graph = tmp_path / "hard-graph"
    hard_source = tmp_path / "hard-source"
    hard_live = _publish(hard_source, "hard")
    _publish(hard_graph, "dst")
    hard_export = _copy_standalone(hard_live, tmp_path / "hard-export")
    hard_plan = snapshot_import_plan(hard_graph, hard_export)
    snapshot_import_apply(
        hard_graph, hard_export, hard_plan["import_revision"], import_confirmed=True
    )
    hard_snap = hard_graph / "snapshots" / hard_live.name
    os.link(hard_snap / "entities.parquet", hard_snap / "alias.parquet")
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="unexpected"):
        snapshot_import_reconcile(
            hard_graph, _save_plan(tmp_path, hard_plan, "hard.json")
        )

    def rewrite_same_size(_path, _records):
        target_file = published / "entities.parquet"
        data = target_file.read_bytes()
        info = target_file.stat()
        replacement = bytes([data[0] ^ 1]) + data[1:]
        target_file.write_bytes(replacement)
        os.utime(target_file, ns=(info.st_atime_ns, info.st_mtime_ns))

    monkeypatch.setattr(
        reconcile_mod, "_after_first_snapshot_observation", rewrite_same_size
    )
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="changed"):
        snapshot_import_reconcile(target, plan_file)
    monkeypatch.setattr(
        reconcile_mod,
        "_after_first_snapshot_observation",
        default_after_first_snapshot_observation,
    )

    replacement = tmp_path / "replacement-snap"
    shutil.copytree(published, replacement)

    def replace_snapshot(_path, _info):
        aside = tmp_path / "aside-live"
        if published.exists() and not published.is_symlink():
            published.rename(aside)
            published.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(
        reconcile_mod, "_after_target_snapshot_first_stat", replace_snapshot
    )
    with pytest.raises(
        SnapshotImportReconcileIntegrityError, match="replaced|changed|symlink"
    ):
        snapshot_import_reconcile(target, plan_file)

    monkeypatch.setattr(
        reconcile_mod,
        "_after_target_snapshot_first_stat",
        default_after_target_snapshot_first_stat,
    )
    late_graph = tmp_path / "late-graph"
    late_source = tmp_path / "late-source"
    late_live = _publish(late_source, "late")
    _publish(late_graph, "dst")
    late_export = _copy_standalone(late_live, tmp_path / "late-export")
    late_plan = snapshot_import_plan(late_graph, late_export)
    snapshot_import_apply(
        late_graph,
        late_export,
        late_plan["import_revision"],
        import_confirmed=True,
    )
    late_plan_file = _save_plan(tmp_path, late_plan, "late.json")

    def rewrite_after_second_target_scan(_path, _scan):
        target_file = (
            late_graph / "snapshots" / late_live.name / "entities.parquet"
        )
        data = target_file.read_bytes()
        info = target_file.stat()
        replacement = bytes([data[0] ^ 1]) + data[1:]
        target_file.write_bytes(replacement)
        os.utime(target_file, ns=(info.st_atime_ns, info.st_mtime_ns))

    monkeypatch.setattr(
        reconcile_mod,
        "_after_second_target_scan",
        rewrite_after_second_target_scan,
    )
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="changed"):
        snapshot_import_reconcile(late_graph, late_plan_file)


def test_exact_staging_appearance_and_disappearance_during_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    _source, target, live, _dest, _export, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    staging = target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}"

    def appear(_path, _info):
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(reconcile_mod, "_after_target_staging_first_stat", appear)
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="staging"):
        snapshot_import_reconcile(target, plan_file)
    if staging.exists():
        shutil.rmtree(staging)

    staging.mkdir()

    def disappear(_path, _info):
        if staging.exists():
            shutil.rmtree(staging)

    monkeypatch.setattr(reconcile_mod, "_after_target_staging_first_stat", disappear)
    with pytest.raises(SnapshotImportReconcileIntegrityError, match="staging"):
        snapshot_import_reconcile(target, plan_file)


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_import_reconcile as reconcile_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    original_json = reconcile_mod.result_to_json
    original_format = reconcile_mod.format_result
    original_lease = reconcile_mod.graph_read_lease
    state = {
        "graph_fd": None,
        "snapshots_fd": None,
        "snapshot_fd": None,
        "payload_fds": {},
        "lease": False,
        "responses": 0,
        "flushes": 0,
        "leases": 0,
    }

    @contextmanager
    def tracked_lease(*args, **kwargs):
        state["leases"] += 1
        with original_lease(*args, **kwargs) as lease:
            state["lease"] = True
            try:
                yield lease
            finally:
                state["lease"] = False

    def capture_ready(_graph, graph_fd, snapshots_fd, snapshot_fd, payload_fds, _result):
        state["graph_fd"] = graph_fd
        state["snapshots_fd"] = snapshots_fd
        state["snapshot_fd"] = snapshot_fd
        state["payload_fds"] = dict(payload_fds)
        os.fstat(graph_fd)
        os.fstat(snapshots_fd)
        assert snapshot_fd is not None
        os.fstat(snapshot_fd)
        for fd in payload_fds.values():
            os.fstat(fd)
        assert state["lease"] is True

    def guarded_json(*args, **kwargs):
        assert state["lease"] is True
        os.fstat(state["graph_fd"])
        os.fstat(state["snapshots_fd"])
        os.fstat(state["snapshot_fd"])
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        assert state["lease"] is True
        os.fstat(state["graph_fd"])
        os.fstat(state["snapshots_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            assert state["lease"] is True
            os.fstat(state["graph_fd"])
            os.fstat(state["snapshots_fd"])
            os.fstat(state["snapshot_fd"])
            for fd in state["payload_fds"].values():
                os.fstat(fd)
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            assert state["lease"] is True
            os.fstat(state["graph_fd"])
            os.fstat(state["snapshots_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(reconcile_mod, "graph_read_lease", tracked_lease)
    monkeypatch.setattr(reconcile_mod, "_after_result_ready", capture_ready)
    monkeypatch.setattr(reconcile_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(reconcile_mod, "format_result", guarded_format)
    monkeypatch.setattr(reconcile_mod.sys, "stdout", GuardedStdout())
    assert (
        reconcile_mod.main(
            ["--graph", str(target), "--plan-file", str(plan_file), "--json"]
        )
        == 0
    )
    assert (
        reconcile_mod.main(["--graph", str(target), "--plan-file", str(plan_file)]) == 0
    )
    assert state["lease"] is False
    assert state["leases"] == 2
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_no_mutation_no_producer_invocation_and_mcp_remains_eleven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as export_apply_mod
    import graphrag_code.snapshot_export_plan as export_plan_mod
    import graphrag_code.snapshot_export_verify as verify_mod
    import graphrag_code.snapshot_import_apply as apply_mod
    import graphrag_code.snapshot_import_plan as plan_mod
    import graphrag_code.snapshot_import_reconcile as reconcile_mod
    import graphrag_code.snapshot_read as read_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    plan_file = _save_plan(tmp_path, plan)
    before_target = _protected_state(target)
    before_export = _payload_hashes(export_dir)
    calls = {"shared": 0}

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public mutating/read scope")

    original_lease = reconcile_mod.graph_read_lease

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 1
        assert kwargs.get("allow_unlocked_managed") is False
        with original_lease(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(reconcile_mod, "graph_read_lease", counted)
    monkeypatch.setattr(reconcile_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "snapshot_import_plan", boom)
    monkeypatch.setattr(apply_mod, "snapshot_import_apply", boom)
    monkeypatch.setattr(export_plan_mod, "snapshot_export_plan", boom)
    monkeypatch.setattr(export_apply_mod, "snapshot_export_apply", boom)
    monkeypatch.setattr(verify_mod, "snapshot_export_verify", boom)
    monkeypatch.setattr(read_mod, "retained_snapshot_read", boom)
    result = snapshot_import_reconcile(target, plan_file)
    assert result["ok"] is True
    assert result["graph_mutated"] is False
    assert result["export_observed"] is False
    assert result["export_mutated"] is False
    assert calls["shared"] == 1
    assert _protected_state(target) == before_target
    assert _payload_hashes(export_dir) == before_export

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
    assert "export_revision_of" in imported
    assert "import_revision_of" in imported
    assert "canonical_import_revision_payload" in imported
    assert "snapshot_import_plan(" not in source
    assert "snapshot_import_apply(" not in source
    assert "snapshot_export_plan(" not in source
    assert "snapshot_export_apply(" not in source
    assert "snapshot_export_verify(" not in source
    assert "graph_exclusive_lease" not in source
    assert "read_bytes" not in source
    assert HASH_CHUNK_BYTES <= 64 * 1024
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 11
    assert "snapshot_import_reconcile" not in TOOL_NAMES
    assert "snapshot_import_plan" not in TOOL_NAMES
    assert "snapshot_import_apply" not in TOOL_NAMES
    session = build_session(target, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 11
            assert "snapshot_import_reconcile" not in names

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
    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    plan_file = _save_plan(tmp_path, plan)
    first = snapshot_import_reconcile(target, plan_file)
    second = snapshot_import_reconcile(target, plan_file)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    names = [item["path"] for item in first["planned_files"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    assert first["observed_snapshot_export_revision"] == export_revision_of(
        {
            "schema_version": 1,
            "resolved_snapshot": live.name,
            "files": first["planned_files"],
        }
    )
    assert first["input_import_revision"] == import_revision_of(plan)
