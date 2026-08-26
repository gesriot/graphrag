"""Read-only snapshot import plan.

Disposable tmp graphs and export dirs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_import_plan.py -q
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
    canonical_export_revision_text,
    export_revision_of,
    snapshot_export_plan,
)
from graphrag_code.snapshot_export_verify import snapshot_export_verify  # type: ignore
from graphrag_code.snapshot_import_plan import (  # type: ignore
    SnapshotImportPlanError,
    SnapshotImportPlanIntegrityError,
    canonical_import_revision_payload,
    canonical_import_revision_text,
    format_result,
    import_revision_of,
    result_to_json,
    snapshot_import_plan,
)
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore

SCRIPT = ROOT / "scripts" / "snapshot_import_plan.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_import_plan.py"
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
    "graph",
    "export_directory",
    "snapshot_id",
    "source_export_revision",
    "files",
    "file_count",
    "total_size_bytes",
    "source_envelope_valid",
    "current",
    "published_snapshots",
    "published_count",
    "target_staging_name",
    "target_snapshot_present",
    "target_staging_present",
    "staging_count",
    "blocking_reasons",
    "import_ready",
    "import_revision",
    "import_performed",
    "graph_mutated",
    "export_mutated",
    "fresh_plan_required_before_import",
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
        settings_text=f"import-plan: {marker}\n",
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


def _rewrite_manifest(export_dir: Path, **updates: object) -> None:
    payload = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    payload.update(updates)
    (export_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _assert_plan_shape(
    result: dict,
    graph: Path,
    export_dir: Path,
    snapshot_id: str,
    *,
    import_ready: bool,
    target_snapshot_present: bool = False,
    target_staging_present: bool = False,
    blocking_reasons: list[str] | None = None,
) -> None:
    for key in REQUIRED_RESULT_KEYS:
        assert key in result
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["graph"] == str(graph.resolve())
    assert result["export_directory"] == str(export_dir.resolve())
    assert result["snapshot_id"] == snapshot_id
    assert result["source_envelope_valid"] is True
    assert result["import_performed"] is False
    assert result["graph_mutated"] is False
    assert result["export_mutated"] is False
    assert result["fresh_plan_required_before_import"] is True
    assert result["file_count"] == len(result["files"])
    assert result["total_size_bytes"] == sum(item["size_bytes"] for item in result["files"])
    paths = [item["path"] for item in result["files"]]
    assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
    assert set(REQUIRED_PAYLOAD_FILES) <= set(paths)
    assert set(paths) <= set(ACCEPTED_PAYLOAD_FILES)
    for item in result["files"]:
        data = (export_dir / item["path"]).read_bytes()
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
    assert result["import_ready"] is import_ready
    if import_ready:
        assert result["blocking_reasons"] == []
        assert result["target_snapshot_present"] is False
        assert result["target_staging_present"] is False
    assert result["published_snapshots"] == sorted(
        result["published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["published_count"] == len(result["published_snapshots"])
    assert result["current"] in result["published_snapshots"]
    assert result["import_revision"] == import_revision_of(result)
    payload = json.loads(canonical_import_revision_text(result))
    assert set(payload) == {
        "blocking_reasons",
        "current",
        "fresh_plan_required_before_import",
        "import_performed",
        "import_ready",
        "published_snapshots",
        "schema_version",
        "snapshot_id",
        "source_envelope_valid",
        "source_export_revision",
        "target_snapshot_present",
        "target_staging_name",
        "target_staging_present",
    }
    assert "graph" not in payload
    assert "export_directory" not in payload
    assert "ok" not in payload
    assert "graph_mutated" not in payload
    codes = [notice["code"] for notice in result["notices"]]
    assert codes[:7] == [
        "plan_is_not_import",
        "plan_is_not_backup",
        "import_revision_is_self_consistency_only",
        "fresh_plan_required_before_import",
        "source_envelope_language_independent_only",
        "advisory_locks_cooperating_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert str(graph.resolve()) in text
    assert snapshot_id in text
    assert "import_performed=false" in text
    assert "not an import" in text
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
    export_dir = _copy_standalone(live, here / "export")
    args = ["--graph", "target", "--export-dir", "export", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_import_plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-import-plan", *args],
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
            "snapshot-import-plan",
            "--graph",
            str(target),
            "--export-dir",
            str(export_dir),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["import_revision"] == bodies[0]["import_revision"]

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_import_plan.py" in names
    help_proc = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "snapshot-import-plan" in help_proc.stdout


def test_valid_standalone_export_into_different_managed_graph(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "src", observations=True)
    _publish(target, "dst")
    before_source = _protected_state(source)
    before_target = _protected_state(target)
    export_dir = _copy_standalone(live, tmp_path / "export")
    before_export = _payload_hashes(export_dir)
    result = snapshot_import_plan(target, export_dir)
    _assert_plan_shape(
        result,
        target,
        export_dir,
        live.name,
        import_ready=True,
    )
    assert result["import_ready"] is True
    assert result["blocking_reasons"] == []
    assert live.name not in result["published_snapshots"]
    assert result["target_staging_name"] == f".staging-{live.name}"
    assert _protected_state(source) == before_source
    assert _protected_state(target) == before_target
    assert _payload_hashes(export_dir) == before_export
    assert not (target / "snapshots" / f".staging-{live.name}").exists()


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
    result = snapshot_import_plan(target, dest)
    _assert_plan_shape(result, target, dest, live.name, import_ready=True)
    assert result["source_export_revision"] == plan["export_revision"]
    assert result["source_export_revision"] == applied["observed_export_revision"]
    assert result["source_export_revision"] == verified["observed_export_revision"]
    assert result["files"] == plan["files"]
    assert json.loads(canonical_export_revision_text(plan))["files"] == result["files"]


def test_schema_and_canonical_import_revision(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "canon")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "export")
    result = snapshot_import_plan(target, export_dir)
    _assert_plan_shape(result, target, export_dir, live.name, import_ready=True)
    digest = hashlib.sha256(
        canonical_import_revision_text(result).encode("utf-8")
    ).hexdigest()
    assert result["import_revision"] == "sha256:" + digest
    payload = canonical_import_revision_payload(result)
    assert payload["schema_version"] == 1
    assert payload["snapshot_id"] == live.name
    assert payload["source_export_revision"] == result["source_export_revision"]
    assert payload["import_ready"] is True
    assert payload["import_performed"] is False
    first = snapshot_import_plan(target, export_dir)
    second = snapshot_import_plan(target, export_dir)
    assert first == second
    assert result_to_json(first) == result_to_json(second)


def test_published_id_conflict_blocks_import(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "conflict")
    _publish(target, "dst")
    shutil.copytree(live, target / "snapshots" / live.name)
    export_dir = _copy_standalone(live, tmp_path / "export")
    before = _protected_state(target)
    result = snapshot_import_plan(target, export_dir)
    _assert_plan_shape(
        result,
        target,
        export_dir,
        live.name,
        import_ready=False,
        target_snapshot_present=True,
        blocking_reasons=["snapshot_id_already_published"],
    )
    proc = _run(
        "--graph",
        str(target),
        "--export-dir",
        str(export_dir),
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["import_ready"] is False
    assert body["blocking_reasons"] == ["snapshot_id_already_published"]
    assert body["ok"] is True
    assert _protected_state(target)["hashes"] == before["hashes"]


def test_exact_staging_name_conflict_blocks_import(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "staging")
    _publish(target, "dst")
    staging = target / "snapshots" / f".staging-{live.name}"
    staging.mkdir()
    export_dir = _copy_standalone(live, tmp_path / "export")
    result = snapshot_import_plan(target, export_dir)
    _assert_plan_shape(
        result,
        target,
        export_dir,
        live.name,
        import_ready=False,
        target_staging_present=True,
        blocking_reasons=["target_staging_name_present"],
    )
    assert result["staging_count"] == 1
    assert staging.is_dir()


def test_both_conflicts_simultaneously(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "both")
    _publish(target, "dst")
    shutil.copytree(live, target / "snapshots" / live.name)
    (target / "snapshots" / f".staging-{live.name}").mkdir()
    export_dir = _copy_standalone(live, tmp_path / "export")
    result = snapshot_import_plan(target, export_dir)
    _assert_plan_shape(
        result,
        target,
        export_dir,
        live.name,
        import_ready=False,
        target_snapshot_present=True,
        target_staging_present=True,
        blocking_reasons=[
            "snapshot_id_already_published",
            "target_staging_name_present",
        ],
    )
    assert result["blocking_reasons"] == sorted(
        result["blocking_reasons"], key=lambda item: item.encode("utf-8")
    )


def test_unrelated_staging_does_not_block_this_id(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "ready")
    _publish(target, "dst")
    unrelated = target / "snapshots" / ".staging-19990101-000000-unrelated"
    unrelated.mkdir()
    export_dir = _copy_standalone(live, tmp_path / "export")
    result = snapshot_import_plan(target, export_dir)
    _assert_plan_shape(result, target, export_dir, live.name, import_ready=True)
    assert result["staging_count"] == 1
    assert result["target_staging_present"] is False
    assert result["blocking_reasons"] == []
    codes = [notice["code"] for notice in result["notices"]]
    assert "unrelated_target_staging_present" in codes
    assert unrelated.is_dir()


def test_missing_unmanaged_and_unlocked_graphs(tmp_path: Path):
    source = tmp_path / "source"
    live = _publish(source, "only")
    export_dir = _copy_standalone(live, tmp_path / "export")
    missing = _run(
        "--graph",
        str(tmp_path / "missing"),
        "--export-dir",
        str(export_dir),
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "entities.parquet").write_bytes(b"x")
    legacy = _run("--graph", str(flat), "--export-dir", str(export_dir), "--json")
    assert legacy.returncode == 2
    assert legacy.stdout == ""

    unlocked = tmp_path / "unlocked"
    _publish(unlocked, "lock")
    (unlocked / PUBLICATION_LOCK_NAME).unlink()
    unlocked_proc = _run(
        "--graph",
        str(unlocked),
        "--export-dir",
        str(export_dir),
        "--json",
    )
    assert unlocked_proc.returncode == 2
    assert unlocked_proc.stdout == ""
    assert "adopt-publication-lock" in unlocked_proc.stderr
    assert not (unlocked / PUBLICATION_LOCK_NAME).exists()

    missing_export = _run(
        "--graph",
        str(source),
        "--export-dir",
        str(tmp_path / "no-export"),
        "--json",
    )
    assert missing_export.returncode == 2
    assert missing_export.stdout == ""


def test_source_and_target_symlink_and_non_directory(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "only")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "export")

    linked_export = tmp_path / "linked-export"
    linked_export.symlink_to(export_dir, target_is_directory=True)
    with pytest.raises(SnapshotImportPlanError, match="symlink"):
        snapshot_import_plan(target, linked_export)
    proc = _run(
        "--graph",
        str(target),
        "--export-dir",
        str(linked_export),
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""

    linked_graph = tmp_path / "linked-graph"
    linked_graph.symlink_to(target, target_is_directory=True)
    with pytest.raises(SnapshotImportPlanError, match="symlink"):
        snapshot_import_plan(linked_graph, export_dir)
    graph_proc = _run(
        "--graph",
        str(linked_graph),
        "--export-dir",
        str(export_dir),
        "--json",
    )
    assert graph_proc.returncode == 1
    assert graph_proc.stdout == ""

    file_export = tmp_path / "file-export"
    file_export.write_text("nope\n", encoding="utf-8")
    with pytest.raises(SnapshotImportPlanError, match="not a real directory"):
        snapshot_import_plan(target, file_export)
    file_graph = tmp_path / "file-graph"
    file_graph.write_text("nope\n", encoding="utf-8")
    with pytest.raises(SnapshotImportPlanError, match="not a real directory"):
        snapshot_import_plan(file_graph, export_dir)


def test_manifest_duplicate_keys_malformed_and_invalid_envelope(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "manifest")
    _publish(target, "dst")

    dup = _copy_standalone(live, tmp_path / "dup")
    (dup / "manifest.json").write_bytes(b'{"id":"a","id":"b"}')
    with pytest.raises(SnapshotImportPlanError, match="duplicate JSON object key"):
        snapshot_import_plan(target, dup)
    dup_proc = _run("--graph", str(target), "--export-dir", str(dup), "--json")
    assert dup_proc.returncode == 2
    assert dup_proc.stdout == ""

    bad_utf8 = _copy_standalone(live, tmp_path / "utf8")
    (bad_utf8 / "manifest.json").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(SnapshotImportPlanError, match="UTF-8"):
        snapshot_import_plan(target, bad_utf8)
    utf8_proc = _run("--graph", str(target), "--export-dir", str(bad_utf8), "--json")
    assert utf8_proc.returncode == 2
    assert utf8_proc.stdout == ""

    bad_json = _copy_standalone(live, tmp_path / "json")
    (bad_json / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotImportPlanError, match="JSON"):
        snapshot_import_plan(target, bad_json)
    json_proc = _run("--graph", str(target), "--export-dir", str(bad_json), "--json")
    assert json_proc.returncode == 2
    assert json_proc.stdout == ""

    nan_dir = _copy_standalone(live, tmp_path / "nan")
    (nan_dir / "manifest.json").write_text('{"id": NaN}', encoding="utf-8")
    with pytest.raises(SnapshotImportPlanError, match="JSON|constant"):
        snapshot_import_plan(target, nan_dir)

    oversized = _copy_standalone(live, tmp_path / "oversize")
    (oversized / "manifest.json").write_bytes(
        b'{"pad":"' + (b"a" * (MAX_MANIFEST_BYTES + 8)) + b'"}'
    )
    with pytest.raises(SnapshotImportPlanError, match="exceeds bound"):
        snapshot_import_plan(target, oversized)
    over_proc = _run("--graph", str(target), "--export-dir", str(oversized), "--json")
    assert over_proc.returncode == 2
    assert over_proc.stdout == ""

    for field, value, match in (
        ("id", ".", "invalid"),
        ("schema_version", 2, "invalid"),
        ("files", ["entities.parquet"], "invalid"),
        ("counts", {"entities": True}, "invalid"),
        ("total_size_bytes", True, "invalid"),
    ):
        bad = _copy_standalone(live, tmp_path / f"bad-{field}")
        if field == "counts":
            payload = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
            payload["counts"]["entities"] = True
            (bad / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        else:
            _rewrite_manifest(bad, **{field: value})
        with pytest.raises(SnapshotImportPlanIntegrityError, match=match):
            snapshot_import_plan(target, bad)
        proc = _run("--graph", str(target), "--export-dir", str(bad), "--json")
        assert proc.returncode == 1, field
        assert proc.stdout == ""


def test_missing_extra_nonregular_symlinked_and_malformed_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "payload")
    _publish(target, "dst")

    missing = _copy_standalone(live, tmp_path / "missing")
    (missing / "entities.parquet").unlink()
    with pytest.raises(SnapshotImportPlanIntegrityError, match="invalid|missing"):
        snapshot_import_plan(target, missing)
    missing_proc = _run("--graph", str(target), "--export-dir", str(missing), "--json")
    assert missing_proc.returncode == 1
    assert missing_proc.stdout == ""

    extra = _copy_standalone(live, tmp_path / "extra")
    (extra / "notes.txt").write_text("nope\n", encoding="utf-8")
    with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected"):
        snapshot_import_plan(target, extra)

    nested = _copy_standalone(live, tmp_path / "nested")
    (nested / "nested-dir").mkdir()
    with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected|not a regular"):
        snapshot_import_plan(target, nested)

    fifo_dir = _copy_standalone(live, tmp_path / "fifo")
    os.mkfifo(fifo_dir / "pipe.fifo")
    with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected|not a regular"):
        snapshot_import_plan(target, fifo_dir)

    remnant = _copy_standalone(live, tmp_path / "tmp")
    (remnant / "entities.parquet.tmp").write_bytes(b"tmp")
    with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected"):
        snapshot_import_plan(target, remnant)

    protocol = _copy_standalone(live, tmp_path / "protocol")
    (protocol / ".export-writer.lock").write_bytes(b"")
    with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected"):
        snapshot_import_plan(target, protocol)

    directory_payload = _copy_standalone(live, tmp_path / "dir-payload")
    (directory_payload / "settings.yaml").unlink()
    (directory_payload / "settings.yaml").mkdir()
    with pytest.raises(SnapshotImportPlanIntegrityError, match="not a regular"):
        snapshot_import_plan(target, directory_payload)

    malformed = _copy_standalone(live, tmp_path / "malformed")
    (malformed / "entities.parquet").write_bytes(b"not-a-parquet")
    with pytest.raises(SnapshotImportPlanIntegrityError, match="cannot read|invalid"):
        snapshot_import_plan(target, malformed)

    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"must-not-follow")
    linked = _copy_standalone(live, tmp_path / "linked-payload")
    payload = linked / "entities.parquet"
    payload.unlink()
    payload.symlink_to(outside)
    outside_ino = outside.stat().st_ino
    original_read = plan_mod.os.read

    def reject_outside(fd, count):
        assert os.fstat(fd).st_ino != outside_ino
        return original_read(fd, count)

    monkeypatch.setattr(plan_mod.os, "read", reject_outside)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="symlink"):
        snapshot_import_plan(target, linked)
    assert outside.read_bytes() == b"must-not-follow"

    short = Path(f"/tmp/sip{os.getpid()}")
    if short.exists():
        shutil.rmtree(short)
    short.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        socket_dir = _copy_standalone(live, short / "e")
        server.bind(str(socket_dir / "s"))
        with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected|not a regular"):
            snapshot_import_plan(target, socket_dir)
    finally:
        server.close()
        shutil.rmtree(short, ignore_errors=True)


def test_parquet_row_count_disagreement(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "counts")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "counts")
    payload = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    payload["counts"]["entities"] = int(payload["counts"]["entities"]) + 1
    (export_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotImportPlanIntegrityError, match="invalid"):
        snapshot_import_plan(target, export_dir)
    proc = _run("--graph", str(target), "--export-dir", str(export_dir), "--json")
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_source_listing_inode_metadata_and_same_size_mtime_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_import_plan as import_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "race")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "same-size")
    target_file = export_dir / "entities.parquet"
    original = target_file.read_bytes()
    original_stat = target_file.stat()

    def rewrite_same_size(_export_dir, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        target_file.write_bytes(replacement)
        os.utime(target_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(
        import_mod, "_after_import_source_first_observation", rewrite_same_size
    )
    with pytest.raises(SnapshotImportPlanIntegrityError, match="changed"):
        snapshot_import_plan(target, export_dir)

    listing_dir = _copy_standalone(live, tmp_path / "listing")

    def add_unexpected(_export_dir, _entries):
        (listing_dir / "notes.txt").write_text("extra\n", encoding="utf-8")

    monkeypatch.setattr(import_mod, "_after_import_source_first_observation", lambda *_: None)
    monkeypatch.setattr(import_mod, "_after_import_source_listed", add_unexpected)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="unexpected|changed"):
        snapshot_import_plan(target, listing_dir)

    swapped = _copy_standalone(live, tmp_path / "anchored")
    replacement = tmp_path / "replacement"
    _copy_standalone(live, replacement)
    (replacement / "entities.parquet").write_bytes(b"replacement-bytes")
    replacement_inodes = {
        path.stat().st_ino for path in replacement.iterdir() if path.is_file()
    }
    hidden = tmp_path / "hidden-original"
    original_read = plan_mod.os.read

    def replace_export(_export_dir, _records):
        swapped.rename(hidden)
        swapped.symlink_to(replacement, target_is_directory=True)

    def reject_replacement(fd, count):
        assert os.fstat(fd).st_ino not in replacement_inodes
        return original_read(fd, count)

    monkeypatch.setattr(import_mod, "_after_import_source_listed", lambda *_: None)
    monkeypatch.setattr(
        import_mod, "_after_import_source_first_observation", replace_export
    )
    monkeypatch.setattr(plan_mod.os, "read", reject_replacement)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="replaced|changed|symlink"):
        snapshot_import_plan(target, swapped)
    assert (replacement / "entities.parquet").read_bytes() == b"replacement-bytes"

    joint = _copy_standalone(live, tmp_path / "joint-window")
    joint_file = joint / "entities.parquet"
    joint_original = joint_file.read_bytes()
    joint_stat = joint_file.stat()

    def rewrite_during_target(_root, _tokens):
        changed = bytes([joint_original[0] ^ 1]) + joint_original[1:]
        joint_file.write_bytes(changed)
        os.utime(joint_file, ns=(joint_stat.st_atime_ns, joint_stat.st_mtime_ns))

    monkeypatch.setattr(import_mod, "_after_import_source_first_observation", lambda *_: None)
    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", rewrite_during_target)
    monkeypatch.setattr(plan_mod.os, "read", original_read)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="changed"):
        snapshot_import_plan(target, joint)

    real_swap = _copy_standalone(live, tmp_path / "real-swap")
    real_replacement = _copy_standalone(live, tmp_path / "real-replacement")
    real_hidden = tmp_path / "real-hidden"

    def replace_source_with_real_directory(path):
        if path == real_swap:
            real_swap.rename(real_hidden)
            shutil.copytree(real_replacement, real_swap)

    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", lambda *_: None)
    monkeypatch.setattr(
        import_mod, "_after_import_source_path_inspected", replace_source_with_real_directory
    )
    with pytest.raises(SnapshotImportPlanIntegrityError, match="changed|replaced"):
        snapshot_import_plan(target, real_swap)


def test_target_current_published_staging_lock_and_pathname_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_import_plan as import_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "src")
    first = _publish(target, "old")
    current = _publish(target, "new")
    export_dir = _copy_standalone(live, tmp_path / "export")

    def switch_current(_root, _tokens):
        (target / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", switch_current)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="current|listing|lock"):
        snapshot_import_plan(target, export_dir)
    (target / "current").write_text(current.name + "\n", encoding="utf-8")

    def add_published(_root, _tokens):
        extra = target / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(target / "snapshots" / current.name, extra)

    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", add_published)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="listing|lock|current"):
        snapshot_import_plan(target, export_dir)
    shutil.rmtree(target / "snapshots" / "19990101-000000-addedone", ignore_errors=True)

    def add_staging(_root, _tokens):
        staging = target / "snapshots" / ".staging-19990101-000000-racer"
        staging.mkdir(exist_ok=True)

    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", add_staging)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="listing|staging|lock"):
        snapshot_import_plan(target, export_dir)
    shutil.rmtree(target / "snapshots" / ".staging-19990101-000000-racer", ignore_errors=True)

    def replace_lock(_root, _tokens):
        lock = target / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-lock")

    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", replace_lock)
    with pytest.raises(SnapshotImportPlanIntegrityError, match="lock|current|listing"):
        snapshot_import_plan(target, export_dir)

    raced = tmp_path / "raced-graph"
    _publish(raced, "raced")
    hidden = tmp_path / "hidden-graph"
    replacement = tmp_path / "replacement-graph"
    _publish(replacement, "other")

    def replace_graph(_path):
        if _path == raced:
            raced.rename(hidden)
            raced.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(import_mod, "_after_import_graph_tokens_captured", lambda *_: None)
    monkeypatch.setattr(import_mod, "_after_import_graph_path_inspected", replace_graph)
    with pytest.raises(
        (SnapshotImportPlanError, SnapshotImportPlanIntegrityError),
        match="symlink|changed|replaced|unsafe",
    ):
        snapshot_import_plan(raced, export_dir)

    raced_real = tmp_path / "raced-real-graph"
    _publish(raced_real, "raced-real")
    hidden_real = tmp_path / "hidden-real-graph"

    def replace_graph_with_real_directory(path):
        if path == raced_real:
            raced_real.rename(hidden_real)
            shutil.copytree(replacement, raced_real)

    monkeypatch.setattr(
        import_mod,
        "_after_import_graph_path_inspected",
        replace_graph_with_real_directory,
    )
    with pytest.raises(SnapshotImportPlanIntegrityError, match="changed|replaced"):
        snapshot_import_plan(raced_real, export_dir)


def test_descriptors_and_lease_held_through_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_import_plan as import_mod

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "held")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "export")
    original_scope = import_mod._snapshot_import_plan_scope
    original_json = import_mod.result_to_json
    original_format = import_mod.format_result
    original_lease = import_mod.graph_read_lease
    state = {
        "export_fd": None,
        "graph_fd": None,
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

    def capture_ready(_export_dir, _graph, export_fd, graph_fd, payload_fds, _result):
        state["export_fd"] = export_fd
        state["graph_fd"] = graph_fd
        state["payload_fds"] = dict(payload_fds)
        os.fstat(export_fd)
        os.fstat(graph_fd)
        for fd in payload_fds.values():
            os.fstat(fd)
        assert state["lease"] is True

    def guarded_json(*args, **kwargs):
        assert state["lease"] is True
        os.fstat(state["export_fd"])
        os.fstat(state["graph_fd"])
        for fd in state["payload_fds"].values():
            os.fstat(fd)
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        assert state["lease"] is True
        os.fstat(state["export_fd"])
        os.fstat(state["graph_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            assert state["lease"] is True
            os.fstat(state["export_fd"])
            os.fstat(state["graph_fd"])
            for fd in state["payload_fds"].values():
                os.fstat(fd)
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            assert state["lease"] is True
            os.fstat(state["export_fd"])
            os.fstat(state["graph_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(import_mod, "graph_read_lease", tracked_lease)
    monkeypatch.setattr(import_mod, "_after_import_result_ready", capture_ready)
    monkeypatch.setattr(import_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(import_mod, "format_result", guarded_format)
    monkeypatch.setattr(import_mod.sys, "stdout", GuardedStdout())
    assert (
        import_mod.main(
            ["--graph", str(target), "--export-dir", str(export_dir), "--json"]
        )
        == 0
    )
    assert (
        import_mod.main(["--graph", str(target), "--export-dir", str(export_dir)]) == 0
    )
    assert state["lease"] is False
    assert state["leases"] == 2
    assert state["responses"] >= 2
    assert state["flushes"] == 2
    assert original_scope is import_mod._snapshot_import_plan_scope


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

    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "safe")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "export")
    before_target = _protected_state(target)
    before_export = _payload_hashes(export_dir)
    calls = {"shared": 0}

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public mutating/read scope")

    original_lease = import_mod.graph_read_lease

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 1
        assert kwargs.get("allow_unlocked_managed") is False
        with original_lease(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(import_mod, "graph_read_lease", counted)
    monkeypatch.setattr(import_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "graph_read_lease", boom)
    monkeypatch.setattr(plan_mod, "snapshot_export_plan", boom)
    monkeypatch.setattr(verify_mod, "snapshot_export_verify", boom)
    monkeypatch.setattr(apply_mod, "snapshot_export_apply", boom)
    monkeypatch.setattr(read_mod, "retained_snapshot_read", boom)
    monkeypatch.setattr(compare_mod, "snapshot_history", boom, raising=False)
    monkeypatch.setattr(audit_mod, "audit_graph_root", boom)
    result = snapshot_import_plan(target, export_dir)
    assert result["ok"] is True
    assert result["graph_mutated"] is False
    assert result["export_mutated"] is False
    assert result["import_performed"] is False
    assert calls["shared"] == 1
    assert _protected_state(target) == before_target
    assert _payload_hashes(export_dir) == before_export

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
    assert "graph_read_lease" in imported
    assert "export_revision_of" in imported
    assert "validate_persisted_byog_snapshot" in imported
    assert "snapshot_export_plan(" not in source_text
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

    assert len(TOOL_NAMES) == 13
    assert "snapshot_import_plan" not in TOOL_NAMES
    session = build_session(target, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 13
            assert "snapshot_import_plan" not in names

    anyio_run(_body)


def test_utf8_byte_order_and_strict_bool_vs_int(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "order")
    _publish(target, "dst")
    (target / "snapshots" / ".staging-19990101-000000-aaa").mkdir()
    shutil.copytree(live, target / "snapshots" / live.name)
    (target / "snapshots" / f".staging-{live.name}").mkdir()
    export_dir = _copy_standalone(live, tmp_path / "export")
    result = snapshot_import_plan(target, export_dir)
    assert result["published_snapshots"] == sorted(
        result["published_snapshots"], key=lambda item: item.encode("utf-8")
    )
    assert result["blocking_reasons"] == sorted(
        result["blocking_reasons"], key=lambda item: item.encode("utf-8")
    )
    assert [item["path"] for item in result["files"]] == sorted(
        item["path"] for item in result["files"]
    )
    with pytest.raises(SnapshotImportPlanError, match="boolean"):
        canonical_import_revision_payload({**result, "import_ready": 1})
    with pytest.raises(SnapshotImportPlanError, match="boolean"):
        canonical_import_revision_payload({**result, "target_snapshot_present": 0})
    with pytest.raises(SnapshotImportPlanError, match="schema_version"):
        canonical_import_revision_payload({**result, "schema_version": True})
    with pytest.raises(SnapshotImportPlanError, match="UTF-8-byte order"):
        canonical_import_revision_payload(
            {
                **result,
                "blocking_reasons": list(reversed(result["blocking_reasons"])),
            }
        )
    with pytest.raises(SnapshotImportPlanError, match="published_snapshots"):
        canonical_import_revision_payload(
            {**result, "target_snapshot_present": False}
        )
    with pytest.raises(SnapshotImportPlanError, match="presence flags"):
        canonical_import_revision_payload(
            {
                **result,
                "blocking_reasons": ["snapshot_id_already_published"],
            }
        )
    with pytest.raises(SnapshotImportPlanError, match="blocking reasons"):
        canonical_import_revision_payload({**result, "import_ready": True})


def test_existing_export_plan_verify_apply_contracts_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "compat", observations=True)
    plan = snapshot_export_plan(graph, "current")
    assert plan["schema_version"] == 1
    assert plan["export_performed"] is False
    assert plan["fresh_plan_required_before_export"] is True
    dest = tmp_path / "applied"
    applied = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    verified = snapshot_export_verify(dest, plan["export_revision"])
    assert verified["ok"] is True
    assert verified["observed_export_revision"] == plan["export_revision"]
    assert applied["observed_export_revision"] == plan["export_revision"]
    assert verified["files"] == plan["files"]
    other = tmp_path / "other"
    _publish(other, "other")
    imported = snapshot_import_plan(other, dest)
    assert imported["source_export_revision"] == plan["export_revision"]
    assert live.name == imported["snapshot_id"]


def test_byog_roots_unchanged_and_no_temp_artifacts(tmp_path: Path):
    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "byog")
    _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "export")
    snapshot_import_plan(target, export_dir)
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
