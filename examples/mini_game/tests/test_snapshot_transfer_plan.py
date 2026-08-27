"""Read-only snapshot transfer plan.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_transfer_plan.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
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
from graphrag_code.snapshot_export_apply import snapshot_export_apply  # type: ignore
from graphrag_code.snapshot_export_plan import (  # type: ignore
    ACCEPTED_PAYLOAD_FILES,
    HASH_CHUNK_BYTES,
    MAX_MANIFEST_BYTES,
    REQUIRED_PAYLOAD_FILES,
    export_revision_of,
    snapshot_export_plan,
)
from graphrag_code.snapshot_export_verify import snapshot_export_verify  # type: ignore
from graphrag_code.snapshot_import_plan import snapshot_import_plan  # type: ignore
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore
from graphrag_code.snapshot_transfer_plan import (  # type: ignore
    SnapshotTransferPlanError,
    SnapshotTransferPlanIntegrityError,
    canonical_transfer_revision_payload,
    canonical_transfer_revision_text,
    format_result,
    ordered_graph_lease_pair,
    result_to_json,
    snapshot_transfer_plan,
    transfer_revision_of,
)

SCRIPT = ROOT / "scripts" / "snapshot_transfer_plan.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_transfer_plan.py"
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
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
REQUIRED_RESULT_KEYS = (
    "schema_version",
    "ok",
    "source_graph",
    "target_graph",
    "requested_snapshot",
    "snapshot_id",
    "source_current",
    "source_published_snapshots",
    "files",
    "file_count",
    "total_size_bytes",
    "source_export_revision",
    "source_envelope_valid",
    "target_current",
    "target_published_snapshots",
    "target_published_count",
    "target_staging_name",
    "target_snapshot_present",
    "target_staging_present",
    "target_staging_count",
    "blocking_reasons",
    "transfer_ready",
    "transfer_revision",
    "transfer_performed",
    "source_graph_mutated",
    "target_graph_mutated",
    "fresh_plan_required_before_transfer",
    "notices",
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
        settings_text=f"transfer-plan: {marker}\n",
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


def _rewrite_manifest(snap_dir: Path, **updates: object) -> None:
    payload = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    payload.update(updates)
    (snap_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _assert_plan_shape(
    result: dict,
    source: Path,
    target: Path,
    snapshot_id: str,
    *,
    requested: str,
    transfer_ready: bool,
    target_snapshot_present: bool = False,
    target_staging_present: bool = False,
    blocking_reasons: list[str] | None = None,
) -> None:
    for key in REQUIRED_RESULT_KEYS:
        assert key in result
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["source_graph"] == str(source.resolve())
    assert result["target_graph"] == str(target.resolve())
    assert result["requested_snapshot"] == requested
    assert result["snapshot_id"] == snapshot_id
    assert result["source_envelope_valid"] is True
    assert result["transfer_performed"] is False
    assert result["source_graph_mutated"] is False
    assert result["target_graph_mutated"] is False
    assert result["fresh_plan_required_before_transfer"] is True
    assert result["file_count"] == len(result["files"])
    assert result["total_size_bytes"] == sum(item["size_bytes"] for item in result["files"])
    paths = [item["path"] for item in result["files"]]
    assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
    assert set(REQUIRED_PAYLOAD_FILES) <= set(paths)
    assert set(paths) <= set(ACCEPTED_PAYLOAD_FILES)
    snap_dir = source / "snapshots" / snapshot_id
    for item in result["files"]:
        data = (snap_dir / item["path"]).read_bytes()
        assert item["size_bytes"] == len(data)
        assert item["content_revision"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert result["source_export_revision"] == export_revision_of(
        {
            "schema_version": 1,
            "resolved_snapshot": snapshot_id,
            "files": result["files"],
        }
    )
    assert result["target_staging_name"] == f"{STAGING_NAME_PREFIX}{snapshot_id}"
    assert result["target_snapshot_present"] is target_snapshot_present
    assert result["target_staging_present"] is target_staging_present
    expected_blocking = blocking_reasons or []
    assert result["blocking_reasons"] == expected_blocking
    assert result["transfer_ready"] is transfer_ready
    if transfer_ready:
        assert result["blocking_reasons"] == []
        assert result["target_snapshot_present"] is False
        assert result["target_staging_present"] is False
    assert result["source_published_snapshots"] == sorted(
        result["source_published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["target_published_snapshots"] == sorted(
        result["target_published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["target_published_count"] == len(result["target_published_snapshots"])
    assert result["source_current"] in result["source_published_snapshots"]
    assert result["target_current"] in result["target_published_snapshots"]
    assert result["transfer_revision"] == transfer_revision_of(result)
    payload = json.loads(canonical_transfer_revision_text(result))
    assert set(payload) == {
        "blocking_reasons",
        "fresh_plan_required_before_transfer",
        "schema_version",
        "snapshot_id",
        "source_envelope_valid",
        "source_export_revision",
        "target_current",
        "target_published_snapshots",
        "target_snapshot_present",
        "target_staging_name",
        "target_staging_present",
        "transfer_performed",
        "transfer_ready",
    }
    assert "source_graph" not in payload
    assert "target_graph" not in payload
    assert "ok" not in payload
    assert "requested_snapshot" not in payload
    assert "file_count" not in payload
    assert "total_size_bytes" not in payload
    assert "source_current" not in payload
    assert "source_published_snapshots" not in payload
    assert "target_published_count" not in payload
    assert "target_staging_count" not in payload
    assert "notices" not in payload
    assert "source_graph_mutated" not in payload
    assert "target_graph_mutated" not in payload
    codes = [notice["code"] for notice in result["notices"]]
    assert codes[:7] == [
        "plan_is_not_transfer",
        "plan_is_not_backup",
        "transfer_revision_is_self_consistency_only",
        "fresh_plan_required_before_transfer",
        "source_envelope_language_independent_only",
        "advisory_locks_cooperating_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert str(source.resolve()) in text
    assert str(target.resolve()) in text
    assert snapshot_id in text
    assert "transfer_performed=false" in text
    assert "not a transfer" in text
    assert "not a backup" in text


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
    args = [
        "--source-graph",
        "source",
        "--snapshot",
        "current",
        "--target-graph",
        "target",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_transfer_plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-transfer-plan", *args],
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

    human = _run(
        "--source-graph",
        "source",
        "--snapshot",
        "current",
        "--target-graph",
        "target",
        cwd=here,
    )
    assert human.returncode == 0, human.stderr
    assert human.stdout.startswith("snapshot-transfer-plan:")
    assert "transfer_performed=false" in human.stdout
    assert json.dumps(bodies[0], indent=2, sort_keys=True, ensure_ascii=True) + "\n" == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-transfer-plan",
            "--source-graph",
            str(source),
            "--snapshot",
            "current",
            "--target-graph",
            str(target),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["transfer_revision"] == bodies[0]["transfer_revision"]

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_transfer_plan.py" in names
    help_proc = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "snapshot-transfer-plan" in help_proc.stdout
    command_help = subprocess.run(
        [sys.executable, str(CLI), "snapshot-transfer-plan", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert command_help.returncode == 0, command_help.stderr
    assert "--source-graph" in command_help.stdout
    assert "--target-graph" in command_help.stdout
    assert "--snapshot" in command_help.stdout


def test_ready_plan_current_and_explicit_selection(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    first = _publish(source, "old", observations=True)
    live = _publish(source, "src", observations=True)
    _publish(target, "dst")
    before_source = _protected_state(source)
    before_target = _protected_state(target)
    current_plan = snapshot_transfer_plan(source, "current", target)
    _assert_plan_shape(
        current_plan,
        source,
        target,
        live.name,
        requested="current",
        transfer_ready=True,
    )
    assert current_plan["source_current"] == live.name
    assert first.name in current_plan["source_published_snapshots"]
    assert live.name in current_plan["source_published_snapshots"]
    assert current_plan["target_current"] == _current(target)
    explicit = snapshot_transfer_plan(source, first.name, target)
    _assert_plan_shape(
        explicit,
        source,
        target,
        first.name,
        requested=first.name,
        transfer_ready=True,
    )
    assert explicit["source_current"] == live.name
    assert explicit["snapshot_id"] == first.name
    assert explicit["source_export_revision"] != current_plan["source_export_revision"]
    assert _protected_state(source) == before_source
    assert _protected_state(target) == before_target


def test_source_export_revision_matches_existing_export_contract(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "rev", observations=True)
    _publish(target, "dst")
    plan = snapshot_export_plan(source, "current")
    dest = tmp_path / "applied"
    applied = snapshot_export_apply(
        source,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    verified = snapshot_export_verify(dest, plan["export_revision"])
    imported = snapshot_import_plan(target, dest)
    result = snapshot_transfer_plan(source, "current", target)
    _assert_plan_shape(
        result, source, target, live.name, requested="current", transfer_ready=True
    )
    assert result["source_export_revision"] == plan["export_revision"]
    assert result["source_export_revision"] == applied["observed_export_revision"]
    assert result["source_export_revision"] == verified["observed_export_revision"]
    assert result["source_export_revision"] == imported["source_export_revision"]
    assert result["files"] == plan["files"]


def test_schema_and_canonical_transfer_revision(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "canon")
    _publish(target, "dst")
    result = snapshot_transfer_plan(source, "current", target)
    _assert_plan_shape(
        result, source, target, live.name, requested="current", transfer_ready=True
    )
    digest = hashlib.sha256(
        canonical_transfer_revision_text(result).encode("utf-8")
    ).hexdigest()
    assert result["transfer_revision"] == "sha256:" + digest
    payload = canonical_transfer_revision_payload(result)
    assert payload["schema_version"] == 1
    assert payload["snapshot_id"] == live.name
    assert payload["source_export_revision"] == result["source_export_revision"]
    assert payload["transfer_ready"] is True
    assert payload["transfer_performed"] is False
    first = snapshot_transfer_plan(source, "current", target)
    second = snapshot_transfer_plan(source, "current", target)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    tampered = {**result, "target_current": result["target_published_snapshots"][0]}
    if tampered["target_current"] == result["target_current"]:
        tampered = {**result, "transfer_ready": False, "blocking_reasons": ["snapshot_id_already_published"], "target_snapshot_present": True}
    with pytest.raises(SnapshotTransferPlanError):
        canonical_transfer_revision_payload(
            {**result, "blocking_reasons": ["snapshot_id_already_published"]}
        )
    with pytest.raises(SnapshotTransferPlanError, match="boolean"):
        canonical_transfer_revision_payload({**result, "transfer_ready": 1})
    with pytest.raises(SnapshotTransferPlanError, match="boolean"):
        canonical_transfer_revision_payload({**result, "target_snapshot_present": 0})
    with pytest.raises(SnapshotTransferPlanError, match="schema_version"):
        canonical_transfer_revision_payload({**result, "schema_version": True})
    with pytest.raises(SnapshotTransferPlanError, match="must be false"):
        canonical_transfer_revision_payload({**result, "transfer_performed": True})


def test_each_blocker_independently_and_together(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "conflict")
    _publish(target, "dst")
    shutil.copytree(live, target / "snapshots" / live.name)
    published = snapshot_transfer_plan(source, "current", target)
    _assert_plan_shape(
        published,
        source,
        target,
        live.name,
        requested="current",
        transfer_ready=False,
        target_snapshot_present=True,
        blocking_reasons=["snapshot_id_already_published"],
    )
    proc = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["blocking_reasons"] == ["snapshot_id_already_published"]

    staging_source = tmp_path / "staging-source"
    staging_target = tmp_path / "staging-target"
    staged_live = _publish(staging_source, "staging")
    _publish(staging_target, "dst")
    staging = staging_target / "snapshots" / f".staging-{staged_live.name}"
    staging.mkdir()
    staged = snapshot_transfer_plan(staging_source, "current", staging_target)
    _assert_plan_shape(
        staged,
        staging_source,
        staging_target,
        staged_live.name,
        requested="current",
        transfer_ready=False,
        target_staging_present=True,
        blocking_reasons=["target_staging_name_present"],
    )
    assert staged["target_staging_count"] == 1
    assert staging.is_dir()

    both_source = tmp_path / "both-source"
    both_target = tmp_path / "both-target"
    both_live = _publish(both_source, "both")
    _publish(both_target, "dst")
    shutil.copytree(both_live, both_target / "snapshots" / both_live.name)
    (both_target / "snapshots" / f".staging-{both_live.name}").mkdir()
    both = snapshot_transfer_plan(both_source, "current", both_target)
    _assert_plan_shape(
        both,
        both_source,
        both_target,
        both_live.name,
        requested="current",
        transfer_ready=False,
        target_snapshot_present=True,
        target_staging_present=True,
        blocking_reasons=[
            "snapshot_id_already_published",
            "target_staging_name_present",
        ],
    )
    assert both["blocking_reasons"] == sorted(
        both["blocking_reasons"], key=lambda item: item.encode("utf-8")
    )


def test_unrelated_staging_does_not_block_this_id(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "ready")
    _publish(target, "dst")
    unrelated = target / "snapshots" / ".staging-19990101-000000-unrelated"
    unrelated.mkdir()
    result = snapshot_transfer_plan(source, "current", target)
    _assert_plan_shape(
        result, source, target, live.name, requested="current", transfer_ready=True
    )
    assert result["target_staging_count"] == 1
    assert result["target_staging_present"] is False
    codes = [notice["code"] for notice in result["notices"]]
    assert "unrelated_target_staging_present" in codes
    assert unrelated.is_dir()


def test_different_source_and_target_history_states(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_old = _publish(source, "sold")
    source_new = _publish(source, "snew")
    target_old = _publish(target, "told")
    target_new = _publish(target, "tnew")
    result = snapshot_transfer_plan(source, source_old.name, target)
    assert result["source_current"] == source_new.name
    assert result["snapshot_id"] == source_old.name
    assert result["target_current"] == target_new.name
    assert set(result["source_published_snapshots"]) == {source_old.name, source_new.name}
    assert set(result["target_published_snapshots"]) == {target_old.name, target_new.name}
    assert result["transfer_ready"] is True


def test_same_graph_identical_path_and_inode_alias_rejected(tmp_path: Path):
    graph = tmp_path / "graph"
    _publish(graph, "only")
    before = _protected_state(graph)
    identical = _run(
        "--source-graph",
        str(graph),
        "--snapshot",
        "current",
        "--target-graph",
        str(graph),
        "--json",
    )
    assert identical.returncode == 2
    assert identical.stdout == ""
    with pytest.raises(SnapshotTransferPlanError, match="different directory identities"):
        snapshot_transfer_plan(graph, "current", graph)

    alias = graph / "."
    alias_proc = _run(
        "--source-graph",
        str(graph),
        "--snapshot",
        "current",
        "--target-graph",
        str(alias),
        "--json",
    )
    assert alias_proc.returncode == 2
    assert alias_proc.stdout == ""
    with pytest.raises(SnapshotTransferPlanError, match="different directory identities"):
        snapshot_transfer_plan(graph, "current", alias)
    assert _protected_state(graph) == before


def test_same_graph_rejected_before_nested_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    graph = tmp_path / "graph"
    _publish(graph, "only")
    calls = {"leases": 0}
    original = transfer_mod.graph_shared_leases

    @contextmanager
    def counted(*args, **kwargs):
        calls["leases"] += 1
        with original(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(transfer_mod, "graph_shared_leases", counted)
    with pytest.raises(SnapshotTransferPlanError, match="different directory identities"):
        snapshot_transfer_plan(graph, "current", graph / ".")
    assert calls["leases"] == 0


def test_deterministic_two_graph_lease_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    aaa = tmp_path / "aaa"
    zzz = tmp_path / "zzz"
    _publish(aaa, "aaa")
    _publish(zzz, "zzz")
    original = transfer_mod.graph_shared_leases
    observed: list[list[str]] = []

    @contextmanager
    def tracked(first, second):
        observed.append([str(Path(first).resolve()), str(Path(second).resolve())])
        with original(first, second) as lease:
            yield lease

    monkeypatch.setattr(transfer_mod, "graph_shared_leases", tracked)
    forward = snapshot_transfer_plan(aaa, "current", zzz)
    reverse = snapshot_transfer_plan(zzz, "current", aaa)
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
    assert str(aaa.resolve()).encode("utf-8") < str(zzz.resolve()).encode("utf-8")
    assert observed[0][0] == str(aaa.resolve())


def test_missing_symlinked_unlocked_and_nonregular_graphs(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "src")
    _publish(target, "dst")

    missing = _run(
        "--source-graph",
        str(tmp_path / "missing"),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""

    missing_target = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(tmp_path / "missing-target"),
        "--json",
    )
    assert missing_target.returncode == 2
    assert missing_target.stdout == ""

    linked_source = tmp_path / "linked-source"
    linked_source.symlink_to(source, target_is_directory=True)
    linked = _run(
        "--source-graph",
        str(linked_source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert linked.returncode == 2
    assert linked.stdout == ""

    linked_target = tmp_path / "linked-target"
    linked_target.symlink_to(target, target_is_directory=True)
    linked_t = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(linked_target),
        "--json",
    )
    assert linked_t.returncode == 2
    assert linked_t.stdout == ""

    file_graph = tmp_path / "file-graph"
    file_graph.write_text("nope\n", encoding="utf-8")
    file_proc = _run(
        "--source-graph",
        str(file_graph),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert file_proc.returncode == 2
    assert file_proc.stdout == ""

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "entities.parquet").write_bytes(b"x")
    legacy = _run(
        "--source-graph",
        str(flat),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert legacy.returncode == 2
    assert legacy.stdout == ""

    unlocked = tmp_path / "unlocked"
    _publish(unlocked, "lock")
    (unlocked / PUBLICATION_LOCK_NAME).unlink()
    unlocked_proc = _run(
        "--source-graph",
        str(unlocked),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert unlocked_proc.returncode == 2
    assert unlocked_proc.stdout == ""
    assert "adopt-publication-lock" in unlocked_proc.stderr
    assert not (unlocked / PUBLICATION_LOCK_NAME).exists()

    lock = target / PUBLICATION_LOCK_NAME
    lock.unlink()
    lock.symlink_to(source / PUBLICATION_LOCK_NAME)
    symlink_lock = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert symlink_lock.returncode == 2
    assert symlink_lock.stdout == ""

    lock.unlink()
    os.mkfifo(lock)
    fifo_lock = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert fifo_lock.returncode == 2
    assert fifo_lock.stdout == ""

    bad_current_source = tmp_path / "bad-current-source"
    _publish(bad_current_source, "bad-current")
    (bad_current_source / "current").write_bytes(b"\xff\xfe")
    bad_source_proc = _run(
        "--source-graph",
        str(bad_current_source),
        "--snapshot",
        "current",
        "--target-graph",
        str(source),
        "--json",
    )
    assert bad_source_proc.returncode == 1
    assert bad_source_proc.stdout == ""

    bad_current_target = tmp_path / "bad-current-target"
    _publish(bad_current_target, "bad-target-current")
    (bad_current_target / "current").write_bytes(b"\xff\xfe")
    bad_target_proc = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(bad_current_target),
        "--json",
    )
    assert bad_target_proc.returncode == 1
    assert bad_target_proc.stdout == ""

    missing_snapshot = _run(
        "--source-graph",
        str(source),
        "--target-graph",
        str(tmp_path / "other-target"),
        "--json",
    )
    assert missing_snapshot.returncode == 2
    assert missing_snapshot.stdout == ""
    for token in ("", " current", "current ", "../x", ".staging-1", "not a snapshot"):
        proc = _run(
            "--source-graph",
            str(source),
            "--snapshot",
            token,
            "--target-graph",
            str(tmp_path / "dst2"),
            "--json",
        )
        assert proc.returncode == 2
        assert proc.stdout == ""


def test_source_snapshot_symlink_non_directory_fifo_socket_device_and_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "payload")
    _publish(target, "dst")

    linked_snap = tmp_path / "source-linked-snap"
    shutil.copytree(source, linked_snap)
    real = linked_snap / "snapshots" / _current(linked_snap)
    hidden = tmp_path / "hidden-snap"
    real.rename(hidden)
    real.symlink_to(hidden, target_is_directory=True)
    with pytest.raises(SnapshotTransferPlanError, match="symlink"):
        snapshot_transfer_plan(linked_snap, "current", target)
    linked_proc = _run(
        "--source-graph",
        str(linked_snap),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--json",
    )
    assert linked_proc.returncode == 1
    assert linked_proc.stdout == ""

    file_snap = tmp_path / "source-file-snap"
    shutil.copytree(source, file_snap)
    current_dir = file_snap / "snapshots" / _current(file_snap)
    shutil.rmtree(current_dir)
    current_dir.write_text("nope\n", encoding="utf-8")
    with pytest.raises(
        SnapshotTransferPlanError, match="not a real directory|not published history"
    ):
        snapshot_transfer_plan(file_snap, "current", target)

    extra = tmp_path / "source-extra"
    extra_live = _publish(extra, "extra")
    (extra_live / "notes.txt").write_text("nope\n", encoding="utf-8")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="unexpected"):
        snapshot_transfer_plan(extra, "current", target)

    nested = tmp_path / "source-nested"
    nested_live = _publish(nested, "nested")
    (nested_live / "nested-dir").mkdir()
    with pytest.raises(
        SnapshotTransferPlanIntegrityError, match="unexpected|not a regular"
    ):
        snapshot_transfer_plan(nested, "current", target)

    fifo_graph = tmp_path / "source-fifo"
    fifo_live = _publish(fifo_graph, "fifo")
    os.mkfifo(fifo_live / "pipe.fifo")
    with pytest.raises(
        SnapshotTransferPlanIntegrityError, match="unexpected|not a regular"
    ):
        snapshot_transfer_plan(fifo_graph, "current", target)

    remnant = tmp_path / "source-tmp"
    remnant_live = _publish(remnant, "tmp")
    (remnant_live / "entities.parquet.tmp").write_bytes(b"tmp")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="unexpected"):
        snapshot_transfer_plan(remnant, "current", target)

    directory_payload = tmp_path / "source-dir-payload"
    dir_live = _publish(directory_payload, "dir")
    (dir_live / "settings.yaml").unlink()
    (dir_live / "settings.yaml").mkdir()
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="not a regular"):
        snapshot_transfer_plan(directory_payload, "current", target)

    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"must-not-follow")
    linked_payload = tmp_path / "source-linked-payload"
    linked_live = _publish(linked_payload, "link")
    payload = linked_live / "entities.parquet"
    payload.unlink()
    payload.symlink_to(outside)
    outside_ino = outside.stat().st_ino
    original_read = plan_mod.os.read

    def reject_outside(fd, count):
        assert os.fstat(fd).st_ino != outside_ino
        return original_read(fd, count)

    monkeypatch.setattr(plan_mod.os, "read", reject_outside)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="symlink"):
        snapshot_transfer_plan(linked_payload, "current", target)
    assert outside.read_bytes() == b"must-not-follow"

    short = Path(f"/tmp/stp{os.getpid()}")
    if short.exists():
        shutil.rmtree(short)
    short.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        socket_graph = short / "g"
        socket_live = _publish(socket_graph, "sock")
        server.bind(str(socket_live / "s"))
        socket_target = short / "t"
        _publish(socket_target, "dst")
        with pytest.raises(
            SnapshotTransferPlanIntegrityError, match="unexpected|not a regular"
        ):
            snapshot_transfer_plan(socket_graph, "current", socket_target)
    finally:
        server.close()
        shutil.rmtree(short, ignore_errors=True)


def test_hardlink_anomaly_and_invalid_manifest_parquet_envelope(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "hard")
    _publish(target, "dst")
    os.link(live / "entities.parquet", live / "alias.parquet")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="hardlink|unexpected"):
        snapshot_transfer_plan(source, "current", target)
    (live / "alias.parquet").unlink()
    (live / "relationships.parquet").unlink()
    os.link(live / "text_units.parquet", live / "relationships.parquet")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="hardlink"):
        snapshot_transfer_plan(source, "current", target)

    env_source = tmp_path / "env-source"
    env_live = _publish(env_source, "env")
    env_target = tmp_path / "env-target"
    _publish(env_target, "dst")
    (env_live / "manifest.json").write_bytes(b'{"id":"a","id":"b"}')
    with pytest.raises(
        SnapshotTransferPlanIntegrityError, match="duplicate JSON object key"
    ):
        snapshot_transfer_plan(env_source, "current", env_target)
    dup_proc = _run(
        "--source-graph",
        str(env_source),
        "--snapshot",
        "current",
        "--target-graph",
        str(env_target),
        "--json",
    )
    assert dup_proc.returncode == 1
    assert dup_proc.stdout == ""

    utf_source = tmp_path / "utf-source"
    utf_live = _publish(utf_source, "utf")
    utf_target = tmp_path / "utf-target"
    _publish(utf_target, "dst")
    (utf_live / "manifest.json").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="UTF-8"):
        snapshot_transfer_plan(utf_source, "current", utf_target)

    json_source = tmp_path / "json-source"
    json_live = _publish(json_source, "json")
    json_target = tmp_path / "json-target"
    _publish(json_target, "dst")
    (json_live / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="JSON"):
        snapshot_transfer_plan(json_source, "current", json_target)

    oversized = tmp_path / "over-source"
    over_live = _publish(oversized, "over")
    over_target = tmp_path / "over-target"
    _publish(over_target, "dst")
    (over_live / "manifest.json").write_bytes(
        b'{"pad":"' + (b"a" * (MAX_MANIFEST_BYTES + 8)) + b'"}'
    )
    with pytest.raises(SnapshotTransferPlanError, match="exceeds bound"):
        snapshot_transfer_plan(oversized, "current", over_target)
    over_proc = _run(
        "--source-graph",
        str(oversized),
        "--snapshot",
        "current",
        "--target-graph",
        str(over_target),
        "--json",
    )
    assert over_proc.returncode == 2
    assert over_proc.stdout == ""

    bad_source = tmp_path / "bad-source"
    bad_live = _publish(bad_source, "bad")
    bad_target = tmp_path / "bad-target"
    _publish(bad_target, "dst")
    _rewrite_manifest(bad_live, schema_version=2)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="invalid"):
        snapshot_transfer_plan(bad_source, "current", bad_target)
    bad_proc = _run(
        "--source-graph",
        str(bad_source),
        "--snapshot",
        "current",
        "--target-graph",
        str(bad_target),
        "--json",
    )
    assert bad_proc.returncode == 1
    assert bad_proc.stdout == ""

    parquet_source = tmp_path / "parquet-source"
    parquet_live = _publish(parquet_source, "pq")
    parquet_target = tmp_path / "parquet-target"
    _publish(parquet_target, "dst")
    (parquet_live / "entities.parquet").write_bytes(b"not-a-parquet")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="cannot read|invalid"):
        snapshot_transfer_plan(parquet_source, "current", parquet_target)

    counts_source = tmp_path / "counts-source"
    counts_live = _publish(counts_source, "counts")
    counts_target = tmp_path / "counts-target"
    _publish(counts_target, "dst")
    payload = json.loads((counts_live / "manifest.json").read_text(encoding="utf-8"))
    payload["counts"]["entities"] = int(payload["counts"]["entities"]) + 1
    (counts_live / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="invalid"):
        snapshot_transfer_plan(counts_source, "current", counts_target)


def test_same_size_restored_mtime_rewrites_initial_and_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "race")
    _publish(target, "dst")
    target_file = live / "entities.parquet"
    original = target_file.read_bytes()
    original_stat = target_file.stat()

    def rewrite_same_size(_root, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        target_file.write_bytes(replacement)
        os.utime(target_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(
        transfer_mod, "_after_transfer_source_first_observation", rewrite_same_size
    )
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="changed"):
        snapshot_transfer_plan(source, "current", target)

    listing_source = tmp_path / "listing-source"
    listing_live = _publish(listing_source, "listing")
    listing_target = tmp_path / "listing-target"
    _publish(listing_target, "dst")

    def add_unexpected(_root, _entries):
        (listing_live / "notes.txt").write_text("extra\n", encoding="utf-8")

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_first_observation", lambda *_: None)
    monkeypatch.setattr(transfer_mod, "_after_transfer_source_listed", add_unexpected)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="unexpected|changed"):
        snapshot_transfer_plan(listing_source, "current", listing_target)

    joint_source = tmp_path / "joint-source"
    joint_live = _publish(joint_source, "joint")
    joint_target = tmp_path / "joint-target"
    _publish(joint_target, "dst")
    joint_file = joint_live / "entities.parquet"
    joint_original = joint_file.read_bytes()
    joint_stat = joint_file.stat()

    def rewrite_during_target(_root, _tokens):
        changed = bytes([joint_original[0] ^ 1]) + joint_original[1:]
        joint_file.write_bytes(changed)
        os.utime(joint_file, ns=(joint_stat.st_atime_ns, joint_stat.st_mtime_ns))

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_listed", lambda *_: None)
    monkeypatch.setattr(
        transfer_mod, "_after_transfer_target_tokens_captured", rewrite_during_target
    )
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="changed"):
        snapshot_transfer_plan(joint_source, "current", joint_target)

    final_source = tmp_path / "final-source"
    final_live = _publish(final_source, "final")
    final_target = tmp_path / "final-target"
    _publish(final_target, "dst")
    final_file = final_live / "entities.parquet"
    final_original = final_file.read_bytes()
    final_stat = final_file.stat()

    def rewrite_before_final(_source, _target):
        changed = bytes([final_original[0] ^ 1]) + final_original[1:]
        final_file.write_bytes(changed)
        os.utime(final_file, ns=(final_stat.st_atime_ns, final_stat.st_mtime_ns))

    monkeypatch.setattr(transfer_mod, "_after_transfer_target_tokens_captured", lambda *_: None)
    monkeypatch.setattr(
        transfer_mod, "_after_transfer_before_source_final_recheck", rewrite_before_final
    )
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="changed"):
        snapshot_transfer_plan(final_source, "current", final_target)

    during_final_source = tmp_path / "during-final-source"
    during_final_live = _publish(during_final_source, "during-final")
    during_final_target = tmp_path / "during-final-target"
    _publish(during_final_target, "dst")
    during_final_file = during_final_live / "entities.parquet"
    during_final_original = during_final_file.read_bytes()
    during_final_stat = during_final_file.stat()

    def rewrite_after_final_payload(_source, name):
        if name != "entities.parquet":
            return
        changed = bytes([during_final_original[0] ^ 1]) + during_final_original[1:]
        during_final_file.write_bytes(changed)
        os.utime(
            during_final_file,
            ns=(during_final_stat.st_atime_ns, during_final_stat.st_mtime_ns),
        )

    monkeypatch.setattr(
        transfer_mod,
        "_after_transfer_before_source_final_recheck",
        lambda *_: None,
    )
    monkeypatch.setattr(
        transfer_mod,
        "_after_transfer_source_final_payload_recheck",
        rewrite_after_final_payload,
    )
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="changed"):
        snapshot_transfer_plan(
            during_final_source, "current", during_final_target
        )


def test_source_current_history_listing_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    first = _publish(source, "old")
    live = _publish(source, "new")
    _publish(target, "dst")

    def switch_current(_root, _tokens):
        (source / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_tokens_captured", switch_current)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="current|listing|lock"):
        snapshot_transfer_plan(source, "current", target)
    (source / "current").write_text(live.name + "\n", encoding="utf-8")

    def add_published(_root, _tokens):
        extra = source / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(source / "snapshots" / live.name, extra)

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_tokens_captured", add_published)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="listing|lock|current"):
        snapshot_transfer_plan(source, live.name, target)
    shutil.rmtree(source / "snapshots" / "19990101-000000-addedone", ignore_errors=True)

    def replace_lock(_root, _tokens):
        lock = source / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-lock")

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_tokens_captured", replace_lock)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="lock|current|listing"):
        snapshot_transfer_plan(source, live.name, target)


def test_target_current_history_exact_staging_races_during_final_source_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "src")
    first = _publish(target, "old")
    current = _publish(target, "new")

    def switch_current(_source, _target):
        (target / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_final_recheck", switch_current)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="current|listing|lock|staging"):
        snapshot_transfer_plan(source, "current", target)
    (target / "current").write_text(current.name + "\n", encoding="utf-8")

    def add_published(_source, _target):
        extra = target / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(target / "snapshots" / current.name, extra)

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_final_recheck", add_published)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="listing|lock|current|staging"):
        snapshot_transfer_plan(source, "current", target)
    shutil.rmtree(target / "snapshots" / "19990101-000000-addedone", ignore_errors=True)

    def add_exact_staging(_source, _target):
        snap_id = _current(source)
        staging = target / "snapshots" / f".staging-{snap_id}"
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(transfer_mod, "_after_transfer_source_final_recheck", add_exact_staging)
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="listing|staging|lock|current"):
        snapshot_transfer_plan(source, "current", target)


def test_replaced_graph_roots_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "src")
    _publish(target, "dst")
    hidden = tmp_path / "hidden-target"
    replacement = tmp_path / "replacement-target"
    _publish(replacement, "other")

    def replace_target(path):
        if path == target:
            target.rename(hidden)
            target.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(transfer_mod, "_after_transfer_target_path_inspected", replace_target)
    with pytest.raises(
        (SnapshotTransferPlanError, SnapshotTransferPlanIntegrityError),
        match="symlink|changed|replaced|unsafe",
    ):
        snapshot_transfer_plan(source, "current", target)

    raced_real = tmp_path / "raced-real-target"
    _publish(raced_real, "raced")
    hidden_real = tmp_path / "hidden-real-target"

    def replace_with_real(path):
        if path == raced_real:
            raced_real.rename(hidden_real)
            shutil.copytree(replacement, raced_real)

    monkeypatch.setattr(transfer_mod, "_after_transfer_target_path_inspected", lambda *_: None)
    monkeypatch.setattr(transfer_mod, "_after_transfer_graphs_identified", lambda *_: None)
    monkeypatch.setattr(
        transfer_mod,
        "_after_transfer_target_path_inspected",
        replace_with_real,
    )
    with pytest.raises(SnapshotTransferPlanIntegrityError, match="changed|replaced"):
        snapshot_transfer_plan(source, "current", raced_real)


def test_count_and_input_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_export_plan as export_mod
    import graphrag_code.snapshot_staging as staging_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "a")
    _publish(source, "b")
    _publish(target, "dst")
    monkeypatch.setattr(export_mod, "MAX_PUBLISHED_SNAPSHOTS", 1)
    monkeypatch.setattr(staging_mod, "MAX_PUBLISHED_SNAPSHOTS", 1)
    with pytest.raises(SnapshotTransferPlanError, match="exceeds bound"):
        snapshot_transfer_plan(source, "current", target)

    staging_source = tmp_path / "ss"
    staging_target = tmp_path / "st"
    _publish(staging_source, "src")
    _publish(staging_target, "dst")
    (staging_target / "snapshots" / ".staging-19990101-000000-one").mkdir()
    (staging_target / "snapshots" / ".staging-19990101-000000-two").mkdir()
    monkeypatch.setattr(export_mod, "MAX_PUBLISHED_SNAPSHOTS", 4096)
    monkeypatch.setattr(staging_mod, "MAX_PUBLISHED_SNAPSHOTS", 4096)
    monkeypatch.setattr(staging_mod, "MAX_STAGING_ENTRIES", 1)
    with pytest.raises(SnapshotTransferPlanError, match="exceeds bound"):
        snapshot_transfer_plan(staging_source, "current", staging_target)

    top_source = tmp_path / "top-source"
    top_live = _publish(top_source, "top")
    top_target = tmp_path / "top-target"
    _publish(top_target, "dst")
    monkeypatch.setattr(staging_mod, "MAX_STAGING_ENTRIES", 64)
    monkeypatch.setattr(export_mod, "MAX_TOP_LEVEL_ENTRIES", 1)
    monkeypatch.setattr(staging_mod, "MAX_TOP_LEVEL_ENTRIES", 1)
    with pytest.raises(SnapshotTransferPlanError, match="exceeds bound"):
        snapshot_transfer_plan(top_source, "current", top_target)
    assert top_live.is_dir()


def test_descriptors_and_both_leases_held_through_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "held")
    _publish(target, "dst")
    original_json = transfer_mod.result_to_json
    original_format = transfer_mod.format_result
    original_lease = transfer_mod.graph_shared_leases
    state = {
        "source_fd": None,
        "target_fd": None,
        "source_snapshots_fd": None,
        "target_snapshots_fd": None,
        "selected_fd": None,
        "payload_fds": {},
        "leases": 0,
        "active_leases": 0,
        "responses": 0,
        "flushes": 0,
    }

    @contextmanager
    def tracked_lease(*args, **kwargs):
        state["leases"] += 2
        state["active_leases"] += 2
        try:
            with original_lease(*args, **kwargs) as lease:
                yield lease
        finally:
            state["active_leases"] -= 2

    def capture_ready(
        _source,
        _target,
        source_fd,
        target_fd,
        source_snapshots_fd,
        target_snapshots_fd,
        selected_fd,
        payload_fds,
        _result,
    ):
        state["source_fd"] = source_fd
        state["target_fd"] = target_fd
        state["source_snapshots_fd"] = source_snapshots_fd
        state["target_snapshots_fd"] = target_snapshots_fd
        state["selected_fd"] = selected_fd
        state["payload_fds"] = dict(payload_fds)
        os.fstat(source_fd)
        os.fstat(target_fd)
        os.fstat(source_snapshots_fd)
        os.fstat(target_snapshots_fd)
        os.fstat(selected_fd)
        for fd in payload_fds.values():
            os.fstat(fd)
        assert state["active_leases"] == 2

    def _assert_held() -> None:
        assert state["active_leases"] == 2
        os.fstat(state["source_fd"])
        os.fstat(state["target_fd"])
        os.fstat(state["source_snapshots_fd"])
        os.fstat(state["target_snapshots_fd"])
        os.fstat(state["selected_fd"])
        for fd in state["payload_fds"].values():
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

    monkeypatch.setattr(transfer_mod, "graph_shared_leases", tracked_lease)
    monkeypatch.setattr(transfer_mod, "_after_transfer_result_ready", capture_ready)
    monkeypatch.setattr(transfer_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(transfer_mod, "format_result", guarded_format)
    monkeypatch.setattr(transfer_mod.sys, "stdout", GuardedStdout())
    assert (
        transfer_mod.main(
            [
                "--source-graph",
                str(source),
                "--snapshot",
                "current",
                "--target-graph",
                str(target),
                "--json",
            ]
        )
        == 0
    )
    assert (
        transfer_mod.main(
            [
                "--source-graph",
                str(source),
                "--snapshot",
                "current",
                "--target-graph",
                str(target),
            ]
        )
        == 0
    )
    assert state["active_leases"] == 0
    assert state["leases"] == 4
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_no_mutation_no_lock_creation_no_producer_no_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_snapshot_graph_audit as audit_mod
    import graphrag_code.snapshot_compare as compare_mod
    import graphrag_code.snapshot_export_apply as apply_mod
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_export_verify as verify_mod
    import graphrag_code.snapshot_import_plan as import_mod
    import graphrag_code.snapshot_read as read_mod
    import graphrag_code.snapshot_transfer_plan as transfer_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "safe")
    _publish(target, "dst")
    before_source = _protected_state(source)
    before_target = _protected_state(target)
    calls = {"shared": 0}

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public mutating/read scope")

    original_lease = transfer_mod.graph_shared_leases

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 2
        with original_lease(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(transfer_mod, "graph_shared_leases", counted)
    monkeypatch.setattr(transfer_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "graph_read_lease", boom)
    monkeypatch.setattr(plan_mod, "snapshot_export_plan", boom)
    monkeypatch.setattr(verify_mod, "snapshot_export_verify", boom)
    monkeypatch.setattr(apply_mod, "snapshot_export_apply", boom)
    monkeypatch.setattr(import_mod, "snapshot_import_plan", boom)
    monkeypatch.setattr(import_mod, "graph_read_lease", boom)
    monkeypatch.setattr(read_mod, "retained_snapshot_read", boom)
    monkeypatch.setattr(compare_mod, "snapshot_history", boom, raising=False)
    monkeypatch.setattr(audit_mod, "audit_graph_root", boom)
    result = snapshot_transfer_plan(source, "current", target)
    assert result["ok"] is True
    assert result["source_graph_mutated"] is False
    assert result["target_graph_mutated"] is False
    assert result["transfer_performed"] is False
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
    assert "snapshot_export_plan(" not in source_text
    assert "snapshot_import_plan(" not in source_text
    assert "snapshot_export_verify(" not in source_text
    assert "snapshot_export_apply(" not in source_text
    assert "graph_exclusive_lease" not in source_text
    assert "read_bytes" not in source_text
    assert HASH_CHUNK_BYTES <= 64 * 1024
    lowered = source_text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    assert "language-independent" in lowered
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 14
    assert "snapshot_transfer_plan" not in TOOL_NAMES
    session = build_session(target, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 14
            assert "snapshot_transfer_plan" not in names

    anyio_run(_body)


def test_byog_roots_unchanged_and_no_temp_artifacts(tmp_path: Path):
    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    source = tmp_path / "source"
    target = tmp_path / "target"
    _publish(source, "byog")
    _publish(target, "dst")
    snapshot_transfer_plan(source, "current", target)
    after = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert after == before
    leftovers: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(ROOT, followlinks=False):
        rel = Path(dirpath).relative_to(ROOT)
        if ".git" in rel.parts or "output" in rel.parts:
            dirnames[:] = []
            continue
        if Path(dirpath).name.startswith(".staging-") or Path(dirpath).name.startswith(
            ".graphrag-export-"
        ):
            leftovers.append(Path(dirpath))
        leftovers.extend(
            Path(dirpath) / name
            for name in dirnames
            if name.startswith(".staging-") or name.startswith(".graphrag-export-")
        )
    assert leftovers == []
